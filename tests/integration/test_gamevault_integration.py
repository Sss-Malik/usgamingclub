# tests/integration/test_gamevault_integration.py
import json

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache
from app.schemas.requests import Operation

WEBHOOK = "https://arcadia.test/api/automation/webhook"
GV = "https://gv.test"


def _settings():
    return Settings(
        api_secret="in",
        webhook_secret="out",
        app_url="https://arcadia.test",
        webhook_max_budget_seconds=600,
    )


def _read_payload(idem="gv-1"):
    return Operation(
        action="read", type="READ_BALANCE", idempotency_key=idem,
        user_id=43, backend_name="GameVault Demo", username="user020301",
        correlation={"read_id": 1},
    ).model_dump()


def _redeem_payload(idem="gv-2", amount=30):
    return Operation(
        action="redeem", type="REDEEM", idempotency_key=idem,
        user_id=43, backend_name="GameVault Demo", username="user020301",
        amount=amount, correlation={"redeem_id": 7},
    ).model_dump()


@respx.mock
async def test_gamevault_read_balance_routes_and_reports(seeded):
    # game "GameVault Demo" has backend_driver='gamevault'; account user020301 has id_from_backend 88880212
    respx.post(f"{GV}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok",
                                               "data": {"user_balance": "60"}, "count": 0})
    )
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient() as client:
        await execute_operation(_read_payload(), session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=InMemoryResultCache())
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "success"
    assert sent["user_data"]["balance"] == 60.0


@respx.mock
async def test_terminal_failure_cached_and_not_recalled(seeded):
    # First call: GameVault returns business failure (code 7). Should be cached.
    gv = respx.post(f"{GV}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 7, "msg": "Insufficient user balance",
                                               "data": None, "count": 0})
    )
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    payload = _redeem_payload(idem="gv-2", amount=30)
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache)
        assert gv.call_count == 1
        # Second run (simulated arq re-run): cache hit -> GameVault NOT called again.
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache)
    assert gv.call_count == 1  # still 1 -> backend not re-called
    cached = await cache.get("gv-2")
    assert cached.status == "failed" and "insufficient_user_balance" in cached.reason


@respx.mock
async def test_transient_failure_not_cached_recalls_backend(seeded):
    gv = respx.post(f"{GV}/api/external/userBalance").mock(return_value=httpx.Response(503))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"success": True}))
    cache = InMemoryResultCache()
    payload = _read_payload(idem="gv-3")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache)
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache)
    assert gv.call_count == 2  # transient -> not cached -> backend called both runs
    assert await cache.get("gv-3") is None
