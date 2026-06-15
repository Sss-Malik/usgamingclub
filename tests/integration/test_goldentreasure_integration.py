# tests/integration/test_goldentreasure_integration.py
import json

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache
from app.schemas.requests import Operation

WEBHOOK = "https://arcadia.test/api/automation/webhook"
GT = "https://gt.test"


def _settings():
    return Settings(
        api_secret="in",
        webhook_secret="out",
        app_url="https://arcadia.test",
        webhook_max_budget_seconds=600,
    )


def _login_ok():
    return {"code": 20000, "token": "Ttok", "name": "Test02Gd1WEB", "data": {}}


def _read_payload(idem="gt-1"):
    return Operation(
        action="read", type="READ_BALANCE", idempotency_key=idem,
        user_id=61, backend_name="Golden Treasure", username="apitest01",
        correlation={"read_id": 1},
    ).model_dump()


def _recharge_payload(idem="gt-2", amount=1):
    return Operation(
        action="recharge", type="RECHARGE", idempotency_key=idem,
        user_id=61, backend_name="Golden Treasure", username="apitest01",
        amount=amount, correlation={"transaction_id": "gt-tx"},
    ).model_dump()


@respx.mock
async def test_goldentreasure_agent_balance_end_to_end(seeded, fake_redis):
    # Exercises the read_balance path: login -> /api/account/getPlayerScore -> success webhook.
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/account/getPlayerScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "curScore": "20.00"}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            _read_payload("gt-ab-1"), session_factory=seeded, http_client=client,
            settings=_settings(), result_cache=cache, redis=fake_redis,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "success"
    assert sent["user_data"]["balance"] == 20.0


@respx.mock
async def test_goldentreasure_terminal_failure_cached_and_not_recalled(seeded, fake_redis):
    # Login succeeds; getPlayerScore returns code:21 (terminal). Second run should NOT re-call.
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    cs = respx.post(f"{GT}/api/account/getPlayerScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "Server maintenance"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    payload = _read_payload("gt-21")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, redis=fake_redis)
        assert cs.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, redis=fake_redis)
    assert cs.call_count == 1                                  # cache hit -> no second call
    cached = await cache.get("gt-21")
    assert cached and cached.status == "failed" and "operation_refused" in cached.reason


@respx.mock
async def test_goldentreasure_rate_limited_167_is_not_cached(seeded, fake_redis):
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    es = respx.post(f"{GT}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 167, "message": "high frequency request"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    payload = _recharge_payload("gt-167", amount=1)
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, redis=fake_redis)
    assert es.call_count == 1
    assert await cache.get("gt-167") is None                   # transient -> not cached


@respx.mock
async def test_goldentreasure_session_is_reused_across_ops(seeded, fake_redis):
    """Two ops on the same game must issue exactly ONE /api/user/login."""
    login = respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/account/getPlayerScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "curScore": "5.00"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            _read_payload("gt-share-1"),
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
        await execute_operation(
            _read_payload("gt-share-2"),
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
        httpx.Response(200, json={"code": 20000, "message": "Score entered"}),
    ])
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    payload = _recharge_payload("gt-relogin", amount=1)
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, redis=fake_redis)
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "success"
