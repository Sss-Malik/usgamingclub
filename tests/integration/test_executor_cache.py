# tests/integration/test_executor_cache.py
import json

import httpx
import respx

from app.backends.base import BackendError
from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import CachedOutcome, InMemoryResultCache
from app.schemas.requests import Operation
from app.schemas.results import ReadBalanceResult

WEBHOOK = "https://arcadia.test/api/automation/webhook"


def _settings():
    return Settings(api_secret="in", webhook_secret="out",
                    app_url="https://arcadia.test", webhook_max_budget_seconds=600)


def _read_payload(key, backend_name="milkyway", username="player_one", read_id=5):
    return Operation(
        action="read", type="READ_BALANCE", idempotency_key=key,
        user_id=42, backend_name=backend_name, username=username,
        correlation={"read_id": read_id},
    ).model_dump()


@respx.mock
async def test_cache_hit_short_circuits_backend(seeded):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    await cache.set("read:cached", CachedOutcome("succeeded", {"balance": 999.0}, None), 900)
    async with httpx.AsyncClient() as client:
        await execute_operation(_read_payload("read:cached"), session_factory=seeded,
                                http_client=client, settings=_settings(), result_cache=cache)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "success" and body["user_data"] == {"balance": 999.0}


@respx.mock
async def test_success_is_cached(seeded):
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    class GoodBackend:
        async def read_balance(self, ctx):
            return ReadBalanceResult(balance=127.5)

    def fake_resolve(driver, **kwargs):
        return GoodBackend()

    async with httpx.AsyncClient() as client:
        await execute_operation(_read_payload("read:new"), session_factory=seeded,
                                http_client=client, settings=_settings(),
                                result_cache=cache, resolve=fake_resolve)
    cached = await cache.get("read:new")
    assert cached is not None and cached.status == "succeeded"


@respx.mock
async def test_invalid_result_payload_is_cached(seeded):
    # A backend that returns a value failing result validation is a terminal state and must be
    # cached, so a worker re-run does not re-call the backend (money-op safety).
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    class BadBackend:
        async def read_balance(self, ctx):
            return ReadBalanceResult(balance=-1)  # raises ValidationError (ge=0) on construction

    def fake_resolve(driver, **kwargs):
        return BadBackend()

    async with httpx.AsyncClient() as client:
        await execute_operation(_read_payload("read:bad"), session_factory=seeded,
                                http_client=client, settings=_settings(),
                                result_cache=cache, resolve=fake_resolve)
    cached = await cache.get("read:bad")
    assert cached is not None and cached.status == "failed" and "invalid_result_payload" in cached.reason
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed"
    assert body["diagnostics"]["failure_kind"] == "invalid_result"


@respx.mock
async def test_invalid_result_payload_reports_snapshot_fields_live_and_replay(seeded):
    # Live delivery must derive external_user_id/balance_before/balance_after from the recorder
    # snapshot (not hardcode None), and a cache replay must agree with the live delivery (fix D).
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    class BadBackend:
        async def read_balance(self, ctx):
            ctx.diag.mark_external_user_id("42:99")
            ctx.diag.mark_balance_before(10.0)
            return ReadBalanceResult(balance=-1)  # raises ValidationError (ge=0) on construction

    def fake_resolve(driver, **kwargs):
        return BadBackend()

    payload = _read_payload("read:invalid-snap")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, resolve=fake_resolve)
    live = json.loads(route.calls.last.request.content.decode())
    assert live["diagnostics"]["failure_kind"] == "invalid_result"
    assert live["diagnostics"]["external_user_id"] == "42:99"
    assert live["diagnostics"]["balance_before"] == 10.0

    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, resolve=fake_resolve)
    replay = json.loads(route.calls.last.request.content.decode())
    assert replay["diagnostics"]["cache_hit"] is True
    assert replay["diagnostics"]["external_user_id"] == live["diagnostics"]["external_user_id"]
    assert replay["diagnostics"]["balance_before"] == live["diagnostics"]["balance_before"]


@respx.mock
async def test_gameroom_without_session_store_reports_failure(seeded):
    # Defensive: if the worker forgot to inject a SessionStore for a gameroom game, the executor
    # must report a clean failure (not crash). Configuration error -> not cached.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = _read_payload("gr-no-store", backend_name="Gameroom", username="apifull9983654")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, session_store=None)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed" and "missing_session_store" in body["message"]
    assert body["diagnostics"]["failure_kind"] == "preflight"
    assert await cache.get("gr-no-store") is None              # config error -> not cached


@respx.mock
async def test_goldentreasure_without_redis_reports_failure(seeded):
    # Config error (no Redis injected for a gtreasure game) -> clean failure, NOT cached.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = _read_payload("gt-no-redis", backend_name="Golden Treasure", username="apitest01")
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, redis=None)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed" and "missing_redis_client" in body["message"]
    assert body["diagnostics"]["failure_kind"] == "preflight"
    assert await cache.get("gt-no-redis") is None        # config error -> not cached


@respx.mock
async def test_retry_blocked_reports_error_without_calling_backend(seeded):
    # arq retried a non-idempotent op (the previous attempt crashed mid-call). The worker passes
    # retry_blocked=True; the executor must NOT call the backend and must deliver a clear error.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    backend_call_count = 0

    class TrackingBackend:
        async def read_balance(self, ctx):
            nonlocal backend_call_count
            backend_call_count += 1
            return ReadBalanceResult(balance=999.0)

    def fake_resolve(driver, **kwargs):
        return TrackingBackend()

    async with httpx.AsyncClient() as client:
        await execute_operation(_read_payload("rb-1"), session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, retry_blocked=True,
                                resolve=fake_resolve)
    assert backend_call_count == 0                       # backend NOT called
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "error" and "Something went wrong" in body["message"]
    # NOT cached — the operation may have been applied on the prior attempt.
    assert await cache.get("rb-1") is None


@respx.mock
async def test_cache_replay_reports_cache_hit_and_no_steps(seeded):
    # First run fails terminally (cached); second run replays.
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()

    class FailingBackend:
        async def read_balance(self, ctx):
            raise BackendError("mock:insufficient")

    def fake_resolve(driver, **kwargs):
        return FailingBackend()

    payload = _read_payload("read:cache-diag")

    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, resolve=fake_resolve)
    first = json.loads(route.calls.last.request.content.decode())

    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client,
                                settings=_settings(), result_cache=cache, resolve=fake_resolve)
    second = json.loads(route.calls.last.request.content.decode())

    d = second["diagnostics"]
    assert d["cache_hit"] is True
    assert d["steps"] == []
    assert d["failure_kind"] == first["diagnostics"]["failure_kind"]
    assert d["reason"] == first["diagnostics"]["reason"]
