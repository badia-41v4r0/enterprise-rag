"""Users at company scale (R16): paginated list with filters, bulk actions, CSV import and export, the audit log
page, and the permission cache staying exact through all of them."""

from __future__ import annotations

import csv
import io

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.policy import effective_permissions
from app.models import Action, AuditLog, Classification, ResourceType, Session, User, UserRole
from tests.factories import grant, login, make_container, make_role, make_user

pytestmark = pytest.mark.db


@pytest.fixture
async def world(db: AsyncSession) -> dict[str, object]:
    sales = await make_role(db, "Sales")
    eng = await make_role(db, "Engineering")
    c = await make_container(db, "Sales docs", Classification.internal)
    await grant(db, sales, ResourceType.container, c.id, Action.read)
    admin = await make_user(db, "admin@x.test", admin=True)
    for i in range(30):
        await make_user(db, f"user{i:02d}@x.test", roles=[sales] if i % 3 == 0 else [eng])
    await db.commit()
    return {"sales": sales, "eng": eng, "container": c, "admin": admin}


async def all_pages(client: httpx.AsyncClient, params: str, limit: int = 7) -> tuple[list[str], int]:
    emails: list[str] = []
    cursor = ""
    total = -1
    while True:
        r = await client.get(f"/api/admin/users/page?limit={limit}{params}{cursor}")
        assert r.status_code == 200, r.text
        body = r.json()
        total = body["total"]
        emails += [u["email"] for u in body["items"]]
        if body["next_cursor"] is None:
            return emails, total
        cursor = f"&cursor={body['next_cursor']}"


async def test_pagination_covers_every_user_once_in_order(client: httpx.AsyncClient, world: dict) -> None:
    await login(client, "admin@x.test")
    emails, total = await all_pages(client, "")
    assert total == 31 and len(emails) == 31 and emails == sorted(set(emails))


async def test_filters_search_role_status_and_last_login(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await login(client, "admin@x.test")
    emails, total = await all_pages(client, f"&role_id={world['sales'].id}")
    assert total == 10 and all(int(e[4:6]) % 3 == 0 for e in emails)
    emails, _ = await all_pages(client, "&q=USER0")  # case-insensitive substring
    assert emails == [f"user0{i}@x.test" for i in range(10)]
    emails, _ = await all_pages(client, "&q=%25")  # a literal %, not a wildcard
    assert emails == []
    emails, _ = await all_pages(client, "&never_logged_in=true")
    assert "admin@x.test" not in emails and len(emails) == 30  # the admin just logged in
    admin = (await client.get("/api/admin/users/page?q=admin")).json()["items"][0]
    assert admin["last_login_at"] is not None


async def test_bulk_role_changes_are_effective_immediately_and_audited(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await login(client, "admin@x.test")
    container_id, sales_id = world["container"].id, str(world["sales"].id)

    async def readable() -> frozenset:
        await db.commit()  # end the test session's transaction: see what the API committed
        target = await db.scalar(select(User).where(User.email == "user01@x.test"))
        assert target is not None
        return (await effective_permissions(db, target)).readable_containers

    assert container_id not in await readable()  # now cached for this epoch
    ids = [str(u.id) for u in (await db.scalars(select(User).where(User.email.like("user0%")))).all()]
    r = await client.post("/api/admin/users/bulk", json={"action": "add_role", "role_id": sales_id, "user_ids": ids})
    assert r.status_code == 200 and r.json()["changed"] == 6  # 00, 03, 06, 09 already had it
    assert container_id in await readable()  # the cache did not hide the new grant
    r = await client.post("/api/admin/users/bulk", json={"action": "remove_role", "role_id": sales_id, "user_ids": ids})
    assert r.json()["changed"] == 10
    assert container_id not in await readable()
    entries = (await db.scalars(select(AuditLog.action).where(AuditLog.action.like("admin.user.bulk.%")))).all()
    assert sorted(entries) == ["admin.user.bulk.add_role", "admin.user.bulk.remove_role"]


async def test_bulk_deactivate_ends_sessions_but_never_the_actor(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    other = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url))
    await login(other, "user05@x.test")
    await login(client, "admin@x.test")
    victim = await db.scalar(select(User).where(User.email == "user05@x.test"))
    assert victim is not None
    ids = [str(victim.id), str(world["admin"].id)]
    r = await client.post("/api/admin/users/bulk", json={"action": "deactivate", "user_ids": ids})
    assert r.json()["changed"] == 1
    assert (await other.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/auth/me")).status_code == 200
    assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == victim.id)) == 0
    await other.aclose()


async def test_bulk_rejects_unknown_ids(client: httpx.AsyncClient, world: dict) -> None:
    await login(client, "admin@x.test")
    r = await client.post(
        "/api/admin/users/bulk",
        json={"action": "deactivate", "user_ids": ["00000000-0000-0000-0000-000000000001"]},
    )
    assert r.status_code == 422


