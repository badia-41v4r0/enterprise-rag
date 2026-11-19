"""The password-change endpoint (v0.4, ADR 0037) is rate limited through the shared limiter (v0.5).

Found by the v0.4/v0.5 integration: the call was not awaited, so the coroutine counted as "allowed" and the
limit never applied. Guessing the current password must stop at the limit."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter, mfa_limiter
from app.core.config import get_settings
from tests.factories import login, make_user

pytestmark = pytest.mark.db


async def test_guessing_the_current_password_is_rate_limited(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await login_limiter.reset()
    await mfa_limiter.reset()
    await make_user(db, "u@x.test")
    await db.commit()
    await login(client, "u@x.test")
    limit = get_settings().login_rate_limit_per_minute
    codes = [
        (
            await client.post(
                "/api/auth/change-password",
                json={"current_password": f"wrong-guess-{i}", "new_password": "A-new-Password-123!"},
            )
        ).status_code
        for i in range(limit + 2)
    ]
    assert 429 not in codes[:limit], codes  # wrong guesses are refused as such until the limit
    assert codes[-1] == 429, codes
    await mfa_limiter.reset()
