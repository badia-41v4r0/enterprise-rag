"""Real decision servers, opt-in (PLAN-v0.3 R8/R9, ADR 0030 to 0032). No fake server: these call the real thing.

- `decider_open`: an open Jev-style server you started (see README "Open decider"), at ERAG_TEST_OPEN_JEV_URL,
  e.g. `ERAG_TEST_OPEN_JEV_URL=http://127.0.0.1:8791 uv run pytest -m decider_open`.
- `decider_official`: TypeSafe's hosted API with ERAG_TEST_JEV_API_KEY (two small paid calls, about 300 input
  tokens each), e.g. `ERAG_TEST_JEV_API_KEY=... uv run pytest -m decider_official`.
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modelrouter import protocol
from app.modelrouter.decision import Meta
from app.models import Classification
from tests.factories import login, make_user


async def _admin(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "admin@acme.test", admin=True)
    await db.commit()
    client.headers["X-CSRF-Token"] = await login(client, "admin@acme.test")


def _check(result: dict) -> None:
    assert result["ok"], result
    a = result["answers"]
    assert a["task_type"] in ("question_answering", "chit_chat") and 1 <= a["difficulty"] <= 5
    assert 0 <= a["needs_tools_p"] <= 1 and 0 <= a["needs_long_context_p"] <= 1


@pytest.mark.db
@pytest.mark.decider_open
async def test_open_jev_server_through_the_test_button(db: AsyncSession, client: httpx.AsyncClient) -> None:
    url = os.environ["ERAG_TEST_OPEN_JEV_URL"]
    # The raw protocol first: the server answers every question type we send.
    payload = protocol.build_request("Where do employees park?", Meta(Classification.public, False, 0))
    data = await protocol.post(
        protocol.endpoint(url), payload, api_key=None, allow_private=True, require_private=True, timeout_s=600
    )
    answers, _usage, _model = protocol.parse_response(data)
    assert 1 <= answers.difficulty <= 5
    await _admin(db, client)
    r = await client.post(
        "/api/admin/router/deciders",
        json={"name": "open-jev", "kind": "protocol", "base_url": url, "self_hosted": True, "timeout_ms": 300_000},
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["locality"] == "local" and d["max_classification"] == "confidential"
    result = (await client.post(f"/api/admin/router/deciders/{d['id']}/test", timeout=700)).json()
    _check(result)


@pytest.mark.db
@pytest.mark.decider_official
async def test_official_jev_api_through_the_test_button(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await _admin(db, client)
    r = await client.post(
        "/api/admin/router/deciders",
        json={
            "name": "jev",
            "kind": "jev_official",
            "api_key": os.environ["ERAG_TEST_JEV_API_KEY"],
            "monthly_budget_usd": 0.05,
            "timeout_ms": 30_000,
        },
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["locality"] == "cloud" and d["max_classification"] == "public" and d["key_source"] == "vault"
    result = (await client.post(f"/api/admin/router/deciders/{d['id']}/test", timeout=60)).json()
    _check(result)
    assert result["usage"]["input_tokens"] > 0  # the first real call reads `usage` (price check, plan section 5)
    assert 0 < result["cost_usd"] < 0.001
