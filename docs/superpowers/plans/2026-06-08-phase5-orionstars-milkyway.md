# Phase 5 — OrionStars + MilkyWay backends with AntiCaptcha — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate two ASP.NET-WebForms cashier backends (OrionStars, MilkyWay — same 3.0.303 build, one known divergence in balance-reading) over a shared `_aspnet_cashier` helper package, plus a top-level AntiCaptcha solver that backs the captcha-required login.

**Architecture:** Two thin backend modules (`app/backends/orionstars/`, `app/backends/milkyway/`) over a shared helper package (`app/backends/_aspnet_cashier/`) holding viewstate scraping, sentinel parsing, the captcha-aware login flow, the Redis cookie-session store + `SET NX` login lock, the `tourl`/`param` handshake, and search helpers. A new top-level `app/captcha/` adapter wraps the official `anticaptchaofficial` PyPI client. Both drivers are added to `NON_IDEMPOTENT_DRIVERS` (no server-side dedupe; the `/operations` endpoint already embeds `_max_tries=1`).

**Tech Stack:** Python 3.12, FastAPI/arq, httpx (async, respx for mocking), Redis (fakeredis for tests), pytest + pytest-asyncio (auto-mode), structlog, `anticaptchaofficial` (new dependency, wrapped via `asyncio.to_thread`).

**Spec:** `docs/superpowers/specs/2026-06-08-phase5-orionstars-milkyway-design.md`
**Findings doc:** `/Applications/development/orionstars-standalone/api_findings.md`
**Branch:** `feat/phase5-orionstars-milkyway` (already created)

---

## Conventions used throughout this plan

- Every task ends with `make lint && make type && make test` and a commit on `feat/phase5-orionstars-milkyway`.
- All tests live in `tests/unit/` flat (matches existing layout — `tests/unit/test_<topic>.py`).
- HTTP mocking uses `respx` (existing convention); Redis tests use the `fake_redis` fixture already in `tests/conftest.py`.
- All raised error reasons are prefixed with the driver name where the driver matters: `"orionstars:..."`, `"milkyway:..."`; shared-package generic errors are prefixed `"aspnet:..."`.
- The shared package uses `_aspnet_cashier` (leading underscore) to signal "internal helper, not a backend driver" — the registry never imports it directly.

---

## Task 1: Add `anticaptchaofficial` dependency

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/pyproject.toml`

- [ ] **Step 1: Add the dependency**

Edit `pyproject.toml`. In the `[project]` `dependencies` list (around line 21), add `"anticaptchaofficial>=1.0.0"` as the last entry. Final shape:

```toml
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "httpx>=0.27",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "sqlalchemy[asyncio]>=2.0.30",
    "arq>=0.26",
    "redis>=5.0",
    "structlog>=24.1",
    "pycryptodome>=3.20",
    "anticaptchaofficial>=1.0.0",
]
```

- [ ] **Step 2: Install**

Run: `make install`
Expected: pip resolves and installs `anticaptchaofficial` plus its transitive deps.

- [ ] **Step 3: Smoke import**

Run: `.venv/bin/python -c "from anticaptchaofficial.imagecaptcha import imagecaptcha; print(imagecaptcha)"`
Expected: prints the class repr (no `ModuleNotFoundError`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "deps(phase5): add anticaptchaofficial for captcha solving

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: `CaptchaSolver` Protocol

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/captcha/__init__.py`
- Create: `/Applications/development/python/casino-app-automation/app/captcha/base.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_captcha_base.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_captcha_base.py`:

```python
from app.captcha.base import CaptchaSolver


class _Solver:
    async def solve_numeric_image(self, image: bytes) -> str:
        return "12345"


def test_protocol_is_satisfied_by_a_class_with_solve_numeric_image():
    # Protocol conformance is structural; a class with the right async method qualifies.
    s: CaptchaSolver = _Solver()
    assert hasattr(s, "solve_numeric_image")
```

- [ ] **Step 2: Run it; should fail with ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_captcha_base.py -v`
Expected: `ModuleNotFoundError: No module named 'app.captcha'`

- [ ] **Step 3: Create the module**

Create `app/captcha/__init__.py`:
```python
```
(empty file)

Create `app/captcha/base.py`:
```python
from typing import Protocol


class CaptchaSolver(Protocol):
    """Abstract solver for image-based captchas.

    Implementations must accept raw image bytes (e.g. JPEG/PNG) and return the decoded text.
    Solver failures should be raised as `TransientBackendError` from `app.backends.base`.
    """

    async def solve_numeric_image(self, image: bytes) -> str: ...
```

- [ ] **Step 4: Test passes**

Run: `.venv/bin/pytest tests/unit/test_captcha_base.py -v`
Expected: 1 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/captcha/ tests/unit/test_captcha_base.py
git commit -m "feat(captcha): add CaptchaSolver protocol

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: `AntiCaptchaSolver` (real implementation)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/captcha/anticaptcha.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_captcha_anticaptcha.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_captcha_anticaptcha.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

from app.backends.base import TransientBackendError
from app.captcha.anticaptcha import AntiCaptchaSolver


@pytest.fixture
def fake_solver_class():
    """Patch anticaptchaofficial.imagecaptcha.imagecaptcha with a MagicMock factory.

    The library's `imagecaptcha()` returns a stateful solver instance; we replace it
    so unit tests never touch the live AntiCaptcha service.
    """
    with patch("app.captcha.anticaptcha.imagecaptcha") as factory:
        instance = MagicMock()
        factory.return_value = instance
        yield factory, instance


async def test_solve_numeric_image_returns_solution_text(fake_solver_class):
    factory, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "34596"
    solver = AntiCaptchaSolver(api_key="testkey")
    out = await solver.solve_numeric_image(b"\xff\xd8FAKE_JPEG")
    assert out == "34596"
    # The library was configured with our key + numeric-only mode + zero verbose
    instance.set_key.assert_called_once_with("testkey")
    instance.set_numeric.assert_called_once_with(2)
    instance.set_verbose.assert_called_once_with(0)


async def test_solve_numeric_image_raises_transient_on_error_code(fake_solver_class):
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = 0
    instance.error_code = "ERROR_KEY_DOES_NOT_EXIST"
    solver = AntiCaptchaSolver(api_key="bad")
    with pytest.raises(TransientBackendError) as ei:
        await solver.solve_numeric_image(b"\xff\xd8FAKE")
    assert "anticaptcha:ERROR_KEY_DOES_NOT_EXIST" in str(ei.value)


async def test_solve_numeric_image_strips_whitespace(fake_solver_class):
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "  12345  \n"
    solver = AntiCaptchaSolver(api_key="k")
    out = await solver.solve_numeric_image(b"x")
    assert out == "12345"


async def test_solve_writes_then_removes_temp_file(fake_solver_class, tmp_path, monkeypatch):
    """The library accepts a file path; we write the bytes to a temp file and unlink in finally."""
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "11111"

    written_paths: list[str] = []

    def fake_solve(path: str) -> str:
        written_paths.append(path)
        # File must exist while solver is reading it
        with open(path, "rb") as f:
            assert f.read() == b"PAYLOAD"
        return "11111"

    instance.solve_and_return_solution.side_effect = fake_solve
    solver = AntiCaptchaSolver(api_key="k")
    await solver.solve_numeric_image(b"PAYLOAD")
    # Tempfile was removed after solve
    assert written_paths and not any(__import__("os").path.exists(p) for p in written_paths)
```

- [ ] **Step 2: Run; should fail with ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_captcha_anticaptcha.py -v`
Expected: `ModuleNotFoundError: No module named 'app.captcha.anticaptcha'`

- [ ] **Step 3: Implement**

Create `app/captcha/anticaptcha.py`:

```python
import asyncio
import os
import tempfile

from anticaptchaofficial.imagecaptcha import imagecaptcha  # noqa: F401 (re-imported by tests)

from app.backends.base import TransientBackendError


class AntiCaptchaSolver:
    """Thin async wrapper over the official `anticaptchaofficial` image-captcha client.

    The upstream library is synchronous and file-path-based; we wrap each solve in
    `asyncio.to_thread` so it doesn't block the event loop. Configured for digit-only
    captchas (OrionStars/MilkyWay use a 5-digit numeric JPEG).
    """

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("AntiCaptchaSolver requires a non-empty api_key")
        self._api_key = api_key

    async def solve_numeric_image(self, image: bytes) -> str:
        return await asyncio.to_thread(self._solve_sync, image)

    def _solve_sync(self, image: bytes) -> str:
        solver = imagecaptcha()
        solver.set_verbose(0)
        solver.set_key(self._api_key)
        solver.set_numeric(2)            # 2 = "only digits"
        # The library reads from a file path, not a bytes buffer. Write to a temp file
        # in the OS temp dir and unlink after the solve completes.
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(image)
            result = solver.solve_and_return_solution(path)
            if result == 0:
                code = getattr(solver, "error_code", "unknown")
                raise TransientBackendError(f"anticaptcha:{code}")
            return str(result).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_captcha_anticaptcha.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/captcha/anticaptcha.py tests/unit/test_captcha_anticaptcha.py
git commit -m "feat(captcha): wrap anticaptchaofficial in async AntiCaptchaSolver

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: `FakeCaptchaSolver` fixture in conftest

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/tests/conftest.py`

- [ ] **Step 1: Add the fixture**

Append to `tests/conftest.py`:

```python
class FakeCaptchaSolver:
    """Reusable test double for CaptchaSolver. Returns canned answers or raises on demand.

    Default behavior: returns a fixed 5-digit string. Tests may construct with `answers=[...]`
    to return different solutions per call, or `raise_exc=...` to simulate solver failure.
    """

    def __init__(
        self, *, answers: list[str] | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._answers = list(answers) if answers else ["34596"]
        self._raise = raise_exc
        self.calls: list[bytes] = []

    async def solve_numeric_image(self, image: bytes) -> str:
        self.calls.append(image)
        if self._raise is not None:
            raise self._raise
        if not self._answers:
            return "00000"
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]


@pytest_asyncio.fixture
async def fake_captcha():
    return FakeCaptchaSolver()
```

- [ ] **Step 2: Quick sanity test**

Run: `.venv/bin/pytest tests/unit/test_captcha_base.py -v`
Expected: still 1 passed; the fixture loads without breaking anything.

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: add FakeCaptchaSolver fixture for reuse across phase5 tests

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Cookie session store + login lock (`_aspnet_cashier/session.py`)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/__init__.py`
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/session.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_session.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_aspnet_session.py`:

```python
import asyncio

import pytest

from app.backends._aspnet_cashier.session import (
    CachedSession,
    CookieSessionStore,
    InMemoryCookieSessionStore,
)


