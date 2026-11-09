"""Step 9 "Does it work?" and step 11's setup report (ADR 0041, PLAN-v0.4 section 8). Real Postgres; the check runs
the real pipeline (with no authorized documents no model is called)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Conversation, Session, User
from tests.factories import make_role, make_user
from tests.setup.helpers import ADMIN_EMAIL, ADMIN_PASSWORD, reset_limiters, through_admin

pytestmark = pytest.mark.db

SECRET_VALUE = "VAULT-REPORT-CANARY-3f9a2c"
PROVIDER_KEY = "sk-REPORT-CANARY-77b1e0"


@pytest.fixture(autouse=True)
def _limits() -> None:
    reset_limiters()


async def test_check_as_user_shows_the_answer_only_during_setup(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await through_admin(db, client)
    role = await make_role(db, "Staff")
    emp = await make_user(db, "emp@example.com", roles=[role])
    emp_id = emp.id
    await db.commit()
    r = await client.post("/api/setup/check", json={"user_id": str(emp_id), "question": "What is our holiday policy?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["user"]["email"] == "emp@example.com" and out["user"]["roles"] == ["Staff"]
    assert out["sources"] == [] and out["model"] is None  # nothing authorized: no model is called
    assert out["answer"] and out["answer_hidden"] is False
    assert (await db.scalars(select(Conversation))).all() == []  # the test conversation is deleted
    r = await client.post("/api/setup/finish")
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    # After setup: the assistant needs a recent re-authentication and never returns the answer text.
    await db.execute(update(Session).values(reauth_at=datetime.now(UTC) - timedelta(minutes=30)))
    await db.commit()
    body = {"user_id": str(emp_id), "question": "What is our holiday policy?"}
    r = await client.post("/api/admin/setup/check", json=body)
    assert r.status_code == 403 and r.json()["detail"] == "step_up_required"
    assert (await client.post("/api/auth/step-up", json={"password": ADMIN_PASSWORD})).status_code == 200
    r = await client.post("/api/admin/setup/check", json=body)
    assert r.status_code == 200 and r.json()["answer"] is None and r.json()["answer_hidden"] is True
    entries = (await db.scalars(select(AuditLog).where(AuditLog.action == "setup.check_as_user"))).all()
    assert [e.details["answer_shown"] for e in entries] == [True, False]
    assert all("holiday" not in str(e.details) for e in entries)  # never the question


async def test_setup_report_contains_no_secrets(db: AsyncSession, client: httpx.AsyncClient) -> None:
    token = await through_admin(db, client)
    assert (
        await client.put("/api/admin/secrets", json={"name": "erp_token", "value": SECRET_VALUE})
    ).status_code == 204
    r = await client.post(
        "/api/admin/providers",
        json={
            "kind": "api",
            "name": "Local vLLM",
            "base_url": "http://127.0.0.1:11434/v1",
            "self_hosted": True,
            "api_key": PROVIDER_KEY,
        },
    )
    assert r.status_code == 201, r.text
    created = (await client.post("/api/setup/users", json={"email": "temp@example.com"})).json()
    r = await client.get("/api/setup/report")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    report = r.text
    assert "# EnterpriseRAG setup report" in report and ADMIN_EMAIL in report and "local-vllm" in report
    for secret in (SECRET_VALUE, PROVIDER_KEY, token, ADMIN_PASSWORD, created["temporary_password"]):
        assert secret not in report
    admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None and admin.password_hash not in report
