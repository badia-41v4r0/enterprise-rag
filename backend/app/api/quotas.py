"""Quotas per user and per role, and usage views (PLAN-v0.3 R6, ADR 0027).

- A role quota is either a standard cap for each member (counted per person) or a collective pool shared by
  everyone holding the role. A user quota is a personal override of the standard allowance.
- Creating, changing or deleting a quota needs step-up and is audited (it shows up in the change history).
- A user sees only their own usage (and, for a shared pool, only whether it still has room); admins see
  aggregates and per-user figures. No endpoint returns prompts or answers: usage rows hold figures only.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, get_auth, require_admin, require_step_up
from app.core.db import get_db
from app.models import LlmProvider, LlmUsage, Quota, Role, User, UserRole
from app.services import usage
from app.services.audit import audit
from app.services.spend import day_key, month_key

router = APIRouter(prefix="/api", tags=["quotas"])

LIMIT_FIELDS = usage.DIMENSIONS


class QuotaIn(BaseModel):
    subject: Literal["user", "role"]
    user_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None
    kind: Literal["standard", "collective"] = "standard"
    provider: str | None = Field(default=None, max_length=40)  # NULL = every provider
    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_month: int | None = Field(default=None, ge=0)
    usd_per_day: float | None = Field(default=None, ge=0)
    usd_per_month: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _shape(self) -> QuotaIn:
        if self.subject == "user" and (self.user_id is None or self.role_id is not None or self.kind != "standard"):
            raise ValueError("a user quota needs user_id and is always 'standard' (a personal override)")
        if self.subject == "role" and (self.role_id is None or self.user_id is not None):
            raise ValueError("a role quota needs role_id")
        if all(getattr(self, f) is None for f in LIMIT_FIELDS):
            raise ValueError("set at least one limit")
        return self


class QuotaPatch(BaseModel):
    """Fields left out are unchanged; an explicit null removes that limit."""

    tokens_per_day: int | None = Field(default=None, ge=0)
    tokens_per_month: int | None = Field(default=None, ge=0)
    usd_per_day: float | None = Field(default=None, ge=0)
    usd_per_month: float | None = Field(default=None, ge=0)


class QuotaOut(BaseModel):
    id: uuid.UUID
    subject: str
    user_id: uuid.UUID | None
    role_id: uuid.UUID | None
    subject_name: str
    kind: str
    provider: str | None
    tokens_per_day: int | None
    tokens_per_month: int | None
    usd_per_day: float | None
    usd_per_month: float | None
    # What it has consumed: the pool for a collective quota, the person's own usage for a user override, and
    # nothing for a standard role cap (it is counted per member: see the per-user usage view).
    used: dict[str, float] | None


def _422(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg)


def _limits(q: Quota) -> dict[str, Any]:
    return {f: getattr(q, f) for f in LIMIT_FIELDS}


async def _out(db: AsyncSession, q: Quota) -> QuotaOut:
    if q.subject == "user":
        name = await db.scalar(select(User.email).where(User.id == q.user_id)) or "?"
    else:
        name = await db.scalar(select(Role.name).where(Role.id == q.role_id)) or "?"
    used: dict[str, float] | None = None
    if q.kind == "collective":
        dt, dc = await usage.pool_used(db, q.id, day_key())
        mt, mc = await usage.pool_used(db, q.id, month_key())
        used = {"tokens_per_day": dt, "tokens_per_month": mt, "usd_per_day": dc, "usd_per_month": mc}
    elif q.subject == "user" and q.user_id is not None:
        d = await usage.totals(db, provider=q.provider, user_id=q.user_id, day=day_key())
        m = await usage.totals(db, provider=q.provider, user_id=q.user_id, month=month_key())
        used = {
            "tokens_per_day": d.tokens,
            "tokens_per_month": m.tokens,
            "usd_per_day": d.cost_usd,
            "usd_per_month": m.cost_usd,
        }
    return QuotaOut(
        id=q.id,
        subject=q.subject,
        user_id=q.user_id,
        role_id=q.role_id,
        subject_name=name,
        kind=q.kind,
        provider=q.provider,
        used=used,
        **_limits(q),
    )


@router.get("/admin/quotas")
async def list_quotas(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[QuotaOut]:
    rows = (await db.scalars(select(Quota).order_by(Quota.subject, Quota.kind, Quota.created_at))).all()
    return [await _out(db, q) for q in rows]


@router.post("/admin/quotas", status_code=201)
async def create_quota(
    body: QuotaIn, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> QuotaOut:
    if body.subject == "user" and await db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if body.subject == "role" and await db.get(Role, body.role_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role not found")
    if (
        body.provider is not None
        and await db.scalar(select(LlmProvider.id).where(LlmProvider.slug == body.provider)) is None
    ):
        raise _422(f"Unknown provider {body.provider!r}")
    exists = await db.scalar(
        select(Quota.id).where(
            Quota.subject == body.subject,
            Quota.user_id.is_(None) if body.user_id is None else Quota.user_id == body.user_id,
            Quota.role_id.is_(None) if body.role_id is None else Quota.role_id == body.role_id,
            Quota.kind == body.kind,
            Quota.provider.is_(None) if body.provider is None else Quota.provider == body.provider,
        )
    )
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This quota already exists; edit it instead")
    q = Quota(**body.model_dump(), created_by=auth.user.id)
    db.add(q)
    await db.flush()
    audit(
        db,
        "admin.quota.create",
        user_id=auth.user.id,
        resource=f"quota:{q.id}",
        details={
            "subject": body.subject,
            "user_id": str(body.user_id) if body.user_id else None,
            "role_id": str(body.role_id) if body.role_id else None,
            "kind": body.kind,
            "provider": body.provider,
            "limits": _limits(q),
        },
    )
    await db.commit()
    return await _out(db, q)


async def _quota_or_404(db: AsyncSession, quota_id: uuid.UUID) -> Quota:
    q = await db.get(Quota, quota_id)
    if q is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quota not found")
    return q


@router.patch("/admin/quotas/{quota_id}")
async def update_quota(
    quota_id: uuid.UUID,
    body: QuotaPatch,
    auth: AuthContext = Depends(require_step_up),
    db: AsyncSession = Depends(get_db),
) -> QuotaOut:
    q = await _quota_or_404(db, quota_id)
    changes = {
        f: {"from": getattr(q, f), "to": getattr(body, f)}
        for f in body.model_fields_set & set(LIMIT_FIELDS)
        if getattr(body, f) != getattr(q, f)
    }
    for f, c in changes.items():
        setattr(q, f, c["to"])
    if all(getattr(q, f) is None for f in LIMIT_FIELDS):
        raise _422("a quota needs at least one limit; delete it instead")
    audit(db, "admin.quota.update", user_id=auth.user.id, resource=f"quota:{q.id}", details={"changes": changes})
    await db.commit()
    return await _out(db, q)


@router.delete("/admin/quotas/{quota_id}", status_code=204)
async def delete_quota(
    quota_id: uuid.UUID, auth: AuthContext = Depends(require_step_up), db: AsyncSession = Depends(get_db)
) -> None:
    q = await _quota_or_404(db, quota_id)
    audit(
        db,
        "admin.quota.delete",
        user_id=auth.user.id,
        resource=f"quota:{q.id}",
        details={"subject": q.subject, "kind": q.kind, "provider": q.provider, "limits": _limits(q)},
    )
    await db.delete(q)
    await db.commit()


# --- Usage views ------------------------------------------------------------------------------------------


def _fig(requests: Any, tokens: Any, cost: Any) -> dict[str, Any]:
    return {"requests": int(requests or 0), "tokens": int(tokens or 0), "cost_usd": round(float(cost or 0.0), 8)}


@router.get("/admin/usage")
async def admin_usage(_: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Aggregates per provider and model, per user and per role pool, for today and this month (UTC).
    Figures only: no prompt, question or answer is stored anywhere in the usage tables."""
    day, month = day_key(), month_key()
    tok = LlmUsage.prompt_tokens + LlmUsage.completion_tokens

    async def grouped(*cols: Any, where: Any) -> list[Any]:
        q = select(*cols, func.sum(LlmUsage.requests), func.sum(tok), func.sum(LlmUsage.cost_usd)).where(where)
        return list((await db.execute(q.group_by(*cols))).all())

    models: dict[tuple[str, str], dict[str, Any]] = {}
    for period, where in (("today", LlmUsage.day == day), ("month", LlmUsage.month == month)):
        for prov, model, r, t, c in await grouped(LlmUsage.provider, LlmUsage.model, where=where):
            models.setdefault((prov, model), {"provider": prov, "model": model})[period] = _fig(r, t, c)
    users: dict[uuid.UUID | None, dict[str, Any]] = {}
    for period, where in (("today", LlmUsage.day == day), ("month", LlmUsage.month == month)):
        for uid, r, t, c in await grouped(LlmUsage.user_id, where=where):
            users.setdefault(uid, {"user_id": str(uid) if uid else None})[period] = _fig(r, t, c)
    emails = dict((await db.execute(select(User.id, User.email).where(User.id.in_([u for u in users if u])))).all())
    for uid, row in users.items():
        row["email"] = emails.get(uid, "(no user)") if uid else "(no user)"
    pools = []
    for q in (await db.scalars(select(Quota).where(Quota.kind == "collective"))).all():
        members = await db.scalar(select(func.count()).select_from(UserRole).where(UserRole.role_id == q.role_id))
        out = await _out(db, q)
        pools.append({**out.model_dump(mode="json"), "members": int(members or 0)})
    empty = _fig(0, 0, 0)
    return {
        "day": day,
        "month": month,
        "models": [{"today": empty, "month": empty, **v} for v in models.values()],
        "users": sorted(
            ({"today": empty, "month": empty, **v} for v in users.values()),
            key=lambda u: -u["month"]["tokens"],
        ),
        "pools": pools,
    }