async def test_in_memory_set_get_clear():
    store = InMemoryCookieSessionStore()
    assert await store.get(1) is None
    await store.set(1, CachedSession(cookie="ABC123", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(1)
    assert got is not None and got.cookie == "ABC123"
    await store.clear(1)
    assert await store.get(1) is None


async def test_redis_set_get_clear_and_key_prefix(fake_redis):
    store = CookieSessionStore(fake_redis)
    await store.set(7, CachedSession(cookie="xyz", expires_at=9_999_999_999), ttl_seconds=120)
    raw = await fake_redis.get("aspnet_session:7")
    assert raw is not None and b"xyz" in raw
    got = await store.get(7)
    assert got is not None and got.cookie == "xyz" and got.expires_at == 9_999_999_999
    await store.clear(7)
    assert await store.get(7) is None


async def test_redis_set_respects_ttl(fake_redis):
    store = CookieSessionStore(fake_redis)
    await store.set(8, CachedSession(cookie="t", expires_at=9_999_999_999), ttl_seconds=1)
    ttl = await fake_redis.ttl("aspnet_session:8")
    assert 0 < ttl <= 1


async def test_redis_login_lock_writes_and_clears_key(fake_redis):
    store = CookieSessionStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("aspnet_login:9")) == 1
    assert (await fake_redis.exists("aspnet_login:9")) == 0


async def test_redis_login_lock_setnx_blocks_second_acquire(fake_redis):
    store = CookieSessionStore(fake_redis)
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock():
        async with store.login_lock(game_id=10, ttl_seconds=30):
            held.set()
            await release.wait()

    task = asyncio.create_task(hold_lock())
    await held.wait()
    with pytest.raises(TimeoutError):
        async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=0.2):
            raise AssertionError("should not have acquired lock")
    release.set()
    await task
    async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=1.0):
        pass
```

- [ ] **Step 2: Run; should fail with ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_aspnet_session.py -v`
Expected: `ModuleNotFoundError: No module named 'app.backends._aspnet_cashier'`

- [ ] **Step 3: Implement**

Create `app/backends/_aspnet_cashier/__init__.py`:
```python
```
(empty)

Create `app/backends/_aspnet_cashier/session.py`:

```python
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    cookie: str           # ASP.NET_SessionId value
    expires_at: int       # unix seconds (when we consider this session stale; not the server's TTL)


class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 20,
        poll_seconds: float = 0.1, acquire_timeout: float = 30.0,
    ):
        """Async context manager that serializes login flows for a game across workers.

        For OrionStars/MilkyWay the lock is purely an efficiency measure (one captcha solve
        serves all concurrent waiters); concurrent valid sessions for the same agent coexist
        on these portals — see Phase 5 design doc, "Open questions".

        Raises TimeoutError if the lock cannot be acquired within acquire_timeout.
        """


class InMemoryCookieSessionStore:
    """Process-local store with per-game asyncio.Lock. For tests / single-process fallback."""

    def __init__(self) -> None:
        self._store: dict[int, CachedSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(self, game_id: int) -> CachedSession | None:
        return self._store.get(game_id)

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        self._store[game_id] = session

    async def clear(self, game_id: int) -> None:
        self._store.pop(game_id, None)

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 20,
                         poll_seconds: float = 0.1, acquire_timeout: float = 30.0):
        lock = self._locks.setdefault(game_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"aspnet login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"aspnet_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"aspnet_login:{game_id}"


class CookieSessionStore:
    """Redis-backed cookie session store + SET NX login lock. Shared across all workers."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, game_id: int) -> CachedSession | None:
        raw = await self._redis.get(_key_session(game_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedSession(cookie=d["cookie"], expires_at=int(d["expires_at"]))

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        raw = json.dumps({"cookie": session.cookie, "expires_at": session.expires_at})
        await self._redis.set(_key_session(game_id), raw, ex=max(1, ttl_seconds))

    async def clear(self, game_id: int) -> None:
        await self._redis.delete(_key_session(game_id))

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 20,
                         poll_seconds: float = 0.1, acquire_timeout: float = 30.0):
        key = _key_lock(game_id)
        deadline = time.monotonic() + acquire_timeout
        acquired = False
        while True:
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"aspnet login lock acquire timeout (game_id={game_id})")
            await asyncio.sleep(poll_seconds)
        try:
            yield
        finally:
            if acquired:
                try:
                    await self._redis.delete(key)
                except Exception:  # noqa: BLE001 - best-effort release; TTL backs us up
                    pass
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_session.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/_aspnet_cashier/ tests/unit/test_aspnet_session.py
git commit -m "feat(aspnet): cookie session store + SET-NX login lock

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Configuration knobs

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/config.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

Read `tests/unit/test_config.py` to find an existing default-values test. Add this test alongside the existing ones:

```python
def test_captcha_and_aspnet_session_defaults():
    from app.config import Settings
    s = Settings()
    assert s.anticaptcha_poll_interval_seconds == 2.0
    assert s.anticaptcha_max_poll_seconds == 120.0
    assert s.captcha_login_max_attempts == 3
    assert s.aspnet_session_ttl_seconds == 1800
    assert s.aspnet_lock_ttl_seconds == 20
    assert s.aspnet_lock_acquire_timeout_seconds == 30.0
```

- [ ] **Step 2: Run; should fail**

Run: `.venv/bin/pytest tests/unit/test_config.py::test_captcha_and_aspnet_session_defaults -v`
Expected: `AttributeError: 'Settings' object has no attribute 'anticaptcha_poll_interval_seconds'`

- [ ] **Step 3: Add fields**

In `app/config.py`, after the `anticaptcha_api_key: str = ""` line, add:

```python
    anticaptcha_poll_interval_seconds: float = 2.0
    anticaptcha_max_poll_seconds: float = 120.0
    captcha_login_max_attempts: int = 3
    aspnet_session_ttl_seconds: int = 1800
    aspnet_lock_ttl_seconds: int = 20
    aspnet_lock_acquire_timeout_seconds: float = 30.0
```

- [ ] **Step 4: Test passes**

Run: `.venv/bin/pytest tests/unit/test_config.py::test_captcha_and_aspnet_session_defaults -v`
Expected: 1 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/unit/test_config.py
git commit -m "config(phase5): add captcha + aspnet session/lock knobs

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: ASP.NET parsers (viewstate, sentinels, search rows, dialog URL, balance widget)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/parsers.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_parsers.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_aspnet_parsers.py`:

```python
import pytest

from app.backends._aspnet_cashier.parsers import (
    ViewState,
    parse_agent_balance_widget,
    parse_dialog_response,
    parse_get_score_response,
    parse_milkyway_balance_row,
    parse_sentinel,
    parse_update_select,
    parse_viewstate,
)

# --- viewstate ---

_VS_WITH_EVENTVALIDATION = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="dDwxNDc=" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="CA0B0334" />
  <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="/wEdAAU=" />
</form>
"""

_VS_NO_EVENTVALIDATION = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="dDwxNDc=" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CF7AEB79" />
</form>
"""


def test_viewstate_scrapes_all_three_hidden_fields():
    vs = parse_viewstate(_VS_WITH_EVENTVALIDATION)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator == "CA0B0334"
    assert vs.event_validation == "/wEdAAU="


def test_viewstate_eventvalidation_is_none_when_absent():
    vs = parse_viewstate(_VS_NO_EVENTVALIDATION)
    assert vs.viewstate == "dDwxNDc="
    assert vs.viewstate_generator == "CF7AEB79"
    assert vs.event_validation is None


def test_viewstate_raises_when_required_field_missing():
    with pytest.raises(ValueError, match="__VIEWSTATE"):
        parse_viewstate("<form></form>")


# --- sentinel ---

def test_sentinel_success_recharge_redeem_includes_balance_arg():
    kind, args = parse_sentinel(
        'foo<script>showAlter("Confirmed successful","Balance:30");</script>bar'
    )
    assert kind == "success"
    assert args == ["Confirmed successful", "Balance:30"]


def test_sentinel_success_reset_password_single_arg():
    kind, args = parse_sentinel('<script>showAlter("Modified success!");</script>')
    assert kind == "success"
    assert args == ["Modified success!"]


def test_sentinel_success_create_account_uses_testAlter():
    kind, args = parse_sentinel('<script>testAlter("Added successfully");</script>')
    assert kind == "success"
    assert args == ["Added successfully"]


def test_sentinel_business_failure_insufficient_agent_funds():
    kind, args = parse_sentinel(
        '<script>showAlter("Sorry, the surplus money is insufficient!");</script>'
    )
    assert kind == "business_failure"
    assert args == ["Sorry, the surplus money is insufficient!"]


def test_sentinel_business_failure_insufficient_player_credit():
    kind, args = parse_sentinel(
        '<script>showAlter("Sorry, there is not enough gold for the operator!");</script>'
    )
    assert kind == "business_failure"
    assert args == ["Sorry, there is not enough gold for the operator!"]


def test_sentinel_business_failure_password_mismatch_via_alert():
    kind, args = parse_sentinel('<script>alert("Inconsistent passwords entered");</script>')
    assert kind == "business_failure"
    assert args == ["Inconsistent passwords entered"]


def test_sentinel_business_failure_create_errors_use_testAlter():
    for msg in [
        "The account number already exists, please re-enter it!",
        "entered passwords differ from the another.",
        "account name should be compose with letters letters, underscore & numbers.",
    ]:
        kind, args = parse_sentinel(f'<script>testAlter("{msg}");</script>')
        assert kind == "business_failure"
        assert args == [msg]


def test_sentinel_unknown_when_no_script_match():
    kind, args = parse_sentinel("<html><body>nothing here</body></html>")
    assert kind == "unknown"
    assert args == []


# --- updateSelect ---

def test_update_select_parses_first_row_uid_gid():
    html = """
    <table>
      <tr><td><a onclick="updateSelect( '21041615,21219386')">Update</a></td></tr>
      <tr><td><a onclick="updateSelect( '99999999,88888888')">Update</a></td></tr>
    </table>
    """
    pairs = parse_update_select(html)
    assert pairs == [("21041615", "21219386"), ("99999999", "88888888")]


def test_update_select_returns_empty_when_no_rows():
    assert parse_update_select("<table></table>") == []


# --- getscoreuserid response ---

def test_parse_get_score_response_returns_credit_and_totalwin():
    body = "0.00@0.00|<full AccountsList HTML...>"
    credit, totalwin = parse_get_score_response(body)
    assert credit == "0.00"
    assert totalwin == "0.00"


def test_parse_get_score_response_handles_nonzero_values():
    body = "1234.56@7890.12|<html>...</html>"
    credit, totalwin = parse_get_score_response(body)
    assert credit == "1234.56"
    assert totalwin == "7890.12"


def test_parse_get_score_response_raises_when_no_prefix():
    with pytest.raises(ValueError):
        parse_get_score_response("not a valid response")


# --- dialog (tourl) response ---

def test_parse_dialog_response_returns_url_and_param():
    body = (
        "Module/AccountManager/GrantTreasure.aspx?param=75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"
        "|<full html...>"
    )
    url, token = parse_dialog_response(body)
    assert url == "Module/AccountManager/GrantTreasure.aspx?param=75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"
    assert token == "75517D2841C0311A6F33B18FBDC9A232DD313A7FF5BA019430EE38F7A28A2F15"


def test_parse_dialog_response_raises_when_empty():
    with pytest.raises(ValueError, match="please_select_first"):
        parse_dialog_response("|<html>...</html>")


# --- agent balance widget ---

def test_parse_agent_balance_widget_extracts_first_balance_int():
    html = '<div class="navTop">Balance:31</div>... other Balance:99 elsewhere'
    assert parse_agent_balance_widget(html) == 31


def test_parse_agent_balance_widget_raises_when_missing():
    with pytest.raises(ValueError, match="agent_balance_widget"):
        parse_agent_balance_widget("<html>no balance widget</html>")


# --- milkyway balance row ---

_MW_ROW_HTML = """
<table>
  <tr>
    <td><a onclick="updateSelect( '21041615,21219386')">Update</a></td>
    <td>21219386</td>
    <td>Saud_Doe892</td>
    <td>Saud</td>
    <td>123.45</td>
    <td>2026-05-30</td>
    <td>2026-06-01</td>
    <td>TestMW159</td>
    <td>Active</td>
  </tr>
</table>
"""


def test_milkyway_balance_row_extracts_balance_for_matching_account():
    bal = parse_milkyway_balance_row(_MW_ROW_HTML, account="Saud_Doe892")
    assert bal == "123.45"


def test_milkyway_balance_row_matches_by_gameid_when_account_misses():
    bal = parse_milkyway_balance_row(_MW_ROW_HTML, account="21219386")
    assert bal == "123.45"


def test_milkyway_balance_row_raises_when_no_matching_row():
    with pytest.raises(ValueError, match="row_not_found"):
        parse_milkyway_balance_row(_MW_ROW_HTML, account="other_account")
```

