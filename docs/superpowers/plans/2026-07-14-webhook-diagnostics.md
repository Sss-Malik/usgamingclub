# Webhook Diagnostics + `op_id` Echo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit a truthful `diagnostics` object on every webhook (success and failure) and echo Arcadia's `op_id`, without touching the player-facing `message`, HMAC, or idempotency/replay semantics.

**Architecture:** Provider error detail (status/code/message) rides on enriched `BackendError`/`TransientBackendError` exceptions populated by the existing `map_*` functions (failure-only, one touch-point each). Steps/timing/session-reuse/balances ride on a mutable `DiagnosticsRecorder` threaded through `BackendContext` and into each client, captured on success and failure. The executor times the op, classifies `failure_kind`, and assembles one `diagnostics` dict which `app/webhook/payload.py` attaches. Everything is additive and optional.

**Tech Stack:** Python 3.12, FastAPI, arq/Redis, httpx, pydantic v2, pytest (async auto-mode via `tests/conftest.py`).

## Global Constraints

- **Never change `app/webhook/payload.py::_message()` behavior** — including the generic sentence `"Something went wrong. Please try again later."` for `status="error"`.
- **Never break the existing webhook body shape** — all new fields are additive and optional; a `diagnostics=None` call to `build_webhook_payload` must produce the byte-identical legacy body.
- **Never write money/account tables. Never log secrets.** `provider_message` is untruncated but carries no credentials; never retain a `data` dict that contains a generated/account password.
- **Omit any field that cannot be populated truthfully; never invent one.** No `provider_txn_id` anywhere. `session_reuse` is `null` (not `false`) where no session concept exists.
- **`op_id` is optional on input** (older Laravel may omit it) and **always echoed when present** — top-level and inside `diagnostics`.
- HMAC re-signs over the raw body on every webhook attempt (unchanged). Only `succeeded`/`failed` outcomes are cached; transient `error` is never cached (unchanged).
- Match existing test style: async tests are bare `async def test_*` (anyio auto-mode); run with `make test` or `pytest <path> -v`.

---

## Task 1: Enrich backend exceptions with provider fields

**Files:**
- Modify: `app/backends/base.py`
- Test: `tests/unit/test_backend_base.py`

**Interfaces:**
- Produces: `BackendError(reason, *, provider_http_status: int | None = None, provider_code: str | int | None = None, provider_message: str | None = None)` with attributes `.reason`, `.provider_http_status`, `.provider_code`, `.provider_message`. `TransientBackendError` inherits the same constructor.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_backend_base.py
from app.backends.base import BackendError, TransientBackendError


def test_backend_error_carries_optional_provider_fields():
    err = BackendError("gamevault:7:insufficient_user_balance",
                       provider_http_status=200, provider_code=7,
                       provider_message="user balance not enough")
    assert err.reason == "gamevault:7:insufficient_user_balance"
    assert err.provider_http_status == 200
    assert err.provider_code == 7
    assert err.provider_message == "user balance not enough"


def test_backend_error_provider_fields_default_none():
    err = BackendError("boom")
    assert err.provider_http_status is None
    assert err.provider_code is None
    assert err.provider_message is None


def test_transient_error_carries_provider_fields():
    err = TransientBackendError("gamevault_http_503", provider_http_status=503)
    assert isinstance(err, BackendError)
    assert err.provider_http_status == 503
    assert str(err) == "gamevault_http_503"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_backend_base.py -v`
Expected: FAIL — `BackendError.__init__() got an unexpected keyword argument 'provider_http_status'`

- [ ] **Step 3: Write minimal implementation**

Replace the `BackendError` class in `app/backends/base.py` (keep `TransientBackendError` as a subclass; it inherits the constructor):

```python
class BackendError(Exception):
    """Raised when a game backend call fails in a way that should be reported as status:failed.

    `reason` is the player-facing slug (unchanged). The optional `provider_*` fields carry
    structured provider detail for the webhook `diagnostics` channel; they are never used for
    the player-facing message.
    """

    def __init__(
        self,
        reason: str,
        *,
        provider_http_status: int | None = None,
        provider_code: str | int | None = None,
        provider_message: str | None = None,
    ) -> None:
        self.reason = reason
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code
        self.provider_message = provider_message
        super().__init__(reason)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_backend_base.py -v`
Expected: PASS (all tests, including any pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add app/backends/base.py tests/unit/test_backend_base.py
git commit -m "feat(diagnostics): enrich backend exceptions with provider fields"
```

---

## Task 2: `DiagnosticsRecorder`

**Files:**
- Create: `app/backends/diagnostics.py`
- Test: `tests/unit/test_diagnostics_recorder.py`

**Interfaces:**
- Produces:
  - `class DiagnosticsRecorder` with:
    - `step(name: str, *, phase: str, http: bool = True, external: bool = False)` → async context manager; records `{name, phase, http, external, ok, ms}` on exit; on exception records `ok=False` and re-raises.
    - `skip(name: str, *, phase: str)` → records `{name, phase, http: False, ok: True, ms: 0, skipped: True}`.
    - `session_event(kind: str)` → sets `session_reuse` (`"relogin"` never downgraded to `"fresh"`/`"hit"`).
    - `mark_external_user_id(value)`, `mark_balance_before(value)`, `mark_balance_after(value)`.
    - `snapshot() -> dict` → `{"steps": [...], "session_reuse": str | None, "external_user_id": ..., "balance_before": ..., "balance_after": ...}`.
  - `NULL_RECORDER` — a shared no-op recorder whose `snapshot()` returns `{"steps": [], "session_reuse": None, "external_user_id": None, "balance_before": None, "balance_after": None}`.
  - A `now` seam: `DiagnosticsRecorder(now=time.monotonic)` so tests can inject a clock.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_diagnostics_recorder.py
import pytest

from app.backends.base import BackendError
from app.backends.diagnostics import NULL_RECORDER, DiagnosticsRecorder


def _clock():
    ticks = iter([1.0, 1.2, 5.0, 5.8])  # start/stop pairs, seconds
    return lambda: next(ticks)


async def test_step_records_name_phase_and_ms_on_success():
    rec = DiagnosticsRecorder(now=_clock())
    async with rec.step("recharge.post", phase="primary"):
        pass
    snap = rec.snapshot()
    assert snap["steps"] == [
        {"name": "recharge.post", "phase": "primary", "http": True,
         "external": False, "ok": True, "ms": 200}
    ]


async def test_step_records_failure_and_reraises():
    rec = DiagnosticsRecorder(now=_clock())
    with pytest.raises(BackendError):
        async with rec.step("recharge.post", phase="primary"):
            raise BackendError("boom")
    step = rec.snapshot()["steps"][0]
    assert step["ok"] is False
    assert step["ms"] == 200


async def test_skip_records_skipped_step():
    rec = DiagnosticsRecorder()
    rec.skip("login.submit", phase="auth")
    step = rec.snapshot()["steps"][0]
    assert step == {"name": "login.submit", "phase": "auth", "http": False,
                    "external": False, "ok": True, "ms": 0, "skipped": True}


async def test_session_event_relogin_is_sticky():
    rec = DiagnosticsRecorder()
    rec.session_event("fresh")
    rec.session_event("relogin")
    rec.session_event("fresh")   # must not downgrade
    assert rec.snapshot()["session_reuse"] == "relogin"


async def test_marks_are_reported():
    rec = DiagnosticsRecorder()
    rec.mark_external_user_id("u:1")
    rec.mark_balance_before(40.0)
    rec.mark_balance_after(90.0)
    snap = rec.snapshot()
    assert snap["external_user_id"] == "u:1"
    assert snap["balance_before"] == 40.0
    assert snap["balance_after"] == 90.0


