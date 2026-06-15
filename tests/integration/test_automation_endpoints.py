import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import automation
from app.main import register_exception_handlers
from app.security.hmac import request_signature


class FakeArq:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.jobs.append((func, payload, _job_id))


@pytest.fixture
def client(monkeypatch, seeded):
    # Build a bare app with just the automation router so the real DB lifespan
    # (asyncmy) never starts; inject the seeded sqlite session_factory + a fake arq.
    monkeypatch.setenv("API_SECRET", "in-secret")
    monkeypatch.setenv("WEBHOOK_SECRET", "out-secret")
    from app.config import get_settings
    get_settings.cache_clear()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(automation.router)
    fake = FakeArq()
    app.state.arq = fake
    app.state.session_factory = seeded
    c = TestClient(app)
    c.fake = fake
    yield c
    get_settings.cache_clear()


def _post(client, path, body):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    import app.config
    secret = app.config.get_settings().api_secret
    sig = request_signature(secret, ts, raw)
    return client.post(
        path, content=raw,
        headers={"X-Request-Timestamp": ts, "X-Request-Signature": sig,
                 "Content-Type": "application/json"},
    )


def test_recharge_enqueues_202(client):
    body = {"user_id": 42, "backend_name": "milkyway", "username": "player_one",
            "amount": 50, "transaction_id": "uuid-1"}
    resp = _post(client, "/recharge", body)
    assert resp.status_code == 202
    func, payload, job_id = client.fake.jobs[-1]
    assert func == "execute_operation_task" and job_id == "recharge:uuid-1"
    assert payload["type"] == "RECHARGE" and payload["amount"] == 50
    assert payload["correlation"] == {"transaction_id": "uuid-1"}
    # milkyway is non-idempotent → capped at 1 try
    assert payload["_max_tries"] == 1


def test_create_generates_username(client):
    resp = _post(client, "/create",
                 {"user_id": 7, "full_name": "Jane Doe", "backend_name": "milkyway"})
    assert resp.status_code == 202
    _f, payload, _j = client.fake.jobs[-1]
    assert payload["type"] == "CREATE_ACCOUNT" and payload["account_username"].startswith("janedoe")


def test_bad_signature_401(client):
    raw = json.dumps({"user_id": 1, "backend_name": "milkyway", "username": "p",
                      "amount": 5, "transaction_id": "t"})
    resp = client.post("/recharge", content=raw,
                       headers={"X-Request-Timestamp": str(int(time.time())),
                                "X-Request-Signature": "bad"})
    assert resp.status_code == 401
    assert client.fake.jobs == []


def test_invalid_body_422(client):
    resp = _post(client, "/recharge", {"user_id": 1, "backend_name": "milkyway"})
    assert resp.status_code == 422
