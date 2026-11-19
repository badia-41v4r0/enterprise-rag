"""Users at the scale of a company (R16, PLAN-v0.5 3.4): search with keyset pagination, bulk actions, and CSV
import and export.

The import is a service, not an endpoint detail, so the first-run wizard (v0.4) can reuse it. Its rules:
- columns `email`, `display_name`, `roles` (role names separated by `;`); a header row is required;
- new users get a random temporary password, returned once in the result: no mail server is assumed
  (PLAN-v0.4 non-goals), so the administrator hands it over and the user changes it;
- existing users keep their password; listed roles are added, never removed (removals are deliberate and go
  through the bulk action or the user's row);
- nobody becomes an administrator through a file: promotion needs a recent re-authentication (ADR 0019);
- the whole file is validated first; with errors, or with `dry_run`, nothing is written.
"""

from __future__ import annotations

import csv
import io
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models import Role, Session, User, UserRole

MAX_IMPORT_ROWS = 20_000
MAX_BULK = 5_000
EMAIL_MAX = 320
NAME_MAX = 200
CSV_COLUMNS = ("email", "display_name", "roles", "is_admin", "is_active", "mfa_enabled", "last_login_at")

Status = Literal["active", "inactive"]


@dataclass(frozen=True)
class UserQuery:
    q: str = ""
    role_id: uuid.UUID | None = None
    status: Status | None = None
    is_admin: bool | None = None
    last_login_before: datetime | None = None
    never_logged_in: bool = False


def filtered(query: UserQuery) -> Select[Any]:
    stmt = select(User)
    if query.q:
        pattern = "%" + query.q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(
            or_(User.email.like(pattern, escape="\\"), func.lower(User.display_name).like(pattern, escape="\\"))
        )
    if query.role_id is not None:
        stmt = stmt.where(exists().where(UserRole.user_id == User.id, UserRole.role_id == query.role_id))
    if query.status is not None:
        stmt = stmt.where(User.is_active.is_(query.status == "active"))
    if query.is_admin is not None:
        stmt = stmt.where(User.is_admin.is_(query.is_admin))
    if query.never_logged_in:
        stmt = stmt.where(User.last_login_at.is_(None))
    elif query.last_login_before is not None:
        stmt = stmt.where(or_(User.last_login_at.is_(None), User.last_login_at < query.last_login_before))
    return stmt