- [ ] **Step 2: Run; expect mass failure**

Run: `.venv/bin/pytest tests/unit/test_aspnet_parsers.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `app/backends/_aspnet_cashier/parsers.py`:

```python
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ViewState:
    viewstate: str
    viewstate_generator: str
    event_validation: str | None     # None on pages with EnableEventValidation="false" (AccountsList.aspx)


_HIDDEN_RE = re.compile(
    r'<input[^>]*name=["\'](?P<name>__[A-Z]+)["\'][^>]*value=["\'](?P<value>[^"\']*)["\']',
    re.IGNORECASE,
)


def parse_viewstate(html: str) -> ViewState:
    """Scrape ASP.NET hidden fields from a rendered form.

    __VIEWSTATE and __VIEWSTATEGENERATOR are required. __EVENTVALIDATION is page-specific:
    dialog pages (GrantTreasure, ChangeTreasure, ResetPassWord, CreateAccount) include it;
    AccountsList.aspx does not (EnableEventValidation="false").
    """
    fields: dict[str, str] = {}
    for m in _HIDDEN_RE.finditer(html):
        fields[m.group("name").upper()] = m.group("value")
    if "__VIEWSTATE" not in fields:
        raise ValueError("__VIEWSTATE not found in form HTML")
    if "__VIEWSTATEGENERATOR" not in fields:
        raise ValueError("__VIEWSTATEGENERATOR not found in form HTML")
    return ViewState(
        viewstate=fields["__VIEWSTATE"],
        viewstate_generator=fields["__VIEWSTATEGENERATOR"],
        event_validation=fields.get("__EVENTVALIDATION"),
    )


# Captures the trailing inline-script sentinel that ASP.NET cashier responses use to signal
# success/failure. Accepts showAlter / testAlter / alert with 1 or 2 string args.
_SENTINEL_RE = re.compile(
    r'(?:showAlter|testAlter|alert)\(\s*"([^"]*)"(?:\s*,\s*"([^"]*)")?\s*\)',
)

# Sentinel messages we know are terminal business failures (the rest of "success" sentinels
# are pinned by exact string in the per-op response handlers). For the parser layer, we only
# need to distinguish "success" (the known-success strings) from "business_failure" (everything
# else that matched a script) from "unknown" (no script match).
_KNOWN_SUCCESS_MESSAGES = frozenset({
    "Confirmed successful",
    "Modified success!",
    "Added successfully",
})


def parse_sentinel(html: str) -> tuple[str, list[str]]:
    """Pattern-match the trailing inline-script sentinel.

    Returns: (kind, args)
      - kind="success",          args=[message, ...extras]   for known-success messages
      - kind="business_failure", args=[message, ...extras]   for any other matched message
      - kind="unknown",          args=[]                     when no sentinel script matched
    """
    m = _SENTINEL_RE.search(html)
    if not m:
        return ("unknown", [])
    args = [m.group(1)] + ([m.group(2)] if m.group(2) is not None else [])
    kind = "success" if args[0] in _KNOWN_SUCCESS_MESSAGES else "business_failure"
    return (kind, args)


_UPDATE_SELECT_RE = re.compile(
    r"updateSelect\(\s*'(?P<uid>\d+)\s*,\s*(?P<gid>\d+)'\s*\)"
)


def parse_update_select(html: str) -> list[tuple[str, str]]:
    """Extract every (UserID, GameID) pair from `updateSelect('<uid>,<gid>')` JS handlers."""
    return [(m.group("uid"), m.group("gid")) for m in _UPDATE_SELECT_RE.finditer(html)]


def parse_get_score_response(body: str) -> tuple[str, str]:
    """Parse `<credit>@<totalwin>|<html...>` from OrionStars `getscoreuserid` POST.

    Returns the two leading string values (we keep them as strings; caller converts to cents).
    """
    if "|" not in body or "@" not in body.split("|", 1)[0]:
        raise ValueError("getscoreuserid response has no `credit@totalwin|` prefix")
    head = body.split("|", 1)[0]
    credit, totalwin = head.split("@", 1)
    return (credit, totalwin)


def parse_dialog_response(body: str) -> tuple[str, str]:
    """Parse the `<dialogURL>?param=<TOKEN>|<html...>` reply from the `tourl` POST.

    Returns (dialog_url, param_token). Raises ValueError if the URL is empty
    (server returns just `|...` when no player is selected).
    """
    head = body.split("|", 1)[0]
    if not head:
        raise ValueError("aspnet:please_select_first")
    if "param=" not in head:
        raise ValueError("aspnet:dialog_url_missing_param")
    token = head.rsplit("param=", 1)[-1]
    return (head, token)


_BALANCE_WIDGET_RE = re.compile(r"Balance\s*:\s*(\d+)")


def parse_agent_balance_widget(html: str) -> int:
    """Extract the agent's `Balance:NN` (integer dollars) from the page chrome."""
    m = _BALANCE_WIDGET_RE.search(html)
    if not m:
        raise ValueError("aspnet:agent_balance_widget_not_found")
    return int(m.group(1))


# Matches a <tr>...</tr> block; we walk its <td>s in order.
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def parse_milkyway_balance_row(html: str, *, account: str) -> str:
    """MilkyWay-specific: locate the row whose Account (td[2]) or GameID (td[1]) matches,
    then return Balance/Credit from td[4]. See findings §4.1 portal-difference note.
    """
    for row_match in _ROW_RE.finditer(html):
        tds = [_strip_tags(m.group(1)) for m in _TD_RE.finditer(row_match.group(1))]
        if len(tds) < 5:
            continue
        # td[1] = GameID, td[2] = Account, td[4] = Balance/Credit
        if tds[1] == account or tds[2] == account:
            return tds[4]
    raise ValueError("aspnet:milkyway_balance_row_not_found")
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_parsers.py -v`
Expected: ~22 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/_aspnet_cashier/parsers.py tests/unit/test_aspnet_parsers.py
git commit -m "feat(aspnet): viewstate/sentinel/search/dialog/balance parsers

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Login error mapping (`_aspnet_cashier/errors.py`)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/errors.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_errors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_aspnet_errors.py`:

```python
from app.backends._aspnet_cashier.errors import (
    LOGIN_ERRTYPE_MESSAGES,
    classify_business_failure_message,
    login_errtype_to_code,
)


def test_login_errtype_known_codes_map_to_short_codes():
    assert login_errtype_to_code("verifycode") == "captcha_wrong"
    assert login_errtype_to_code("overtime") == "session_overtime"
    assert login_errtype_to_code("errorNamePassowrd") == "bad_credentials"
    assert login_errtype_to_code("errorBlockIPErr") == "ip_blocked"
    assert login_errtype_to_code("errorBindIP") == "ip_not_bound"
    assert login_errtype_to_code("errorNullity") == "account_banned"
    assert login_errtype_to_code("errUser") == "session_stolen"


def test_login_errtype_unknown_falls_through_to_passthrough():
    assert login_errtype_to_code("totally_made_up") == "unknown:totally_made_up"


def test_login_errtype_table_keys_match_findings_doc_dictionary():
    # Confidence check: keys we mapped exist in the documented dictionary.
    for k in ["verifycode", "overtime", "errorNamePassowrd", "errorBindIP", "errUser"]:
        assert k in LOGIN_ERRTYPE_MESSAGES


def test_business_failure_messages_recharge_redeem_create_reset():
    assert classify_business_failure_message(
        "Sorry, the surplus money is insufficient!"
    ) == "insufficient_agent_funds"
    assert classify_business_failure_message(
        "Sorry, there is not enough gold for the operator!"
    ) == "insufficient_player_credit"
    assert classify_business_failure_message(
        "The account number already exists, please re-enter it!"
    ) == "account_exists"
    assert classify_business_failure_message(
        "account name should be compose with letters letters, underscore & numbers."
    ) == "account_invalid_chars"
    assert classify_business_failure_message(
        "entered passwords differ from the another."
    ) == "password_mismatch"
    assert classify_business_failure_message(
        "Inconsistent passwords entered"
    ) == "password_mismatch"


def test_business_failure_unknown_message_returns_unknown_slug():
    out = classify_business_failure_message("Some surprise message we have not seen")
    assert out.startswith("unknown:")
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_aspnet_errors.py -v`

- [ ] **Step 3: Implement**

Create `app/backends/_aspnet_cashier/errors.py`:

```python
# Best-effort mapping from the findings doc §2.6 message-id dictionary to short error codes
# our system surfaces. Several entries (everything except verifycode and overtime) had their
# exact `errtype` query value inferred rather than individually exercised — confirm in the wild.
LOGIN_ERRTYPE_MESSAGES: dict[str, str] = {
    "verifycode":          "captcha_wrong",
    "overtime":            "session_overtime",
    "errorNamePassowrd":   "bad_credentials",
    "errorUserName":       "bad_username",
    "errorBlockIPErr":     "ip_blocked",
    "errorBindIP":         "ip_not_bound",
    "errorNullity":        "account_banned",
    "errorLogonTimeout":   "logon_timeout",
    "errorAuthParam":      "auth_param",
    "errorUnknown":        "server_unknown",
    "frequent":            "rate_limited",
    "errUser":             "session_stolen",
    "errorPassowrdTooLong":"password_too_long",
    "errorUserRole":       "not_admin",
}


def login_errtype_to_code(errtype: str) -> str:
    """Translate the `errtype` query value from a login 301 into a short, stable error code."""
    return LOGIN_ERRTYPE_MESSAGES.get(errtype, f"unknown:{errtype}")


# Substring-based classifier for the sentinel-string business failures. Mirrors §5.1 of the
# findings doc verbatim (typos and all — "letters letters", "differ from the another").
_BUSINESS_FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("surplus money is insufficient",               "insufficient_agent_funds"),
    ("not enough gold for the operator",            "insufficient_player_credit"),
    ("account number already exists",               "account_exists"),
    ("account name should be compose",              "account_invalid_chars"),
    ("entered passwords differ",                    "password_mismatch"),
    ("inconsistent passwords entered",              "password_mismatch"),
]


def classify_business_failure_message(message: str) -> str:
    """Map a sentinel message string to a short, stable error slug.

    Returns "unknown:<truncated>" when no known pattern matches — the caller surfaces this so
    we notice and add a mapping.
    """
    low = (message or "").lower()
    for needle, slug in _BUSINESS_FAILURE_PATTERNS:
        if needle in low:
            return slug
    return f"unknown:{message[:60]}"
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_errors.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/_aspnet_cashier/errors.py tests/unit/test_aspnet_errors.py
git commit -m "feat(aspnet): login errtype + business-failure message classifiers

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Password generator (`_aspnet_cashier/passwords.py`)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/passwords.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_passwords.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_aspnet_passwords.py`:

