"""Credential store (ADR 0018): encrypted at rest, write-only through the API, audited on use.

Only internal components call `reveal()`, each with a consumer label ("provider:openrouter",
"connector:<id>", "mcp:<id>") so the audit log shows who used which secret, never the value.
Consumers that hold a list of assigned secret names must go through `reveal_assigned()`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import Cipher, Sealed, get_cipher
from app.models import Secret
from app.services.audit import audit

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
OPENROUTER_SECRET = "openrouter_api_key"  # noqa: S105 - the secret's name, not its value


class VaultError(RuntimeError):
    pass


class SecretNotFound(VaultError):
    pass


class SecretNotAssigned(VaultError):
    pass


def _aad(name: str) -> str:
    return f"secret:{name}"


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise VaultError("secret names use lowercase letters, digits, '.', '_' and '-' (max 100)")
    return name


async def set_secret(
    db: AsyncSession,
    name: str,
    value: str,
    *,
    user_id: uuid.UUID | None,
    description: str | None = None,
    cipher: Cipher | None = None,
) -> Secret:
    """Create or replace a secret. The caller commits."""
    validate_name(name)
    if not value:
        raise VaultError("secret value is empty")
    sealed = (cipher or get_cipher()).seal(value.encode(), _aad(name))
    row = await db.scalar(select(Secret).where(Secret.name == name))
    if row is None:
        row = Secret(
            name=name,
            description=description or "",
            ciphertext=sealed.ciphertext,
            nonce=sealed.nonce,
            key_id=sealed.key_id,
            version=1,
            created_by=user_id,
        )
        db.add(row)
        action = "secret.create"
    else:
        row.ciphertext, row.nonce, row.key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        row.version += 1
        row.updated_at = datetime.now(UTC)
        if description is not None:
            row.description = description
        action = "secret.replace"
    await db.flush()
    audit(db, action, user_id=user_id, resource=f"secret:{name}", details={"version": row.version})
    return row


async def delete_secret(db: AsyncSession, name: str, *, user_id: uuid.UUID | None) -> None:
    row = await db.scalar(select(Secret).where(Secret.name == name))
    if row is None:
        raise SecretNotFound(name)
    await db.delete(row)
    audit(db, "secret.delete", user_id=user_id, resource=f"secret:{name}")


async def exists(db: AsyncSession, name: str) -> bool:
    return await db.scalar(select(Secret.id).where(Secret.name == name)) is not None


async def reveal(db: AsyncSession, name: str, *, consumer: str, cipher: Cipher | None = None) -> str:
    """Decrypt a secret for an internal consumer. Audited (never the value). The caller commits."""
    row = await db.scalar(select(Secret).where(Secret.name == name))
    if row is None:
        audit(db, "secret.use", resource=f"secret:{name}", decision="deny", details={"consumer": consumer})
        raise SecretNotFound(name)
    value = (cipher or get_cipher()).open(Sealed(row.nonce, row.ciphertext, row.key_id), _aad(name)).decode()
    row.last_used_at = datetime.now(UTC)
    row.last_used_by = consumer[:200]
    audit(db, "secret.use", resource=f"secret:{name}", details={"consumer": consumer, "version": row.version})
    return value


async def reveal_assigned(db: AsyncSession, name: str, *, consumer: str, assigned: list[str]) -> str:
    """For connectors and MCP servers: only the secrets the admin assigned to that consumer."""
    if name not in assigned:
        audit(db, "secret.use", resource=f"secret:{name}", decision="deny", details={"consumer": consumer})
        raise SecretNotAssigned(f"secret {name!r} is not assigned to {consumer}")
    return await reveal(db, name, consumer=consumer)


# --- References (what uses which secret) --------------------------------------------------------

ReferenceFinder = Callable[[AsyncSession], Awaitable[dict[str, list[str]]]]
_finders: list[ReferenceFinder] = []


def register_reference_finder(fn: ReferenceFinder) -> ReferenceFinder:
    _finders.append(fn)
    return fn


async def references(db: AsyncSession) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for fn in _finders:
        for name, users in (await fn(db)).items():
            out.setdefault(name, []).extend(users)
    return out


@register_reference_finder
async def _provider_refs(db: AsyncSession) -> dict[str, list[str]]:
    return {OPENROUTER_SECRET: ["provider: OpenRouter"]}


# --- Master key rotation -------------------------------------------------------------------------


@dataclass
class RotationResult:
    secrets: int
    mfa: int


async def rotate_master(db: AsyncSession, old: Cipher, new: Cipher) -> RotationResult:
    """Re-encrypt every secret and MFA secret from `old` to `new` in one transaction (caller commits)."""
    from app.models import User

    n_secrets = 0
    for row in (await db.scalars(select(Secret))).all():
        plain = old.open(Sealed(row.nonce, row.ciphertext, row.key_id), _aad(row.name))
        sealed = new.seal(plain, _aad(row.name))
        row.ciphertext, row.nonce, row.key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        n_secrets += 1
    n_mfa = 0
    for u in (await db.scalars(select(User).where(User.mfa_secret_ct.is_not(None)))).all():
        assert u.mfa_secret_ct is not None and u.mfa_secret_nonce is not None and u.mfa_key_id is not None
        aad = f"mfa:{u.id}"
        plain = old.open(Sealed(u.mfa_secret_nonce, u.mfa_secret_ct, u.mfa_key_id), aad)
        sealed = new.seal(plain, aad)
        u.mfa_secret_ct, u.mfa_secret_nonce, u.mfa_key_id = sealed.ciphertext, sealed.nonce, sealed.key_id
        n_mfa += 1
    audit(db, "secret.rotate_master", details={"secrets": n_secrets, "mfa": n_mfa, "new_key_id": new.key_id})
    return RotationResult(n_secrets, n_mfa)