async def page(
    db: AsyncSession, query: UserQuery, *, after: str | None, limit: int
) -> tuple[list[User], str | None, int]:
    """One page ordered by email (unique), the cursor for the next one, and the total matching count."""
    stmt = filtered(query)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    if after:
        stmt = stmt.where(User.email > after)
    rows = list((await db.scalars(stmt.order_by(User.email).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, (rows[-1].email if more and rows else None), total


# --- Bulk actions -------------------------------------------------------------------------------------


BulkAction = Literal["add_role", "remove_role", "deactivate", "activate", "sign_out"]


async def bulk(
    db: AsyncSession,
    action: BulkAction,
    user_ids: Iterable[uuid.UUID],
    *,
    actor_id: uuid.UUID,
    role_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Apply one action to many users; returns the ids that changed. The caller audits and commits.

    The actor is never deactivated or signed out by their own bulk action."""
    ids = set(user_ids)
    if len(ids) > MAX_BULK:
        raise ValueError(f"at most {MAX_BULK} users per bulk action")
    found = set((await db.scalars(select(User.id).where(User.id.in_(ids)))).all()) if ids else set()
    if found != ids:
        raise LookupError("unknown user id")
    if action in ("add_role", "remove_role"):
        if role_id is None or await db.get(Role, role_id) is None:
            raise LookupError("unknown role id")
        members = set(
            (
                await db.scalars(select(UserRole.user_id).where(UserRole.role_id == role_id, UserRole.user_id.in_(ids)))
            ).all()
        )
        if action == "add_role":
            changed = sorted(ids - members, key=str)
            db.add_all(UserRole(user_id=u, role_id=role_id) for u in changed)
        else:
            changed = sorted(members, key=str)
            if changed:
                await db.execute(delete(UserRole).where(UserRole.role_id == role_id, UserRole.user_id.in_(changed)))
        await db.flush()
        return changed
    ids.discard(actor_id)
    if action in ("deactivate", "activate"):
        want = action == "activate"
        users = (await db.scalars(select(User).where(User.id.in_(ids), User.is_active.is_(not want)))).all()
        for u in users:
            u.is_active = want
        changed = sorted((u.id for u in users), key=str)
        if not want and changed:
            await db.execute(delete(Session).where(Session.user_id.in_(changed)))
        await db.flush()
        return changed
    if action == "sign_out":
        with_sessions = set((await db.scalars(select(Session.user_id).where(Session.user_id.in_(ids)))).all())
        if with_sessions:
            await db.execute(delete(Session).where(Session.user_id.in_(with_sessions)))
        return sorted(with_sessions, key=str)
    raise ValueError(f"unknown action {action}")


# --- CSV ----------------------------------------------------------------------------------------------


def _cell(value: str) -> str:
    """Neutralize spreadsheet formulas in exported cells (CSV injection)."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


async def export_rows(db: AsyncSession, query: UserQuery) -> Iterable[str]:
    """CSV text in chunks of 500 users, in email order, with the same filters as the list."""
    names = {r.id: r.name for r in (await db.scalars(select(Role))).all()}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    after: str | None = None
    chunks = []
    while True:
        users, after, _ = await page(db, query, after=after, limit=500)
        for u in users:
            writer.writerow(
                [
                    _cell(u.email),
                    _cell(u.display_name),
                    _cell(";".join(sorted(names.get(r.id, str(r.id)) for r in u.roles))),
                    str(u.is_admin).lower(),
                    str(u.is_active).lower(),
                    str(u.mfa_enabled).lower(),
                    u.last_login_at.isoformat() if u.last_login_at else "",
                ]
            )
        chunks.append(buf.getvalue())
        buf.seek(0)
        buf.truncate()
        if after is None:
            break
    return chunks


@dataclass
class ImportRow:
    line: int
    email: str
    action: Literal["create", "update", "unchanged"] = "unchanged"
    added_roles: list[str] = field(default_factory=list)
    temporary_password: str | None = None  # set only when the import was applied


@dataclass
class ImportResult:
    applied: bool
    rows: list[ImportRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(r.action == "create" for r in self.rows)

    @property
    def updated(self) -> int:
        return sum(r.action == "update" for r in self.rows)


def _valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(local) and "." in domain and " " not in email and len(email) <= EMAIL_MAX


async def import_csv(db: AsyncSession, content: str, *, dry_run: bool) -> ImportResult:
    """Validate the whole file, then (unless `dry_run` or errors) create and update users. The caller audits
    and commits. Returns per-row outcomes; temporary passwords only when applied."""
    result = ImportResult(applied=False)
    reader = csv.DictReader(io.StringIO(content.lstrip("﻿")))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    if "email" not in header:
        result.errors.append("the header must contain an 'email' column")
        return result
    unknown_cols = set(header) - {"email", "display_name", "roles"}
    if unknown_cols:
        result.errors.append(f"unknown columns: {', '.join(sorted(unknown_cols))} (admin rights are not imported)")
        return result
    roles = {r.name.lower(): r for r in (await db.scalars(select(Role))).all()}
    parsed: list[tuple[int, str, str, list[Role]]] = []
    seen: set[str] = set()
    for i, raw in enumerate(reader, start=2):
        if i - 1 > MAX_IMPORT_ROWS:
            result.errors.append(f"more than {MAX_IMPORT_ROWS} rows")
            break
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        email = row.get("email", "").lower()
        if not email and not any(row.values()):
            continue  # blank line
        if not _valid_email(email):
            result.errors.append(f"line {i}: invalid email {email!r}")
            continue
        if email in seen:
            result.errors.append(f"line {i}: {email} appears twice")
            continue
        seen.add(email)
        name = row.get("display_name", "")
        if len(name) > NAME_MAX:
            result.errors.append(f"line {i}: display name longer than {NAME_MAX}")
            continue
        wanted = [n.strip() for n in row.get("roles", "").split(";") if n.strip()]
        missing = [n for n in wanted if n.lower() not in roles]
        if missing:
            result.errors.append(f"line {i}: unknown role {', '.join(missing)}")
            continue
        parsed.append((i, email, name, [roles[n.lower()] for n in wanted]))
    if result.errors:
        return result

    existing = {
        u.email: u for u in (await db.scalars(select(User).where(User.email.in_([p[1] for p in parsed])))).all()
    }
    for line, email, name, wanted_roles in parsed:
        user = existing.get(email)
        out = ImportRow(line=line, email=email)
        if user is None:
            out.action = "create"
            out.added_roles = [r.name for r in wanted_roles]
            if not dry_run:
                password = secrets.token_urlsafe(12)
                user = User(email=email, display_name=name or email, password_hash=hash_password(password))
                db.add(user)
                await db.flush()
                db.add_all(UserRole(user_id=user.id, role_id=r.id) for r in wanted_roles)
                out.temporary_password = password
        else:
            have = {r.id for r in user.roles}
            new_roles = [r for r in wanted_roles if r.id not in have]
            rename = bool(name) and name != user.display_name
            if new_roles or rename:
                out.action = "update"
                out.added_roles = [r.name for r in new_roles]
                if not dry_run:
                    if rename:
                        user.display_name = name
                    db.add_all(UserRole(user_id=user.id, role_id=r.id) for r in new_roles)
        result.rows.append(out)
    if not dry_run:
        await db.flush()
        result.applied = True
    return result