```python
import re

from app.backends._aspnet_cashier.passwords import generate_aspnet_password


def test_password_charset_letters_digits_underscore_only():
    for _ in range(50):
        pw = generate_aspnet_password()
        assert re.fullmatch(r"[A-Za-z0-9_]+", pw), pw


def test_password_length_within_limits():
    for _ in range(50):
        pw = generate_aspnet_password()
        assert 1 <= len(pw) <= 32, pw


def test_password_varies():
    assert len({generate_aspnet_password() for _ in range(20)}) > 1


def test_password_is_memorable_word_plus_digits():
    # Format: capitalized word + 4 digits (e.g. "Tiger4783").
    pw = generate_aspnet_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_aspnet_passwords.py -v`

- [ ] **Step 3: Implement**

Create `app/backends/_aspnet_cashier/passwords.py`:

```python
# Re-use the existing memorable-password generator (word + 4 digits, alphanumeric).
# Findings doc §4.7 charset rule: `[A-Za-z0-9_]`, max 32 — the GameVault generator
# emits letters+digits only (≤12 chars), satisfying both constraints with margin.
from app.backends.gamevault.passwords import generate_memorable_password


def generate_aspnet_password() -> str:
    """Memorable password for OrionStars/MilkyWay create + reset.

    Charset restricted to `[A-Za-z0-9_]` (no underscores actually emitted; the form
    allows them but the GameVault generator doesn't use them). ≤32 characters.
    """
    return generate_memorable_password()
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_passwords.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/backends/_aspnet_cashier/passwords.py tests/unit/test_aspnet_passwords.py
git commit -m "feat(aspnet): memorable password generator (reuses gamevault)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Login flow (`_aspnet_cashier/login.py`)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/login.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_login.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_aspnet_login.py`:

```python
import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends._aspnet_cashier.login import login
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"

_LOGIN_PAGE = """
<form id="form1" method="post" action="default.aspx">
  <input type="hidden" name="__VIEWSTATE" value="VS_A" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
  <input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
  <img id="img1" src="Tools/VerifyImagePage.aspx?12345" />
</form>
"""


def _login_page_response():
    return httpx.Response(
        200,
        text=_LOGIN_PAGE,
        headers={
            # ASP.NET issues this on the first GET
            "Set-Cookie": "ASP.NET_SessionId=COOKIE_FIRST; path=/; HttpOnly",
        },
    )


def _captcha_image_response():
    return httpx.Response(200, content=b"\xff\xd8FAKE_JPEG", headers={"Content-Type": "image/jpeg"})


def _login_success_response():
    return httpx.Response(
        301, text="", headers={"Location": "Cashier.aspx"}
    )


def _login_bad_captcha_response():
    return httpx.Response(
        301, text="", headers={"Location": "default.aspx?errtype=verifycode"}
    )


def _login_bad_creds_response():
    return httpx.Response(
        301, text="", headers={"Location": "default.aspx?errtype=errorNamePassowrd"}
    )


# --- happy path ---

@respx.mock
async def test_login_happy_path_returns_session_cookie():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    post = respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())

    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE,
            username="TestOS159", password="Test@159872!!",
            captcha_solver=FakeCaptchaSolver(answers=["34596"]),
            max_attempts=3,
        )

    assert cookie == "COOKIE_FIRST"
    body = post.calls.last.request.content.decode()
    assert "txtLoginName=TestOS159" in body
    assert "txtVerifyCode=34596" in body
    assert "__VIEWSTATE=VS_A" in body
    assert "__EVENTVALIDATION=EV_A" in body
    assert "ddlRole=0" in body


@respx.mock
async def test_login_includes_accept_language_on_every_request():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_success_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        await login(http=http, base_url=BASE, username="u", password="p",
                    captcha_solver=FakeCaptchaSolver(), max_attempts=1)
    for call in respx.calls:
        assert call.request.headers.get("accept-language", "").startswith("en")


# --- captcha retry ---

@respx.mock
async def test_login_retries_on_verifycode_with_fresh_image_and_viewstate():
    get_default = respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    posts = respx.post(f"{BASE}/default.aspx").mock(
        side_effect=[_login_bad_captcha_response(), _login_success_response()]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        cookie = await login(
            http=http, base_url=BASE, username="u", password="p",
            captcha_solver=FakeCaptchaSolver(answers=["WRONG", "34596"]),
            max_attempts=3,
        )
    assert cookie == "COOKIE_FIRST"
    # GET /default.aspx happened twice — once per attempt — guaranteeing fresh viewstate.
    assert get_default.call_count == 2
    assert posts.call_count == 2


@respx.mock
async def test_login_raises_after_max_attempts_of_verifycode():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_bad_captcha_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(BackendError, match="captcha_failed_max_attempts"):
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(answers=["WRONG"]),
                max_attempts=2,
                driver_prefix="orionstars",
            )


# --- terminal failures ---

@respx.mock
async def test_login_terminal_on_bad_credentials():
    respx.get(f"{BASE}/default.aspx").mock(return_value=_login_page_response())
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?12345").mock(return_value=_captcha_image_response())
    respx.post(f"{BASE}/default.aspx").mock(return_value=_login_bad_creds_response())
    async with httpx.AsyncClient(base_url=BASE) as http:
        with pytest.raises(BackendError) as ei:
            await login(
                http=http, base_url=BASE, username="u", password="p",
                captcha_solver=FakeCaptchaSolver(), max_attempts=3,
                driver_prefix="orionstars",
            )
    assert ei.value.reason == "orionstars:login_failed:bad_credentials"
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_aspnet_login.py -v`

- [ ] **Step 3: Implement**

Create `app/backends/_aspnet_cashier/login.py`:

```python
import re
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends._aspnet_cashier.errors import login_errtype_to_code
from app.backends._aspnet_cashier.parsers import parse_viewstate
from app.captcha.base import CaptchaSolver

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"

# Browser-flavored headers. `Accept-Language` is mandatory — without it the ASP.NET
# InitializeCulture() throws NRE and returns a 500 yellow-screen (findings §3).
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_CAPTCHA_IMG_RE = re.compile(
    r'<img[^>]+src=["\'](Tools/VerifyImagePage\.aspx\?[^"\']+)["\']',
    re.IGNORECASE,
)
_SESSION_COOKIE = "ASP.NET_SessionId"


async def login(
    *, http: httpx.AsyncClient, base_url: str,
    username: str, password: str,
    captcha_solver: CaptchaSolver,
    max_attempts: int = 3,
    driver_prefix: str = "aspnet",
) -> str:
    """Captcha-aware login. Returns the `ASP.NET_SessionId` cookie value.

    On `errtype=verifycode` we restart the attempt from a fresh GET (viewstate and captcha
    are both single-use and session-rotated). Any other `errtype` is terminal — we map it
    to a short code via `login_errtype_to_code()` and raise `BackendError`.

    `driver_prefix` is used in raised error codes ("orionstars" or "milkyway") so logs can
    distinguish the portal.
    """
    base = base_url.rstrip("/")
    last_errtype: str | None = None
    for _attempt in range(max_attempts):
        # Each attempt uses a fresh cookie jar (cookies live on the http client only for the
        # duration of the attempt; on failure we forget them and start over).
        cookies: dict[str, str] = {}

        # 1. GET the login page.
        try:
            r1 = await http.get(f"{base}/default.aspx", headers=_BASE_HEADERS, cookies=cookies)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r1.status_code >= 500:
            raise TransientBackendError(f"{driver_prefix}:login_http_{r1.status_code}")
        cookies.update({k: v for k, v in r1.cookies.items()})
        vs = parse_viewstate(r1.text)
        m = _CAPTCHA_IMG_RE.search(r1.text)
        if not m:
            raise TransientBackendError(f"{driver_prefix}:login_no_captcha_img")
        captcha_url = f"{base}/{m.group(1)}"

        # 2. GET the captcha image (must reuse the same cookie jar).
        try:
            r2 = await http.get(captcha_url, headers=_BASE_HEADERS, cookies=cookies)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r2.status_code != 200:
            raise TransientBackendError(f"{driver_prefix}:captcha_http_{r2.status_code}")
        cookies.update({k: v for k, v in r2.cookies.items()})

        # 3. Solve.
        text = await captcha_solver.solve_numeric_image(r2.content)

        # 4. POST credentials.
        form_fields = {
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
            "__VIEWSTATE": vs.viewstate,
            "__VIEWSTATEGENERATOR": vs.viewstate_generator,
            "__EVENTVALIDATION": vs.event_validation or "",
            "ddlRole": "0",
            "txtLoginName": username,
            "txtLoginPass": password,
            "txtVerifyCode": text,
            "btnLogin": "Login in",
        }
        body = urlencode(form_fields).encode()
        try:
            r3 = await http.post(
                f"{base}/default.aspx", content=body,
                headers={**_BASE_HEADERS, "Content-Type": _FORM_CT},
                cookies=cookies, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{driver_prefix}:login_transport:{type(exc).__name__}") from exc
        if r3.status_code >= 500:
            raise TransientBackendError(f"{driver_prefix}:login_http_{r3.status_code}")
        if r3.status_code != 301:
            raise TransientBackendError(f"{driver_prefix}:login_unexpected_{r3.status_code}")

        loc = r3.headers.get("Location", "")
        if loc.startswith("Cashier.aspx"):
            # The ASP.NET_SessionId cookie in our jar is the authenticated one.
            cookie_val = cookies.get(_SESSION_COOKIE)
            if not cookie_val:
                raise TransientBackendError(f"{driver_prefix}:login_no_session_cookie")
            return cookie_val

        # Failure redirect — parse `errtype` from the query string.
        qs = parse_qs(urlparse(loc).query)
        errtype = (qs.get("errtype") or [""])[0]
        last_errtype = errtype
        if errtype == "verifycode":
            continue                                # captcha wrong — restart attempt with fresh GET
        # Terminal: bad creds, banned IP, banned account, etc.
        code = login_errtype_to_code(errtype)
        raise BackendError(f"{driver_prefix}:login_failed:{code}")

    # Exhausted attempts (only reachable via repeated `verifycode`).
    raise BackendError(
        f"{driver_prefix}:captcha_failed_max_attempts (last_errtype={last_errtype})"
    )
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_login.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/_aspnet_cashier/login.py tests/unit/test_aspnet_login.py
git commit -m "feat(aspnet): captcha-aware login flow with bounded retry

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: Shared client (`_aspnet_cashier/client.py`)

This is the largest task. It pulls the session store, the login flow, the parsers, and the per-request HTTP logic into one class. Split into sub-steps per public method to keep tests focused.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/_aspnet_cashier/client.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_aspnet_client.py`

