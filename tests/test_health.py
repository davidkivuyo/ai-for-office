import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoints(app_client: AsyncClient) -> None:
    r = await app_client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] in ("ok", "degraded")

    # nodes health — will try to hit fake ollama urls; should return degraded/offline but not 500
    r2 = await app_client.get("/api/nodes/health")
    assert r2.status_code == 200
    assert "nodes" in r2.json()
