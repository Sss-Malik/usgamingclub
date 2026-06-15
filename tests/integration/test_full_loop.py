# tests/integration/test_full_loop.py
"""End-to-end loop: signed Arcadia request -> enqueue -> executor -> signed webhook.

Uses the mock driver via a local Game(name="MockGame", backend_driver="mock") seed so the
backend network surface never engages. respx mocks the Arcadia webhook URL so the executor
can deliver without going over the wire.
"""
import json
import time

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import automation
from app.config import Settings, get_settings
from app.db.models import Game, GameAccount
from app.main import register_exception_handlers
from app.operations.executor import execute_operation
from app.security.hmac import request_signature, webhook_signature

API_SECRET = "in-secret"
WEBHOOK_SECRET = "out-secret"
APP_URL = "https://arcadia.test"
WEBHOOK_URL = f"{APP_URL}/api/automation/webhook"


@pytest_asyncio.fixture
async def mock_seed(session_factory):
    """Seed a mock-driver Game + GameAccount in addition to the standard fixture."""
    async with session_factory() as s:
        s.add(Game(id=900, name="MockGame", active=True, backend_driver="mock"))
        s.add(GameAccount(
            id=9001, user_id=42, game_id=900, username="loop_player",
            password="x", id_from_backend=None,
        ))
        await s.commit()
    return session_factory


class CapturingArq:
    """Records jobs in place of a real arq pool — caller drives the executor inline."""

    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, func, payload, _job_id=None):
        self.jobs.append((func, payload, _job_id))


def _sign_post(client, path, body, secret):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = request_signature(secret, ts, raw)
    return client.post(
        path,
        content=raw,
        headers={
            "X-Request-Timestamp": ts,
            "X-Request-Signature": sig,
            "Content-Type": "application/json",
        },
    )


@pytest.mark.asyncio
async def test_signed_recharge_round_trip(monkeypatch, mock_seed):
    """A signed POST /recharge enqueues an Operation payload, which the executor processes
    into a signed Arcadia webhook envelope (action/status/transaction_id/amount echoed)."""
    monkeypatch.setenv("API_SECRET", API_SECRET)
    monkeypatch.setenv("WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("APP_URL", APP_URL)
    get_settings.cache_clear()
    try:
        # Bare app + automation router; inject session_factory + fake arq.
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(automation.router)
        arq = CapturingArq()
        app.state.arq = arq
        app.state.session_factory = mock_seed

        body = {
            "user_id": 42,
            "backend_name": "MockGame",
            "username": "loop_player",
            "amount": 50,
            "transaction_id": "uuid-loop-1",
        }
        with TestClient(app) as c:
            resp = _sign_post(c, "/recharge", body, API_SECRET)
        assert resp.status_code == 202
        assert len(arq.jobs) == 1
        func, payload, job_id = arq.jobs[0]
        assert func == "execute_operation_task"
        assert job_id == "recharge:uuid-loop-1"
        assert payload["type"] == "RECHARGE"
        assert payload["amount"] == 50

        # Run the executor inline against a respx-mocked webhook endpoint.
        with respx.mock(assert_all_called=True) as r:
            route = r.post(WEBHOOK_URL).mock(
                return_value=httpx.Response(200, json={"success": True})
            )
            settings = Settings(
                api_secret=API_SECRET,
                webhook_secret=WEBHOOK_SECRET,
                app_url=APP_URL,
            )
            async with httpx.AsyncClient() as http_client:
                await execute_operation(
                    payload,
                    session_factory=mock_seed,
                    http_client=http_client,
                    settings=settings,
                )

            assert route.call_count == 1
            sent = route.calls.last.request
            raw_body = sent.content.decode()
            sent_body = json.loads(raw_body)

            assert sent_body["action"] == "recharge"
            assert sent_body["status"] == "success"
            assert sent_body["user_id"] == 42
            assert sent_body["backend_name"] == "MockGame"
            assert sent_body["backend_id"] == 900
            assert sent_body["transaction_id"] == "uuid-loop-1"
            assert sent_body["amount"] == 50
            assert isinstance(sent_body["timestamp"], int)

            # Signature validates over the raw body with the webhook secret.
            assert sent.headers["X-Webhook-Signature"] == webhook_signature(
                WEBHOOK_SECRET, raw_body
            )
    finally:
        get_settings.cache_clear()