- [ ] **Step 1: Write failing tests for `get_or_login` (cache hit, cache miss, DCL)**

Create `tests/unit/test_aspnet_client.py`:

```python
import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"

_LOGIN_PAGE = """
<form><input type="hidden" name="__VIEWSTATE" value="VS_A" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="CA0B0334" />
<input type="hidden" name="__EVENTVALIDATION" value="EV_A" />
<img src="Tools/VerifyImagePage.aspx?1" /></form>
"""


def _mock_login_chain(cookie_value: str = "COOKIE_NEW"):
    """Stand up the GET-default + GET-captcha + POST-default chain returning success."""
    respx.get(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(
            200, text=_LOGIN_PAGE,
            headers={"Set-Cookie": f"ASP.NET_SessionId={cookie_value}; path=/"},
        )
    )
    respx.get(f"{BASE}/Tools/VerifyImagePage.aspx?1").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8FAKE")
    )
    respx.post(f"{BASE}/default.aspx").mock(
        return_value=httpx.Response(301, text="", headers={"Location": "Cashier.aspx"})
    )


def _client(http, store=None, captcha=None) -> AspnetCashierClient:
    return AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http,
        session_store=store or InMemoryCookieSessionStore(),
        captcha_solver=captcha or FakeCaptchaSolver(),
        game_id=42,
        session_ttl_seconds=1800,
        lock_ttl_seconds=20,
        lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3,
        driver_prefix="orionstars",
    )


@respx.mock
async def test_get_or_login_returns_cached_cookie_when_fresh():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="CACHED", expires_at=int(time.time()) + 3600),
        ttl_seconds=3600,
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "CACHED"
    # No HTTP calls were made
    assert len(respx.calls) == 0


@respx.mock
async def test_get_or_login_performs_login_on_cache_miss():
    _mock_login_chain("NEW_COOKIE")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http)
        cookie = await c.get_or_login()
    assert cookie == "NEW_COOKIE"


@respx.mock
async def test_get_or_login_treats_expired_cache_as_miss():
    store = InMemoryCookieSessionStore()
    await store.set(
        42, CachedSession(cookie="EXPIRED", expires_at=int(time.time()) - 60),
        ttl_seconds=60,
    )
    _mock_login_chain("REFRESHED")
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        cookie = await c.get_or_login()
    assert cookie == "REFRESHED"
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py -v`

- [ ] **Step 3: Implement minimal client with `get_or_login` only**

Create `app/backends/_aspnet_cashier/client.py`:

```python
import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends._aspnet_cashier.errors import classify_business_failure_message
from app.backends._aspnet_cashier.login import _BASE_HEADERS, _FORM_CT, _SESSION_COOKIE, login
from app.backends._aspnet_cashier.parsers import (
    parse_agent_balance_widget,
    parse_dialog_response,
    parse_get_score_response,
    parse_milkyway_balance_row,
    parse_sentinel,
    parse_update_select,
    parse_viewstate,
)
from app.backends._aspnet_cashier.session import CachedSession, SessionStore
from app.captcha.base import CaptchaSolver


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class AspnetCashierClient:
    """HTTP client shared by OrionStars + MilkyWay backends.

    Responsibilities: (1) session cache + double-checked locking around login,
    (2) cookie+Accept-Language injection on every request, (3) session-death detection
    with retry-once-after-relogin, (4) the AccountsList/dialog helpers used by ops.
    """

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: SessionStore,
        captcha_solver: CaptchaSolver,
        game_id: int,
        session_ttl_seconds: int,
        lock_ttl_seconds: int,
        lock_acquire_timeout_seconds: float,
        captcha_login_max_attempts: int,
        driver_prefix: str,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._captcha = captcha_solver
        self._game_id = game_id
        self._session_ttl = session_ttl_seconds
        self._lock_ttl = lock_ttl_seconds
        self._lock_acquire = lock_acquire_timeout_seconds
        self._max_attempts = captcha_login_max_attempts
        self._driver = driver_prefix

    # ---- session ----

    async def get_or_login(self) -> str:
        cached = await self._store.get(self._game_id)
        if not _expired(cached):
            return cached.cookie       # type: ignore[union-attr]
        try:
            async with self._store.login_lock(
                self._game_id, ttl_seconds=self._lock_ttl,
                acquire_timeout=self._lock_acquire,
            ):
                cached = await self._store.get(self._game_id)
                if not _expired(cached):
                    return cached.cookie       # type: ignore[union-attr]
                return await self._do_login()
        except TimeoutError:
            # Lock contention. The lock is efficiency-only here (sessions coexist), so
            # fall through to an unlocked login — a wasted captcha beats a failed op.
            return await self._do_login()

    async def _do_login(self) -> str:
        cookie = await login(
            http=self._http, base_url=self._base,
            username=self._username, password=self._password,
            captcha_solver=self._captcha,
            max_attempts=self._max_attempts,
            driver_prefix=self._driver,
        )
        await self._store.set(
            self._game_id,
            CachedSession(cookie=cookie, expires_at=int(time.time()) + self._session_ttl),
            ttl_seconds=self._session_ttl,
        )
        return cookie
```

- [ ] **Step 4: First three tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit progress**

```bash
git add app/backends/_aspnet_cashier/client.py tests/unit/test_aspnet_client.py
git commit -m "feat(aspnet): AspnetCashierClient get_or_login with DCL

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 6: Add tests for the `request()` helper (cookie + Accept-Language + session-death retry)**

Append to `tests/unit/test_aspnet_client.py`:

```python
# --- request() ---

_NRE_500 = """
<html><body>Server Error in '/' Application.
<br>System.NullReferenceException: Object reference not set...</body></html>
"""


@respx.mock
async def test_request_attaches_session_cookie_and_accept_language():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="USE_ME", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="0.00@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("0.00@0.00|")
    sent = route.calls.last.request
    assert sent.headers.get("accept-language", "").startswith("en")
    cookie_hdr = sent.headers.get("cookie", "")
    assert "ASP.NET_SessionId=USE_ME" in cookie_hdr


@respx.mock
async def test_request_retries_once_after_session_death_500_nre():
    """First call returns the NRE 500 (dead session). Client clears cache, re-logs in, retries."""
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    # Mount the login chain (relogin will fire)
    _mock_login_chain("REVIVED")
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            httpx.Response(500, text=_NRE_500),
            httpx.Response(200, text="9.99@0.00|<html/>"),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        body = await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                    form={"getscoreuserid": "1"})
    assert body.startswith("9.99@0.00|")
    assert route.call_count == 2
    # Second POST used the revived cookie
    second = route.calls[-1].request
    assert "ASP.NET_SessionId=REVIVED" in second.headers.get("cookie", "")


@respx.mock
async def test_request_does_not_retry_more_than_once_on_repeated_500():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    _mock_login_chain("REVIVED")
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(500, text=_NRE_500),
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        with pytest.raises(TransientBackendError):
            await c.request_text("POST", "/Module/AccountManager/AccountsList.aspx",
                                 form={"getscoreuserid": "1"})
