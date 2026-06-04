# tests/integration/test_executor_cache.py
import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import CachedOutcome, InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


@respx.mock
async def test_cache_hit_short_circuits_backend(seeded):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    await cache.set("k-cached", CachedOutcome("succeeded", {"balance_cents": 999}, None), 900)
    payload = {"idempotency_key": "k-cached", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    body = route.calls.last.request.content.decode()
    assert '"balance_cents":999' in body and '"status":"succeeded"' in body


@respx.mock
async def test_success_is_cached(seeded):
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "k-new", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    cached = await cache.get("k-new")
    assert cached is not None and cached.status == "succeeded"
