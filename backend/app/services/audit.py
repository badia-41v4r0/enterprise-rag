"""Audit log writer. Callers commit; entries ride in the same transaction as the action."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def audit(
    db: AsyncSession,
    action: str,
    *,
    user_id: uuid.UUID | None = None,
    resource: str = "",
    decision: str = "allow",
    details: dict[str, Any] | None = None,
) -> None:
    db.add(AuditLog(user_id=user_id, action=action, resource=resource, decision=decision, details=details or {}))
