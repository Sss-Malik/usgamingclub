# tests/integration/test_executor_cache.py
import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import CachedOutcome, InMemoryResultCache
from app.schemas.results import ReadBalanceResult

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


@respx.mock
async def test_invalid_result_payload_is_cached(seeded):
    # A backend that returns a value failing result validation is a terminal state and must be
    # cached, so a worker re-run does not re-call the backend (money-op safety).
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    class BadBackend:
        async def read_balance(self, ctx):
            return ReadBalanceResult(balance_cents=-1)  # raises ValidationError (ge=0) on construction

    def fake_resolve(driver, *, credentials, http_client, settings, session_store=None, redis=None):
        return BadBackend()

    payload = {"idempotency_key": "k-bad", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, resolve=fake_resolve,
        )
    cached = await cache.get("k-bad")
    assert cached is not None and cached.status == "failed" and "invalid_result_payload" in cached.reason


@respx.mock
async def test_gameroom_without_session_store_reports_failure(seeded):
    # Defensive: if the worker forgot to inject a SessionStore for a gameroom game, the executor
    # must report a clean failure (not crash). Configuration error -> not cached.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gr-no-store", "type": "AGENT_BALANCE", "game_id": 11}
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=None,
        )
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "missing_session_store" in body
    assert await cache.get("gr-no-store") is None              # config error -> not cached


@respx.mock
async def test_goldentreasure_without_redis_reports_failure(seeded):
    # Config error (no Redis injected for a gtreasure game) -> clean failure, NOT cached.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-no-redis", "type": "AGENT_BALANCE", "game_id": 13}
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=None,
        )
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "missing_redis_client" in body
    assert await cache.get("gt-no-redis") is None        # config error -> not cached


@respx.mock
async def test_retry_blocked_reports_failure_without_calling_backend(seeded):
    # arq retried a non-idempotent op (the previous attempt crashed mid-call). The worker passes
    # retry_blocked=True; the executor must NOT call the backend and must deliver a clear failure.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    backend_call_count = 0

    class TrackingBackend:
        async def read_balance(self, ctx):
            nonlocal backend_call_count
            backend_call_count += 1
            from app.schemas.results import ReadBalanceResult
            return ReadBalanceResult(balance_cents=999)

    def fake_resolve(driver, *, credentials, http_client, settings, session_store=None, redis=None):
        return TrackingBackend()

    payload = {"idempotency_key": "rb-1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7,
               "game_account_id": 1001}
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, retry_blocked=True, resolve=fake_resolve,
        )
    assert backend_call_count == 0                       # backend NOT called
    body = route.calls.last.request.content.decode()
    assert '"status":"failed"' in body and "retry_blocked" in body
    # NOT cached — the operation may have been applied on the prior attempt; we don't want a
    # later replay (different idempotency_key edge case) to also short-circuit.
    assert await cache.get("rb-1") is None
