"""Deleting users (ADR 0033), against real Postgres."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter
from app.models import AuditLog, Conversation, LlmUsage, Session, User, UserRole
from tests.factories import login, make_role, make_user

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    login_limiter.reset()


async def _admin_session(db: AsyncSession, client: httpx.AsyncClient) -> uuid.UUID:
    u = await make_user(db, "admin@acme.test", admin=True)
    uid = u.id
    await db.commit()
    await login(client, "admin@acme.test")
    return uid


async def test_delete_removes_dependents_and_keeps_audit_and_usage(db: AsyncSession, client: httpx.AsyncClient) -> None:
    admin_id = await _admin_session(db, client)
    role = await make_role(db, "Tester")
    victim = await make_user(db, "gone@acme.test", roles=[role])
    victim_id = victim.id
    db.add(Conversation(user_id=victim_id, title="private chat"))
    db.add(LlmUsage(provider="ollama", model="m", user_id=victim_id, day="2026-11-09", month="2026-11", requests=3))
    await db.commit()
    # Give the victim a session (the client holds one cookie, so log back in as the admin afterwards).
    await login(client, "gone@acme.test")
    await login(client, "admin@acme.test")

    assert (await client.patch(f"/api/admin/users/{victim_id}", json={"is_active": False})).status_code == 200
    r = await client.delete(f"/api/admin/users/{victim_id}")
    assert r.status_code == 204, r.text

    db.expire_all()
    assert await db.get(User, victim_id) is None
    assert (await db.scalars(select(UserRole).where(UserRole.user_id == victim_id))).all() == []
    assert (await db.scalars(select(Session).where(Session.user_id == victim_id))).all() == []
    assert (await db.scalars(select(Conversation).where(Conversation.user_id == victim_id))).all() == []
    # Usage figures (numbers only) and the audit trail survive.
    assert (await db.scalars(select(LlmUsage).where(LlmUsage.user_id == victim_id))).one().requests == 3
    entry = (await db.scalars(select(AuditLog).where(AuditLog.action == "admin.user.delete"))).one()
    assert entry.user_id == admin_id and entry.details["email"] == "gone@acme.test"
    assert entry.details["roles"] == ["Tester"]
    # The role itself is untouched.
    assert (await client.get("/api/admin/roles")).status_code == 200


async def test_delete_is_refused_for_active_users_self_and_missing_users(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    admin_id = await _admin_session(db, client)
    active = await make_user(db, "still@acme.test")
    active_id = active.id
    await db.commit()

    r = await client.delete(f"/api/admin/users/{active_id}")
    assert r.status_code == 409 and "Deactivate" in r.json()["detail"]
    assert (await client.delete(f"/api/admin/users/{admin_id}")).status_code == 400
    assert (await client.delete(f"/api/admin/users/{uuid.uuid4()}")).status_code == 404
    db.expire_all()
    assert await db.get(User, active_id) is not None


async def test_delete_needs_an_admin_and_a_recent_reauthentication(db: AsyncSession, client: httpx.AsyncClient) -> None:
    admin_id = await _admin_session(db, client)
    victim = await make_user(db, "victim@acme.test")
    victim.is_active = False
    victim_id = victim.id
    await make_user(db, "plain@acme.test")
    await db.commit()

    # Step-up: age the admin's re-authentication.
    await db.execute(
        update(Session).where(Session.user_id == admin_id).values(reauth_at=datetime.now(UTC) - timedelta(minutes=10))
    )
    await db.commit()
    r = await client.delete(f"/api/admin/users/{victim_id}")
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    db.expire_all()
    assert await db.get(User, victim_id) is not None

    # A non-admin cannot delete anyone.
    await login(client, "plain@acme.test")
    assert (await client.delete(f"/api/admin/users/{victim_id}")).status_code == 403