```

(Import `TransientBackendError` at the top of the test file: `from app.backends.base import TransientBackendError`.)

- [ ] **Step 7: Extend client with `request_text()`**

Append to `app/backends/_aspnet_cashier/client.py`:

```python
    # ---- request ----

    async def request_text(
        self, method: str, path: str, *,
        form: dict[str, str | int] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> str:
        """One module-page request with cookie + Accept-Language. Retries exactly once
        if the response indicates a dead session (500 NRE, or 200 returning the login page).

        Returns the raw response text; callers parse it.
        """
        cookie = await self.get_or_login()
        resp = await self._do_request(method, path, cookie, form=form, params=params)
        if self._looks_dead(resp):
            await self._store.clear(self._game_id)
            cookie = await self.get_or_login()
            resp = await self._do_request(method, path, cookie, form=form, params=params)
            if self._looks_dead(resp):
                raise TransientBackendError(f"{self._driver}:session_dead_after_relogin")
        if resp.status_code >= 500:
            raise TransientBackendError(f"{self._driver}:http_{resp.status_code}")
        if resp.status_code >= 400 and resp.status_code != 301:
            raise BackendError(f"{self._driver}:http_{resp.status_code}")
        return resp.text

    async def _do_request(
        self, method: str, path: str, cookie: str, *,
        form: dict[str, str | int] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        url = f"{self._base}{path}" if path.startswith("/") else f"{self._base}/{path}"
        headers = {**_BASE_HEADERS}
        cookies = {_SESSION_COOKIE: cookie}
        try:
            if method == "GET":
                return await self._http.get(
                    url, params=_str_map(params or {}), headers=headers, cookies=cookies,
                    follow_redirects=False,
                )
            headers["Content-Type"] = _FORM_CT
            body = urlencode(_str_map(form or {})).encode()
            return await self._http.post(
                url, content=body, headers=headers, cookies=cookies, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{self._driver}:transport:{type(exc).__name__}") from exc

    @staticmethod
    def _looks_dead(resp: httpx.Response) -> bool:
        """True if the response looks like the dead-session NRE or the login page."""
        if resp.status_code == 500 and "Server Error in '/' Application" in resp.text:
            return True
        if resp.status_code == 200 and 'name="txtLoginName"' in resp.text:
            return True
        if resp.status_code == 301 and "errtype=overtime" in resp.headers.get("Location", ""):
            return True
        return False


def _str_map(d: dict) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}
```

- [ ] **Step 8: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py -v`
Expected: 6 passed.

- [ ] **Step 9: Commit**

```bash
git add app/backends/_aspnet_cashier/client.py tests/unit/test_aspnet_client.py
git commit -m "feat(aspnet): request_text with session-death detection + relogin

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

- [ ] **Step 10: Add tests for op-facing helpers (search, tourl, dialog GET/POST, agent_balance fetch)**

Append to `tests/unit/test_aspnet_client.py`:

```python
# --- op-facing helpers ---

_ACCOUNTS_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="ALV" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="CF7AEB79" />
  <div class="nav">Balance:42</div>
  <table>
    <tr><td><a onclick="updateSelect( '21041615,21219386')">Update</a></td></tr>
  </table>
</form>
"""

_DIALOG_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="DLG" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="DB3B1D51" />
  <input type="hidden" name="__EVENTVALIDATION" value="DLEV" />
</form>
"""


@respx.mock
async def test_search_returns_uid_gid_pairs():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        pairs = await c.search_account("Saud_Doe892")
    assert pairs == [("21041615", "21219386")]
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in body
    assert "txtSearch=Saud_Doe892" in body
    assert "ShowHideAccount=1" in body
    assert "__VIEWSTATE=ALV" in body
    assert "__EVENTVALIDATION" not in body                 # AccountsList: EnableEventValidation=false


@respx.mock
async def test_get_dialog_url_returns_url_and_token():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="Module/AccountManager/GrantTreasure.aspx?param=TOKENAAAA|<html/>",
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        url, token = await c.get_dialog_url(tourl=0, uid="21041615", gid="21219386")
    assert url == "Module/AccountManager/GrantTreasure.aspx?param=TOKENAAAA"
    assert token == "TOKENAAAA"


@respx.mock
async def test_submit_dialog_get_then_post_uses_scraped_viewstate():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOKEN").mock(
        return_value=httpx.Response(200, text=_DIALOG_HTML)
    )
    route = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOKEN").mock(
        return_value=httpx.Response(
            200,
            text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        text = await c.submit_dialog(
            dialog_url="Module/AccountManager/GrantTreasure.aspx?param=TOKEN",
            extra_fields={"txtAddGold": "1", "txtReason": ""},
        )
    assert "Confirmed successful" in text
    body = route.calls.last.request.content.decode()
    assert "__EVENTTARGET=Button1" in body
    assert "__VIEWSTATE=DLG" in body
    assert "__EVENTVALIDATION=DLEV" in body
    assert "txtAddGold=1" in body


@respx.mock
async def test_fetch_agent_balance_widget_returns_int_cents():
    store = InMemoryCookieSessionStore()
    await store.set(42, CachedSession(cookie="C", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_ACCOUNTS_LIST_HTML)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store)
        bal = await c.fetch_agent_balance_dollars()
    assert bal == 42
```

- [ ] **Step 11: Extend client with op-facing helpers**

Append to `app/backends/_aspnet_cashier/client.py`:

```python
    # ---- op-facing helpers ----

    async def fetch_accounts_list_html(self) -> str:
        """Authenticated GET of the workhorse panel. Used by agent_balance + search bootstrap."""
        return await self.request_text("GET", "/Module/AccountManager/AccountsList.aspx",
                                       params={"timestamp": str(int(time.time()))})

    async def search_account(self, query: str) -> list[tuple[str, str]]:
        """Run the ctl16 search and return all (uid, gid) pairs from the result HTML.

        `query` matches against both GameID and Account (server-side `LIKE 'x%'` per §4.8 SQL).
        """
        # First GET the page to scrape viewstate (AccountsList has no __EVENTVALIDATION).
        html = await self.fetch_accounts_list_html()
        vs = parse_viewstate(html)
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx",
            form={
                "__EVENTTARGET": "ctl16",
                "__EVENTARGUMENT": "",
                "__VIEWSTATE": vs.viewstate,
                "__VIEWSTATEGENERATOR": vs.viewstate_generator,
                "__SCROLLPOSITIONX": "0",
                "__SCROLLPOSITIONY": "0",
                "txtSearch": query,
                "ShowHideAccount": "1",
            },
        )
        return parse_update_select(body)

    async def get_dialog_url(self, *, tourl: int, uid: str, gid: str) -> tuple[str, str]:
        """POST the tourl handshake; returns (dialog_url, param_token)."""
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx",
            form={"tourl": str(tourl), "getpassuid": uid, "getpassgid": gid},
        )
        return parse_dialog_response(body)

    async def submit_dialog(
        self, *, dialog_url: str, extra_fields: dict[str, str],
    ) -> str:
        """GET the dialog page (scraping viewstate + __EVENTVALIDATION), then POST the action.

        `extra_fields` is the op-specific payload (txtAddGold/txtReason for money ops,
        txtConfirmPass/txtSureConfirmPass for reset). Returns the POST response text.
        """
        get_body = await self.request_text("GET", "/" + dialog_url if not dialog_url.startswith("/") else dialog_url)
        vs = parse_viewstate(get_body)
        form: dict[str, str] = {
            "__EVENTTARGET": "Button1",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__VIEWSTATEGENERATOR": vs.viewstate_generator,
        }
        if vs.event_validation is not None:
            form["__EVENTVALIDATION"] = vs.event_validation
        form.update(extra_fields)
        return await self.request_text(
            "POST", "/" + dialog_url if not dialog_url.startswith("/") else dialog_url, form=form,
        )

    async def fetch_agent_balance_dollars(self) -> int:
        """Read the agent's `Balance:NN` widget from AccountsList.aspx. Returns whole dollars."""
        html = await self.fetch_accounts_list_html()
        return parse_agent_balance_widget(html)

    async def post_getscoreuserid(self, uid: str) -> tuple[str, str]:
        """OrionStars-only: read player credit & total-win via the getscoreuserid POST."""
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx",
            form={"getscoreuserid": uid},
        )
        return parse_get_score_response(body)

    async def milkyway_read_balance(self, *, query: str) -> str:
        """MilkyWay-only: search and parse the Balance column from the matching row.

        `query` is the account name or the GameID — either matches the LIKE clause; for cached
        callers GameID is preferred (more selective).
        """
        html = await self.fetch_accounts_list_html()
        vs = parse_viewstate(html)
        body = await self.request_text(
            "POST", "/Module/AccountManager/AccountsList.aspx",
            form={
                "__EVENTTARGET": "ctl16",
                "__EVENTARGUMENT": "",
                "__VIEWSTATE": vs.viewstate,
                "__VIEWSTATEGENERATOR": vs.viewstate_generator,
                "__SCROLLPOSITIONX": "0",
                "__SCROLLPOSITIONY": "0",
                "txtSearch": query,
                "ShowHideAccount": "1",
            },
        )
        return parse_milkyway_balance_row(body, account=query)

    # ---- sentinel helper used by ops ----

    def classify(self, html: str) -> tuple[str, list[str]]:
        """Returns (kind, args). Raises BackendError on `kind == 'unknown'`."""
        kind, args = parse_sentinel(html)
        if kind == "unknown":
            raise BackendError(f"{self._driver}:unknown_sentinel:{html[:80]!r}")
        return kind, args

    def business_failure_to_error(self, message: str) -> BackendError:
        """Map a business-failure sentinel message to a driver-prefixed BackendError."""
        slug = classify_business_failure_message(message)
        return BackendError(f"{self._driver}:{slug}")
```

- [ ] **Step 12: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_aspnet_client.py -v`
Expected: 10 passed.

- [ ] **Step 13: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 14: Commit**

```bash
git add app/backends/_aspnet_cashier/client.py tests/unit/test_aspnet_client.py
git commit -m "feat(aspnet): op-facing helpers (search, tourl, dialog, balance)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: OrionStars backend module

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/orionstars/__init__.py`
- Create: `/Applications/development/python/casino-app-automation/app/backends/orionstars/backend.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_orionstars_backend.py`

- [ ] **Step 1: Write failing tests (one per op)**

Create `tests/unit/test_orionstars_backend.py`:

```python
import time

import httpx
import pytest
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.orionstars.backend import OrionStarsBackend
from tests.conftest import FakeCaptchaSolver

BASE = "https://os.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=42, name="OS Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestOS159", backend_password="Test@159872!!",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )


def _ctx(*, account: AccountIdentity | None = None, username: str | None = None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem-xyz", account_username=username,
    )


def _account(*, external: str | None = None, username: str = "Saud_Doe892") -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=42,
        username=username, external_user_id=external,
    )


def _make_backend(http):
    store = InMemoryCookieSessionStore()
    # Pre-seed a session so individual op tests don't have to mock the login chain.
    import asyncio
    asyncio.get_event_loop()  # ensures loop binding for direct .set on the in-memory store
    client = AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store,
        captcha_solver=FakeCaptchaSolver(),
        game_id=42, session_ttl_seconds=1800,
        lock_ttl_seconds=20, lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3, driver_prefix="orionstars",
    )
    return OrionStarsBackend(client), store


async def _seed_session(store):
    await store.set(42, CachedSession(cookie="SESS", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- read_balance ---

@respx.mock
async def test_read_balance_posts_getscoreuserid_and_returns_cents():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text="12.34@0.00|<html/>")
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account(external="21041615:21219386")))
    assert result.balance_cents == 1234


@respx.mock
async def test_read_balance_searches_when_external_user_id_missing():
    """No external_user_id -> search by username first to obtain UID:GID, then getscoreuserid."""
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        side_effect=[
            # 1: search response with one row
            httpx.Response(
                200,
                text="""<table><tr><td><a onclick="updateSelect( '111,222')">Update</a></td></tr></table>""",
            ),
            # 2: getscoreuserid response
            httpx.Response(200, text="7.50@0.00|<html/>"),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account(external=None)))
    assert result.balance_cents == 750
    # Confirm last POST used the UID returned by the search
    last_body = posts.calls[-1].request.content.decode()
    assert "getscoreuserid=111" in last_body


# --- agent_balance ---

@respx.mock
async def test_agent_balance_scrapes_widget():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text='<div>Balance:31</div>')
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents == 3100


# --- recharge ---

@respx.mock
async def test_recharge_full_flow_success():
    # tourl POST
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="Module/AccountManager/GrantTreasure.aspx?param=TOK|<html/>",
        )
    )
    # GET dialog
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="GTV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="DB3B1D51" />
                    <input type="hidden" name="__EVENTVALIDATION" value="GTEV" /></form>""",
        )
    )
    # POST dialog
    submit = respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=TOK").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:30");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.recharge(
            _ctx(account=_account(external="21041615:21219386")),
            amount_cents=100, bonus_cents=0, total_credit_cents=100,
        )
    # Spec: omit balance_cents (player balance isn't in this response)
    assert result.balance_cents is None
    sent = submit.calls.last.request.content.decode()
    assert "txtAddGold=1" in sent              # ceil(100/100) = 1
    assert "__EVENTTARGET=Button1" in sent
    # tourl POST sent the right indices
    tourl_body = posts.calls[0].request.content.decode()
    assert "tourl=0" in tourl_body
    assert "getpassuid=21041615" in tourl_body
    assert "getpassgid=21219386" in tourl_body


@respx.mock
async def test_recharge_insufficient_agent_funds_raises_terminal():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/GrantTreasure.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/GrantTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Sorry, the surplus money is insufficient!");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(
                _ctx(account=_account(external="111:222")),
                amount_cents=10_000_000, bonus_cents=0, total_credit_cents=10_000_000,
            )
    assert ei.value.reason == "orionstars:insufficient_agent_funds"


# --- redeem ---

@respx.mock
async def test_redeem_success_uses_ChangeTreasure_and_tourl_1():
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/ChangeTreasure.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/ChangeTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="19F86183" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/ChangeTreasure.aspx?param=T").mock(
        return_value=httpx.Response(
            200, text='<script>showAlter("Confirmed successful","Balance:31");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.redeem(_ctx(account=_account(external="111:222")), amount_cents=100)
    assert result.balance_cents is None
    tourl_body = posts.calls[0].request.content.decode()
    assert "tourl=1" in tourl_body


# --- reset_password ---

@respx.mock
async def test_reset_password_success():
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200, text="Module/AccountManager/ResetPassWord.aspx?param=T|<html/>",
        )
    )
    respx.get(f"{BASE}/Module/AccountManager/ResetPassWord.aspx?param=T").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="C02DB422" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    submit = respx.post(f"{BASE}/Module/AccountManager/ResetPassWord.aspx?param=T").mock(
        return_value=httpx.Response(200, text='<script>showAlter("Modified success!");</script>')
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.reset_password(_ctx(account=_account(external="111:222")))
    assert result.password and len(result.password) >= 5
    sent = submit.calls.last.request.content.decode()
    assert f"txtConfirmPass={result.password}" in sent
    assert f"txtSureConfirmPass={result.password}" in sent


# --- create_account ---

@respx.mock
async def test_create_account_does_followup_search_to_pack_external_user_id():
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="0E9FD35B" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200, text='<script>testAlter("Added successfully");</script>',
        )
    )
    # follow-up search: GET AccountsList -> POST search
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="ALV" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" /></form>""",
        )
    )
    respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(
            200,
            text="""<table><tr><td><a onclick="updateSelect( '99988877,77766655')">U</a></td></tr></table>""",
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        result = await backend.create_account(_ctx(username="ApiTest_0530"))
    assert result.username == "ApiTest_0530"
    assert result.external_user_id == "99988877:77766655"
    assert result.password


@respx.mock
async def test_create_account_existing_account_raises_terminal():
    respx.get(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text="""<form><input type="hidden" name="__VIEWSTATE" value="V" />
                    <input type="hidden" name="__VIEWSTATEGENERATOR" value="0E9FD35B" />
                    <input type="hidden" name="__EVENTVALIDATION" value="EV" /></form>""",
        )
    )
    respx.post(__import__("re").compile(r".*CreateAccount\.aspx.*")).mock(
        return_value=httpx.Response(
            200,
            text='<script>testAlter("The account number already exists, please re-enter it!");</script>',
        )
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.create_account(_ctx(username="duplicate"))
    assert ei.value.reason == "orionstars:account_exists"
```