async def test_null_recorder_is_inert():
    async with NULL_RECORDER.step("x", phase="primary"):
        pass
    NULL_RECORDER.skip("y", phase="auth")
    NULL_RECORDER.session_event("fresh")
    NULL_RECORDER.mark_balance_after(1.0)
    assert NULL_RECORDER.snapshot() == {
        "steps": [], "session_reuse": None,
        "external_user_id": None, "balance_before": None, "balance_after": None,
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_diagnostics_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.backends.diagnostics'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/backends/diagnostics.py
import time
from contextlib import asynccontextmanager

_SESSION_RANK = {None: 0, "hit": 1, "fresh": 2, "relogin": 3}


class DiagnosticsRecorder:
    """Mutable per-operation recorder for step timing, session reuse, and success marks.

    Lives on the executor stack and is threaded through BackendContext + clients. Survives
    exception unwinding, so a failing op still yields the steps that ran before the raise.
    """

    def __init__(self, *, now=time.monotonic) -> None:
        self._now = now
        self._steps: list[dict] = []
        self._session_reuse: str | None = None
        self._external_user_id = None
        self._balance_before = None
        self._balance_after = None

    @asynccontextmanager
    async def step(self, name: str, *, phase: str, http: bool = True, external: bool = False):
        start = self._now()
        ok = True
        try:
            yield self
        except BaseException:
            ok = False
            raise
        finally:
            ms = int((self._now() - start) * 1000)
            self._steps.append({
                "name": name, "phase": phase, "http": http,
                "external": external, "ok": ok, "ms": ms,
            })

    def skip(self, name: str, *, phase: str) -> None:
        self._steps.append({
            "name": name, "phase": phase, "http": False,
            "external": False, "ok": True, "ms": 0, "skipped": True,
        })

    def session_event(self, kind: str) -> None:
        if _SESSION_RANK.get(kind, 0) >= _SESSION_RANK.get(self._session_reuse, 0):
            self._session_reuse = kind

    def mark_external_user_id(self, value) -> None:
        self._external_user_id = value

    def mark_balance_before(self, value) -> None:
        self._balance_before = value

    def mark_balance_after(self, value) -> None:
        self._balance_after = value

    def snapshot(self) -> dict:
        return {
            "steps": list(self._steps),
            "session_reuse": self._session_reuse,
            "external_user_id": self._external_user_id,
            "balance_before": self._balance_before,
            "balance_after": self._balance_after,
        }


class _NullRecorder(DiagnosticsRecorder):
    """No-op recorder used when diagnostics is not wired (direct client construction, tests)."""

    @asynccontextmanager
    async def step(self, name: str, *, phase: str, http: bool = True, external: bool = False):
        yield self

    def skip(self, name: str, *, phase: str) -> None:
        return None

    def session_event(self, kind: str) -> None:
        return None

    def mark_external_user_id(self, value) -> None:
        return None

    def mark_balance_before(self, value) -> None:
        return None

    def mark_balance_after(self, value) -> None:
        return None


NULL_RECORDER = _NullRecorder()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_diagnostics_recorder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backends/diagnostics.py tests/unit/test_diagnostics_recorder.py
git commit -m "feat(diagnostics): add DiagnosticsRecorder"
```

---

## Task 3: `op_id` on request schemas + endpoints

**Files:**
- Modify: `app/schemas/requests.py` (add `op_id` to `_In` and `Operation`)
- Modify: `app/api/automation.py` (thread `op_id=req.op_id` through all six `Operation(...)`)
- Test: `tests/unit/test_schemas_requests.py`, `tests/integration/test_automation_endpoints.py`

**Interfaces:**
- Produces: `Operation.op_id: str | None` and `_In.op_id: str | None` (all six request models inherit it). Enqueued payload carries `op_id`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_schemas_requests.py
from app.schemas.requests import Operation, RechargeRequest


def test_recharge_request_parses_optional_op_id():
    req = RechargeRequest.model_validate({
        "user_id": 1, "backend_name": "milkyway", "username": "u",
        "amount": 5, "transaction_id": "t1", "op_id": "01JXYZ",
    })
    assert req.op_id == "01JXYZ"


def test_recharge_request_op_id_absent_is_none():
    req = RechargeRequest.model_validate({
        "user_id": 1, "backend_name": "milkyway", "username": "u",
        "amount": 5, "transaction_id": "t1",
    })
    assert req.op_id is None


def test_operation_carries_op_id():
    op = Operation(action="read", type="READ_BALANCE", idempotency_key="read:1",
                   user_id=1, backend_name="milkyway", op_id="01JABC")
    assert op.op_id == "01JABC"
    assert "op_id" in op.model_dump()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_schemas_requests.py -v`
Expected: FAIL — `AttributeError: 'RechargeRequest' object has no attribute 'op_id'`

- [ ] **Step 3: Write minimal implementation**

In `app/schemas/requests.py`, add `op_id` to `_In`:

```python
class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: int
    backend_name: str = Field(min_length=1)
    op_id: str | None = None
```

And to `Operation` (add the field near `correlation`):

```python
    op_id: str | None = None
    correlation: dict[str, str | int] = Field(default_factory=dict)
```

In `app/api/automation.py`, add `op_id=req.op_id` to each of the six `Operation(...)` constructions. Example for `recharge` (apply the same to create/withdraw/reset_password/freeplay/read):

```python
    op = Operation(
        action="recharge", type="RECHARGE",
        idempotency_key=f"recharge:{req.transaction_id}",
        user_id=req.user_id, backend_name=req.backend_name, username=req.username,
        amount=req.amount, correlation={"transaction_id": req.transaction_id},
        op_id=req.op_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_schemas_requests.py tests/integration/test_automation_endpoints.py -v`
Expected: PASS

- [ ] **Step 5: Add an endpoint test that op_id rides the enqueued payload**

```python
# append to tests/integration/test_automation_endpoints.py, following the file's existing
# signing + enqueue-capture helpers (mirror an existing recharge test for setup).
async def test_recharge_enqueues_op_id(client, signed, captured_jobs):
    body = {"user_id": 1, "backend_name": "milkyway", "username": "u",
            "amount": 5, "transaction_id": "t1", "op_id": "01JOP"}
    resp = await client.post("/recharge", **signed(body))
    assert resp.status_code == 202
    assert captured_jobs[-1]["payload"]["op_id"] == "01JOP"
```

(If the existing suite captures enqueued jobs differently, match that mechanism; the assertion is that `op_id` is present in the enqueued payload.)

- [ ] **Step 6: Run + commit**

Run: `pytest tests/integration/test_automation_endpoints.py -v`
Expected: PASS

```bash
git add app/schemas/requests.py app/api/automation.py tests/unit/test_schemas_requests.py tests/integration/test_automation_endpoints.py
git commit -m "feat(diagnostics): accept and thread op_id through operations"
```

---

## Task 4: `BackendContext` diagnostics fields

**Files:**
- Modify: `app/backends/context.py`
- Test: `tests/unit/test_backend_base.py` (or a new `tests/unit/test_context.py`)

**Interfaces:**
- Produces: `BackendContext.diagnostics: DiagnosticsRecorder | None = None`, `.op_id: str | None = None`, `.attempt: int = 1`, and a read-only `.diag` property returning `self.diagnostics or NULL_RECORDER`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_context.py
from app.backends.context import BackendContext, GameCredentials
from app.backends.diagnostics import NULL_RECORDER, DiagnosticsRecorder


def _creds():
    return GameCredentials(game_id=1, name="g", backend_url=None, login_page_url=None,
                           backend_username=None, backend_password=None, api_base_url=None,
                           api_agent_id=None, api_secret_key=None, binding_key=None)


def test_context_defaults_diag_to_null_recorder():
    ctx = BackendContext(credentials=_creds(), user_id=1, account=None)
    assert ctx.diagnostics is None
    assert ctx.diag is NULL_RECORDER
    assert ctx.op_id is None
    assert ctx.attempt == 1


def test_context_diag_returns_supplied_recorder():
    rec = DiagnosticsRecorder()
    ctx = BackendContext(credentials=_creds(), user_id=1, account=None,
                         diagnostics=rec, op_id="01J", attempt=2)
    assert ctx.diag is rec
    assert ctx.op_id == "01J"
    assert ctx.attempt == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_context.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'diagnostics'`

- [ ] **Step 3: Write minimal implementation**

In `app/backends/context.py`:

```python
from dataclasses import dataclass

from app.backends.diagnostics import NULL_RECORDER, DiagnosticsRecorder

# ...existing GameCredentials and AccountIdentity unchanged...


@dataclass(frozen=True)
class BackendContext:
    credentials: GameCredentials
    user_id: int | None
    account: AccountIdentity | None
    idempotency_key: str = ""
    account_username: str | None = None
    diagnostics: DiagnosticsRecorder | None = None
    op_id: str | None = None
    attempt: int = 1

    @property
    def diag(self) -> DiagnosticsRecorder:
        return self.diagnostics or NULL_RECORDER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_context.py tests/unit/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backends/context.py tests/unit/test_context.py
git commit -m "feat(diagnostics): add recorder/op_id/attempt to BackendContext"
```

---

## Task 5: `CachedOutcome.detail` for replay-stable diagnostics

**Files:**
- Modify: `app/operations/result_cache.py`
- Test: `tests/unit/test_result_cache.py`

**Interfaces:**
- Produces: `CachedOutcome(status, result, reason, detail: dict | None = None)`. `RedisResultCache` round-trips `detail` (JSON).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_result_cache.py
from app.operations.result_cache import CachedOutcome, RedisResultCache


class _FakeRedis:
    def __init__(self):
        self.store = {}
    async def get(self, k):
        return self.store.get(k)
    async def set(self, k, v, ex=None):
        self.store[k] = v


async def test_cached_outcome_detail_defaults_none():
    assert CachedOutcome("succeeded", {"balance": 1}, None).detail is None


async def test_redis_cache_round_trips_detail():
    cache = RedisResultCache(_FakeRedis())
    detail = {"failure_kind": "backend",
              "provider": {"http_status": 200, "code": "7", "message": "no funds"},
              "external_user_id": "u:1", "balance_before": None, "balance_after": None}
    await cache.set("recharge:1", CachedOutcome("failed", None, "gamevault:7:x", detail=detail), 60)
    got = await cache.get("recharge:1")
    assert got.detail == detail
    assert got.reason == "gamevault:7:x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_result_cache.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'detail'`

- [ ] **Step 3: Write minimal implementation**

In `app/operations/result_cache.py`, add `detail` to the dataclass and the Redis (de)serialization:

```python
@dataclass
class CachedOutcome:
    status: str               # "succeeded" | "failed" | "error"
    result: dict | None       # present when succeeded
    reason: str | None        # present when failed/error
    detail: dict | None = None  # replay-stable diagnostics subset (failed/succeeded only)
```

In `RedisResultCache.get`, extend the return:

```python
        return CachedOutcome(status=d["status"], result=d.get("result"),
                             reason=d.get("reason"), detail=d.get("detail"))
```

In `RedisResultCache.set`, extend the serialized dict:

```python
        raw = json.dumps({
            "status": outcome.status, "result": outcome.result,
            "reason": outcome.reason, "detail": outcome.detail,
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_result_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/operations/result_cache.py tests/unit/test_result_cache.py
git commit -m "feat(diagnostics): add replay-stable detail to CachedOutcome"
```

---

## Task 6: Thread the recorder into `resolve_backend` + all client constructors

**Files:**
- Modify: `app/backends/registry.py`
- Modify: `app/backends/gamevault/client.py`, `app/backends/gameroom/client.py`, `app/backends/goldentreasure/client.py`, `app/backends/_aspnet_cashier/client.py`, `app/backends/ultrapanda/client.py`, `app/backends/yolo/client.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Produces: `resolve_backend(driver, *, credentials, http_client, settings, session_store=None, redis=None, diagnostics=None)`. Each client `__init__` accepts `diagnostics: DiagnosticsRecorder | None = None` and stores `self._diag = diagnostics or NULL_RECORDER`. No behavior change yet — only plumbing.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_registry.py
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.registry import resolve_backend
# reuse the file's existing credential/settings fixtures/builders


def test_resolve_backend_passes_diagnostics_to_client(gamevault_credentials, settings_stub, http_stub):
    rec = DiagnosticsRecorder()
    backend = resolve_backend("gamevault", credentials=gamevault_credentials,
                              http_client=http_stub, settings=settings_stub, diagnostics=rec)
    assert backend._client._diag is rec


def test_resolve_backend_defaults_diagnostics_null(gamevault_credentials, settings_stub, http_stub):
    from app.backends.diagnostics import NULL_RECORDER
    backend = resolve_backend("gamevault", credentials=gamevault_credentials,
                              http_client=http_stub, settings=settings_stub)
    assert backend._client._diag is NULL_RECORDER
```

(Use the existing fixtures/builders in `test_registry.py`; if they don't exist, construct `GameCredentials` inline as in Task 4.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_registry.py -v`
Expected: FAIL — `TypeError: resolve_backend() got an unexpected keyword argument 'diagnostics'`

- [ ] **Step 3: Write minimal implementation**

In `app/backends/registry.py`, add the parameter and forward it to every client constructor:

```python
def resolve_backend(
    driver: str | None, *,
    credentials: GameCredentials,
    http_client,
    settings: Settings,
    session_store=None,
    redis=None,
    diagnostics=None,
) -> GameBackend:
```

Then pass `diagnostics=diagnostics` into each `*Client(...)` call in the function (GameVaultClient, GameroomClient, GoldenTreasureClient, AspnetCashierClient, UltraPandaClient, YoloClient). The `MockBackend` branch takes no client; leave it unchanged (the mock reads `ctx.diag` directly in Task 10).

In each of the six client `__init__` signatures add `diagnostics=None` (keyword, last) and, at the top of `__init__`, store it. Example for `GameVaultClient`:

```python
from app.backends.diagnostics import NULL_RECORDER

class GameVaultClient:
    def __init__(self, *, base_url, agent_id, secret_key, http_client, diagnostics=None) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = str(agent_id)
        self._secret_key = secret_key
        self._http = http_client
        self._diag = diagnostics or NULL_RECORDER
```

Apply the identical `diagnostics=None` param + `self._diag = diagnostics or NULL_RECORDER` line to the other five clients.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_registry.py tests/unit/test_gamevault_client.py tests/unit/test_gameroom_client.py tests/unit/test_ultrapanda_client.py tests/unit/test_goldentreasure_client.py tests/unit/test_yolo_client.py tests/unit/test_aspnet_client.py -v`
Expected: PASS (existing client tests still construct clients without `diagnostics` → default `NULL_RECORDER`)

- [ ] **Step 5: Commit**

```bash
git add app/backends/registry.py app/backends/*/client.py app/backends/_aspnet_cashier/client.py tests/unit/test_registry.py
git commit -m "feat(diagnostics): thread recorder into resolve_backend and clients"
```

---

## Task 7: `assemble_diagnostics` + webhook payload wiring

**Files:**
- Modify: `app/webhook/payload.py`
- Test: `tests/unit/test_webhook_payload.py`

**Interfaces:**
- Produces:
  - `assemble_diagnostics(*, op_id, idempotency_key, attempt, cache_hit, duration_ms, snapshot=None, failure_kind=None, reason=None, provider=None) -> dict` — a pure shaper that always includes `idempotency_key`, `attempt`, `cache_hit`, `duration_ms`, and `steps` (from `snapshot`, default `[]`); includes `op_id`, `session_reuse`, `external_user_id`, `balance_before`, `balance_after` only when non-`None`; includes `failure_kind`, `reason`, `provider` only when provided/truthy.
  - `build_webhook_payload(op, outcome, *, backend_id, diagnostics=None)` — when `diagnostics` is a dict, sets `body["diagnostics"]`; when `op.op_id` is set, sets top-level `body["op_id"]`. `diagnostics=None` → byte-identical legacy body.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_webhook_payload.py
from app.webhook.payload import assemble_diagnostics, build_webhook_payload


def test_assemble_omits_untruthful_fields():
    d = assemble_diagnostics(op_id=None, idempotency_key="read:1", attempt=1,
                             cache_hit=False, duration_ms=12)
    assert d == {"idempotency_key": "read:1", "attempt": 1, "cache_hit": False,
                 "duration_ms": 12, "steps": []}
    assert "op_id" not in d and "session_reuse" not in d and "provider" not in d


def test_assemble_includes_failure_and_provider():
    snap = {"steps": [{"name": "recharge.post", "phase": "primary", "http": True,
                       "external": False, "ok": False, "ms": 800}],
            "session_reuse": "hit", "external_user_id": None,
            "balance_before": None, "balance_after": None}
    d = assemble_diagnostics(op_id="01J", idempotency_key="recharge:1", attempt=1,
                             cache_hit=False, duration_ms=900, snapshot=snap,
                             failure_kind="backend",
                             reason="gamevault:7:insufficient_user_balance",
                             provider={"http_status": 200, "code": "7", "message": "no funds"})
    assert d["op_id"] == "01J"
    assert d["session_reuse"] == "hit"
    assert d["failure_kind"] == "backend"
    assert d["reason"] == "gamevault:7:insufficient_user_balance"
    assert d["provider"] == {"http_status": 200, "code": "7", "message": "no funds"}
    assert d["steps"][0]["name"] == "recharge.post"
    assert "external_user_id" not in d  # None → omitted


def test_assemble_drops_empty_provider():
    d = assemble_diagnostics(op_id=None, idempotency_key="k", attempt=1, cache_hit=False,
                             duration_ms=1, failure_kind="transient", reason="gamevault_http_503",
                             provider={"http_status": None, "code": None, "message": None})
    assert "provider" not in d


def test_build_payload_without_diagnostics_is_legacy_shape():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5})
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    body = build_webhook_payload(op, out, backend_id=1)
    assert "diagnostics" not in body and "op_id" not in body


def test_build_payload_attaches_diagnostics_and_top_level_op_id():
    op = _op("read", "READ_BALANCE", correlation={"read_id": 5}, op_id="01J")
    out = CachedOutcome("succeeded", {"balance": 127.5}, None)
    diag = {"idempotency_key": "read:1", "attempt": 1, "cache_hit": False,
            "duration_ms": 3, "steps": [], "op_id": "01J"}
    body = build_webhook_payload(op, out, backend_id=1, diagnostics=diag)
    assert body["op_id"] == "01J"
    assert body["diagnostics"] is diag
    assert body["message"] == ""  # unchanged


def test_error_message_still_generic_but_reason_can_differ():
    op = _op("recharge", "RECHARGE", amount=5, correlation={"transaction_id": "t"})
    out = CachedOutcome("error", None, "backend_error: gamevault_http_503")
    diag = {"idempotency_key": "recharge:t", "attempt": 1, "cache_hit": False,
            "duration_ms": 5, "steps": [], "failure_kind": "transient",
            "reason": "gamevault_http_503"}
    body = build_webhook_payload(op, out, backend_id=1, diagnostics=diag)
    assert body["message"] == GENERIC
    assert body["diagnostics"]["reason"] == "gamevault_http_503"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_webhook_payload.py -v`
Expected: FAIL — `ImportError: cannot import name 'assemble_diagnostics'`

- [ ] **Step 3: Write minimal implementation**

Add to `app/webhook/payload.py`:

```python
def assemble_diagnostics(
    *, op_id, idempotency_key, attempt, cache_hit, duration_ms,
    snapshot=None, failure_kind=None, reason=None, provider=None,
) -> dict:
    snap = snapshot or {}
    diag: dict = {
        "idempotency_key": idempotency_key,
        "attempt": attempt,
        "cache_hit": cache_hit,
        "duration_ms": duration_ms,
        "steps": snap.get("steps", []),
    }
    if op_id is not None:
        diag["op_id"] = op_id
    for key in ("session_reuse", "external_user_id", "balance_before", "balance_after"):
        value = snap.get(key)
        if value is not None:
            diag[key] = value
    if failure_kind is not None:
        diag["failure_kind"] = failure_kind
    if reason is not None:
        diag["reason"] = reason
    if provider:
        pruned = {k: v for k, v in provider.items() if v is not None}
        if pruned:
            diag["provider"] = pruned
    return diag
```

Change the `build_webhook_payload` signature and add the two attachments just before the `if status != "success": return body` line:

```python
def build_webhook_payload(
    op: Operation, outcome: CachedOutcome, *, backend_id: int | None, diagnostics: dict | None = None
) -> dict:
    # ...existing body assembly unchanged...
    if op.op_id is not None:
        body["op_id"] = op.op_id
    if diagnostics is not None:
        body["diagnostics"] = diagnostics
    # ...existing amount echo + `if status != "success"` block + action-specific fields unchanged...
```

(Place the `op_id`/`diagnostics` attachment after `body.update(op.correlation)` and before the success-only action fields so it applies to every status.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_webhook_payload.py -v`
Expected: PASS (including all pre-existing payload tests — legacy shape preserved)

- [ ] **Step 5: Commit**

```bash
git add app/webhook/payload.py tests/unit/test_webhook_payload.py
git commit -m "feat(diagnostics): assemble diagnostics + echo op_id in webhook payload"
```

---

## Task 8: Worker passes `job_try` as `attempt`

**Files:**
- Modify: `app/worker/tasks.py`
- Modify: `app/operations/executor.py` (add `attempt: int = 1` param, no use yet)
- Test: `tests/unit/test_worker_tasks.py`

**Interfaces:**
- Produces: `execute_operation(..., attempt: int = 1)`. Worker computes `attempt = ctx.get("job_try", 1) or 1` and passes it.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_worker_tasks.py — assert execute_operation receives attempt=job_try.
# Mirror the file's existing monkeypatch of execute_operation; capture kwargs.
async def test_worker_passes_job_try_as_attempt(monkeypatch):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("app.worker.tasks.execute_operation", fake_execute)
    from app.worker.tasks import execute_operation_task
    ctx = {"session_factory": None, "http_client": None, "result_cache": None,
           "session_store": None, "redis_cache": None, "job_try": 3}
    await execute_operation_task(ctx, {"action": "read"})
    assert captured["attempt"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_worker_tasks.py -v`
Expected: FAIL — `KeyError: 'attempt'`

- [ ] **Step 3: Write minimal implementation**

In `app/operations/executor.py`, add `attempt: int = 1` to the `execute_operation` signature (place it after `retry_blocked`):

```python
    retry_blocked: bool = False,
    attempt: int = 1,
    resolve=_resolve_backend,
```

In `app/worker/tasks.py`, pass it:

```python
    await execute_operation(
        payload,
        session_factory=ctx["session_factory"],
        http_client=ctx["http_client"],
        settings=get_settings(),
        result_cache=ctx["result_cache"],
        session_store=ctx["session_store"],
        redis=ctx["redis_cache"],
        retry_blocked=retry_blocked,
        attempt=job_try,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_worker_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/worker/tasks.py app/operations/executor.py tests/unit/test_worker_tasks.py
git commit -m "feat(diagnostics): pass arq job_try as attempt into executor"
```

---

## Task 9: `build_context` accepts the recorder

**Files:**
- Modify: `app/preflight/checks.py`
- Test: `tests/unit/test_preflight.py`

**Interfaces:**
- Produces: `build_context(session, *, type, backend_name, username, user_id, idempotency_key="", account_username=None, diagnostics=None, op_id=None, attempt=1)` — attaches `diagnostics`/`op_id`/`attempt` to the returned `BackendContext`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_preflight.py — reuse the file's in-memory session/repository stubs.
from app.backends.diagnostics import DiagnosticsRecorder


async def test_build_context_attaches_recorder(session_with_game):  # existing-style fixture
    rec = DiagnosticsRecorder()
    ctx = await build_context(session_with_game, type="READ_BALANCE", backend_name="milkyway",
                              username="u", user_id=1, diagnostics=rec, op_id="01J", attempt=2)
    assert ctx.diag is rec
    assert ctx.op_id == "01J"
    assert ctx.attempt == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_preflight.py -v`
Expected: FAIL — `TypeError: build_context() got an unexpected keyword argument 'diagnostics'`

- [ ] **Step 3: Write minimal implementation**

In `app/preflight/checks.py`, add the params to `build_context` and to the `BackendContext(...)` return:

```python
async def build_context(
    session: AsyncSession, *, type: str, backend_name: str, username: str | None,
    user_id: int | None, idempotency_key: str = "", account_username: str | None = None,
    diagnostics=None, op_id: str | None = None, attempt: int = 1,
) -> BackendContext:
    # ...unchanged body...
    return BackendContext(
        credentials=credentials, user_id=user_id, account=account,
        idempotency_key=idempotency_key, account_username=account_username,
        diagnostics=diagnostics, op_id=op_id, attempt=attempt,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/preflight/checks.py tests/unit/test_preflight.py
git commit -m "feat(diagnostics): thread recorder/op_id/attempt through preflight"
```

---

## Task 10: Executor assembly seam

**Files:**
- Modify: `app/operations/executor.py`
- Test: `tests/integration/test_executor.py`, `tests/integration/test_executor_cache.py`

**Interfaces:**
- Consumes: `DiagnosticsRecorder` (Task 2), `assemble_diagnostics` (Task 7), enriched exceptions (Task 1), `CachedOutcome.detail` (Task 5), `build_context(diagnostics=...)` (Task 9), `resolve_backend(diagnostics=...)` (Task 6), `attempt` (Task 8).
- Produces: every webhook now carries a `diagnostics` dict; `failure_kind` is set per branch; failed outcomes cache their `detail`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/integration/test_executor.py — reuse the file's existing harness
# (fake session_factory, http capture, mock backend resolve). Assert the delivered webhook
# body's diagnostics block. `delivered` below is however the suite captures the posted body.

async def test_success_webhook_has_diagnostics_with_op_id(executor_env):
    body = await run_executor(executor_env, payload_for("read", op_id="01J"))
    d = body["diagnostics"]
    assert body["op_id"] == "01J" and d["op_id"] == "01J"
    assert d["cache_hit"] is False and d["attempt"] == 1
    assert isinstance(d["duration_ms"], int) and d["duration_ms"] >= 0
    assert "failure_kind" not in d  # success


async def test_backend_failure_sets_failure_kind_and_reason(executor_env_failing):
    # mock backend forced to raise BackendError("mock:insufficient")
    body = await run_executor(executor_env_failing, payload_for("recharge"))
    assert body["status"] == "failed"
    assert body["message"] == "mock:insufficient"          # unchanged player path
    assert body["diagnostics"]["failure_kind"] == "backend"
    assert body["diagnostics"]["reason"] == "mock:insufficient"


async def test_preflight_failure_kind(executor_env_no_game):
    body = await run_executor(executor_env_no_game, payload_for("read"))
    assert body["diagnostics"]["failure_kind"] == "preflight"


async def test_retry_blocked_kind(executor_env):
    body = await run_executor(executor_env, payload_for("recharge"), retry_blocked=True)
    assert body["status"] == "error"
    assert body["diagnostics"]["failure_kind"] == "retry_blocked"
```

```python
# append to tests/integration/test_executor_cache.py
async def test_cache_replay_reports_cache_hit_and_no_steps(cache_env):
    # First run fails terminally (cached); second run replays.
    first = await run_executor(cache_env, payload_for("recharge"))   # failed → cached w/ detail
    second = await run_executor(cache_env, payload_for("recharge"))  # replay
    d = second["diagnostics"]
    assert d["cache_hit"] is True
    assert d["steps"] == []
    assert d["failure_kind"] == first["diagnostics"]["failure_kind"]
    assert d["reason"] == first["diagnostics"]["reason"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_executor.py tests/integration/test_executor_cache.py -v`
Expected: FAIL — `KeyError: 'diagnostics'`

- [ ] **Step 3: Write the implementation**

Rewrite `app/operations/executor.py` to create a recorder, time the op, classify each branch, and deliver with diagnostics. Key changes (full replacement of the function body):

```python
import time

from app.backends.diagnostics import DiagnosticsRecorder
from app.webhook.payload import assemble_diagnostics, build_webhook_payload

# ...existing imports...

_REASON_PREFIXES = ("backend_error: ", "preflight_failed: ", "invalid_payload: ",
                    "invalid_result_payload: ", "retry_blocked: ")


def _public_reason(reason: str | None) -> str | None:
    """The REAL reason for diagnostics — strip the internal stage prefix, keep the meat."""
    if reason is None:
        return None
    for prefix in _REASON_PREFIXES:
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


def _provider_from_exc(exc: BackendError) -> dict | None:
    provider = {"http_status": exc.provider_http_status,
                "code": exc.provider_code,
                "message": exc.provider_message}
    return provider if any(v is not None for v in provider.values()) else None


async def execute_operation(
    payload: dict, *, session_factory, http_client, settings,
    result_cache=None, session_store=None, redis=None,
    retry_blocked: bool = False, attempt: int = 1, resolve=_resolve_backend,
) -> None:
    if result_cache is None:
        result_cache = InMemoryResultCache()

    try:
        op = Operation.model_validate(payload)
    except ValidationError as exc:
        logger.error("operation_unparseable_op", error=_summarize(exc))
        return

    key = op.idempotency_key
    log = logger.bind(idempotency_key=key, action=op.action, type=op.type)
    recorder = DiagnosticsRecorder()
    started = time.monotonic()

    async def deliver(outcome, *, backend_id, failure_kind=None, provider=None,
                      cache_hit=False, snapshot=None):
        duration_ms = int((time.monotonic() - started) * 1000)
        snap = snapshot if snapshot is not None else recorder.snapshot()
        reason = _public_reason(outcome.reason) if failure_kind else None
        diagnostics = assemble_diagnostics(
            op_id=op.op_id, idempotency_key=key, attempt=attempt, cache_hit=cache_hit,
            duration_ms=duration_ms, snapshot=snap, failure_kind=failure_kind,
            reason=reason, provider=provider,
        )
        body = build_webhook_payload(op, outcome, backend_id=backend_id, diagnostics=diagnostics)
        await deliver_webhook(
            http_client, settings.webhook_url, settings.webhook_secret, body,
            max_budget_seconds=settings.webhook_max_budget_seconds,
            backoff_base=settings.webhook_backoff_base, backoff_max=settings.webhook_backoff_max,
        )

    # 0. Retry blocked.
    if retry_blocked:
        outcome = CachedOutcome("error", None, "retry_blocked: manual reconcile may be required")
        log.warning("operation_retry_blocked")
        await deliver(outcome, backend_id=None, failure_kind="retry_blocked")
        return

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        detail = cached.detail or {}
        replay_snapshot = {
            "steps": [], "session_reuse": None,
            "external_user_id": detail.get("external_user_id"),
            "balance_before": detail.get("balance_before"),
            "balance_after": detail.get("balance_after"),
        }
        await deliver(cached, backend_id=None, cache_hit=True,
                      failure_kind=detail.get("failure_kind"),
                      provider=detail.get("provider"), snapshot=replay_snapshot)
        return

    # 3. Pre-flight.
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session, type=op.type, backend_name=op.backend_name, username=op.username,
                user_id=op.user_id, idempotency_key=key, account_username=op.account_username,
                diagnostics=recorder, op_id=op.op_id, attempt=attempt,
            )
    except PreflightError as exc:
        await deliver(CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"),
                      backend_id=None, failure_kind="preflight")
        return

    backend_id = ctx.credentials.game_id

    # 4. Resolve backend.
    try:
        backend = resolve(ctx.credentials.backend_driver, credentials=ctx.credentials,
                          http_client=http_client, settings=settings,
                          session_store=session_store, redis=redis, diagnostics=recorder)
    except BackendError as exc:
        await deliver(CachedOutcome("failed", None, exc.reason),
                      backend_id=backend_id, failure_kind="preflight",
                      provider=_provider_from_exc(exc))
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call", backend_id=backend_id)
    try:
        result = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await deliver(CachedOutcome("error", None, f"backend_error: {exc.reason}"),
                      backend_id=backend_id, failure_kind="transient",
                      provider=_provider_from_exc(exc))
        return
    except BackendError as exc:
        provider = _provider_from_exc(exc)
        snap = recorder.snapshot()
        detail = {"failure_kind": "backend", "provider": provider,
                  "external_user_id": snap.get("external_user_id"),
                  "balance_before": snap.get("balance_before"),
                  "balance_after": snap.get("balance_after")}
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}", detail=detail)
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await deliver(outcome, backend_id=backend_id, failure_kind="backend", provider=provider)
        return
    except ValidationError as exc:
        detail = {"failure_kind": "invalid_result", "provider": None,
                  "external_user_id": None, "balance_before": None, "balance_after": None}
        outcome = CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}",
                                detail=detail)
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.error("operation_invalid_result", reason=outcome.reason)
        await deliver(outcome, backend_id=backend_id, failure_kind="invalid_result")
        return
    except Exception:  # noqa: BLE001
        log.exception("operation_unexpected_error")
        await deliver(CachedOutcome("error", None, "backend_error: unexpected"),
                      backend_id=backend_id, failure_kind="unexpected")
        return

    snap = recorder.snapshot()
    detail = {"failure_kind": None, "provider": None,
              "external_user_id": snap.get("external_user_id"),
              "balance_before": snap.get("balance_before"),
              "balance_after": snap.get("balance_after")}
    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None, detail=detail)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded")
    await deliver(outcome, backend_id=backend_id, snapshot=snap)
    await apply_post_effects(key, op.type, outcome.result or {})
```

(The cache-replay branch already reads `external_user_id`/`balance_*` from `detail`, so a replayed
success echoes them with `steps: []` and `cache_hit: true`.)

Delete the now-unused `_deliver` helper (its call sites are replaced by the inner `deliver`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_executor.py tests/integration/test_executor_cache.py tests/integration/test_full_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/operations/executor.py tests/integration/test_executor.py tests/integration/test_executor_cache.py
git commit -m "feat(diagnostics): assemble + deliver diagnostics from the executor"
```

---

> **Tasks 11–17:** consult **Appendix A** (end of this document) for the authoritative,
> source-pinned step names → exact client call, provider-attach sites, `mark_*` response keys,
> and `session_event` points for each backend. Where a task body below differs, **Appendix A wins**.

## Task 11: mock backend — single build step

**Files:**
- Modify: `app/backends/mock/backend.py`
- Test: `tests/unit/test_mock_backend.py`

**Interfaces:**
- Produces: each mock op records exactly one `{op}.build` step (`http=False`); no session event (`session_reuse` stays `null`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_mock_backend.py
from app.backends.context import BackendContext, GameCredentials
from app.backends.diagnostics import DiagnosticsRecorder
from app.backends.mock.backend import MockBackend


def _ctx(rec):
    creds = GameCredentials(game_id=1, name="g", backend_url=None, login_page_url=None,
                            backend_username=None, backend_password=None, api_base_url=None,
                            api_agent_id=None, api_secret_key=None, binding_key=None)
    return BackendContext(credentials=creds, user_id=7, account=None,
                          account_username="u", diagnostics=rec)


async def test_mock_recharge_records_single_build_step():
    rec = DiagnosticsRecorder()
    await MockBackend().recharge(_ctx(rec), amount=5)
    snap = rec.snapshot()
    assert snap["steps"] == [{"name": "recharge.build", "phase": "finalize", "http": False,
                              "external": False, "ok": True, "ms": snap["steps"][0]["ms"]}]
    assert snap["session_reuse"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_mock_backend.py -v`
Expected: FAIL — `assert [] == [...]`

- [ ] **Step 3: Write minimal implementation**

Wrap each mock op body in a build step. Example for `recharge` (apply the same pattern to create_account/read_balance/reset_password/redeem/agent_balance with the matching step name):

```python
    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        async with ctx.diag.step("recharge.build", phase="finalize", http=False):
            self._maybe_fail()
            return RechargeResult(balance=float(amount))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_mock_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/backends/mock/backend.py tests/unit/test_mock_backend.py
git commit -m "feat(diagnostics): record mock build steps"
```

---

## Task 12: gamevault — steps + provider fields

**Files:**
- Modify: `app/backends/gamevault/errors.py` (return provider fields), `app/backends/gamevault/client.py` (attach to exceptions + wrap round trips), `app/backends/gamevault/backend.py` (named steps + `external_user_id` mark)
- Test: `tests/unit/test_gamevault_errors.py`, `tests/unit/test_gamevault_client.py`, `tests/unit/test_gamevault_backend.py`

**Interfaces:**
- Consumes: enriched exceptions, recorder-on-client (`self._diag`), `ctx.diag`.
- Produces: `map_code(code, msg) -> tuple[str, int | None, str]` returning `(slug, code, raw_msg)`; client raises `BackendError(slug, provider_http_status=..., provider_code=code, provider_message=msg)`; backend records `resolve.user_id` + `primary` steps and marks `external_user_id`.

- [ ] **Step 1: Write the failing test (errors)**

```python
# append to tests/unit/test_gamevault_errors.py
from app.backends.gamevault.errors import map_code


def test_map_code_returns_slug_code_and_message():
    slug, code, msg = map_code(7, "user balance not enough")
    assert slug == "gamevault:7:insufficient_user_balance"
    assert code == 7
    assert msg == "user balance not enough"


def test_map_code_unknown_keeps_raw_message_untruncated():
    long = "x" * 200
    slug, code, msg = map_code(999, long)
    assert code == 999
    assert msg == long            # untruncated in the structured field
    assert slug.startswith("gamevault:999:")
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_gamevault_errors.py -v`
Expected: FAIL — `ValueError: not enough values to unpack`

- [ ] **Step 3: Implement `map_code`**

```python
def map_code(code: int, msg: str) -> tuple[str, int, str]:
    slug = GAMEVAULT_STATUS.get(code)
    raw = msg or ""
    if slug is not None:
        return (f"gamevault:{code}:{slug}", code, raw)
    return (f"gamevault:{code}:{raw[:80] or 'error'}", code, raw)
```

- [ ] **Step 4: Update the client to attach provider fields + wrap round trips**

In `app/backends/gamevault/client.py`, change `call` to accept a step name and attach provider detail:

```python
    async def call(self, path: str, fields: dict[str, str], *,
                   step: str = "primary", phase: str = "primary") -> dict:
        form = {**self._auth_fields(), **{k: str(v) for k, v in fields.items()}}
        multipart = {k: (None, v) for k, v in form.items()}
        url = f"{self._base_url}{path}"
        async with self._diag.step(step, phase=phase):
            try:
                resp = await self._http.post(url, files=multipart)
            except httpx.HTTPError as exc:
                raise TransientBackendError(f"gamevault_transport:{type(exc).__name__}") from exc
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                raise TransientBackendError(f"gamevault_http_{resp.status_code}",
                                           provider_http_status=resp.status_code)
            if resp.status_code >= 300:
                raise BackendError(f"gamevault_http_{resp.status_code}",
                                   provider_http_status=resp.status_code)
            try:
                body = resp.json()
            except ValueError as exc:
                raise TransientBackendError("gamevault_bad_response",
                                           provider_http_status=resp.status_code) from exc
            code = body.get("code")
            if code == 0:
                data = body.get("data")
                return data if isinstance(data, dict) else {}
            slug, pcode, pmsg = map_code(code, body.get("msg", ""))
            if code in TRANSIENT_CODES:
                raise TransientBackendError(slug, provider_http_status=resp.status_code,
                                            provider_code=pcode, provider_message=pmsg)
            raise BackendError(slug, provider_http_status=resp.status_code,
                               provider_code=pcode, provider_message=pmsg)
```

- [ ] **Step 5: Update the backend to name steps + mark external_user_id**

In `app/backends/gamevault/backend.py`, pass step names and mark ids. Example (`recharge` and `_user_id`):

```python
    async def _user_id(self, ctx: BackendContext) -> str:
        if ctx.account and ctx.account.external_user_id:
            ctx.diag.mark_external_user_id(ctx.account.external_user_id)
            return ctx.account.external_user_id
        if ctx.account and ctx.account.username:
            data = await self._client.call(
                "/api/external/getUserID", {"account_name": ctx.account.username},
                step="resolve.user_id", phase="resolve")
            user_id = data.get("user_id")
            if not user_id:
                raise BackendError("user_id_unresolved")
            ctx.diag.mark_external_user_id(str(user_id))
            return str(user_id)
        raise BackendError("user_id_unresolved")

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/recharge",
            {"user_id": uid, "amount": _to_dollars_str(amount), "order_id": ctx.idempotency_key},
            step="recharge.post", phase="primary")
        balance = _balance_opt(data.get("user_balance"))
        if balance is not None:
            ctx.diag.mark_balance_after(balance)
        return RechargeResult(balance=balance)
```

Apply the analogous `step=`/`phase=` and marks to `create_account` (`step="addUser.post"`, `mark_external_user_id(str(data["user_id"]))`), `read_balance` (`step="balance.read"` — **no balance mark**; the balance already flows via `user_data.balance`), `reset_password` (`step="reset.post"`), and `redeem` (`step="withdraw.post"`, `mark_balance_after(data.get("user_balance"))` when not None).

- [ ] **Step 6: Client + backend tests**

```python
# append to tests/unit/test_gamevault_client.py — assert provider fields on the raised error.
async def test_business_error_carries_provider_fields(gamevault_client_env):
    # env returns HTTP 200 with {"code": 7, "msg": "user balance not enough"}
    with pytest.raises(BackendError) as ei:
        await gamevault_client_env.client.call("/api/external/recharge", {"user_id": "1"},
                                               step="recharge.post", phase="primary")
    err = ei.value
    assert err.provider_code == 7
    assert err.provider_message == "user balance not enough"
    assert err.provider_http_status == 200
```

```python
# append to tests/unit/test_gamevault_backend.py — assert steps recorded on a happy path.
async def test_recharge_records_resolve_and_primary_steps(gamevault_backend_env):
    rec = DiagnosticsRecorder()
    ctx = gamevault_backend_env.ctx(rec, external_user_id=None, username="u")
    await gamevault_backend_env.backend.recharge(ctx, amount=5)
    names = [s["name"] for s in rec.snapshot()["steps"]]
    assert names == ["resolve.user_id", "recharge.post"]
    assert rec.snapshot()["external_user_id"] is not None
```

(Adapt to the file's existing fake-HTTP harness. If existing client tests call `call(path, fields)` positionally, they still work — `step`/`phase` are keyword-defaulted.)

- [ ] **Step 7: Run + commit**

Run: `pytest tests/unit/test_gamevault_errors.py tests/unit/test_gamevault_client.py tests/unit/test_gamevault_backend.py -v`
Expected: PASS

```bash
git add app/backends/gamevault/ tests/unit/test_gamevault_*.py
git commit -m "feat(diagnostics): gamevault steps + provider error fields"
```

---

## Task 13: gameroom — steps + snapshot balance_before + provider + session events

**Files:**
- Modify: `app/backends/gameroom/errors.py` (return provider fields), `app/backends/gameroom/client.py` (session events + step wraps + provider on raises), `app/backends/gameroom/backend.py` (named steps, `recharge.snapshot`/`redeem.snapshot` → `mark_balance_before`, `mark_external_user_id`)
- Test: `tests/unit/test_gameroom_errors.py`, `tests/unit/test_gameroom_client.py`, `tests/unit/test_gameroom_backend.py`

**Interfaces:**
- Produces: `map_response(status_code, message) -> tuple[str, bool, int, str]` = `(slug, terminal, envelope_status, raw_message)`; client emits `session_event("hit"|"fresh"|"relogin")` from `get_token`, records `login.submit`/`recovery.relogin`/primary steps, and raises `BackendError(slug, provider_http_status=<transport>, provider_code=envelope_status, provider_message=raw)`; backend records `recharge.snapshot` and marks `balance_before`.

- [ ] **Step 1: Write the failing test (errors)**

```python
# append to tests/unit/test_gameroom_errors.py
from app.backends.gameroom.errors import map_response


def test_map_response_returns_envelope_status_and_message():
    slug, terminal, status, msg = map_response(400, "withdrawal amount is greater than balance")
    assert slug == "gameroom:insufficient_user_balance"
    assert terminal is True
    assert status == 400
    assert msg == "withdrawal amount is greater than balance"
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_gameroom_errors.py -v`
Expected: FAIL — unpack error.

- [ ] **Step 3: Implement `map_response`**

```python
def map_response(status_code: int, message: str) -> tuple[str, bool, int, str]:
    msg = message or ""
    if status_code == 500:
        return ("gameroom:server_error", False, status_code, msg)
    if status_code == 430:
        return ("gameroom:auth_failed", True, status_code, msg)
    if status_code == 401:
        return ("gameroom:auth_missing", False, status_code, msg)
    if status_code == 400:
        low = msg.lower()
        for needle, slug in _MESSAGE_PATTERNS:
            if needle in low:
                return (f"gameroom:{slug}", True, status_code, msg)
        return (f"gameroom:business_error: {msg[:80]}", True, status_code, msg)
    return (f"gameroom:status_{status_code}: {msg[:60]}", True, status_code, msg)
```

- [ ] **Step 4: Update the client** (session events, step wraps, provider on raises)

In `get_token`, emit session events:

```python
    async def get_token(self, *, invalidate: str | None = None) -> str:
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate and not _expired(cached):
            self._diag.session_event("relogin" if invalidate else "hit")
            return cached.token
        async with self._session.login_lock(self._game_id, ttl_seconds=10, acquire_timeout=10.0):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate and not _expired(cached):
                self._diag.session_event("relogin" if invalidate else "hit")
                return cached.token
            async with self._diag.step("login.submit", phase="auth"):
                token, expires_at = await self._do_login()
            self._diag.session_event("relogin" if invalidate else "fresh")
            ttl = max(60, expires_at - int(time.time()) - 60)
            await self._session.set(self._game_id, CachedSession(token=token, expires_at=expires_at),
                                    ttl_seconds=ttl)
            return token
```

In `call`, wrap the primary request and the 410 recovery, and pass a step name:

```python
    async def call(self, method: str, path: str, *, fields=None, params=None,
                   step: str = "primary", phase: str = "primary") -> dict:
        token = await self.get_token()
        async with self._diag.step(step, phase=phase):
            resp = await self._http_request(method, path, token, fields=fields, params=params)
        if self._is_410(resp):
            async with self._diag.step("recovery.relogin", phase="recovery"):
                fresh = await self.get_token(invalidate=token)
                resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if self._is_410(resp):
                raise BackendError("gameroom:auth_failed")
        return self._classify(resp)
```

Update `_classify` and `_parse_or_raise` raise sites to attach provider fields:

```python
    def _parse_or_raise(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:http_{resp.status_code}",
                                        provider_http_status=resp.status_code)
        if resp.status_code >= 300:
            raise BackendError(f"gameroom:http_{resp.status_code}",
                               provider_http_status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:bad_response",
                                        provider_http_status=resp.status_code) from exc

    def _classify(self, resp: httpx.Response) -> dict:
        body = self._parse_or_raise(resp)
        sc = body.get("status_code")
        if sc == 200:
            data = body.get("data")
            if isinstance(data, dict):
                return data
            return {k: v for k, v in body.items() if k not in {"status_code", "message", "code", "data"}}
        slug, terminal, status, msg = map_response(int(sc) if isinstance(sc, int) else 0,
                                                   str(body.get("message", "")))
        exc_kwargs = {"provider_http_status": resp.status_code, "provider_code": status,
                      "provider_message": msg}
        if not terminal:
            raise TransientBackendError(slug, **exc_kwargs)
        raise BackendError(slug, **exc_kwargs)
```

Apply the same 4-tuple unpack + `exc_kwargs` to `call_raw` and `_do_login`'s `map_response` call site.

- [ ] **Step 5: Update the backend** (named steps, snapshot → `mark_balance_before`, `mark_external_user_id`)

In `app/backends/gameroom/backend.py`, give each `self._client.call(...)` a `step=`/`phase=` name matching Task-13 catalog (`resolve.user_id`, `recharge.snapshot`, `recharge.post`, `redeem.snapshot`, `redeem.post`, `create.post`, `reset.post`, `balance.read`). At the snapshot read, `ctx.diag.mark_balance_before(<player balance from agentMoney>)`; at create, `ctx.diag.mark_external_user_id(<data.id>)`; wherever a post-balance is available, `ctx.diag.mark_balance_after(...)`. (Consult the existing method bodies for the exact response keys; do not add new provider calls — only name the calls already made and mark values already read.)

- [ ] **Step 6: Tests**

```python
# tests/unit/test_gameroom_client.py — session events + provider fields.
async def test_get_token_cache_hit_emits_session_hit(gameroom_client_cached):
    rec = DiagnosticsRecorder()
    client = gameroom_client_cached(diagnostics=rec)   # session store pre-seeded, unexpired
    await client.get_token()
    assert rec.snapshot()["session_reuse"] == "hit"

async def test_business_error_carries_envelope_code(gameroom_client_env):
    with pytest.raises(BackendError) as ei:
        await gameroom_client_env.call("POST", "/api/agent/recharge",
                                       fields={}, step="recharge.post", phase="primary")
    assert ei.value.provider_code == 400
    assert ei.value.provider_message  # untruncated raw text
```

```python
# tests/unit/test_gameroom_backend.py — snapshot marks balance_before.
async def test_recharge_marks_balance_before_from_snapshot(gameroom_backend_env):
    rec = DiagnosticsRecorder()
    await gameroom_backend_env.backend.recharge(gameroom_backend_env.ctx(rec), amount=5)
    assert rec.snapshot()["balance_before"] is not None
    assert "recharge.snapshot" in [s["name"] for s in rec.snapshot()["steps"]]
```

- [ ] **Step 7: Run + commit**

Run: `pytest tests/unit/test_gameroom_errors.py tests/unit/test_gameroom_client.py tests/unit/test_gameroom_backend.py -v`
Expected: PASS

```bash
git add app/backends/gameroom/ tests/unit/test_gameroom_*.py
git commit -m "feat(diagnostics): gameroom steps, session events, balance_before, provider fields"
```

---

## Task 14: goldentreasure — steps + throttle + provider + origin-code preservation

**Files:**
- Modify: `app/backends/goldentreasure/errors.py`, `client.py`, `backend.py`
- Test: `tests/unit/test_goldentreasure_errors.py`, `test_goldentreasure_client.py`, `test_goldentreasure_backend.py`

**Interfaces:**
- Produces: `map_response(code, message) -> tuple[str, bool, int, str]` = `(slug, terminal, code, raw_message)`; client attaches provider fields and, on the auth-dead retry path, sets `provider_code` to the **origin** code (`−3/−17/52`) rather than only reporting `auth_failed`; records `throttle.acquire`, `login.submit`, `primary`, `recovery.relogin` steps + session events; backend marks `balance_after` on read and `external_user_id` where known (always `null` here).

- [ ] **Step 1: Write the failing test (errors)**

```python
# append to tests/unit/test_goldentreasure_errors.py
from app.backends.goldentreasure.errors import map_response


def test_map_response_returns_code_and_message():
    slug, terminal, code, msg = map_response(21, "server maintenance")
    assert slug == "gtreasure:operation_refused"
    assert terminal is True
    assert code == 21
    assert msg == "server maintenance"


def test_transient_167_returns_code():
    slug, terminal, code, msg = map_response(167, "too fast")
    assert terminal is False and code == 167
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_goldentreasure_errors.py -v`
Expected: FAIL — unpack error.

- [ ] **Step 3: Implement `map_response`**

```python
def map_response(code: int, message: str) -> tuple[str, bool, int, str]:
    raw = message or ""
    if code in TRANSIENT_CODES:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", False, code, raw)
    if code in GTREASURE_STATUS:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", True, code, raw)
    return (f"gtreasure:code_{code}: {raw[:80]}", True, code, raw)
```

- [ ] **Step 4: Update client + backend**

In `app/backends/goldentreasure/client.py`: at every `map_response(...)` raise site, unpack the 4-tuple and pass `provider_code=code, provider_message=msg` (and `provider_http_status=200` for body-carried business errors, or the real status for transport errors). Wrap `throttle.acquire` (the `SET NX` gate), `login.submit` (inside the login lock), the primary op, and `recovery.relogin` in `self._diag.step(...)`. Emit `session_event` from `get_session` (`hit`/`fresh`/`relogin`). On the auth-dead retry path where the reason collapses to `auth_failed`, pass `provider_code=<origin code>` and `provider_message=<origin message>` from the first failing response so the origin is preserved.

In `app/backends/goldentreasure/backend.py`: pass `step=` to each `call(...)` — `create.post` (savePlayer), `balance.read` (getPlayerScore), `reset.post` (updatePlayer), `recharge.post`/`redeem.post` (enterScore). **No `mark_*` calls** — savePlayer returns no uid (`external_user_id` stays null), enterScore returns no balance, and the read balance already flows via `user_data`. See Appendix A for the throttle/login/recovery wrap points and the origin-code preservation on `gtreasure:auth_failed`.

- [ ] **Step 5: Tests** (client attaches provider code on business error; throttle+login steps recorded; read marks balance_after) — mirror the Task-13 test shapes against the file's existing harness.

- [ ] **Step 6: Run + commit**

Run: `pytest tests/unit/test_goldentreasure_errors.py tests/unit/test_goldentreasure_client.py tests/unit/test_goldentreasure_backend.py -v`
Expected: PASS

```bash
git add app/backends/goldentreasure/ tests/unit/test_goldentreasure_*.py
git commit -m "feat(diagnostics): goldentreasure steps, throttle timing, provider + origin code"
```

---

## Task 15: ASP.NET cashier (orionstars + milkyway) — 6-step scrape + provider + captcha flag

**Files:**
- Modify: `app/backends/_aspnet_cashier/errors.py`, `client.py`, `app/backends/orionstars/backend.py`, `app/backends/milkyway/backend.py`
- Test: `tests/unit/test_aspnet_errors.py`, `test_aspnet_client.py`, `test_orionstars_backend.py`, `test_milkyway_backend.py`

**Interfaces:**
- Produces: `business_failure_to_error(message)` returns a `BackendError` carrying `provider_message=<untruncated sentinel>` and `provider_code=None` (OP business failures have no provider code); login `errtype` mapping supplies `provider_code=<errtype>` on login failures. The client records `login.page`/`login.captcha_solve`(`external=True`)/`login.submit`/`login.confirm`, `resolve.accounts_list_get`/`resolve.search_post`, `dialog.tourl_post`/`dialog.get`/`dialog.post`, and `recovery` steps, and emits session events from `get_or_login`.

- [ ] **Step 1: Write the failing test (errors)**

Construct a client with the file's existing fixture (or inline: `AspnetCashierClient(base_url="http://x", username="u", password="p", http_client=<stub>, session_store=<stub>, captcha_solver=<stub>, game_id=1, session_ttl_seconds=1, lock_ttl_seconds=1, lock_acquire_timeout_seconds=1.0, captcha_login_max_attempts=1, driver_prefix="orionstars")`) and assert the mapped error carries the untruncated sentinel:

```python
# append to tests/unit/test_aspnet_errors.py
def test_business_failure_to_error_sets_provider_message(aspnet_client):
    msg = "surplus money is insufficient for this operation"
    err = aspnet_client.business_failure_to_error(msg)
    assert err.provider_message == msg          # untruncated
    assert err.provider_code is None
    assert err.reason == "orionstars:insufficient_agent_funds"  # driver_prefix from fixture
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_aspnet_errors.py -v`
Expected: FAIL — `assert None == "surplus money is insufficient for this operation"` (the method doesn't set `provider_message` yet).

- [ ] **Step 3: Implement**

In `app/backends/_aspnet_cashier/client.py`, `business_failure_to_error`:

```python
    def business_failure_to_error(self, message: str) -> BackendError:
        slug = classify_business_failure_message(message)
        return BackendError(f"{self._driver}:{slug}", provider_message=message)
```

And in `classify`, when raising on an unknown sentinel, keep the untruncated text as `provider_message`:

```python
    def classify(self, html: str) -> tuple[str, list[str]]:
        kind, args = parse_sentinel(html)
        if kind == "unknown":
            raise BackendError(f"{self._driver}:unknown_sentinel:{html[:80]!r}",
                               provider_message=html)
        return kind, args
```

Wrap each `request_text(...)` call site in the op-facing helpers (`fetch_accounts_list_html`, `search_account`, `get_dialog_url`, `submit_dialog`, `milkyway_read_balance`, `post_getscoreuserid`) with a named `self._diag.step(...)` per the catalog. In `request_text` itself, wrap the retry-after-relogin path as a `recovery` step. In `get_or_login`, emit `session_event("hit")` on the cache-hit return and `session_event("fresh")` after `_do_login`; wrap the login sub-requests as `login.page`/`login.submit` and the captcha solve as `self._diag.step("login.captcha_solve", phase="auth", external=True)` inside `app/backends/_aspnet_cashier/login.py`.

In `orionstars/backend.py` and `milkyway/backend.py`, mark `external_user_id` on create (the resolved `uid:gid`) and `balance_after` on read (OrionStars `credit` / MilkyWay row balance).

- [ ] **Step 4: Tests** — assert the six step names appear on a recharge (with a forced login), `external=True` on the captcha step, and `provider_message` on a business failure. Mirror the file's existing HTML-fixture harness.

- [ ] **Step 5: Run + commit**

Run: `pytest tests/unit/test_aspnet_errors.py tests/unit/test_aspnet_client.py tests/unit/test_orionstars_backend.py tests/unit/test_milkyway_backend.py -v`
Expected: PASS

```bash
git add app/backends/_aspnet_cashier/ app/backends/orionstars/ app/backends/milkyway/ tests/unit/test_aspnet_*.py tests/unit/test_orionstars_backend.py tests/unit/test_milkyway_backend.py
git commit -m "feat(diagnostics): aspnet cashier scrape steps + captcha flag + provider message"
```

---

## Task 16: ultrapanda (vpower) — steps + throttle + provider code (no message)

**Files:**
- Modify: `app/backends/ultrapanda/errors.py`, `client.py`, `backend.py`
- Test: `tests/unit/test_ultrapanda_errors.py`, `test_ultrapanda_client.py`, `test_ultrapanda_backend.py`

**Interfaces:**
- Produces: the client attaches `provider_code=body['code']` on business failures (no `provider_message` — none exists), records `throttle.acquire`/`login.submit`/primary/`recovery.relogin` steps, and emits session events from `get_session`; backend marks `balance_after` on read. `map_code` is unchanged (already returns `(slug, terminal)`); the code value is read from `body['code']` at the raise site.

- [ ] **Step 1: Write the failing test (client)**

```python
# append to tests/unit/test_ultrapanda_client.py
async def test_business_error_carries_provider_code(ultrapanda_client_env):
    # env returns a 200 body {"code": 21, ...} on a recharge
    with pytest.raises(BackendError) as ei:
        await ultrapanda_client_env.recharge_call()   # harness helper that hits the mapped path
    assert ei.value.provider_code == 21
    assert ei.value.provider_message is None          # no message field exists
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_ultrapanda_client.py -v`
Expected: FAIL — `provider_code is None`.

- [ ] **Step 3: Implement**

Provider fields attach in **two** places (business codes are raised by the *backend*, not the client):

1. `app/backends/ultrapanda/backend.py` `_raise_for_code(body, *, op, driver)` — the business path (HTTP was <400, so 200):

```python
def _raise_for_code(body: dict, *, op: str, driver: str) -> None:
    code = body.get("code")
    if code == 20000:
        return
    mapped = map_code(int(code) if isinstance(code, int) else 0, op=op)
    if mapped is None:
        raise TransientBackendError(f"{driver}:malformed_response")
    slug, terminal = mapped
    kwargs = {"provider_http_status": 200, "provider_code": code}
    if terminal:
        raise BackendError(f"{driver}:{slug}", **kwargs)
    raise TransientBackendError(f"{driver}:{slug}", **kwargs)
```

2. `app/backends/ultrapanda/client.py` `_do_login` — the login-time `map_code` raise: pass `provider_code=code`.

There is **no** `provider_message` (the vpower API returns none). Wrap `throttle.acquire` (in `call_throttled`), `login.submit` (in `_do_login`), the `<op>.post` (in `call`, via a `step=` arg), and `recovery.relogin` (the `1086` retry in `call`) in `self._diag.step(...)`; emit `session_event` from `get_or_login` and set `relogin` in the `1086` recovery branch. **No `mark_*`** — read balance flows via `user_data`; money ops return no balance; no provider uid exists.

- [ ] **Step 4: Tests** — provider_code present, message absent; throttle+login steps recorded; read marks balance_after.

- [ ] **Step 5: Run + commit**

Run: `pytest tests/unit/test_ultrapanda_errors.py tests/unit/test_ultrapanda_client.py tests/unit/test_ultrapanda_backend.py -v`
Expected: PASS

```bash
git add app/backends/ultrapanda/ tests/unit/test_ultrapanda_*.py
git commit -m "feat(diagnostics): ultrapanda steps, throttle timing, provider code"
```

---

## Task 17: yolo — 3-step login + provider (http + message) + retained balance_after

**Files:**
- Modify: `app/backends/yolo/errors.py`, `client.py`, `backend.py`
- Test: `tests/unit/test_yolo_errors.py`, `test_yolo_client.py`, `test_yolo_backend.py`

**Interfaces:**
- Produces: `map_envelope(http_status, body)` raises errors carrying `provider_http_status=http_status` and untruncated `provider_message` (no `provider_code` — YOLO has no provider code; our slug lives in `reason`). Client records `login.page`/`login.submit`/`login.confirm`, `resolve.search`, primary, `recovery` steps + session events. Backend **retains the money-op success `data`** (no password field) to `mark_balance_after` when present; marks `external_user_id` from the resolved id.

- [ ] **Step 1: Write the failing test (errors)**

```python
# append to tests/unit/test_yolo_errors.py
import pytest

from app.backends.base import BackendError
from app.backends.yolo.errors import map_envelope


def test_business_error_carries_http_status_and_untruncated_message():
    long = "score is insufficient " + "x" * 200
    with pytest.raises(BackendError) as ei:
        map_envelope(200, {"status": False, "data": {"message": long}})
    err = ei.value
    assert err.provider_http_status == 200
    assert err.provider_message == long        # untruncated
    assert err.provider_code is None
    assert err.reason == "yolo:insufficient_balance"
```

- [ ] **Step 2: Run + fail**

Run: `pytest tests/unit/test_yolo_errors.py -v`
Expected: FAIL — provider fields are None.

- [ ] **Step 3: Implement `map_envelope`**

Attach provider fields at each raise (validation, business, 5xx):

```python
def map_envelope(http_status: int, body: dict | None) -> dict:
    if http_status >= 500:
        raise TransientBackendError(f"yolo:http_{http_status}", provider_http_status=http_status)
    if body is None:
        raise TransientBackendError("yolo:bad_response", provider_http_status=http_status)

    if http_status == 422 or "errors" in body:
        errors = body.get("errors") or {}
        field, msgs = next(iter(errors.items()), ("", [""]))
        msg = msgs[0] if isinstance(msgs, list) and msgs else ""
        slug = _slug(msg)
        if slug:
            raise BackendError(f"yolo:{slug}", provider_http_status=http_status, provider_message=msg)
        raise BackendError(f"yolo:validation_error: {field}: {msg[:60]}",
                           provider_http_status=http_status, provider_message=msg)

    if body.get("status") is True:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    data = body.get("data")
    msg = data.get("message", "") if isinstance(data, dict) else ""
    slug = _slug(msg)
    if slug:
        raise BackendError(f"yolo:{slug}", provider_http_status=http_status, provider_message=msg)
    raise BackendError(f"yolo:business_error: {msg[:80]}",
                       provider_http_status=http_status, provider_message=msg)
```

- [ ] **Step 4: Update client + backend**

In `app/backends/yolo/client.py`: wrap the three `_do_login` round trips as `login.page`/`login.submit`/`login.confirm`, wrap the primary/search request in `post_form`/`get_text` (backend passes `step=`), and the auth-failure retry as `recovery` (set `session_event("relogin")` there); emit `session_event` from `get_session`. In `backend.py`: `mark_external_user_id(uid)` from `_player_id`/`_search`/create's resolved `uid` (grid column 0). **No `balance_after` mark** — see the flag in Appendix A: yolo's `post_form` success envelope is a Dcat `{status,message}` dict with no balance field, so there is nothing to retain (read balance still flows via `user_data`). Never retain any password field from a `data` dict.

- [ ] **Step 5: Tests** — provider http+message present, code None; login steps recorded; recharge marks balance_after when the success data carries a balance.

- [ ] **Step 6: Run + commit**

Run: `pytest tests/unit/test_yolo_errors.py tests/unit/test_yolo_client.py tests/unit/test_yolo_backend.py -v`
Expected: PASS

```bash
git add app/backends/yolo/ tests/unit/test_yolo_*.py
git commit -m "feat(diagnostics): yolo login steps, provider http+message, retained balance_after"
```

---

## Task 18: Full-suite gate + docs

**Files:**
- Modify: `docs/architecture.md` (add a "Webhook diagnostics" section), `docs/runbook.md` (operator field reference)
- Create: `docs/diagnostics-contract-arcadia-diff.md` (handover note for the maintainer's Arcadia edit)

**Interfaces:** none — verification + documentation.

- [ ] **Step 1: Run the entire suite + lint + types**

Run: `make test && make lint && make type`
Expected: all green. Fix any backend/integration test that constructed a client/context positionally and now needs the keyword defaults (they should already pass — every new param is keyword-defaulted).

- [ ] **Step 2: Add the diagnostics section to `docs/architecture.md`**

Document: the `diagnostics` object shape, the `failure_kind` taxonomy, `session_reuse` vs `cache_hit`, and the "omit-don't-invent" rule. Point at the spec (`docs/superpowers/specs/2026-07-14-webhook-diagnostics-design.md`).

- [ ] **Step 3: Add the operator field reference to `docs/runbook.md`**

A short table: each `diagnostics` field → what it means → what to check when it's set (e.g. `failure_kind=transient` → provider 5xx/timeout, Laravel already refunded; `session_reuse=relogin` → session churn/contention).

- [ ] **Step 4: Write the Arcadia contract diff handover**

Create `docs/diagnostics-contract-arcadia-diff.md` with the exact edits for
`/Applications/development/laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md` (from spec §8):
§3.2 add optional `op_id` to all six endpoints; §4.2 add top-level `op_id` + the optional
`diagnostics` object (full shape, every field optional, present on success and failure, purely
observational); §4.4 note `op_id` is the create correlation id; §4.5 note diagnostics is
redaction-safe.

- [ ] **Step 5: Commit**

```bash
git add docs/architecture.md docs/runbook.md docs/diagnostics-contract-arcadia-diff.md
git commit -m "docs(diagnostics): operator reference + Arcadia contract diff handover"
```

---

## Self-review notes (for the executor)

- **Spec coverage:** provider status/code/message (Tasks 12–17 + assembly Task 7); steps/timing (Tasks 2, 10–17); session_reuse (Tasks 2, 13–17); failure_kind (Task 10); op_id (Tasks 3, 7, 10); cache-replay honesty (Tasks 5, 10); balances (Tasks 13, 14, 16, 17); external_user_id (Tasks 12, 13, 15, 17); message-unchanged (Task 7 assertions); docs + Arcadia diff (Task 18).
- **Deploy-order independence:** every schema/context/client/executor param is keyword-defaulted; `build_webhook_payload(diagnostics=None)` reproduces the legacy body; provider fields absent until a backend task lands → provider block simply omitted.
- **Type consistency:** `map_code`/`map_response` return-tuple arities are defined per backend in their task's Interfaces block (gamevault 3-tuple; gameroom/goldentreasure 4-tuple; ultrapanda unchanged 2-tuple; yolo raises directly). `snapshot()` keys are fixed in Task 2 and consumed unchanged in Tasks 7 and 10.
- **Money safety:** no new provider calls anywhere; only `succeeded`/`failed` cache writes (with `detail`); transient `error` still never cached.

---

## Appendix A — Pinned instrumentation reference (authoritative)

Source-verified against the backend/client method bodies. Legend: **(S)** step name → the exact
method/HTTP call it wraps; **(P)** where `provider_*` are attached; **(M)** `mark_*` with exact
response keys; **(SE)** `session_event` points. Where a task body differs, **this appendix wins**.

Global: `read` ops never mark `balance_after` (the balance already flows via `user_data.balance`);
`primary` HTTP steps carry `http=True`; `throttle.acquire` and `login.captcha_solve` carry
`http=False`.

### mock (Task 11)
- **(S)** `{op}.build` (`phase=finalize`, `http=False`) wraps the whole method body.
- **(P)** none (synthetic reason). **(M)** none. **(SE)** none — `session_reuse` stays `null`.

### gamevault (Task 12) — `app/backends/gamevault/`
- **(S)** `resolve.user_id` → `call("/api/external/getUserID", …)` in `_user_id` (skipped when `external_user_id` cached); `addUser.post` / `balance.read` / `reset.post` / `recharge.post` / `withdraw.post` → the respective `call(...)`.
- **(P)** in client `call`: `provider_http_status=resp.status_code`; on `code!=0`, `provider_code`+`provider_message` from `map_code` (3-tuple `(slug, code, msg)`).
- **(M)** `mark_external_user_id`: create → `str(data["user_id"])`; `_user_id` → cached `ctx.account.external_user_id` or resolved `str(data["user_id"])`. `mark_balance_after`: recharge/redeem → `data.get("user_balance")` when not None.
- **(SE)** none — stateless MD5; `session_reuse` stays `null`.

### gameroom (Task 13) — `app/backends/gameroom/`
- **(S)** `resolve.user_id` → `call_raw("GET","/api/player/userList",…)` in `_player_id`; `recharge.snapshot`/`redeem.snapshot` (`phase=snapshot`) → `call("GET","/api/player/agentMoney",…)` in `_agent_money` (add a `step` arg); `recharge.post`→`agentRecharge`, `redeem.post`→`agentWithdraw`, `create.post`→`playerInsert`, `reset.post`→`/api/player/reset`, `balance.read`→`call("GET","/api/player/agentMoney",…)` in `read_balance`.
- **(P)** in `_classify` / `_parse_or_raise` / `call_raw`: `provider_http_status=resp.status_code`; on envelope error `provider_code=<envelope status_code>`, `provider_message=<raw message>` (via `map_response` 4-tuple `(slug, terminal, status, msg)`).
- **(M)** `mark_balance_before`: recharge **and** redeem → `float(snapshot["balance"])` (player balance from `_agent_money`, whose dict is `{"username","balance","cusBlance"}`). `mark_balance_after`: recharge → `data.get("total_balance")`. `mark_external_user_id`: create → `str(data["id"])`; `_player_id` → cached value or `str(row["id"] or row["Id"])`.
- **(SE)** `get_token`: cache hit → `hit`; after `_do_login` → `fresh`; the 410-recovery `get_token(invalidate=…)` → `relogin`.

### goldentreasure (Task 14) — `app/backends/goldentreasure/`
- **(S)** `throttle.acquire` (`phase=preflight`, `http=False`) → `_acquire_throttle()` in `call` when `throttle=True`; `login.submit` → `_do_login` (wrap in `get_token`); `create.post`(savePlayer) / `balance.read`(getPlayerScore) / `reset.post`(updatePlayer) / `recharge.post`+`redeem.post`(enterScore) → the authed `_post_raw` in `call` (add a `step` arg); `recovery.relogin` → the `{-3,-17,52}` retry block in `call`.
- **(P)** business error (`map_response` 4-tuple, HTTP 200) → `provider_http_status=200`, `provider_code=code`, `provider_message=msg` in `call` and `_do_login`; transport `gtreasure:http_{status}` in `_post_raw` → `provider_http_status=resp.status_code`. On the `gtreasure:auth_failed` raise, pass `provider_code`/`provider_message` from the **first (origin)** response (capture its `code`/`message` before the retry).
- **(M)** none. **(SE)** `get_token`: hit / fresh / relogin (as gameroom).

### aspnet — orionstars + milkyway (Task 15) — `app/backends/_aspnet_cashier/`, `orionstars/`, `milkyway/`
- **(S)** named **inside the client helpers**: `resolve.accounts_list_get`→`fetch_accounts_list_html`; `resolve.search_post`→the POST in `search_account`/`milkyway_read_balance`; `dialog.tourl_post`→`get_dialog_url`; `dialog.get`+`dialog.post`→`submit_dialog`; `balance.getscore_post`→`post_getscoreuserid`. `create.get`+`create.post` are named by the backend via `request_text(step=…)`. `recovery` wraps the dead-session retry inside `request_text`. Login sub-steps in `login.py`: `login.page`→r1 GET, `login.captcha_img`→r2 GET, `login.captcha_solve`→`solve_numeric_image` (`http=False`, **`external=True`**), `login.submit`→r3 POST.
- **(P)** `business_failure_to_error(message)` → `provider_message=message` (untruncated), `provider_code=None`; `classify` unknown-sentinel → `provider_message=html`; login terminal in `login.py` → `provider_code=errtype`.
- **(M)** `mark_external_user_id`: create → `f"{uid}:{gid}"` from the follow-up `search_account`; `_player_ids` → cached split or `f"{pairs[0][0]}:{pairs[0][1]}"`. No balance marks (money-op success is a bare sentinel — OrionStars `recharge` comments "player balance not in this response").
- **(SE)** `get_or_login`: hit / fresh; dead-session retry in `request_text` → `relogin`. Add a `diag=NULL_RECORDER` param to `login()` and pass `self._diag` from `_do_login`.

### ultrapanda / vblink (Task 16) — `app/backends/ultrapanda/`
- **(S)** `throttle.acquire` (`http=False`) → `_acquire_throttle()` in `call_throttled`; `login.submit` → the `/user/login` POST in `_do_login`; `<op>.post` → `_do_call` in `call` (add a `step` arg); `recovery.relogin` → the `1086` retry in `call`.
- **(P)** in **backend `_raise_for_code`** → `provider_http_status=200`, `provider_code=code`; in client `_do_login` map_code raise → `provider_code=code`. No `provider_message` (none exists).
- **(M)** none. **(SE)** `get_or_login`: hit / fresh; set `relogin` explicitly in the `1086` recovery branch of `call`.

### yolo (Task 17) — `app/backends/yolo/`
- **(S)** login sub-steps in `_do_login`: `login.page`→r1 `_get`, `login.submit`→r2 `_post`, `login.confirm`→r3 `_get(/admin/player_list)`; `resolve.search`→`get_text(_PLAYER_LIST,…)` in `_search`; `<op>.post`→`post_form` (backend passes `step`); `balance.read`→the read-path `get_text` search; `recovery` wraps the auth-fail retry in `post_form`/`get_text`.
- **(P)** `map_envelope` → `provider_http_status=http_status`, `provider_message=<raw msg>`, `provider_code=None`.
- **(M)** `mark_external_user_id`: resolved `uid` = grid column 0 (`parse_player_row` first element) from `_player_id`/`_search`/create's follow-up search.
- **(SE)** `get_session`: hit / fresh; the auth-fail `get_session(invalidate=…)` → `relogin`.

**Flag — yolo/aspnet `balance_after` (narrows decision 2).** Decision 2 included "stop discarding
yolo/aspnet success data to fill `balance_after`," premised on that data carrying a post-balance. The
source shows it does **not**: aspnet money-op success is a bare success sentinel (OrionStars comments
"player balance not in this response"), and yolo's `post_form` success envelope is a Dcat
`{status,message}` dict with no balance key. So `balance_after` is honestly **null** for yolo/aspnet
money ops — read balances still flow via `user_data`. This preserves the "honest-only" primary
decision; it only removes a bonus sub-clause whose premise didn't hold.
```
