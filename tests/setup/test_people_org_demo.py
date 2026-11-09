"""Forced password change, people (one by one and CSV), organization and cloud ceiling, demo installation and
removal, the post-setup checklist (PLAN-v0.4 R10/R11, ADR 0034/0036/0037). Real Postgres; nothing mocked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import registry
from app.models import AuditLog, Container, Document, McpServer, Role, Session, User
from demo.acme.seed import CONTAINERS, PERSONAS
from tests.factories import PASSWORD, login, make_role, make_user
from tests.setup.helpers import ADMIN_EMAIL, ADMIN_PASSWORD, reset_limiters, through_admin

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _limits() -> None:
    reset_limiters()


async def _age_reauth(db: AsyncSession, email: str) -> None:
    uid = await db.scalar(select(User.id).where(User.email == email))
    await db.execute(
        update(Session).where(Session.user_id == uid).values(reauth_at=datetime.now(UTC) - timedelta(minutes=30))
    )
    await db.commit()


async def test_temporary_passwords_must_be_changed_at_first_login(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "boss@acme.test", admin=True)
    await db.commit()
    await login(client, "boss@acme.test")
    r = await client.post("/api/admin/users", json={"email": "new@acme.test", "password": "temporary-pass-123"})
    assert r.status_code == 201 and r.json()["must_change_password"] is True

    new = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    r = await new.post("/api/auth/login", json={"email": "new@acme.test", "password": "temporary-pass-123"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    new.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    # Nothing but /me, /logout and /change-password works.
    for method, path in (
        ("GET", "/api/conversations"),
        ("GET", "/api/models"),
        ("GET", "/api/usage/me"),
        ("POST", "/api/auth/mfa/setup"),
        ("GET", "/api/containers"),
    ):
        rr = await new.request(method, path)
        assert rr.status_code == 403 and rr.json()["detail"] == "password_change_required", path
    assert (await new.get("/api/auth/me")).json()["must_change_password"] is True
    body = {"current_password": "wrong-one-12345", "new_password": "My-own-secret-pass-9"}
    assert (await new.post("/api/auth/change-password", json=body)).status_code == 401
    body["current_password"] = "temporary-pass-123"
    for bad in ("temporary-pass-123", "short", "new@acme.test", "zzzzzzzzzzzzzz"):
        assert (await new.post("/api/auth/change-password", json={**body, "new_password": bad})).status_code == 422
    r = await new.post("/api/auth/change-password", json=body)
    assert r.status_code == 200 and r.json()["must_change_password"] is False
    new.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert (await new.get("/api/conversations")).status_code == 200
    # The old temporary password no longer works.
    other = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    r = await other.post("/api/auth/login", json={"email": "new@acme.test", "password": "temporary-pass-123"})
    assert r.status_code == 401
    # An admin resetting someone's password makes it temporary again and ends their sessions.
    uid = (await new.get("/api/auth/me")).json()["id"]
    r = await client.patch(f"/api/admin/users/{uid}", json={"password": "another-temp-pass-1"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert (await new.get("/api/auth/me")).status_code == 401
    actions = (await db.scalars(select(AuditLog.action).where(AuditLog.action == "auth.password.change"))).all()
    assert len(actions) == 2  # one denied, one allowed
    await new.aclose()
    await other.aclose()


async def test_people_one_by_one_and_csv_with_per_row_errors(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await through_admin(db, client)
    eng = await make_role(db, "Engineering")
    await make_role(db, "HR")
    await db.commit()
    r = await client.post(
        "/api/setup/users", json={"email": "Ann@Example.com", "display_name": "Ann", "role_ids": [str(eng.id)]}
    )
    assert r.status_code == 201
    temp = r.json()["temporary_password"]
    assert temp and len(temp) >= 12
    assert (await client.post("/api/setup/users", json={"email": "ann@example.com"})).status_code == 422
    assert (await client.post("/api/setup/users", json={"email": "not-an-email"})).status_code == 422

    csv_text = "\n".join(
        [
            "email,name,role",
            "bob@example.com,Bob,Engineering",
            "carol@example.com,Carol,HR;Engineering",
            "dave@example.com,Dave,Marketing",  # unknown role
            "bob@example.com,Bob again,HR",  # duplicate in the list
            "not-an-email,X,HR",  # invalid email
            "ann@example.com,Ann,HR",  # already exists
            "",
            "erin@example.com,Erin,",  # no role is fine
            "a,b,c,d",  # too many columns
        ]
    )
    r = await client.post("/api/setup/users/import", json={"csv": csv_text})
    assert r.status_code == 200
    out = r.json()
    assert [c["email"] for c in out["created"]] == ["bob@example.com", "carol@example.com", "erin@example.com"]
    errs = {e["line"]: e["error"] for e in out["errors"]}
    assert set(errs) == {4, 5, 6, 7, 10}
    assert "unknown role" in errs[4] and "duplicate" in errs[5] and "invalid email" in errs[6]
    assert "already exists" in errs[7] and "expected" in errs[10]
    carol = await db.scalar(select(User).where(User.email == "carol@example.com"))
    assert carol is not None and carol.must_change_password
    await db.refresh(carol, ["roles"])
    assert sorted(r.name for r in carol.roles) == ["Engineering", "HR"]
    assert await db.scalar(select(User).where(User.email == "dave@example.com")) is None
    # Temporary passwords are shown once and never stored in the audit log.
    audit_text = " ".join(str(a.details) for a in (await db.scalars(select(AuditLog))).all())
    for c in out["created"]:
        assert c["temporary_password"] not in audit_text
    assert temp not in audit_text
    # The temporary password works once, for the forced change.
    other = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    pw = out["created"][0]["temporary_password"]
    r = await other.post("/api/auth/login", json={"email": "bob@example.com", "password": pw})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    await other.aclose()


async def test_organization_and_cloud_ceiling_stored_in_the_database(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    s = get_settings()
    assert s.cloud_max_classification == "internal"  # the environment default
    await through_admin(db, client)
    org = (await client.get("/api/setup/organization")).json()
    assert org["cloud_max_classification"] == "internal" and org["cloud_max_source"] == "environment"
    r = await client.put(
        "/api/setup/organization", json={"company_name": "Example GmbH", "cloud_max_classification": "public"}
    )
    assert r.status_code == 200 and r.json()["cloud_max_source"] == "database"
    assert registry.cloud_cap(s).value == "public"
    assert (await client.get("/api/info")).json()["company_name"] == "Example GmbH"
    # The provider screen and the router use the stored ceiling.
    assert (await client.get("/api/admin/providers")).json()["cloud_max_classification"] == "public"
    assert (
        await client.put("/api/setup/organization", json={"cloud_max_classification": "confidential"})
    ).status_code == 422
    # Raising it needs a recent re-authentication.
    await _age_reauth(db, ADMIN_EMAIL)
    r = await client.put("/api/setup/organization", json={"cloud_max_classification": "internal"})
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    assert (await client.post("/api/auth/step-up", json={"password": ADMIN_PASSWORD})).status_code == 200
    assert (
        await client.put("/api/setup/organization", json={"cloud_max_classification": "internal"})
    ).status_code == 200
    assert registry.cloud_cap(s).value == "internal"
    # The company name alone does not need it.
    await _age_reauth(db, ADMIN_EMAIL)
    assert (await client.put("/api/setup/organization", json={"company_name": "Example AG"})).status_code == 200


async def test_skipping_the_organization_step_leaves_the_stricter_ceiling(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await through_admin(db, client)
    assert (await client.post("/api/setup/steps/organization", json={"status": "skipped"})).status_code == 200
    assert (await client.get("/api/setup/organization")).json()["cloud_max_classification"] == "public"


async def test_demo_installation_without_a_known_password_admin_and_its_removal(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await through_admin(db, client)
    r = await client.post("/api/setup/install-type", json={"type": "demo"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["type"] == "demo" and out["containers"] == len(CONTAINERS) and out["documents_queued"] > 0
    emails = set((await db.scalars(select(User.email))).all())
    assert "admin@acme.test" not in emails  # no administrator with the published password
    assert {p.email for p in PERSONAS if not p.is_admin} <= emails
    assert [u.email for u in (await db.scalars(select(User).where(User.is_admin))).all()] == [ADMIN_EMAIL]
    info = (await client.get("/api/info")).json()
    assert info["demo"] is True and info["company_name"] == "ACME Corp"
    # Choosing "empty" afterwards is refused while demo data exists.
    assert (await client.post("/api/setup/install-type", json={"type": "empty"})).status_code == 409

    inv = (await client.get("/api/setup/demo")).json()
    assert inv["present"] and len(inv["containers"]) == len(CONTAINERS) and inv["documents"] > 0
    assert "Executive" in inv["roles"] and ADMIN_EMAIL not in inv["users"]
    # Removal needs a recent re-authentication.
    await _age_reauth(db, ADMIN_EMAIL)
    r = await client.delete("/api/setup/demo")
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    assert (await client.post("/api/auth/step-up", json={"password": ADMIN_PASSWORD})).status_code == 200
    r = await client.delete("/api/setup/demo")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert [u.email for u in (await db.scalars(select(User))).all()] == [ADMIN_EMAIL]
    assert (await db.scalars(select(Role))).all() == []
    assert (await db.scalars(select(Container))).all() == []
    assert (await db.scalars(select(Document))).all() == []
    assert (await db.scalars(select(McpServer))).all() == []
    assert (await client.get("/api/info")).json()["demo"] is False
    assert (await client.delete("/api/setup/demo")).status_code == 409
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "setup.demo.remove")) is not None


async def test_finish_refused_in_production_while_a_known_password_admin_exists(
    db: AsyncSession, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await through_admin(db, client)
    # A second administrator with a temporary (known) password.
    r = await client.post(
        "/api/admin/users", json={"email": "two@example.com", "password": "temporary-pass-12", "is_admin": True}
    )
    assert r.status_code == 201
    prod = get_settings().model_copy(update={"env": "prod"})
    monkeypatch.setattr("app.api.setup.get_settings", lambda: prod)
    r = await client.post("/api/setup/finish")
    assert r.status_code == 409 and "two@example.com" in r.json()["detail"]
    # Once that password is replaced, finishing works.
    await db.execute(update(User).where(User.email == "two@example.com").values(must_change_password=False))
    await db.commit()
    assert (await client.post("/api/setup/finish")).status_code == 200


async def test_post_setup_checklist_and_assistant(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await through_admin(db, client)
    r = await client.post("/api/setup/finish")
    assert r.status_code == 200
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]  # finishing starts a fresh session
    cl = (await client.get("/api/admin/setup/checklist")).json()
    items = {i["key"]: i for i in cl["items"]}
    assert cl["visible"] is True
    assert items["mfa"]["done"] is False and items["second_admin"]["done"] is False
    assert items["remove_demo"]["done"] is True and items["known_password_admin"]["done"] is True
    assert (await client.post("/api/admin/setup/checklist/remove_demo/dismiss")).status_code == 422
    for key in ("mfa", "master_key_backup", "second_admin", "provider_budgets", "review_router"):
        assert (await client.post(f"/api/admin/setup/checklist/{key}/dismiss")).status_code == 204
    assert (await client.get("/api/admin/setup/checklist")).json()["visible"] is False
    st = (await client.get("/api/admin/setup/state")).json()
    assert st["completed"] and st["completed_via"] == "wizard" and st["license_accepted_by"] == "Ada IT"
    # The assistant uses the normal admin session; employees cannot reach it.
    await make_user(db, "emp@acme.test")
    await db.commit()
    emp = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    await login(emp, "emp@acme.test", PASSWORD)
    assert (await emp.get("/api/admin/setup/checklist")).status_code == 403
    assert (await emp.get("/api/admin/setup/environment")).status_code == 403
    await emp.aclose()
