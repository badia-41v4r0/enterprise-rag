"""Periodic housekeeping (R16): expired and idle sessions, conversation retention.

Runs inside the app process every `maintenance_interval_minutes`, and on demand with `erag maintenance`. Several
replicas may run it at once: a transaction-level advisory lock lets one of them do the work and the others skip.
Deletes go in batches so a large backlog never holds long locks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.services import genqueue, reservations
from app.services.audit import audit

log = logging.getLogger(__name__)

LOCK_KEY = "erag.maintenance"
BATCH = 1000

_EXPIRED_SESSIONS = text(
    """
    DELETE FROM sessions WHERE id IN (
        SELECT id FROM sessions
        WHERE expires_at <= :now
           OR (CAST(:idle_cutoff AS timestamptz) IS NOT NULL AND NOT mfa_pending
               AND coalesce(last_seen_at, created_at) < CAST(:idle_cutoff AS timestamptz))
        LIMIT :batch)
    """
)

# A conversation's last activity is its newest message (or its creation, when empty).
_OLD_CONVERSATIONS = text(
    """
    DELETE FROM conversations WHERE id IN (
        SELECT c.id FROM conversations c
        WHERE c.created_at < :cutoff
          AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.created_at >= :cutoff)
        LIMIT :batch)
    RETURNING user_id
    """
)


@dataclass
class MaintenanceReport:
    ran: bool
    sessions_deleted: int = 0
    conversations_deleted: int = 0
    reservations_expired: int = 0
    queue_rows_expired: int = 0


async def run_once(now: datetime | None = None) -> MaintenanceReport:
    s = get_settings()
    now = now or datetime.now(UTC)
    idle_cutoff = now - timedelta(minutes=s.session_idle_minutes) if s.session_idle_minutes else None
    report = MaintenanceReport(ran=False)
    async with get_sessionmaker()() as db:
        got = await db.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": LOCK_KEY})
        if not got:
            return report
        report.ran = True
        # R17 state left by requests or replicas that died: it already stopped counting when it expired.
        report.reservations_expired = await reservations.purge_expired(db, now)
        await db.execute(
            text("DELETE FROM rate_limit_counters WHERE window_start < :old"), {"old": int(now.timestamp()) - 86_400}
        )
        while True:
            res = await db.execute(_EXPIRED_SESSIONS, {"now": now, "idle_cutoff": idle_cutoff, "batch": BATCH})
            n = res.rowcount  # type: ignore[attr-defined]
            report.sessions_deleted += n
            if n < BATCH:
                break
        if s.conversation_retention_days > 0:
            cutoff = now - timedelta(days=s.conversation_retention_days)
            owners: set[str] = set()
            while True:
                rows = (await db.execute(_OLD_CONVERSATIONS, {"cutoff": cutoff, "batch": BATCH})).all()
                report.conversations_deleted += len(rows)
                owners.update(str(r[0]) for r in rows)
                if len(rows) < BATCH:
                    break
            if report.conversations_deleted:
                # Counts only: titles and contents are what retention removes.
                audit(
                    db,
                    "retention.conversations.delete",
                    details={
                        "deleted": report.conversations_deleted,
                        "users": len(owners),
                        "older_than_days": s.conversation_retention_days,
                    },
                )
        await db.commit()
    report.queue_rows_expired = await genqueue.purge_expired(now)
    return report


async def loop() -> None:
    """The in-process schedule. Errors are logged and retried at the next interval."""
    interval = max(get_settings().maintenance_interval_minutes, 1) * 60
    while True:
        try:
            report = await run_once()
            if report.sessions_deleted or report.conversations_deleted:
                log.info(
                    "maintenance: %d sessions, %d conversations deleted",
                    report.sessions_deleted,
                    report.conversations_deleted,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("maintenance run failed")
        await asyncio.sleep(interval)
