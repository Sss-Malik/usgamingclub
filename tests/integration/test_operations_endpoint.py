# tests/integration/test_operations_endpoint.py
import json

import httpx
import pytest

from app.api.operations import router
from app.config import get_settings
from app.security.hmac import sign
from fastapi import FastAPI


class FakeArq:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.jobs.append((func, payload, _job_id))
        return object()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("PYTHON_SIGNING_SECRET", "s")
    monkeypatch.setenv("APP_URL", "https://laravel.test")
    get_settings.cache_clear()
    application = FastAPI()
    application.include_router(router)
    application.state.arq = FakeArq()
    return application


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_valid_trigger_acks_202_and_enqueues(app):
    body = json.dumps(
        {"idempotency_key": "k1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    assert app.state.arq.jobs[0][2] == "k1"  # _job_id == idempotency_key


async def test_bad_signature_returns_401(app):
    body = json.dumps({"idempotency_key": "k1", "type": "READ_BALANCE"}, separators=(",", ":"))
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers={"X-Timestamp": "1", "X-Signature": "sha256=bad"})
    assert resp.status_code == 401
    assert app.state.arq.jobs == []


async def test_missing_idempotency_key_returns_400(app):
    body = json.dumps({"type": "READ_BALANCE"}, separators=(",", ":"))
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 400
    assert app.state.arq.jobs == []


async def test_enqueue_failure_returns_500(app):
    class FailingArq:
        async def enqueue_job(self, func, payload, _job_id=None):
            raise RuntimeError("redis down")

    app.state.arq = FailingArq()
    body = json.dumps(
        {"idempotency_key": "k1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 500
