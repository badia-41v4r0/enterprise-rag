"""Sessions, conversation retention and usage per role (R16)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AuditLog, Conversation, LlmUsage, Message, Session
from app.services import maintenance
from tests.factories import login, make_role, make_user

pytestmark = pytest.mark.db


def second_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url))


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    s = get_settings()

    def set_(**values: object) -> None:
        for k, v in values.items():
            monkeypatch.setattr(s, k, v)

    return set_


async def test_idle_session_ends_and_activity_is_recorded(
    db: AsyncSession, client: httpx.AsyncClient, settings_override
) -> None:  # type: ignore[no-untyped-def]
    settings_override(session_idle_minutes=30)
    await make_user(db, "u@x.test")
    await db.commit()
    await login(client, "u@x.test")
    assert (await client.get("/api/auth/me")).status_code == 200
    sess = await db.scalar(select(Session))
    assert sess is not None and sess.last_seen_at is not None  # touched by the request
    # 31 minutes without a request: the session is gone.
    await db.execute(update(Session).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=31)))
    await db.commit()
    assert (await client.get("/api/auth/me")).status_code == 401
    assert await db.scalar(select(func.count()).select_from(Session)) == 0


async def test_idle_timeout_can_be_disabled(db: AsyncSession, client: httpx.AsyncClient, settings_override) -> None:  # type: ignore[no-untyped-def]
    settings_override(session_idle_minutes=0)
    await make_user(db, "u@x.test")
    await db.commit()
    await login(client, "u@x.test")
    await db.execute(update(Session).values(last_seen_at=datetime.now(UTC) - timedelta(hours=11)))
    await db.commit()
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_sign_out_everywhere_ends_every_session_of_that_user_only(
    db: AsyncSession, client: httpx.AsyncClient
) -> None:
    await make_user(db, "u@x.test")
    await make_user(db, "other@x.test")
    await db.commit()
    laptop, phone, other = client, second_client(client), second_client(client)
    await login(laptop, "u@x.test")
    await login(phone, "u@x.test")
    await login(other, "other@x.test")
    assert (await laptop.post("/api/auth/logout-all")).status_code == 204
    assert (await phone.get("/api/auth/me")).status_code == 401
    assert (await laptop.get("/api/auth/me")).status_code == 401
    assert (await other.get("/api/auth/me")).status_code == 200
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "auth.logout_all"))
    assert entry is not None and entry.details["sessions"] == 2
    await phone.aclose()
    await other.aclose()


async def test_maintenance_deletes_expired_and_idle_sessions(
    db: AsyncSession, client: httpx.AsyncClient, settings_override
) -> None:  # type: ignore[no-untyped-def]
    settings_override(session_idle_minutes=60)
    for email in ("a@x.test", "b@x.test", "c@x.test"):
        await make_user(db, email)
    await db.commit()
    for email in ("a@x.test", "b@x.test", "c@x.test"):
        await login(second_client(client), email)
    ids = list((await db.scalars(select(Session.id).order_by(Session.id))).all())
    now = datetime.now(UTC)
    await db.execute(update(Session).where(Session.id == ids[0]).values(expires_at=now - timedelta(seconds=1)))
    await db.execute(update(Session).where(Session.id == ids[1]).values(last_seen_at=now - timedelta(hours=2)))
    await db.commit()
    report = await maintenance.run_once()
    assert report.ran and report.sessions_deleted == 2
    assert list((await db.scalars(select(Session.id))).all()) == [ids[2]]


async def test_retention_deletes_inactive_conversations_and_audits_counts_only(
    db: AsyncSession, settings_override
) -> None:  # type: ignore[no-untyped-def]
    u = await make_user(db, "u@x.test")
    now = datetime.now(UTC)
    old, active, empty_old = (
        Conversation(user_id=u.id, title="secret old title", created_at=now - timedelta(days=100)),
        Conversation(user_id=u.id, title="old but active", created_at=now - timedelta(days=100)),
        Conversation(user_id=u.id, title="empty", created_at=now - timedelta(days=100)),
    )
    db.add_all([old, active, empty_old])
    await db.flush()
    db.add(Message(conversation_id=old.id, role="user", content="hi", created_at=now - timedelta(days=95)))
    db.add(Message(conversation_id=active.id, role="user", content="hi", created_at=now - timedelta(days=1)))
    await db.commit()

    settings_override(conversation_retention_days=0)
    assert (await maintenance.run_once()).conversations_deleted == 0  # default: keep

    settings_override(conversation_retention_days=90)
    report = await maintenance.run_once()
    assert report.conversations_deleted == 2
    titles = set((await db.scalars(select(Conversation.title))).all())
    assert titles == {"old but active"}
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "retention.conversations.delete"))
    assert entry is not None and entry.details["deleted"] == 2 and "secret" not in str(entry.details)


async def test_maintenance_runs_once_across_replicas(db: AsyncSession) -> None:
    from sqlalchemy import text

    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as holder:  # another replica in the middle of its run
        assert await holder.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": maintenance.LOCK_KEY})
        assert not (await maintenance.run_once()).ran
        await holder.rollback()
    assert (await maintenance.run_once()).ran


async def test_usage_by_role_json_and_csv(db: AsyncSession, client: httpx.AsyncClient) -> None:
    sales, eng = await make_role(db, "Sales"), await make_role(db, "=Engineering")
    s1 = await make_user(db, "s1@x.test", roles=[sales])
    both = await make_user(db, "both@x.test", roles=[sales, eng])
    lone = await make_user(db, "lone@x.test")
    await make_user(db, "admin@x.test", admin=True)
    for uid, day, req, cost in [
        (s1.id, "2026-11-01", 3, 0.3),
        (both.id, "2026-11-02", 2, 0.2),
        (lone.id, "2026-11-02", 1, 0.1),
        (s1.id, "2026-10-31", 50, 5.0),  # outside the range
    ]:
        db.add(
            LlmUsage(
                provider="p",
                model="m",
                user_id=uid,
                day=day,
                month=day[:7],
                requests=req,
                prompt_tokens=10 * req,
                completion_tokens=req,
                cost_usd=cost,
            )
        )
    await db.commit()
    await login(client, "admin@x.test")
    body = (await client.get("/api/admin/usage/by-role?since=2026-11-01&until=2026-11-30")).json()
    by = {r["role"]: r for r in body["roles"]}
    assert by["Sales"]["requests"] == 5 and by["Sales"]["users"] == 2
    assert by["=Engineering"]["requests"] == 2
    assert by["(no role)"]["requests"] == 1 and by["(no role)"]["users"] == 1
    r = await client.get("/api/admin/usage/by-role?since=2026-11-01&until=2026-11-30&format=csv")
    rows = {row["role"]: row for row in csv.DictReader(io.StringIO(r.text))}
    assert "'=Engineering" in rows and rows["Sales"]["requests"] == "5"
    assert (await client.get("/api/admin/usage/by-role?since=2026-12-01&until=2026-11-01")).status_code == 422
