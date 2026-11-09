"""Drive the setup wizard through its real API (no mocks): a fresh setup row, the token, the steps."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import login_limiter, mfa_limiter
from app.api.setup import token_global_limiter, token_limiter
from app.services import setup as svc

ADMIN_EMAIL = "it.admin@example.com"
ADMIN_PASSWORD = "Correct-Horse-Battery-77"


def reset_limiters() -> None:
    for lim in (login_limiter, mfa_limiter, token_limiter, token_global_limiter):
        lim.reset()


async def fresh_setup(db: AsyncSession) -> str:
    """What `erag bootstrap` does at the first start of a fresh installation. Returns the printed token."""
    res = await svc.init_on_start(db)
    assert res.mode == "setup" and res.new_token
    return res.new_token


async def enter_token(client: httpx.AsyncClient, token: str) -> str:
    r = await client.post("/api/setup/token", json={"token": token})
    assert r.status_code == 200, r.text
    csrf: str = r.json()["setup_csrf"]
    client.headers["X-Setup-CSRF"] = csrf
    return csrf


async def accept_license(client: httpx.AsyncClient, name: str = "Ada IT") -> None:
    lic = (await client.get("/api/setup/license")).json()
    r = await client.post("/api/setup/license", json={"accepted": True, "name": name, "text_sha256": lic["sha256"]})
    assert r.status_code == 200, r.text


async def create_admin(
    client: httpx.AsyncClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
) -> dict[str, Any]:
    r = await client.post("/api/setup/admin", json={"email": email, "display_name": "IT Admin", "password": password})
    assert r.status_code == 201, r.text
    me: dict[str, Any] = r.json()
    client.headers["X-CSRF-Token"] = me["csrf_token"]
    return me


async def through_admin(db: AsyncSession, client: httpx.AsyncClient) -> str:
    """Token, license, environment, administrator. Returns the token."""
    reset_limiters()
    token = await fresh_setup(db)
    await enter_token(client, token)
    await accept_license(client)
    assert (await client.post("/api/setup/steps/environment", json={"status": "done"})).status_code == 200
    await create_admin(client)
    return token
