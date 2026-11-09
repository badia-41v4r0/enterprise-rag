"""Setup mode, the one-time token, license acceptance, the administrator, resume, completion and the upgrade
invariant (PLAN-v0.4 R10, ADR 0034/0035). Real Postgres, real HTTP handling; nothing mocked."""

from __future__ import annotations

import os
import stat
import subprocess
import sys

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.crypto import generate_key
from app.core.db import dispose_engine
from app.models import AuditLog, Container, Role, SetupState, User
from app.services import setup as svc
from tests.conftest import ROOT, TEST_DB_URL
from tests.factories import login, make_user
from tests.setup.helpers import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    accept_license,
    create_admin,
    enter_token,
    fresh_setup,
    reset_limiters,
    through_admin,
)

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _limits() -> None:
    reset_limiters()


async def test_no_setup_row_means_no_setup_mode(db: AsyncSession, client: httpx.AsyncClient) -> None:
    """Development and test databases that were never bootstrapped are not in setup mode, and setup endpoints
    do not exist for them."""
    await make_user(db, "a@acme.test", admin=True)
    await db.commit()
    assert (await client.get("/api/info")).json()["setup_required"] is False
    assert (await client.get("/api/setup/status")).status_code == 404
    await login(client, "a@acme.test")
    assert (await client.get("/api/admin/users")).status_code == 200


async def test_existing_installation_is_never_put_into_setup_mode(db: AsyncSession, client: httpx.AsyncClient) -> None:
    """Upgrade invariant: users already exist and no setup record -> bootstrap records a completed setup."""
    await make_user(db, "boss@acme.test", admin=True)
    await make_user(db, "worker@acme.test")
    await db.commit()
    res = await svc.init_on_start(db)
    assert res.mode == "upgraded" and res.new_token is None
    row = await svc.get_state(db)
    assert row is not None and row.completed_at is not None and row.completed_via == "upgrade"
    assert not await svc.setup_required(db)
    # A second start changes nothing.
    assert (await svc.init_on_start(db)).mode == "completed"
    assert not get_settings().setup_token_path.exists()
    await login(client, "worker@acme.test")
    assert (await client.get("/api/conversations")).status_code == 200


