# tests/integration/test_gameroom_integration.py
import json
import time

import httpx
import respx

from app.config import Settings
from app.backends.gameroom.session import InMemorySessionStore
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GR = "https://gr.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


def _login_ok():
    return {"status_code": 200, "message": "ok", "token": "Tjwt",
            "expires_time": int(time.time()) + 3600, "money": "5.00"}


@respx.mock
async def test_gameroom_agent_balance_end_to_end(seeded):
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gr-ab-1", "type": "AGENT_BALANCE", "game_id": 11}
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded" and sent["result"]["agent_balance_cents"] == 500


@respx.mock
async def test_gameroom_terminal_failure_cached_and_not_recalled(seeded):
    # 430 (wrong creds) is terminal: cached so a re-run does NOT re-call gameroom.
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(
        200, json={"status_code": 430, "message": "Username or password error"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = {"idempotency_key": "gr-430", "type": "AGENT_BALANCE", "game_id": 11}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
        assert login.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
    assert login.call_count == 1                          # cache hit -> no second login
    cached = await cache.get("gr-430")
    assert cached and cached.status == "failed" and "auth_failed" in cached.reason


@respx.mock
async def test_gameroom_transient_failure_not_cached_recalls_backend(seeded):
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    gm = respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(500))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = {"idempotency_key": "gr-500", "type": "AGENT_BALANCE", "game_id": 11}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
    assert gm.call_count == 2                              # transient -> not cached -> called twice
    assert await cache.get("gr-500") is None


@respx.mock
async def test_gameroom_session_is_reused_across_ops(seeded):
    """Two ops in a row should issue exactly ONE /api/login (shared via the session store)."""
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            {"idempotency_key": "gr-share-1", "type": "AGENT_BALANCE", "game_id": 11},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
        await execute_operation(
            {"idempotency_key": "gr-share-2", "type": "AGENT_BALANCE", "game_id": 11},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
    assert login.call_count == 1                            # shared session -> only one login
