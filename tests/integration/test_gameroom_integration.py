# tests/integration/test_gameroom_integration.py
import json
import time

import httpx
import respx

from app.backends.gameroom.session import InMemorySessionStore
from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache
from app.schemas.requests import Operation

WEBHOOK = "https://arcadia.test/api/automation/webhook"
GR = "https://gr.test"


def _settings():
    return Settings(
        api_secret="in",
        webhook_secret="out",
        app_url="https://arcadia.test",
        webhook_max_budget_seconds=600,
    )


def _login_ok():
    return {"status_code": 200, "message": "ok", "token": "Tjwt",
            "expires_time": int(time.time()) + 3600, "money": "5.00"}


def _read_payload(idem="gr-1"):
    return Operation(
        action="read", type="READ_BALANCE", idempotency_key=idem,
        user_id=51, backend_name="Gameroom", username="apifull9983654",
        correlation={"read_id": 1},
    ).model_dump()


@respx.mock
async def test_gameroom_agent_balance_end_to_end(seeded):
    # Verifies the read_balance path: login -> /api/player/agentMoney -> success webhook.
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.get(f"{GR}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"balance": 5, "cusBlance": "0", "username": "apifull9983654"}}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            _read_payload("gr-ab-1"), session_factory=seeded, http_client=client,
            settings=_settings(), result_cache=cache, session_store=store,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "success"
    assert sent["user_data"]["balance"] == 5.0


@respx.mock
async def test_gameroom_terminal_failure_cached_and_not_recalled(seeded):
    # 430 (wrong creds) is terminal: cached so a re-run does NOT re-call gameroom.
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(
        200, json={"status_code": 430, "message": "Username or password error"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = _read_payload("gr-430")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, session_store=store)
        assert login.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, session_store=store)
    assert login.call_count == 1                          # cache hit -> no second login
    cached = await cache.get("gr-430")
    assert cached and cached.status == "failed" and "auth_failed" in cached.reason


@respx.mock
async def test_gameroom_transient_failure_not_cached_recalls_backend(seeded):
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    am = respx.get(f"{GR}/api/player/agentMoney").mock(return_value=httpx.Response(500))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = _read_payload("gr-500")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, session_store=store)
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, session_store=store)
    assert am.call_count == 2                              # transient -> not cached -> called twice
    assert await cache.get("gr-500") is None


@respx.mock
async def test_gameroom_session_is_reused_across_ops(seeded):
    """Two ops in a row should issue exactly ONE /api/login (shared via the session store)."""
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.get(f"{GR}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"balance": 5, "cusBlance": "0", "username": "apifull9983654"}}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            _read_payload("gr-share-1"),
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
        await execute_operation(
            _read_payload("gr-share-2"),
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
    assert login.call_count == 1                            # shared session -> only one login