async def test_export_csv_is_filtered_and_neutralizes_formulas(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await make_user(db, "evil@x.test")
    u = await db.scalar(select(User).where(User.email == "evil@x.test"))
    assert u is not None
    u.display_name = "=HYPERLINK(1)"
    await db.commit()
    await login(client, "admin@x.test")
    r = await client.get(f"/api/admin/users/export.csv?role_id={world['sales'].id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 10 and all(row["roles"] == "Sales" for row in rows)
    r = await client.get("/api/admin/users/export.csv?q=evil")
    assert next(iter(csv.DictReader(io.StringIO(r.text))))["display_name"] == "'=HYPERLINK(1)"


async def test_import_validates_everything_before_writing(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await login(client, "admin@x.test")
    bad = "email,display_name,roles\nnew1@x.test,New One,Sales\nnot-an-email,,\nnew2@x.test,,Nope\n"
    r = await client.post("/api/admin/users/import", json={"csv": bad, "dry_run": False})
    body = r.json()
    assert r.status_code == 200 and not body["applied"] and len(body["errors"]) == 2
    assert await db.scalar(select(User).where(User.email == "new1@x.test")) is None
    admin_col = "email,roles,is_admin\nx@x.test,,true\n"
    assert (
        "admin rights are not imported"
        in (await client.post("/api/admin/users/import", json={"csv": admin_col})).json()["errors"][0]
    )


async def test_import_dry_run_then_apply_with_temporary_passwords(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await login(client, "admin@x.test")
    content = (
        "﻿Email,Display_Name,Roles\nNew1@x.test,New One,Sales;Engineering\nuser01@x.test,,Sales\nuser03@x.test,,Sales\n"
    )
    dry = (await client.post("/api/admin/users/import", json={"csv": content})).json()
    assert not dry["applied"] and dry["created"] == 1 and dry["updated"] == 1
    assert all(r["temporary_password"] is None for r in dry["rows"])
    assert await db.scalar(select(User).where(User.email == "new1@x.test")) is None

    applied = (await client.post("/api/admin/users/import", json={"csv": content, "dry_run": False})).json()
    assert applied["applied"] and applied["created"] == 1 and applied["updated"] == 1
    created = next(r for r in applied["rows"] if r["action"] == "create")
    assert created["temporary_password"] and created["added_roles"] == ["Sales", "Engineering"]
    # The temporary password works, and it is not in the audit log.
    fresh = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url))
    await login(fresh, "new1@x.test", created["temporary_password"])
    me = (await fresh.get("/api/auth/me")).json()
    assert me["must_change_password"] is True  # a temporary password (ADR 0037)
    assert (await fresh.get("/api/containers")).status_code == 403  # nothing else works until it is changed
    await fresh.aclose()
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "admin.user.import"))
    assert entry is not None and created["temporary_password"] not in str(entry.details)
    # Roles are only added: user01 keeps Engineering and gains Sales.
    u1 = await db.scalar(select(User).where(User.email == "user01@x.test"))
    assert u1 is not None
    names = set((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == u1.id))).all())
    assert names == {world["sales"].id, world["eng"].id}


async def test_applying_an_import_needs_recent_reauthentication(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    await login(client, "admin@x.test")
    sess = await db.scalar(select(Session))
    assert sess is not None
    sess.reauth_at = None
    await db.commit()
    r = await client.post("/api/admin/users/import", json={"csv": "email\nz@x.test\n", "dry_run": False})
    assert r.status_code == 403
    assert (await client.post("/api/admin/users/import", json={"csv": "email\nz@x.test\n"})).status_code == 200


async def test_audit_page_is_keyset_paginated_and_filtered(
    db: AsyncSession, client: httpx.AsyncClient, world: dict
) -> None:
    for i in range(25):
        db.add(AuditLog(action="test.event", resource=f"doc:{i}", details={}))
    db.add(AuditLog(action="other.event", resource="doc:x", details={}))
    await db.commit()
    await login(client, "admin@x.test")
    seen: list[int] = []
    cursor = ""
    while True:
        body = (await client.get(f"/api/admin/audit/page?limit=10&action=test.{cursor}")).json()
        seen += [e["id"] for e in body["items"]]
        assert all(e["action"] == "test.event" for e in body["items"])
        if body["next_cursor"] is None:
            break
        cursor = f"&before={body['next_cursor']}"
    assert len(seen) == 25 and seen == sorted(seen, reverse=True)
    body = (await client.get("/api/admin/audit/page?resource=doc:1")).json()
    assert {e["resource"] for e in body["items"]} == {"doc:1"} | {f"doc:{i}" for i in range(10, 20)}
    # `_` and `%` in a filter are literal, not wildcards.
    assert (await client.get("/api/admin/audit/page?action=test_")).json()["items"] == []
