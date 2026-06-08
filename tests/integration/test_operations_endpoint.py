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

    async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
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
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
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


async def test_idempotent_driver_uses_default_max_tries(seeded, app):
    # Wire a real session_factory + a gamevault game (id=9 in conftest seed) so the endpoint's
    # driver peek actually runs and confirms gamevault is NOT in NON_IDEMPOTENT_DRIVERS.
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gv-1", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"func": func, "payload": payload, "_job_id": _job_id, "_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    # gamevault driver -> endpoint leaves _max_tries at None so arq uses WorkerSettings.max_tries (3).
    assert app.state.arq.jobs[0]["_max_tries"] is None


async def test_gameroom_driver_uses_max_tries_1(seeded, app):
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gr-1", "type": "AGENT_BALANCE", "game_id": 11},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    assert app.state.arq.jobs[0]["_max_tries"] == 1


async def test_unknown_game_id_falls_back_to_default(seeded, app):
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "u-1", "type": "AGENT_BALANCE", "game_id": 99999},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    # Default policy (None / 3); preflight in the worker will fail with game_not_found later.
    assert app.state.arq.jobs[0]["_max_tries"] in (None, 3)
