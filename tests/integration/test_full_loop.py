# tests/integration/test_full_loop.py
import json

import httpx
import respx

from app.config import Settings, get_settings
from app.operations.executor import execute_operation
from app.security.hmac import sign

WEBHOOK = "https://laravel.test/webhooks/games/operation"


class CapturingArq:
    """Stands in for the arq pool: records jobs, then runs the executor inline."""

    def __init__(self, seeded):
        self.seeded = seeded
        self.enqueued = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.enqueued.append((func, payload, _job_id))


@respx.mock
async def test_create_account_round_trip(monkeypatch, seeded):
    monkeypatch.setenv("PYTHON_SIGNING_SECRET", "s")
    monkeypatch.setenv("APP_URL", "https://laravel.test")
    get_settings.cache_clear()

    from app.api.operations import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    arq = CapturingArq(seeded)
    app.state.arq = arq

    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))

    body = json.dumps(
        {"idempotency_key": "loop-1", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7, "game_account_id": None},
        separators=(",", ":"),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/operations", content=body, headers=sign("s", body))
    assert resp.status_code == 202
    assert arq.enqueued[0][2] == "loop-1"

    # Simulate the worker picking up the enqueued job:
    _, payload, _ = arq.enqueued[0]
    settings = Settings(python_signing_secret="s", app_url="https://laravel.test")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=settings)

    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["idempotency_key"] == "loop-1"
    assert sent["status"] == "succeeded"
    assert sent["result"]["username"] == "mock_42_7"
    get_settings.cache_clear()
