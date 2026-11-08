"""Runtime settings stored in `app_settings` (editable from the admin UI without a restart)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

OPENROUTER_KEY = "provider.openrouter"


async def get_setting(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(AppSetting, key)
    return dict(row.value) if row is not None else {}


async def put_setting(db: AsyncSession, key: str, value: dict[str, Any], user_id: uuid.UUID | None) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by=user_id))
    else:
        row.value = value
        row.updated_by = user_id
        row.updated_at = datetime.now(UTC)
    await db.flush()