- [ ] **Step 2: Run; expect mass failure**

Run: `.venv/bin/pytest tests/unit/test_orionstars_backend.py -v`

- [ ] **Step 3: Implement backend**

Create `app/backends/orionstars/__init__.py` (empty).

Create `app/backends/orionstars/backend.py`:

```python
import math
from datetime import datetime

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.passwords import generate_aspnet_password
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _to_cents(value: str | float) -> int:
    return round(float(value) * 100)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


def _now_query_param() -> str:
    # CreateAccount.aspx?time=<dd/MM/yyyy HH:mm:ss> (URL-encoded by httpx).
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


class OrionStarsBackend:
    """OrionStars cashier backend. Reads balance via the getscoreuserid POST."""

    def __init__(self, client: AspnetCashierClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        dollars = await self._client.fetch_agent_balance_dollars()
        return AgentBalanceResult(agent_balance_cents=dollars * 100)

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        uid, _gid = await self._player_ids(ctx)
        credit, _totalwin = await self._client.post_getscoreuserid(uid)
        return ReadBalanceResult(balance_cents=_to_cents(credit))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=2, uid=uid, gid=gid)
        pwd = generate_aspnet_password()
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtConfirmPass": pwd, "txtSureConfirmPass": pwd},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return ResetPasswordResult(password=pwd)
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=0, uid=uid, gid=gid)
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtAddGold": _to_dollars(amount_cents), "txtReason": ""},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return RechargeResult(balance_cents=None)   # player balance not in this response
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        uid, gid = await self._player_ids(ctx)
        dialog_url, _ = await self._client.get_dialog_url(tourl=1, uid=uid, gid=gid)
        text = await self._client.submit_dialog(
            dialog_url=dialog_url,
            extra_fields={"txtAddGold": _to_dollars(amount_cents), "txtReason": ""},
        )
        kind, args = self._client.classify(text)
        if kind == "success":
            return RedeemResult()
        raise self._client.business_failure_to_error(args[0] if args else "")

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError("orionstars:account_username_required")
        pwd = generate_aspnet_password()
        time_q = _now_query_param()
        # GET the form to scrape viewstate (CreateAccount has EnableEventValidation=true).
        get_body = await self._client.request_text(
            "GET", "/Module/AccountManager/CreateAccount.aspx", params={"time": time_q},
        )
        from app.backends._aspnet_cashier.parsers import parse_viewstate
        vs = parse_viewstate(get_body)
        form = {
            "__EVENTTARGET": "ctl07",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": vs.viewstate,
            "__VIEWSTATEGENERATOR": vs.viewstate_generator,
            "__EVENTVALIDATION": vs.event_validation or "",
            "txtAccount": username,
            "txtNickName": username,
            "txtLogonPass": pwd,
            "txtLogonPass2": pwd,
        }
        text = await self._client.request_text(
            "POST", "/Module/AccountManager/CreateAccount.aspx",
            params={"time": time_q}, form=form,
        )
        kind, args = self._client.classify(text)
        if kind != "success":
            raise self._client.business_failure_to_error(args[0] if args else "")
        # Follow-up search to obtain UID:GID for the new account.
        pairs = await self._client.search_account(username)
        if not pairs:
            raise BackendError("orionstars:create_followup_search_no_rows")
        uid, gid = pairs[0]
        return CreateAccountResult(
            username=username, password=pwd, external_user_id=f"{uid}:{gid}",
        )

    # ---- internal ----

    async def _player_ids(self, ctx: BackendContext) -> tuple[str, str]:
        """Return (UserID, GameID) for ctx.account: split cached external_user_id or search."""
        if ctx.account and ctx.account.external_user_id and ":" in ctx.account.external_user_id:
            uid, gid = ctx.account.external_user_id.split(":", 1)
            return uid, gid
        if ctx.account and ctx.account.username:
            pairs = await self._client.search_account(ctx.account.username)
            if pairs:
                return pairs[0]
            raise BackendError("orionstars:player_not_found")
        raise BackendError("orionstars:player_not_found")
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_orionstars_backend.py -v`
Expected: 8 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/orionstars/ tests/unit/test_orionstars_backend.py
git commit -m "feat(orionstars): backend with all 6 ops over _aspnet_cashier

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: MilkyWay backend module

Mirrors OrionStars; the only divergence is `read_balance` (search-row Balance column).

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/milkyway/__init__.py`
- Create: `/Applications/development/python/casino-app-automation/app/backends/milkyway/backend.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_milkyway_backend.py`

- [ ] **Step 1: Write failing tests focused on the divergence**

Create `tests/unit/test_milkyway_backend.py`:

```python
import time

import httpx
import respx

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CachedSession, InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.milkyway.backend import MilkyWayBackend
from tests.conftest import FakeCaptchaSolver

BASE = "https://mw.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=43, name="MW Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestMW159", backend_password="Test@159872!!",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )


def _account(external: str | None = None) -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=43,
        username="Saud_Doe892", external_user_id=external,
    )


def _ctx(account=None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem", account_username="Saud_Doe892",
    )


def _make_backend(http):
    store = InMemoryCookieSessionStore()
    client = AspnetCashierClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store,
        captcha_solver=FakeCaptchaSolver(),
        game_id=43, session_ttl_seconds=1800,
        lock_ttl_seconds=20, lock_acquire_timeout_seconds=5.0,
        captcha_login_max_attempts=3, driver_prefix="milkyway",
    )
    return MilkyWayBackend(client), store


_MW_LIST_HTML = """
<form id="form1">
  <input type="hidden" name="__VIEWSTATE" value="V" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" value="G" />
</form>
"""

_MW_SEARCH_RESULT = """
<table>
  <tr>
    <td><a onclick="updateSelect( '21041615,21219386')">Update</a></td>
    <td>21219386</td>
    <td>Saud_Doe892</td>
    <td>Saud</td>
    <td>456.78</td>
    <td>2026-05-30</td>
    <td>2026-06-01</td>
    <td>TestMW159</td>
    <td>Active</td>
  </tr>
</table>
"""


@respx.mock
async def test_milkyway_read_balance_parses_row_no_getscoreuserid_call():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_LIST_HTML)
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_SEARCH_RESULT)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await store.set(43, CachedSession(cookie="S", expires_at=int(time.time()) + 3600),
                        ttl_seconds=3600)
        result = await backend.read_balance(_ctx(account=_account(external="21041615:21219386")))
    assert result.balance_cents == 45678
    # Verify the POST body is the ctl16 search (NOT getscoreuserid).
    sent = posts.calls.last.request.content.decode()
    assert "__EVENTTARGET=ctl16" in sent
    assert "getscoreuserid" not in sent
    # Cached external -> GameID portion (more selective) used as txtSearch.
    assert "txtSearch=21219386" in sent


@respx.mock
async def test_milkyway_read_balance_uses_username_when_external_missing():
    respx.get(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_LIST_HTML)
    )
    posts = respx.post(f"{BASE}/Module/AccountManager/AccountsList.aspx").mock(
        return_value=httpx.Response(200, text=_MW_SEARCH_RESULT)
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http)
        await store.set(43, CachedSession(cookie="S", expires_at=int(time.time()) + 3600),
                        ttl_seconds=3600)
        result = await backend.read_balance(_ctx(account=_account(external=None)))
    assert result.balance_cents == 45678
    sent = posts.calls.last.request.content.decode()
    assert "txtSearch=Saud_Doe892" in sent
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_milkyway_backend.py -v`

- [ ] **Step 3: Implement**

Create `app/backends/milkyway/__init__.py` (empty).

Create `app/backends/milkyway/backend.py`:

```python
from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends.context import BackendContext
from app.backends.orionstars.backend import OrionStarsBackend, _to_cents
from app.schemas.results import ReadBalanceResult


class MilkyWayBackend(OrionStarsBackend):
    """MilkyWay portal (same 3.0.303 build as OrionStars).

    Only divergence vs. OrionStars: `read_balance` bypasses `getscoreuserid` (which
    re-renders the page without the `credit@totalwin|` prefix on MilkyWay) and instead
    parses the Balance column directly from the ctl16 search result row.
    See findings doc §4.1 portal-difference note.
    """

    def __init__(self, client: AspnetCashierClient) -> None:
        super().__init__(client)

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        # Prefer GameID as the search query when external_user_id is cached (more selective
        # than account name); fall back to the account username when nothing is cached.
        query: str
        if ctx.account and ctx.account.external_user_id and ":" in ctx.account.external_user_id:
            _uid, gid = ctx.account.external_user_id.split(":", 1)
            query = gid
        elif ctx.account and ctx.account.username:
            query = ctx.account.username
        else:
            from app.backends.base import BackendError
            raise BackendError("milkyway:player_not_found")
        credit = await self._client.milkyway_read_balance(query=query)
        return ReadBalanceResult(balance_cents=_to_cents(credit))
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_milkyway_backend.py -v`
Expected: 2 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/milkyway/ tests/unit/test_milkyway_backend.py
git commit -m "feat(milkyway): backend extends OrionStars with row-Balance read

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 14: Registry wiring

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/backends/registry.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_registry.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_registry.py`:

```python
def test_orionstars_and_milkyway_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "orionstars" in NON_IDEMPOTENT_DRIVERS
    assert "milkyway" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_orionstars_returns_orionstars_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.orionstars.backend import OrionStarsBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="OS",
        backend_url="https://os.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "orionstars", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, OrionStarsBackend)


async def test_resolve_milkyway_returns_milkyway_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.milkyway.backend import MilkyWayBackend
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=100, name="MW",
        backend_url="https://mw.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )
    settings = Settings(anticaptcha_api_key="testkey")
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "milkyway", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, MilkyWayBackend)


async def test_resolve_orionstars_requires_anticaptcha_key(fake_redis):
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="OS",
        backend_url="https://os.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    settings = Settings(anticaptcha_api_key="")
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_anticaptcha_api_key"):
            resolve_backend(
                "orionstars", credentials=creds, http_client=http,
                settings=settings, redis=fake_redis,
            )
```

