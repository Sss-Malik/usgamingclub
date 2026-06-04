# tests/integration/test_gamevault_integration.py
import json

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GV = "https://gv.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


@respx.mock
async def test_gamevault_read_balance_routes_and_reports(seeded):
    # game 9 has backend_driver='gamevault'; account 2001 has external_user_id 88880212
    respx.post(f"{GV}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "60"}, "count": 0})
    )
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gv-1", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=InMemoryResultCache())
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded" and sent["result"]["balance_cents"] == 6000


@respx.mock
async def test_terminal_failure_cached_and_not_recalled(seeded):
    # First call: GameVault returns business failure (code 7). Should be cached.
    gv = respx.post(f"{GV}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 7, "msg": "Insufficient user balance", "data": None, "count": 0})
    )
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gv-2", "type": "REDEEM", "user_id": 43, "game_id": 9, "game_account_id": 2001, "amount_cents": 3000}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
        assert gv.call_count == 1
        # Second run (simulated arq re-run): cache hit -> GameVault NOT called again.
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    assert gv.call_count == 1  # still 1 -> backend not re-called
    cached = await cache.get("gv-2")
    assert cached.status == "failed" and "insufficient_user_balance" in cached.reason


@respx.mock
async def test_transient_failure_not_cached_recalls_backend(seeded):
    gv = respx.post(f"{GV}/api/external/userBalance").mock(return_value=httpx.Response(503))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gv-3", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    assert gv.call_count == 2  # transient -> not cached -> backend called both runs
    assert await cache.get("gv-3") is None
