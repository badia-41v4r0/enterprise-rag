"""Identity, sessions, CSRF, effective permissions and the admin API against real Postgres."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter
from app.authz.policy import effective_permissions
from app.models import Action, AuditLog, Classification, ResourceType, User
from app.services.users import create_admin
from tests.factories import PASSWORD, grant, login, make_container, make_document, make_role, make_user

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    login_limiter.reset()


async def test_effective_permissions_union_of_roles(db: AsyncSession) -> None:
    hr, eng = await make_role(db, "hr"), await make_role(db, "eng")
    c_hr = await make_container(db, "HR", Classification.confidential)
    c_eng = await make_container(db, "Eng", Classification.internal)
    d = await make_document(db, c_hr, "salaries", restricted=True)
    await grant(db, hr, ResourceType.container, c_hr.id)
    await grant(db, hr, ResourceType.document, d.id)
    await grant(db, eng, ResourceType.container, c_eng.id, Action.manage)
    both = await make_user(db, "both@acme.test", roles=[hr, eng])
    only_eng = await make_user(db, "eng@acme.test", roles=[eng])
    await db.commit()

    p = await effective_permissions(db, both)
    assert p.readable_containers == {c_hr.id, c_eng.id}
    assert p.manageable_containers == {c_eng.id}
    assert p.readable_restricted_documents == {d.id}

    p2 = await effective_permissions(db, only_eng)
    assert p2.readable_containers == {c_eng.id}
    assert p2.readable_restricted_documents == frozenset()


async def test_inactive_user_has_no_permissions(db: AsyncSession) -> None:
    r = await make_role(db, "r")
    c = await make_container(db, "C", Classification.public)
    await grant(db, r, ResourceType.container, c.id)
    u = await make_user(db, "x@acme.test", roles=[r])
    u.is_active = False
    await db.commit()
    p = await effective_permissions(db, u)
    assert p.readable_containers == frozenset()


async def test_login_me_logout_and_bad_password(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "alice@acme.test")
    await db.commit()

    r = await client.post("/api/auth/login", json={"email": "alice@acme.test", "password": "wrong-password"})
    assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"email": "nobody@acme.test", "password": PASSWORD})
    assert r.status_code == 401

    r = await client.post("/api/auth/login", json={"email": "Alice@acme.test", "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    csrf = r.json()["csrf_token"]

    r = await client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["email"] == "alice@acme.test"

    # Mutating request without CSRF header is rejected.
    r = await client.post("/api/auth/logout")
    assert r.status_code == 403
    r = await client.post("/api/auth/logout", headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    r = await client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    client.cookies.clear()
    assert (await client.get("/api/auth/me")).status_code == 401

    denies = (await db.scalars(select(AuditLog).where(AuditLog.decision == "deny"))).all()
    assert len(denies) == 2


async def test_login_rate_limit(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "bob@acme.test")
    await db.commit()
    codes = [
        (
            await client.post("/api/auth/login", json={"email": "bob@acme.test", "password": "nope-nope-nope"})
        ).status_code
        for _ in range(12)
    ]
    assert codes[-1] == 429


async def test_admin_api_requires_admin(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "user@acme.test")
    await db.commit()
    await login(client, "user@acme.test")
    assert (await client.get("/api/admin/users")).status_code == 403
    assert (await client.post("/api/admin/roles", json={"name": "x"})).status_code == 403


async def test_cli_admin_can_set_up_roles_containers_grants(db: AsyncSession, client: httpx.AsyncClient) -> None:
    assert await create_admin("root@acme.test", "a-very-long-admin-password")
    assert not await create_admin("root@acme.test", "a-very-long-admin-password")
    await login(client, "root@acme.test", "a-very-long-admin-password")

    role = (await client.post("/api/admin/roles", json={"name": "Engineer"})).json()
    cont = (
        await client.post("/api/admin/containers", json={"name": "Engineering", "classification": "internal"})
    ).json()
    assert cont["slug"] == "engineering"
    r = await client.post(
        "/api/admin/users",
        json={"email": "eve@acme.test", "password": "eve-password-123", "role_ids": [role["id"]]},
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]

    g = await client.post(
        "/api/admin/grants",
        json={"role_id": role["id"], "resource_type": "container", "resource_id": cont["id"], "action": "read"},
    )
    assert g.status_code == 201
    bad = await client.post(
        "/api/admin/grants",
        json={"role_id": role["id"], "resource_type": "container", "resource_id": cont["id"], "action": "execute"},
    )
    assert bad.status_code == 422
    dup = await client.post(
        "/api/admin/grants",
        json={"role_id": role["id"], "resource_type": "container", "resource_id": cont["id"], "action": "read"},
    )
    assert dup.status_code == 409

    eve = await db.scalar(select(User).where(User.email == "eve@acme.test"))
    assert eve is not None
    p = await effective_permissions(db, eve)
    assert {str(c) for c in p.readable_containers} == {cont["id"]}

    # Revoking the grant takes effect immediately.
    assert (await client.delete(f"/api/admin/grants/{g.json()['id']}")).status_code == 204
    await db.refresh(eve)
    p = await effective_permissions(db, eve)
    assert p.readable_containers == frozenset()

    # Deactivation kills sessions.
    other = httpx.AsyncClient(transport=client._transport, base_url="http://testserver")
    await login(other, "eve@acme.test", "eve-password-123")
    assert (await other.get("/api/auth/me")).status_code == 200
    r = await client.patch(f"/api/admin/users/{user_id}", json={"is_active": False})
    assert r.status_code == 200
    assert (await other.get("/api/auth/me")).status_code == 401
    await other.aclose()

    actions = set((await db.scalars(select(AuditLog.action))).all())
    assert {"admin.role.create", "admin.grant.create", "admin.grant.delete", "admin.user.update"} <= actions
