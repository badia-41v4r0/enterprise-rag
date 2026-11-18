"""Budget reservations (R17, PLAN-v0.5 3.2; closes the open item of ADR 0026).

A check followed by the request is not enough with several requests in flight, in one replica or many: two of
them can both see room for one. So before anything is sent, a request reserves its worst case on every budget
that applies to it, and the checks count active reservations as if they were spent:

1. In a short transaction of its own, the request takes a transaction-level advisory lock per scope, always in
   sorted order (no deadlocks), re-checks every limit with the reservations of others included, inserts its own
   reservation rows and commits. Another replica that wants the same budget waits for that commit.
2. After the model answers (or fails mid-stream) the real usage is recorded and the reservation deleted in the
   same transaction: the budget never counts the request twice, nor zero times.
3. A request that dies leaves rows that expire after `RESERVATION_TTL`; they stop counting at once and the
   maintenance task deletes them.

Scopes: `provider:<p>`, `model:<p>:<m>`, `user:<user id>:<p>`, `pool:<quota id>`. This table is logged (not
UNLOGGED like rate limits) because it is money.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BudgetReservation

RESERVATION_TTL = timedelta(minutes=10)


def provider_scope(provider: str) -> str:
    return f"provider:{provider}"


def model_scope(provider: str, model: str) -> str:
    return f"model:{provider}:{model}"


def user_scope(user_id: uuid.UUID, provider: str) -> str:
    return f"user:{user_id}:{provider}"


def pool_scope(quota_id: uuid.UUID) -> str:
    return f"pool:{quota_id}"


async def reserved(
    db: AsyncSession, scope: str, *, prefix: bool = False, now: datetime | None = None
) -> tuple[int, float]:
    """Tokens and dollars held by active reservations of other requests in a scope (or every scope that starts
    with `scope` when `prefix`)."""
    now = now or datetime.now(UTC)
    match = BudgetReservation.scope.startswith(scope, autoescape=True) if prefix else BudgetReservation.scope == scope
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(BudgetReservation.tokens), 0),
                func.coalesce(func.sum(BudgetReservation.usd), 0.0),
            ).where(match, BudgetReservation.expires_at > now)
        )
    ).one()
    return int(row[0]), float(row[1])


async def lock_scopes(db: AsyncSession, scopes: list[str]) -> None:
    for scope in sorted(set(scopes)):
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "budget:" + scope})


def add(db: AsyncSession, request_id: uuid.UUID, scopes: list[str], tokens: int, usd: float) -> None:
    expires = datetime.now(UTC) + RESERVATION_TTL
    db.add_all(
        BudgetReservation(request_id=request_id, scope=s, tokens=tokens, usd=usd, expires_at=expires)
        for s in sorted(set(scopes))
    )


async def release(db: AsyncSession, request_id: uuid.UUID) -> None:
    """Delete a request's reservations; call it in the transaction that records the real usage."""
    await db.execute(delete(BudgetReservation).where(BudgetReservation.request_id == request_id))


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    res = await db.execute(delete(BudgetReservation).where(BudgetReservation.expires_at <= (now or datetime.now(UTC))))
    return int(res.rowcount)  # type: ignore[attr-defined]
