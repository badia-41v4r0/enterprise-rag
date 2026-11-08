"""TOTP (RFC 6238) second factor with replay protection and single-use recovery codes (ADR 0019)."""

from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import time
import uuid
from datetime import UTC, datetime

import pyotp
import segno
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import Sealed, get_cipher
from app.models import RecoveryCode, User

STEP = 30
WINDOW = 1  # accept the previous and next step to tolerate clock drift
RECOVERY_CODES = 10


def _aad(user: User) -> str:
    return f"mfa:{user.id}"


def store_secret(user: User, secret_b32: str) -> None:
    sealed = get_cipher().seal(secret_b32.encode(), _aad(user))
    user.mfa_secret_ct, user.mfa_secret_nonce, user.mfa_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id


def load_secret(user: User) -> str | None:
    if user.mfa_secret_ct is None or user.mfa_secret_nonce is None or user.mfa_key_id is None:
        return None
    return get_cipher().open(Sealed(user.mfa_secret_nonce, user.mfa_secret_ct, user.mfa_key_id), _aad(user)).decode()


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(user: User, secret_b32: str) -> str:
    return pyotp.TOTP(secret_b32).provisioning_uri(name=user.email, issuer_name=get_settings().mfa_issuer)


def qr_svg(uri: str) -> str:
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2)
    return buf.getvalue().decode()


def verify_totp(user: User, code: str, now: float | None = None) -> bool:
    """Accept a code only for a time step later than the last one used (no replay)."""
    secret = load_secret(user)
    code = code.strip().replace(" ", "")
    if secret is None or not code.isdigit() or len(code) != 6:
        return False
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step <= user.mfa_last_step:
            continue
        if hmac.compare_digest(totp.at(step * STEP), code):
            user.mfa_last_step = step
            return True
    return False


def _hash_code(code: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, b"recovery:" + code.strip().lower().replace("-", "").encode(), hashlib.sha256).hexdigest()


async def new_recovery_codes(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Replace the user's recovery codes. Returns the plaintext codes (shown once, never stored)."""
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = secrets.token_hex(5)  # 40 bits each, single use, rate limited
        codes.append(f"{raw[:5]}-{raw[5:]}")
        db.add(RecoveryCode(user_id=user_id, code_hash=_hash_code(raw)))
    return codes


async def use_recovery_code(db: AsyncSession, user_id: uuid.UUID, code: str) -> bool:
    row = await db.scalar(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.code_hash == _hash_code(code),
            RecoveryCode.used_at.is_(None),
        )
    )
    if row is None:
        return False
    row.used_at = datetime.now(UTC)
    return True


async def remaining_recovery_codes(db: AsyncSession, user_id: uuid.UUID) -> int:
    rows = await db.scalars(
        select(RecoveryCode.id).where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
    )
    return len(rows.all())


async def reset(db: AsyncSession, user: User) -> None:
    user.mfa_enabled = False
    user.mfa_secret_ct = user.mfa_secret_nonce = user.mfa_key_id = None
    user.mfa_last_step = 0
    await db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
