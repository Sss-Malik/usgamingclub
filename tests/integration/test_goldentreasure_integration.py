# tests/integration/test_goldentreasure_integration.py
import json

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GT = "https://gt.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


def _login_ok():
    return {"code": 20000, "token": "Ttok", "name": "Test02Gd1WEB", "data": {}}


@respx.mock
async def test_goldentreasure_agent_balance_end_to_end(seeded, fake_redis):
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gt-ab-1", "type": "AGENT_BALANCE", "game_id": 13}
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded"
    assert sent["result"]["agent_balance_cents"] == 2000      # "20.00" -> 2000 cents


@respx.mock
async def test_goldentreasure_terminal_failure_cached_and_not_recalled(seeded, fake_redis):
    # Login succeeds; CurScore returns code:21 (terminal). Second run should NOT re-call CurScore.
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    cs = respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "服务器维护中"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-21", "type": "AGENT_BALANCE", "game_id": 13}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
        assert cs.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    assert cs.call_count == 1                                  # cache hit -> no second call
    cached = await cache.get("gt-21")
    assert cached and cached.status == "failed" and "operation_refused" in cached.reason


@respx.mock
async def test_goldentreasure_rate_limited_167_is_not_cached(seeded, fake_redis):
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    es = respx.post(f"{GT}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 167, "message": "high frequency request"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-167", "type": "RECHARGE", "user_id": 61, "game_id": 13,
               "game_account_id": 4001, "amount_cents": 100, "bonus_cents": 0, "total_credit_cents": 100}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    assert es.call_count == 1
    assert await cache.get("gt-167") is None                   # transient -> not cached


@respx.mock
async def test_goldentreasure_session_is_reused_across_ops(seeded, fake_redis):
    """Two AGENT_BALANCE ops on the same game must issue exactly ONE /api/user/login."""
    login = respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "5.00"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            {"idempotency_key": "gt-share-1", "type": "AGENT_BALANCE", "game_id": 13},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
        await execute_operation(
            {"idempotency_key": "gt-share-2", "type": "AGENT_BALANCE", "game_id": 13},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
    assert login.call_count == 1                              # shared Redis session -> one login


@respx.mock
async def test_goldentreasure_recharge_relogin_on_minus3_then_success(seeded, fake_redis):
    """RECHARGE returns code:-3 -> client relogs in transparently -> retry once -> success."""
    respx.post(f"{GT}/api/user/login").mock(side_effect=[
        httpx.Response(200, json={"code": 20000, "token": "T1", "name": "x", "data": {}}),
        httpx.Response(200, json={"code": 20000, "token": "T2", "name": "x", "data": {}}),
    ])
    respx.post(f"{GT}/api/account/enterScore").mock(side_effect=[
        httpx.Response(200, json={"code": -3, "message": "token invalid"}),
        httpx.Response(200, json={"code": 20000, "message": "进分成功"}),
    ])
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-relogin", "type": "RECHARGE", "user_id": 61, "game_id": 13,
               "game_account_id": 4001, "amount_cents": 100, "bonus_cents": 0, "total_credit_cents": 100}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded"
