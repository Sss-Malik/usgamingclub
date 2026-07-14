# tests/integration/test_executor.py
import json

import httpx
import respx

from app.backends.base import BackendError, TransientBackendError
from app.config import Settings
from app.operations.executor import execute_operation
from app.schemas.requests import Operation
from app.schemas.results import (
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
)

URL = "https://arcadia.test/api/automation/webhook"


def _settings():
    return Settings(
        api_secret="in",
        webhook_secret="out",
        app_url="https://arcadia.test",
        webhook_max_budget_seconds=600,
    )


def _recharge_payload():
    return Operation(
        action="recharge", type="RECHARGE", idempotency_key="recharge:t1",
        user_id=42, backend_name="milkyway", username="player_one", amount=50,
        correlation={"transaction_id": "t1"},
    ).model_dump()


class _FakeBackend:
    """Configurable backend injected via the executor `resolve=` hook."""

    def __init__(self, *, raise_exc=None, recharge_balance=1234.0):
        self.raise_exc = raise_exc
        self.recharge_balance = recharge_balance
        self.calls = 0

    async def recharge(self, ctx, *, amount):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return RechargeResult(balance=self.recharge_balance)

    async def read_balance(self, ctx):
        self.calls += 1
        return ReadBalanceResult(balance=127.5)

    async def create_account(self, ctx):
        self.calls += 1
        return CreateAccountResult(
            username=ctx.account_username or "gen1234", password="pw", external_user_id="u:g"
        )


def _resolver(backend):
    def _resolve(driver, **kwargs):
        return backend
    return _resolve


async def _run(payload, session_factory, *, resolve=None, retry_blocked=False, result_cache=None):
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload,
            session_factory=session_factory,
            http_client=client,
            settings=_settings(),
            resolve=resolve,
            retry_blocked=retry_blocked,
            result_cache=result_cache,
        )


@respx.mock
async def test_recharge_success_posts_success_webhook(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    backend = _FakeBackend()
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["action"] == "recharge" and body["status"] == "success"
    assert body["transaction_id"] == "t1" and body["amount"] == 50
    assert backend.calls == 1


@respx.mock
async def test_create_account_success_includes_account_created(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = Operation(
        action="create", type="CREATE_ACCOUNT", idempotency_key="create:1",
        user_id=42, backend_name="milkyway", account_username="janedoe1234",
    ).model_dump()
    await _run(payload, seeded, resolve=_resolver(_FakeBackend()))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "success"
    assert body["account_created"][0]["username"] == "janedoe1234"
    assert body["account_created"][0]["id_from_backend"] == "u:g"


@respx.mock
async def test_backend_error_posts_failed_and_caches(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    from app.operations.result_cache import InMemoryResultCache
    cache = InMemoryResultCache()
    backend = _FakeBackend(raise_exc=BackendError("Insufficient balance"))
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), result_cache=cache)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed" and body["message"] == "Insufficient balance"
    # replay re-delivers from cache without re-calling the backend
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), result_cache=cache)
    assert backend.calls == 1


@respx.mock
async def test_transient_error_posts_error_and_not_cached(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    from app.operations.result_cache import InMemoryResultCache
    cache = InMemoryResultCache()
    backend = _FakeBackend(raise_exc=TransientBackendError(
        "timeout", provider_http_status=503, provider_code="T1", provider_message="Gateway timeout",
    ))
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), result_cache=cache)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "error"
    assert body["diagnostics"]["failure_kind"] == "transient"
    assert body["diagnostics"]["provider"] == {
        "http_status": 503, "code": "T1", "message": "Gateway timeout",
    }
    # not cached → a re-run re-calls the backend
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), result_cache=cache)
    assert backend.calls == 2


@respx.mock
async def test_unexpected_exception_posts_error_and_not_cached(seeded):
    # A bare (non-BackendError) exception from the backend must still be reported cleanly —
    # never crash the worker — with failure_kind "unexpected", and must NOT be cached (an arq
    # re-run should get a fresh chance rather than replay an unexpected internal failure).
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    from app.operations.result_cache import InMemoryResultCache
    cache = InMemoryResultCache()
    backend = _FakeBackend(raise_exc=RuntimeError("boom"))
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), result_cache=cache)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "error"
    assert body["diagnostics"]["failure_kind"] == "unexpected"
    assert await cache.get("recharge:t1") is None


@respx.mock
async def test_retry_blocked_posts_error_without_calling_backend(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    backend = _FakeBackend()
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), retry_blocked=True)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "error"
    assert backend.calls == 0


@respx.mock
async def test_preflight_game_not_found_posts_failed(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = Operation(
        action="recharge", type="RECHARGE", idempotency_key="recharge:t9",
        user_id=42, backend_name="does-not-exist", username="player_one", amount=50,
        correlation={"transaction_id": "t9"},
    ).model_dump()
    await _run(payload, seeded, resolve=_resolver(_FakeBackend()))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed" and body["message"].startswith("game_not_found")


@respx.mock
async def test_success_webhook_has_diagnostics_with_op_id(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = Operation(
        action="read", type="READ_BALANCE", idempotency_key="read:diag1",
        user_id=42, backend_name="milkyway", username="player_one", op_id="01J",
        correlation={"read_id": 1},
    ).model_dump()
    backend = _FakeBackend()
    await _run(payload, seeded, resolve=_resolver(backend))
    body = json.loads(route.calls.last.request.content.decode())
    d = body["diagnostics"]
    assert body["op_id"] == "01J" and d["op_id"] == "01J"
    assert d["cache_hit"] is False and d["attempt"] == 1
    assert isinstance(d["duration_ms"], int) and d["duration_ms"] >= 0
    assert "failure_kind" not in d  # success


@respx.mock
async def test_backend_failure_sets_failure_kind_and_reason(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    backend = _FakeBackend(raise_exc=BackendError("mock:insufficient"))
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "failed"
    assert body["message"] == "mock:insufficient"          # unchanged player path
    assert body["diagnostics"]["failure_kind"] == "backend"
    assert body["diagnostics"]["reason"] == "mock:insufficient"


@respx.mock
async def test_preflight_failure_kind(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = Operation(
        action="recharge", type="RECHARGE", idempotency_key="recharge:t10",
        user_id=42, backend_name="does-not-exist", username="player_one", amount=50,
        correlation={"transaction_id": "t10"},
    ).model_dump()
    await _run(payload, seeded, resolve=_resolver(_FakeBackend()))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["diagnostics"]["failure_kind"] == "preflight"


@respx.mock
async def test_diagnostics_assembly_failure_still_delivers_legacy_webhook(seeded, monkeypatch):
    # Spec invariant: a bug in diagnostics assembly must never suppress the webhook itself.
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    import app.operations.executor as executor_mod

    def _boom(**kwargs):
        raise RuntimeError("diagnostics blew up")

    monkeypatch.setattr(executor_mod, "assemble_diagnostics", _boom)
    backend = _FakeBackend()
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend))
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "success"           # correct status/message still delivered
    assert "diagnostics" not in body              # assembly failed -> legacy body (diagnostics=None)


@respx.mock
async def test_retry_blocked_kind(seeded):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    backend = _FakeBackend()
    await _run(_recharge_payload(), seeded, resolve=_resolver(backend), retry_blocked=True)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["status"] == "error"
    assert body["diagnostics"]["failure_kind"] == "retry_blocked"