- [ ] **Step 2: Run; expect failures**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v`

- [ ] **Step 3: Update registry**

Edit `app/backends/registry.py`. Update the `NON_IDEMPOTENT_DRIVERS` line:

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({
    "gameroom", "goldentreasure", "orionstars", "milkyway",
})
```

Add imports near the top:

```python
from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import CookieSessionStore
from app.backends.milkyway.backend import MilkyWayBackend
from app.backends.orionstars.backend import OrionStarsBackend
from app.captcha.anticaptcha import AntiCaptchaSolver
```

Add two new branches to `resolve_backend`, just before the trailing `raise BackendError(...)`:

```python
    if key in {"orionstars", "milkyway"}:
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError(f"missing_{key}_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        if not settings.anticaptcha_api_key:
            raise BackendError("missing_anticaptcha_api_key")
        client = AspnetCashierClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=CookieSessionStore(redis),
            captcha_solver=AntiCaptchaSolver(api_key=settings.anticaptcha_api_key),
            game_id=credentials.game_id,
            session_ttl_seconds=settings.aspnet_session_ttl_seconds,
            lock_ttl_seconds=settings.aspnet_lock_ttl_seconds,
            lock_acquire_timeout_seconds=settings.aspnet_lock_acquire_timeout_seconds,
            captcha_login_max_attempts=settings.captcha_login_max_attempts,
            driver_prefix=key,
        )
        return OrionStarsBackend(client) if key == "orionstars" else MilkyWayBackend(client)
```

Update the docstring for `resolve_backend` to mention the two new drivers.

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v`
Expected: previously passing + 4 new tests pass.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(registry): wire orionstars + milkyway drivers

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 15: Logging redaction

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/logging.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_logging.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_logging.py`:

```python
def test_aspnet_password_fields_are_redacted():
    from app.logging import _redact_in_place
    d = {
        "txtLoginPass": "secret1",
        "txtLogonPass": "secret2",
        "txtLogonPass2": "secret2",
        "txtConfirmPass": "secret3",
        "txtSureConfirmPass": "secret3",
        "ASP.NET_SessionId": "ABC123",
        "anticaptcha_api_key": "key",
        "other": "visible",
    }
    _redact_in_place(d)
    assert d["txtLoginPass"] == "***"
    assert d["txtLogonPass"] == "***"
    assert d["txtLogonPass2"] == "***"
    assert d["txtConfirmPass"] == "***"
    assert d["txtSureConfirmPass"] == "***"
    assert d["ASP.NET_SessionId"] == "***"
    assert d["anticaptcha_api_key"] == "***"
    assert d["other"] == "visible"
```

- [ ] **Step 2: Run; expect failure**

Run: `.venv/bin/pytest tests/unit/test_logging.py -v`
Expected: fails — the new keys aren't redacted yet.

- [ ] **Step 3: Add the keys**

Edit `app/logging.py`. Extend `SECRET_KEYS` (around line 9):

```python
SECRET_KEYS = {
    "password",
    "pwd",
    "login_pwd",
    "backend_password",
    "api_secret_key",
    "binding_key",
    "secret",
    "token",
    "x-signature",
    "x-token",
    # Phase 5: ASP.NET cashier form fields + session cookie + AntiCaptcha key
    "txtloginpass",
    "txtlogonpass",
    "txtlogonpass2",
    "txtconfirmpass",
    "txtsureconfirmpass",
    "asp.net_sessionid",
    "anticaptcha_api_key",
}
```

(Note: `_redact_in_place` already lowercases the key on lookup — `key.lower() in SECRET_KEYS` — so we store the lowercase form here. The matching test asserts the original-case keys are redacted via the lowercase membership check.)

- [ ] **Step 4: Test passes**

Run: `.venv/bin/pytest tests/unit/test_logging.py -v`
Expected: all green.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/logging.py tests/unit/test_logging.py
git commit -m "log(phase5): redact aspnet password fields + session cookie + anticaptcha key

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 16: Live-gated integration test scaffolding

These tests exercise the full path against the real OrionStars and MilkyWay portals. They are skipped unless both `ANTICAPTCHA_API_KEY` and per-portal `_TEST_AGENT_USER` / `_TEST_AGENT_PASS` / `_TEST_BASE_URL` env vars are set.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_orionstars_integration.py`
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_milkyway_integration.py`

- [ ] **Step 1: Create OrionStars live test**

Create `tests/integration/test_orionstars_integration.py`:

```python
"""Live-gated end-to-end test against the real OrionStars portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  ORIONSTARS_TEST_BASE_URL    e.g. https://orionstars.vip:8781
  ORIONSTARS_TEST_AGENT_USER  e.g. TestOS159
  ORIONSTARS_TEST_AGENT_PASS  e.g. Test@159872!!
  ORIONSTARS_TEST_PLAYER      e.g. Saud_Doe892   (must already exist under the agent)

Costs ~$0.001 per login (one AntiCaptcha solve). Run manually with:
  .venv/bin/pytest tests/integration/test_orionstars_integration.py -v
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.orionstars.backend import OrionStarsBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "ORIONSTARS_TEST_BASE_URL",
    "ORIONSTARS_TEST_AGENT_USER", "ORIONSTARS_TEST_AGENT_PASS",
    "ORIONSTARS_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["ORIONSTARS_TEST_BASE_URL"]
    user = os.environ["ORIONSTARS_TEST_AGENT_USER"]
    pwd = os.environ["ORIONSTARS_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9999, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="orionstars",
        )
        yield OrionStarsBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9999, name="OS Live",
        backend_url=os.environ["ORIONSTARS_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["ORIONSTARS_TEST_AGENT_USER"],
        backend_password=os.environ["ORIONSTARS_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="orionstars",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,        # forces search-by-username path
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents


async def test_live_reset_password_then_login_unaffected(backend):
    """Reset the test player's password. This is destructive — only run with a disposable player."""
    player = os.environ["ORIONSTARS_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.reset_password(_ctx(account=account))
    assert result.password and len(result.password) >= 5
```

- [ ] **Step 2: Create MilkyWay live test**

Create `tests/integration/test_milkyway_integration.py` as a copy of the OrionStars test but:
- Module docstring references MilkyWay and the `MILKYWAY_TEST_*` env vars.
- `_required` list uses the `MILKYWAY_*` prefix.
- Fixture wires `MilkyWayBackend` (import `from app.backends.milkyway.backend import MilkyWayBackend`).
- `driver_prefix="milkyway"`.
- `GameCredentials.backend_driver="milkyway"`.

Full file content:

```python
"""Live-gated end-to-end test against the real MilkyWay portal.

Skipped unless all of these are set:
  ANTICAPTCHA_API_KEY
  MILKYWAY_TEST_BASE_URL      e.g. https://milkywayapp.xyz:8781
  MILKYWAY_TEST_AGENT_USER    e.g. TestMW159
  MILKYWAY_TEST_AGENT_PASS
  MILKYWAY_TEST_PLAYER

Costs ~$0.001 per login (one AntiCaptcha solve). Run manually with:
  .venv/bin/pytest tests/integration/test_milkyway_integration.py -v
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.backends._aspnet_cashier.client import AspnetCashierClient
from app.backends._aspnet_cashier.session import InMemoryCookieSessionStore
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.milkyway.backend import MilkyWayBackend
from app.captcha.anticaptcha import AntiCaptchaSolver

_required = [
    "ANTICAPTCHA_API_KEY", "MILKYWAY_TEST_BASE_URL",
    "MILKYWAY_TEST_AGENT_USER", "MILKYWAY_TEST_AGENT_PASS",
    "MILKYWAY_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["MILKYWAY_TEST_BASE_URL"]
    user = os.environ["MILKYWAY_TEST_AGENT_USER"]
    pwd = os.environ["MILKYWAY_TEST_AGENT_PASS"]
    async with httpx.AsyncClient(timeout=60.0) as http:
        client = AspnetCashierClient(
            base_url=base, username=user, password=pwd,
            http_client=http,
            session_store=InMemoryCookieSessionStore(),
            captcha_solver=AntiCaptchaSolver(api_key=os.environ["ANTICAPTCHA_API_KEY"]),
            game_id=9998, session_ttl_seconds=1800,
            lock_ttl_seconds=20, lock_acquire_timeout_seconds=30.0,
            captcha_login_max_attempts=3, driver_prefix="milkyway",
        )
        yield MilkyWayBackend(client)


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9998, name="MW Live",
        backend_url=os.environ["MILKYWAY_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["MILKYWAY_TEST_AGENT_USER"],
        backend_password=os.environ["MILKYWAY_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="milkyway",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["MILKYWAY_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["MILKYWAY_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents
```

- [ ] **Step 3: Run; both should skip (no env vars set in CI)**

Run: `.venv/bin/pytest tests/integration/test_orionstars_integration.py tests/integration/test_milkyway_integration.py -v`
Expected: 7 skipped (4 + 3), 0 failed.

- [ ] **Step 4: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; live tests skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_orionstars_integration.py tests/integration/test_milkyway_integration.py
git commit -m "test(phase5): live-gated integration scaffolding for both portals

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Final task: Manual verification and merge

This is the user's gate — same workflow as the prior four phases.

- [ ] **Step 1: Verify the suite is fully green**

Run: `make lint && make type && make test`
Expected: all green; ~250-260 tests pass; the 7 live tests are skipped.

- [ ] **Step 2: Push the branch**

Run: `git push -u origin feat/phase5-orionstars-milkyway`

- [ ] **Step 3: Hand off to the user**

Tell the user:
> Phase 5 is implemented on `feat/phase5-orionstars-milkyway`. Suite: `<N>` tests passing, lint + mypy clean. Live integration tests are scaffolded but skipped (set `ANTICAPTCHA_API_KEY` + `ORIONSTARS_TEST_*` / `MILKYWAY_TEST_*` env vars to exercise the real portals). Ready for manual verification against real agent accounts — let me know when to merge to `main`.

Do **not** merge to main without the user's explicit go-ahead.

---

## Self-review checklist (run after completing the plan, before handing off)

- [ ] **Spec coverage:** Walk each section of `docs/superpowers/specs/2026-06-08-phase5-orionstars-milkyway-design.md` and confirm a task covers it (Architecture → Tasks 2, 5, 7, 10, 11, 12, 13; Data flow → Tasks 12, 13; Login & session → Tasks 10, 11; Errors → Tasks 8, 12, 13; Config → Task 6; Logging → Task 15; Testing → all tasks + Task 16).
- [ ] **NON_IDEMPOTENT_DRIVERS update applied** (Task 14).
- [ ] **No silent type drift:** `CaptchaSolver.solve_numeric_image(bytes) -> str` is the same name in Task 2, 3, 4, 10, 11.
- [ ] **All commits land on `feat/phase5-orionstars-milkyway`** — verify with `git log feat/phase5-orionstars-milkyway --oneline`.
- [ ] **All bullets above are exact instructions, not placeholders.**
