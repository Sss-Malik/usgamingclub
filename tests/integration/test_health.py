# tests/integration/test_health.py
import httpx

from app.api.health import router
from fastapi import FastAPI


async def test_health_returns_ok():
    app = FastAPI()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