@router.get("/admin/users/{user_id}/usage")
async def admin_user_usage(
    user_id: uuid.UUID, _: AuthContext = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    role_ids = frozenset((await db.scalars(select(UserRole.role_id).where(UserRole.user_id == user_id))).all())
    return await _own_usage(db, user_id, role_ids, show_pools=True)


@router.get("/usage/me")
async def my_usage(auth: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """The caller's own usage and allowances. For a shared pool only whether it has room is shown, never how
    much colleagues used."""
    return await _own_usage(db, auth.user.id, auth.perms.role_ids, show_pools=False)


async def _own_usage(
    db: AsyncSession, user_id: uuid.UUID, role_ids: frozenset[uuid.UUID], *, show_pools: bool
) -> dict[str, Any]:
    day, month = day_key(), month_key()
    today = await usage.totals(db, user_id=user_id, day=day)
    this_month = await usage.totals(db, user_id=user_id, month=month)
    scopes: set[str | None] = {None}
    conds = [Quota.user_id == user_id]
    if role_ids:
        conds.append(Quota.role_id.in_(list(role_ids)))
    for p in (await db.scalars(select(Quota.provider).where(or_(*conds)))).all():
        scopes.add(p)
    seen: set[tuple[Any, ...]] = set()
    allowances: list[dict[str, Any]] = []
    for scope in sorted(scopes, key=lambda x: x or ""):
        # Global caps come with every scope (deduplicated below).
        for cap in await usage.applicable_caps(db, user_id, role_ids, scope):
            key = (cap.kind, cap.provider, cap.quota_id)
            if key in seen or (scope is None and cap.provider is not None):
                continue
            seen.add(key)
            used = await usage.cap_usage(db, cap, user_id)
            item: dict[str, Any] = {
                "kind": cap.kind,
                "provider": cap.provider,
                "source": cap.source,
                "limits": cap.limits,
            }
            if cap.kind == "standard" or show_pools:
                item["used"] = used
            item["exhausted"] = [d for d, lim in cap.limits.items() if lim is not None and used[d] >= lim]
            allowances.append(item)
    return {
        "day": day,
        "month": month,
        "today": _fig(today.requests, today.tokens, today.cost_usd),
        "this_month": _fig(this_month.requests, this_month.tokens, this_month.cost_usd),
        "daily": await usage.daily_series(db, user_id=user_id, days=30),
        "allowances": allowances,
        "resets": {"day": "00:00 UTC", "month": "the 1st of each month, 00:00 UTC"},
        "_pool_usage_hidden": not show_pools,
    }
