# tests/integration/test_operations_endpoint.py
import json

import httpx
import pytest

from app.api.operations import router
from app.config import get_settings
from app.security.hmac import sign
from fastapi import FastAPI


class FakeArq:
    """Mirrors real arq's enqueue_job signature: only the documented control kwargs are accepted as
    named params. ANY unknown _* kwarg would land in **kwargs and be forwarded to the task function
    as a normal kwarg — which is the exact production bug this enforces against.
    """

    def __init__(self):
        self.jobs = []

    async def enqueue_job(
        self, func, *args,
        _job_id=None, _queue_name=None, _defer_until=None, _defer_by=None,
        _expires=None, _job_try=None,
        **kwargs,
    ):
        if kwargs:
            raise TypeError(
                f"enqueue_job got unexpected control kwargs {sorted(kwargs)} — these would be "
                f"forwarded to the task function by real arq and crash with TypeError."
            )
        payload = args[0] if args else None
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
        async def enqueue_job(self, func, *args, _job_id=None, **kwargs):
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


async def test_idempotent_driver_omits_max_tries_from_payload(seeded, app):
    # Real arq has NO _max_tries control kwarg on enqueue_job — passing one forwards it to the task
    # function and crashes with TypeError. So we embed the retry limit INSIDE the payload dict.
    # For idempotent drivers (gamevault family), we don't set it at all -> worker uses the default.
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gv-1", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    _, enqueued_payload, _ = app.state.arq.jobs[0]
    assert "_max_tries" not in enqueued_payload          # not set for idempotent drivers


async def test_gameroom_driver_embeds_max_tries_1_in_payload(seeded, app):
    # Worker reads payload["_max_tries"] and uses ctx["job_try"] to short-circuit retries.
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gr-1", "type": "AGENT_BALANCE", "game_id": 11},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    _, enqueued_payload, _ = app.state.arq.jobs[0]
    assert enqueued_payload["_max_tries"] == 1


async def test_goldentreasure_driver_embeds_max_tries_1_in_payload(seeded, app):
    # Same financial safety property as gameroom (no order_id -> no double-apply guarantee).
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gt-mt-1", "type": "AGENT_BALANCE", "game_id": 13},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    _, enqueued_payload, _ = app.state.arq.jobs[0]
    assert enqueued_payload["_max_tries"] == 1


async def test_unknown_game_id_falls_back_to_default(seeded, app):
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "u-1", "type": "AGENT_BALANCE", "game_id": 99999},
        separators=(",", ":"),
    )
    headers = sign("s", body)
    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    # Unknown game -> no driver peek hit -> _max_tries not embedded; worker uses the default.
    _, enqueued_payload, _ = app.state.arq.jobs[0]
    assert "_max_tries" not in enqueued_payload
