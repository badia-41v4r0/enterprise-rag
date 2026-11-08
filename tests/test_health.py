import httpx
import pytest

pytestmark = pytest.mark.db


async def test_health_reports_db(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": True}


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-request-id"]


def test_prod_refuses_dev_secrets() -> None:
    from app.core.config import Settings

    with pytest.raises(RuntimeError):
        Settings(env="prod").check_secrets()
    Settings(env="prod", secret_key="x" * 40, mcp_context_secret="y" * 40).check_secrets()
