"""Observability (R19): Prometheus metrics on an internal endpoint, health, and the correlation id shared by
the logs, the response and the audit entry. Metrics never carry user text."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.models import AuditLog
from tests.factories import login, make_user

pytestmark = pytest.mark.db


def test_prometheus_text_format() -> None:
    c = metrics.Counter("t_total", "a counter", ("route",))
    c.inc(route='/api/x"y')
    h = metrics.Histogram("t_seconds", "a histogram", ("stage",), buckets=(0.1, 1.0))
    h.observe(0.05, stage="embed")
    h.observe(0.5, stage="embed")
    lines = c.render() + h.render()
    assert "# TYPE t_total counter" in lines
    assert 't_total{route="/api/x\\"y"} 1' in lines
    assert 't_seconds_bucket{stage="embed",le="0.1"} 1' in lines
    assert 't_seconds_bucket{stage="embed",le="1"} 2' in lines
    assert 't_seconds_bucket{stage="embed",le="+Inf"} 2' in lines
    assert 't_seconds_count{stage="embed"} 2' in lines
    with pytest.raises(ValueError):
        c.inc(other="x")  # labels are fixed per metric


async def test_internal_endpoint_serves_metrics_with_shared_state(db: AsyncSession) -> None:
    server = await metrics.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"http://127.0.0.1:{port}/metrics")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
            assert "erag_ingestion_oldest_queued_seconds" in r.text
            assert "erag_acl_recheck_mismatches_total" in r.text
            assert (await client.get(f"http://127.0.0.1:{port}/other")).status_code == 404
    finally:
        server.close()
        await server.wait_closed()


async def test_correlation_id_reaches_response_logs_and_audit(
    db: AsyncSession, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await make_user(db, "u@x.test")
    await db.commit()
    from app.core.logging import RequestIdFilter

    for h in logging.getLogger().handlers:
        h.addFilter(RequestIdFilter())
    caplog.handler.addFilter(RequestIdFilter())
    r = await client.post(
        "/api/auth/login", json={"email": "u@x.test", "password": "wrong"}, headers={"X-Request-ID": "trace-abc.123"}
    )
    assert r.status_code == 401 and r.headers["X-Request-ID"] == "trace-abc.123"
    entry = await db.scalar(select(AuditLog).where(AuditLog.action == "auth.login"))
    assert entry is not None and entry.correlation_id == "trace-abc.123"
    assert any(getattr(rec, "request_id", None) == "trace-abc.123" for rec in caplog.records)
    # A header that is not a plain token is replaced, never echoed into logs.
    r = await client.get("/api/auth/me", headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["X-Request-ID"] != "bad id\nwith newline" and len(r.headers["X-Request-ID"]) == 16


async def test_security_signals_are_counted_without_user_text(db: AsyncSession, client: httpx.AsyncClient) -> None:
    await make_user(db, "u@x.test")
    await db.commit()
    before = metrics.LOGIN_FAILURES.value(step="password")
    await client.post("/api/auth/login", json={"email": "u@x.test", "password": "secret-guess-123"})
    assert metrics.LOGIN_FAILURES.value(step="password") == before + 1
    await login(client, "u@x.test")
    await client.get("/api/auth/me")
    text = await metrics.REGISTRY.render()
    assert "secret-guess-123" not in text and "u@x.test" not in text
    assert 'erag_http_requests_total{route="/api/auth/me",status="200"}' in text  # route template


async def test_health_live_and_ready(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    from app.api import health

    async def embed_down() -> tuple[bool, str]:
        return False, "ConnectError"

    async def embed_up() -> tuple[bool, str]:
        return True, "HTTP 200"

    monkeypatch.setattr(health, "_embedding_endpoint", embed_down)
    r = await client.get("/health/ready")
    assert r.status_code == 503 and r.json()["checks"]["embedding_endpoint"]["ok"] is False
    monkeypatch.setattr(health, "_embedding_endpoint", embed_up)
    r = await client.get("/health/ready")
    body = r.json()
    assert r.status_code == 200 and body["checks"]["database"]["ok"]
    # The reranker is configured but not installed here: a warning, the replica stays in rotation.
    assert body["checks"]["reranker"]["warning"] and body["status"] in ("ok", "degraded")
    await asyncio.sleep(0)
