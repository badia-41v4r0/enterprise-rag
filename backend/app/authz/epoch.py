"""Permission-cache invalidation by epoch (R16, ADR 0034).

`authz_state.epoch` is increased in the same transaction as any change that can alter what someone may read or
do: grants, role memberships, roles, containers, a user's active/admin flags, and a document's container or
restricted flag. SQLAlchemy events catch both ORM changes (before_flush) and bulk UPDATE/DELETE statements
(do_orm_execute), so services do not have to remember it. Every replica keys its permission cache by
(user, epoch): after the commit, the next request anywhere recomputes.

Changes made with raw SQL outside the application must bump the epoch themselves (or restart the app).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import ORMExecuteState, Session

from app.models import Container, Document, Grant, Role, User, UserRole

BUMP = text(
    "INSERT INTO authz_state (id, epoch) VALUES (1, 1) ON CONFLICT (id) DO UPDATE SET epoch = authz_state.epoch + 1"
)
READ = text("SELECT epoch FROM authz_state WHERE id = 1")

_ALWAYS = (Grant, UserRole, Role, Container)
_TABLES = {"grants", "user_roles", "roles", "containers", "users", "documents"}
_ATTRS: dict[type[Any], tuple[str, ...]] = {User: ("is_active", "is_admin"), Document: ("restricted", "container_id")}
_installed = False


def _relevant(obj: object, deleted: bool) -> bool:
    if isinstance(obj, _ALWAYS):
        return True
    for cls, attrs in _ATTRS.items():
        if isinstance(obj, cls):
            if deleted:
                return True
            state = inspect(obj)
            return any(state.attrs[a].history.has_changes() for a in attrs)
    return False


def _before_flush(session: Session, _ctx: object, _instances: object) -> None:
    if session.info.get("authz_bumped"):
        return
    changed = (
        any(_relevant(o, False) for o in session.dirty)
        or any(isinstance(o, _ALWAYS) for o in session.new)
        or any(_relevant(o, True) for o in session.deleted)
    )
    if changed:
        session.connection().execute(BUMP)
        session.info["authz_bumped"] = True


def _on_execute(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete) or state.session.info.get("authz_bumped"):
        return
    table = getattr(state.statement, "table", None)
    if getattr(table, "name", None) in _TABLES:
        state.session.connection().execute(BUMP)
        state.session.info["authz_bumped"] = True


def _reset(session: Session, *_: object) -> None:
    session.info.pop("authz_bumped", None)


def install() -> None:
    """Register the listeners once per process (called when app.authz.policy is imported)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _before_flush)
    event.listen(Session, "do_orm_execute", _on_execute)
    event.listen(Session, "after_commit", _reset)
    event.listen(Session, "after_rollback", _reset)
    _installed = True