async def test_migration_marks_an_existing_database_completed(db: AsyncSession) -> None:
    """Migration 0012 on a database with users (here ACME personas) writes a completed record of type demo."""
    await make_user(db, "admin@acme.test", admin=True)
    await db.commit()
    await dispose_engine()
    env = {**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL}
    run = [sys.executable, "-m", "alembic"]
    subprocess.run([*run, "downgrade", "0011"], cwd=ROOT, env=env, check=True, capture_output=True)
    try:
        subprocess.run([*run, "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    finally:
        subprocess.run([*run, "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    db.expire_all()
    row = (await db.execute(text("SELECT completed_at, completed_via, install_type FROM setup_state"))).one()
    assert row.completed_at is not None and row.completed_via == "upgrade" and row.install_type == "demo"
    assert (await db.execute(text("SELECT must_change_password FROM users"))).scalar() is False


async def test_fresh_install_setup_mode_token_and_completion(db: AsyncSession, client: httpx.AsyncClient) -> None:
    s = get_settings()
    token = await fresh_setup(db)
    # The token file holds the token, readable only by its owner; the database only its hash.
    path = s.setup_token_path
    assert path.read_text().strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    row = await svc.get_state(db)
    assert row is not None and row.token_hash == svc.token_hash(token) and token not in str(row.steps)

    # Setup mode: /health and the wizard answer, everything else is 503 "setup required".
    assert (await client.get("/health")).status_code == 200
    info = (await client.get("/api/info")).json()
    assert info["setup_required"] is True and info["demo"] is False
    for method, path_ in (
        ("GET", "/api/admin/users"),
        ("GET", "/api/conversations"),
        ("GET", "/api/models"),
        ("GET", "/api/admin/providers"),
        ("GET", "/api/admin/setup/checklist"),
    ):
        r = await client.request(method, path_)
        assert r.status_code == 503 and r.json()["detail"] == "setup required", path_
    st = (await client.get("/api/setup/status")).json()
    assert st["mode"] == "setup" and st["current_step"] == 0 and st["session"] is False
    assert token not in str(st)
    # Step endpoints need the setup session.
    assert (await client.get("/api/setup/license")).status_code == 401
    assert (await client.post("/api/setup/admin", json={"email": "x@y.z", "password": "p" * 20})).status_code == 401

    # A wrong token is refused and audited (without the value).
    r = await client.post("/api/setup/token", json={"token": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF"})
    assert r.status_code == 401
    await enter_token(client, token.lower().replace("-", " "))  # case and separators do not matter
    st = (await client.get("/api/setup/status")).json()
    assert st["session"] is True and st["setup_csrf"]

    # Mutations need the setup CSRF header.
    csrf = client.headers.pop("X-Setup-CSRF")
    lic = (await client.get("/api/setup/license")).json()
    body = {"accepted": True, "name": "Ada IT", "text_sha256": lic["sha256"]}
    assert (await client.post("/api/setup/license", json=body)).status_code == 403
    client.headers["X-Setup-CSRF"] = csrf

    # Step 0: the checkbox is required; the hash must match the text shown.
    assert (await client.post("/api/setup/license", json={**body, "accepted": False})).status_code == 422
    assert (await client.post("/api/setup/license", json={**body, "text_sha256": "0" * 64})).status_code == 409
    assert (await client.post("/api/setup/license", json={**body, "name": " "})).status_code == 422
    assert "PolyForm Noncommercial License 1.0.0" in lic["text"] and "60 days" in lic["text"]
    assert "https://badiapartners.com" in lic["text"] and "open source" in lic["text"]  # "It is not open source."
    # The administrator cannot be created before the license is accepted.
    r = await client.post("/api/setup/admin", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 409
    assert (await client.post("/api/setup/license", json=body)).status_code == 200
    db.expire_all()
    row = await svc.get_state(db)
    assert row is not None and row.license_accepted_by == "Ada IT"
    assert row.license_text_sha256 == svc.license_text_sha256() and row.license_accepted_at is not None
    # Required steps cannot be skipped.
    assert (await client.post("/api/setup/steps/admin", json={"status": "skipped"})).status_code == 401
    # Admin-only steps need the administrator.
    assert (await client.post("/api/setup/steps/models", json={"status": "skipped"})).status_code == 401

    # Step 2: weak passwords are refused; then the administrator is created and signed in.
    for weak in ("short-pw-1", "aaaaaaaaaaaaaaaa", "it.admin-is-me-2026", "acme-demo-password"):
        r = await client.post("/api/setup/admin", json={"email": ADMIN_EMAIL, "password": weak})
        assert r.status_code == 422, weak
    me = await create_admin(client)
    assert me["is_admin"] and me["must_change_password"] is False
    assert (
        await client.post("/api/setup/admin", json={"email": "b@example.com", "password": ADMIN_PASSWORD})
    ).status_code == 409
    # The administrator's session works on the normal admin API during setup.
    assert (await client.get("/api/admin/users")).status_code == 200

    # A non-admin user created during setup still gets 503 until setup is finished.
    other = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    r = await client.post("/api/setup/users", json={"email": "emp@example.com", "display_name": "Emp"})
    assert r.status_code == 201
    temp = r.json()["temporary_password"]
    r = await other.post("/api/auth/login", json={"email": "emp@example.com", "password": temp})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    other.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert (await other.get("/api/conversations")).status_code == 503

    # Finish: completion recorded, token and setup session destroyed, token file removed, audited.
    r = await client.post("/api/setup/finish")
    assert r.status_code == 200, r.text
    db.expire_all()
    row = await svc.get_state(db)
    assert row is not None and row.completed_at is not None and row.completed_via == "wizard"
    assert row.token_hash is None and row.session_hash is None
    assert row.install_type == "empty"
    assert not path.exists()
    assert {"setup.start", "setup.token", "setup.license.accept", "setup.admin.create", "setup.complete"} <= set(
        (await db.scalars(select(AuditLog.action))).all()
    )
    denied = await db.scalar(select(AuditLog).where(AuditLog.action == "setup.token", AuditLog.decision == "deny"))
    assert denied is not None and token not in str(denied.details)
    # Every setup endpoint is gone; the token can never be used again.
    for method, path_ in (
        ("GET", "/api/setup/status"),
        ("GET", "/api/setup/license"),
        ("POST", "/api/setup/finish"),
        ("GET", "/api/setup/environment"),
        ("POST", "/api/setup/admin"),
    ):
        assert (await client.request(method, path_, json={})).status_code == 404, path_
    assert (await client.post("/api/setup/token", json={"token": token})).status_code == 404
    assert (await client.get("/api/info")).json()["setup_required"] is False
    # Skipped organization step -> the stricter cloud ceiling.
    assert (await client.get("/api/admin/setup/organization")).json()["cloud_max_classification"] == "public"
    # The employee can now use the app, after changing the temporary password.
    assert (await other.get("/api/conversations")).status_code == 403
    await other.aclose()


async def test_token_attempts_are_rate_limited(db: AsyncSession, client: httpx.AsyncClient) -> None:
    token = await fresh_setup(db)
    codes = [(await client.post("/api/setup/token", json={"token": f"WRONG-{i}"})).status_code for i in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]
    # Even the right token is refused while limited.
    assert (await client.post("/api/setup/token", json={"token": token})).status_code == 429


async def test_expired_token_is_refused_and_replaced_at_next_start(db: AsyncSession, client: httpx.AsyncClient) -> None:
    token = await fresh_setup(db)
    await db.execute(text("UPDATE setup_state SET token_expires_at = now() - interval '1 minute'"))
    await db.commit()
    assert (await client.post("/api/setup/token", json={"token": token})).status_code == 401
    res = await svc.init_on_start(db)
    assert res.mode == "setup" and res.new_token and res.new_token != token
    reset_limiters()
    await enter_token(client, res.new_token)


async def test_token_from_erag_init_is_used_once_hash_only(db: AsyncSession, client: httpx.AsyncClient) -> None:
    printed = svc.generate_token()
    s = get_settings().model_copy(update={"setup_token_sha256": svc.token_hash(printed)})
    res = await svc.init_on_start(db, s)
    assert res.mode == "setup" and res.new_token is None and res.token_from_init
    assert not s.setup_token_path.exists()
    await enter_token(client, printed)


async def test_wizard_resumes_after_a_restart(db: AsyncSession, client: httpx.AsyncClient) -> None:
    token = await through_admin(db, client)
    assert (await client.post("/api/setup/install-type", json={"type": "empty"})).status_code == 200
    # Restart: a new process (cache gone, new app) and bootstrap runs again; the token stays valid.
    svc.invalidate_cache()
    res = await svc.init_on_start(db)
    assert res.mode == "setup" and res.new_token is None
    from app.main import create_app

    fresh = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://testserver")
    st = (await fresh.get("/api/setup/status")).json()
    assert st["session"] is False and st["license_accepted"] and st["install_type"] == "empty"
    assert [x["status"] for x in st["steps"][:4]] == ["done", "done", "done", "done"]
    assert st["current_step"] == 4  # organization
    await enter_token(fresh, token)
    r = await fresh.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    fresh.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    st = (await fresh.get("/api/setup/status")).json()
    assert st["admin_signed_in"] is True
    # Steps are idempotent: doing one again is fine.
    r = await fresh.put("/api/setup/organization", json={"company_name": "Example GmbH"})
    assert r.status_code == 200 and r.json()["company_name"] == "Example GmbH"
    r = await fresh.put("/api/setup/organization", json={"company_name": "Example GmbH"})
    assert r.status_code == 200
    assert (await fresh.get("/api/setup/status")).json()["current_step"] == 5
    await fresh.aclose()


async def test_empty_installation_creates_nothing_beyond_the_administrator(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await through_admin(db, client)
    assert (await client.post("/api/setup/install-type", json={"type": "empty"})).status_code == 200
    assert (await client.post("/api/setup/finish")).status_code == 200
    assert [u.email for u in (await db.scalars(select(User))).all()] == [ADMIN_EMAIL]
    assert (await db.scalars(select(Role))).all() == []
    assert (await db.scalars(select(Container))).all() == []
    assert (await client.get("/api/info")).json() == {
        "setup_required": False,
        "company_name": "",
        "demo": False,
        "product": "EnterpriseRAG",
    }


async def test_cli_create_admin_completes_a_pending_setup(db: AsyncSession) -> None:
    await fresh_setup(db)
    env = {**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL, "ERAG_ADMIN_PASSWORD": "a-very-long-admin-password"}
    await dispose_engine()
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "create-admin", "--email", "ops@example.com"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Setup marked as completed" in out.stdout
    db.expire_all()
    row = await db.scalar(select(SetupState))
    assert row is not None and row.completed_via == "cli" and row.token_hash is None
    svc.invalidate_cache()
    assert not await svc.setup_required(db)


async def test_cli_reset_reopens_setup_with_a_new_token(db: AsyncSession, client: httpx.AsyncClient) -> None:
    old = await through_admin(db, client)
    assert (await client.post("/api/setup/finish")).status_code == 200
    env = {**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL}
    await dispose_engine()
    no = subprocess.run(
        [sys.executable, "-m", "app.cli", "setup", "--reset"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert no.returncode == 2  # needs --yes
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "setup", "--reset", "--yes"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    new = next(line.split(": ", 1)[1] for line in out.stdout.splitlines() if line.startswith("Setup token: "))
    svc.invalidate_cache()
    assert await svc.setup_required(db)
    reset_limiters()
    assert (await client.post("/api/setup/token", json={"token": old})).status_code == 401
    await enter_token(client, new)
    await accept_license(client, "Second Run")


async def test_bootstrap_fresh_database_enters_setup_mode_and_prints_a_token(db: AsyncSession) -> None:
    env = {**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL, "ERAG_SEED_DEMO": "false"}
    await dispose_engine()
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "bootstrap"], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    assert "SETUP MODE" in out.stdout
    token = next(line.split(": ", 1)[1] for line in out.stdout.splitlines() if "Setup token: " in line)
    assert get_settings().setup_token_path.read_text().strip() == token
    db.expire_all()
    row = await db.scalar(select(SetupState))
    assert row is not None and row.completed_at is None and row.token_hash == svc.token_hash(token)
    # A second start keeps the same (still valid) token and does not print it again.
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "bootstrap"], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    assert token not in out.stdout and "unchanged" in out.stdout


async def test_seed_demo_completes_setup_in_dev_and_is_ignored_in_prod(db: AsyncSession) -> None:
    """ADR 0040: ERAG_SEED_DEMO=true seeds the full demo (with admin@acme.test) and completes setup; production
    ignores it and enters setup mode instead."""
    await dispose_engine()
    prod = {
        **os.environ,
        "ERAG_DATABASE_URL": TEST_DB_URL,
        "ERAG_SEED_DEMO": "true",
        "ERAG_ENV": "prod",
        "ERAG_SECRET_KEY": "s" * 40,
        "ERAG_MCP_CONTEXT_SECRET": "m" * 40,
        "ERAG_SECRETS_KEY": generate_key(),
    }
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "bootstrap"], cwd=ROOT, env=prod, capture_output=True, text=True, check=True
    )
    assert "ignored in production" in out.stdout and "SETUP MODE" in out.stdout
    assert (await db.scalars(select(User))).all() == []
    await db.execute(text("TRUNCATE setup_state"))
    await db.commit()
    await dispose_engine()
    dev = {**os.environ, "ERAG_DATABASE_URL": TEST_DB_URL, "ERAG_SEED_DEMO": "true"}
    out = subprocess.run(
        [sys.executable, "-m", "app.cli", "bootstrap"], cwd=ROOT, env=dev, capture_output=True, text=True, check=True
    )
    assert "setup counts as completed" in out.stdout
    db.expire_all()
    row = await db.scalar(select(SetupState))
    assert row is not None and row.completed_via == "seed_demo" and row.completed_at is not None
    assert await db.scalar(select(User).where(User.email == "admin@acme.test", User.is_admin)) is not None
