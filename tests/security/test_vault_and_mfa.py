"""Credential store, OpenRouter key lookup, MFA (TOTP) and step-up, against real Postgres.

TOTP codes are computed from the secret actually stored (decrypted from the database), not from
a value the API handed back. Nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pyotp
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter, mfa_limiter
from app.core.config import get_settings
from app.core.crypto import Cipher, CryptoError, Sealed, decode_key, generate_key, get_cipher
from app.llm.router import load_router, resolve_openrouter
from app.models import AuditLog, Secret, Session, User
from app.services import mfa, vault
from tests.conftest import ROOT, TABLES
from tests.factories import PASSWORD, login, make_user

pytestmark = pytest.mark.db

PLAINTEXT = "sk-or-v1-PLAINTEXT-CANARY-0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset_limiters() -> None:
    login_limiter.reset()
    mfa_limiter.reset()


async def _admin(db: AsyncSession, client: httpx.AsyncClient, email: str = "admin@acme.test") -> uuid.UUID:
    u = await make_user(db, email, admin=True)
    uid = u.id
    await db.commit()
    await login(client, email)
    return uid


async def _age_reauth(db: AsyncSession, user_id: object, minutes: int = 10) -> None:
    await db.execute(
        update(Session)
        .where(Session.user_id == user_id)
        .values(reauth_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await db.commit()


async def _dump(db: AsyncSession) -> str:
    """Every row of every table as text (bytea appears as hex), like a database dump would contain."""
    parts = []
    for t in TABLES:
        parts.append(str(await db.scalar(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {t} t"))))
    return "\n".join(parts)


# --- Crypto and vault ------------------------------------------------------------------------------


def test_cipher_binds_name_and_key() -> None:
    c = Cipher(decode_key(generate_key()))
    sealed = c.seal(b"value", "secret:a")
    assert c.open(sealed, "secret:a") == b"value"
    with pytest.raises(CryptoError):
        c.open(sealed, "secret:b")  # swapped between names
    with pytest.raises(CryptoError):
        Cipher(decode_key(generate_key())).open(sealed, "secret:a")  # another master key
    tampered = Sealed(sealed.nonce, bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:], sealed.key_id)
    with pytest.raises(CryptoError):
        c.open(tampered, "secret:a")


async def test_secret_api_is_write_only_audited_and_never_stored_in_clear(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    admin = await _admin(db, client)
    r = await client.put("/api/admin/secrets", json={"name": "erp_token", "value": PLAINTEXT, "description": "ERP"})
    assert r.status_code == 204, r.text
    listed = (await client.get("/api/admin/secrets")).json()
    assert [s["name"] for s in listed] == ["erp_token"]
    assert PLAINTEXT not in r.text and PLAINTEXT not in str(listed)

    # A component reveals it: audited with the consumer, never the value.
    assert await vault.reveal(db, "erp_token", consumer="connector:test") == PLAINTEXT
    await db.commit()
    uses = (await db.scalars(select(AuditLog).where(AuditLog.action == "secret.use"))).all()
    assert [u.details["consumer"] for u in uses] == ["connector:test"]
    with pytest.raises(vault.SecretNotAssigned):
        await vault.reveal_assigned(db, "erp_token", consumer="connector:other", assigned=["something_else"])

    # Swapping ciphertexts between names is detected.
    await client.put("/api/admin/secrets", json={"name": "other", "value": "x" * 20})
    a = await db.scalar(select(Secret).where(Secret.name == "erp_token"))
    b = await db.scalar(select(Secret).where(Secret.name == "other"))
    assert a is not None and b is not None
    b.ciphertext, b.nonce = a.ciphertext, a.nonce
    await db.commit()
    with pytest.raises(CryptoError):
        await vault.reveal(db, "other", consumer="test")
    await db.rollback()

    # Scan the stored bytes of every table: the plaintext is nowhere, in text or hex form.
    dump = await _dump(db)
    assert PLAINTEXT not in dump and PLAINTEXT.encode().hex() not in dump
    actions = {a.action for a in (await db.scalars(select(AuditLog).where(AuditLog.user_id == admin))).all()}
    assert "secret.create" in actions
    assert (await client.delete("/api/admin/secrets/erp_token")).status_code == 204


async def test_sensitive_actions_need_recent_reauthentication(db: AsyncSession, client: httpx.AsyncClient) -> None:
    admin = await _admin(db, client)
    target_user = await make_user(db, "someone@acme.test")
    target = target_user.id
    await db.commit()
    assert (await client.put("/api/admin/secrets", json={"name": "k", "value": "v1"})).status_code == 204

    await _age_reauth(db, admin)
    for r in (
        await client.put("/api/admin/secrets", json={"name": "k", "value": "v2"}),
        await client.delete("/api/admin/secrets/k"),
        await client.put("/api/admin/providers/openrouter", json={"enabled": False}),
        await client.patch(f"/api/admin/users/{target}", json={"is_admin": True}),
        await client.post("/api/admin/users", json={"email": "new@acme.test", "password": "x" * 12, "is_admin": True}),
        await client.post(f"/api/admin/users/{target}/reset-mfa"),
    ):
        assert r.status_code == 403 and r.json()["detail"] == "step_up_required", r.text
    # Non-sensitive admin work still works without step-up.
    assert (await client.patch(f"/api/admin/users/{target}", json={"display_name": "S"})).status_code == 200

    assert (await client.post("/api/auth/step-up", json={"password": "wrong-password"})).status_code == 401
    r = await client.post("/api/auth/step-up", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json()["step_up_valid"] is True
    assert (await client.patch(f"/api/admin/users/{target}", json={"is_admin": True})).status_code == 200


async def test_openrouter_key_vault_first_env_fallback_and_hot_reload(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await _admin(db, client)
    settings = get_settings().model_copy(update={"openrouter_api_key": None})
    conf = await resolve_openrouter(db, settings)
    assert conf.key_source == "none" and not conf.api_key
    router = await load_router(db, settings)
    assert [p.provider for p in router.providers] == ["ollama"]

    env_settings = get_settings().model_copy(update={"openrouter_api_key": SecretStr("env-key-value")})
    assert (await resolve_openrouter(db, env_settings)).key_source == "env"

    # Paid model and a ceiling above the env cap are refused.
    r = await client.put("/api/admin/providers/openrouter", json={"model": "openai/gpt-5"})
    assert r.status_code == 422
    r = await client.put("/api/admin/providers/openrouter", json={"monthly_budget_usd": 100})
    assert r.status_code == 422

    r = await client.put(
        "/api/admin/providers/openrouter",
        json={"api_key": PLAINTEXT, "model": "nvidia/nemotron-3.5-lightning:free", "monthly_budget_usd": 0.005},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["key_source"] == "vault" and out["configured"] and out["monthly_budget_usd"] == 0.005
    assert PLAINTEXT not in r.text
    # Vault beats env; the providers are rebuilt without a restart.
    assert (await resolve_openrouter(db, env_settings)).api_key == PLAINTEXT
    router = await load_router(db, settings)
    cloud = [p for p in router.providers if p.provider == "openrouter"]
    assert [p.model for p in cloud] == ["nvidia/nemotron-3.5-lightning:free"]
    assert router.budgets["openrouter"] == 0.005
    models = (await client.get("/api/models")).json()
    assert any(m["id"] == "openrouter:nvidia/nemotron-3.5-lightning:free" for m in models)

    assert (await client.put("/api/admin/providers/openrouter", json={"enabled": False})).status_code == 200
    assert all(p.provider != "openrouter" for p in (await load_router(db, settings)).providers)


# --- MFA ------------------------------------------------------------------------------------------


async def _stored_totp(db: AsyncSession, email: str) -> pyotp.TOTP:
    db.expire_all()
    u = await db.scalar(select(User).where(User.email == email))
    assert u is not None
    secret = mfa.load_secret(u)
    assert secret is not None
    return pyotp.TOTP(secret)


async def _enroll(db: AsyncSession, client: httpx.AsyncClient, email: str) -> tuple[pyotp.TOTP, list[str]]:
    r = await client.post("/api/auth/mfa/setup")
    assert r.status_code == 200 and "<svg" in r.json()["qr_svg"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    totp = await _stored_totp(db, email)
    assert totp.secret == r.json()["secret"]
    assert (await client.post("/api/auth/mfa/enable", json={"code": "000000"})).status_code == 401
    r = await client.post("/api/auth/mfa/enable", json={"code": totp.now()})
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    return totp, codes


async def test_mfa_enrolment_two_step_login_replay_and_recovery(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "alice@acme.test")
    await db.commit()
    await login(client, "alice@acme.test")
    totp, codes = await _enroll(db, client, "alice@acme.test")
    used_code = totp.now()  # the step used by /enable

    # The TOTP secret and recovery codes are not stored in clear.
    dump = await _dump(db)
    assert totp.secret not in dump and all(c.replace("-", "") not in dump for c in codes)

    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json={"email": "alice@acme.test", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mfa_required"] is True
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    pending_cookie = client.cookies.get(get_settings().session_cookie_name)
    # Password alone grants nothing.
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/containers")).status_code == 401
    # Replay of the code already used is refused; the next time step is accepted.
    assert (await client.post("/api/auth/mfa/verify", json={"code": used_code})).status_code == 401
    r = await client.post("/api/auth/mfa/verify", json={"code": totp.at(time.time() + 30)})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert client.cookies.get(get_settings().session_cookie_name) != pending_cookie  # session rotated
    me = (await client.get("/api/auth/me")).json()
    assert me["mfa_enabled"] is True and me["mfa_required"] is False

    # A recovery code works exactly once.
    for expected in (200, 401):
        await client.post("/api/auth/logout")
        r = await client.post("/api/auth/login", json={"email": "alice@acme.test", "password": PASSWORD})
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        r = await client.post("/api/auth/mfa/verify", json={"recovery_code": codes[0]})
        assert r.status_code == expected
    assert (await client.get("/api/auth/mfa")).status_code == 401  # still pending after the failed attempt


async def test_mfa_attempts_are_limited_and_step_up_needs_totp(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _admin(db, client)
    totp, _ = await _enroll(db, client, "admin@acme.test")
    # With MFA on, the password is not enough for step-up.
    assert (await client.post("/api/auth/step-up", json={"password": PASSWORD})).status_code == 401
    assert (await client.post("/api/auth/step-up", json={"code": totp.at(time.time() + 30)})).status_code == 200

    await client.post("/api/auth/logout")
    r = await client.post("/api/auth/login", json={"email": "admin@acme.test", "password": PASSWORD})
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    statuses = [(await client.post("/api/auth/mfa/verify", json={"code": "123456"})).status_code for _ in range(6)]
    assert statuses[:5] == [401] * 5 and statuses[5] == 429
    # The pending session is gone: even a valid code cannot be used with it.
    r = await client.post("/api/auth/mfa/verify", json={"code": totp.now()})
    assert r.status_code == 401
    denied = await db.scalar(
        select(AuditLog).where(AuditLog.action == "auth.mfa", AuditLog.details["reason"].astext == "too many attempts")
    )
    assert denied is not None


async def test_admin_and_cli_mfa_reset(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "bob@acme.test")
    await db.commit()
    bob = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    await login(bob, "bob@acme.test")
    await _enroll(db, bob, "bob@acme.test")
    admin = await _admin(db, client)
    bob_id = await db.scalar(select(User.id).where(User.email == "bob@acme.test"))
    assert (await client.post(f"/api/admin/users/{bob_id}/reset-mfa")).status_code == 204
    assert (await bob.get("/api/auth/me")).status_code == 401  # sessions ended
    db.expire_all()
    u = await db.scalar(select(User).where(User.email == "bob@acme.test"))
    assert u is not None and not u.mfa_enabled and u.mfa_secret_ct is None
    await bob.aclose()

    # Admin who lost their device: CLI on the server.
    await _enroll(db, client, "admin@acme.test")
    env = {**os.environ, "ERAG_DATABASE_URL": os.environ["ERAG_DATABASE_URL"]}
    out = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "app.cli", "reset-mfa", "--email", "admin@acme.test"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "MFA reset" in out.stdout
    db.expire_all()
    assert await db.scalar(select(User.mfa_enabled).where(User.id == admin)) is False


async def test_master_key_rotation(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _admin(db, client)
    await client.put("/api/admin/secrets", json={"name": "rot", "value": PLAINTEXT})
    await _enroll(db, client, "admin@acme.test")
    old, new = get_cipher(), Cipher(decode_key(generate_key()))
    res = await vault.rotate_master(db, old, new)
    await db.commit()
    assert (res.secrets, res.mfa) == (1, 1)
    row = await db.scalar(select(Secret).where(Secret.name == "rot"))
    assert row is not None and row.key_id == new.key_id
    assert new.open(Sealed(row.nonce, row.ciphertext, row.key_id), "secret:rot").decode() == PLAINTEXT
    with pytest.raises(CryptoError):
        old.open(Sealed(row.nonce, row.ciphertext, row.key_id), "secret:rot")
    # Put things back so the rest of the test session (old key) is unaffected.
    await vault.rotate_master(db, new, old)
    await db.commit()
