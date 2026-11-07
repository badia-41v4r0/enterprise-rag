import httpx
import pytest

pytestmark = pytest.mark.db


async def test_health_reports_db(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": True}
