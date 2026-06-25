# YOLO777 Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `yolo` game backend driver (YOLO777 Laravel/Dcat agent panel) implementing the `GameBackend` protocol, wired into the registry/preflight/config.

**Architecture:** New self-contained `app/backends/yolo/` package mirroring the gameroom pattern: session-cookie + scraped CSRF token auth (Redis-cached, double-checked-locking login, best-effort re-login on auth failure), form-urlencoded writes returning JSON envelopes, HTML/number reads. Dollar-native money. Non-idempotent (no `order_id`).

**Tech Stack:** Python 3.12, httpx (async), SQLAlchemy (unused here), redis.asyncio, pydantic v2 result models, pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-06-18-yolo-backend-design.md`

## Global Constraints

- Money is **dollar-native**: backend `recharge`/`redeem` take `*, amount: int` (whole dollars); result balances are `float` dollars. No cents anywhere.
- `GameBackend` protocol methods: `create_account(ctx)`, `read_balance(ctx)`, `reset_password(ctx)`, `recharge(ctx, *, amount: int)`, `redeem(ctx, *, amount: int)`, `agent_balance(ctx)`.
- Result models (`app/schemas/results.py`): `CreateAccountResult(username, password, external_user_id=None)`, `ReadBalanceResult(balance: float)`, `ResetPasswordResult(password)`, `RechargeResult(balance: float | None = None)`, `RedeemResult(balance: float | None = None)`, `AgentBalanceResult(agent_balance: float)`.
- Errors: `BackendError(reason)` = terminal (cached by executor); `TransientBackendError(reason)` = retry-worthy (not cached). Both from `app.backends.base`.
- `BackendContext` fields: `.credentials` (`GameCredentials`: `game_id, backend_url, backend_username, backend_password, …`), `.account` (`AccountIdentity | None`: `username, external_user_id, …`), `.account_username: str | None`, `.idempotency_key: str`.
- Tests run with `pytest` (asyncio_mode=auto — async tests need no decorator marker, but match existing files' style). Use `respx` for httpx mocking.
- Run scoped tests with `.venv/bin/python -m pytest <path> -v`. Final gates: `.venv/bin/python -m pytest -q`, `.venv/bin/ruff check app tests`, `.venv/bin/mypy app`.

## File Structure

**Create:**
- `app/backends/yolo/__init__.py` (empty)
- `app/backends/yolo/session.py` — `CachedSession`, `SessionStore`, `InMemorySessionStore`, `RedisSessionStore`
- `app/backends/yolo/parsers.py` — HTML/text parsers
- `app/backends/yolo/errors.py` — envelope → reason mapping + auth-failure detection
- `app/backends/yolo/passwords.py` — re-export memorable password generator
- `app/backends/yolo/client.py` — `YoloClient`
- `app/backends/yolo/backend.py` — `YoloBackend`
- `tests/unit/test_yolo_session.py`, `test_yolo_parsers.py`, `test_yolo_errors.py`, `test_yolo_passwords.py`, `test_yolo_client.py`, `test_yolo_backend.py`
- `tests/integration/test_yolo_integration.py` (live-gated)

**Modify:**
- `app/backends/registry.py` — `yolo` branch + add to `NON_IDEMPOTENT_DRIVERS`
- `app/preflight/checks.py` — add `yolo` to the session-family credential check
- `app/config.py` — `yolo_*` settings
- `app/logging.py` — extend `SECRET_KEYS`
- `tests/unit/test_registry.py`, `tests/unit/test_preflight.py`, `tests/unit/test_config.py`, `tests/unit/test_logging.py`

---

## Phase 0 — Branch

### Task 0: Branch + commit spec & plan

- [ ] **Step 1: Branch from main**

```bash
cd /Applications/development/python/usgamingclub
git checkout main && git pull --ff-only 2>/dev/null; git checkout -b feat/yolo-backend
```

- [ ] **Step 2: Commit spec + plan**

```bash
git add docs/superpowers/specs/2026-06-18-yolo-backend-design.md docs/superpowers/plans/2026-06-18-yolo-backend.md
git commit -m "docs(yolo): backend design + implementation plan"
```

---

## Phase 1 — Leaf modules (no intra-package deps)

### Task 1: Session store

**Files:**
- Create: `app/backends/yolo/__init__.py` (empty), `app/backends/yolo/session.py`
- Test: `tests/unit/test_yolo_session.py`

**Interfaces:**
- Produces: `CachedSession(cookies: dict[str,str], csrf_token: str, expires_at: int)`;
  `SessionStore` Protocol with `get/set/clear(game_id)` + `login_lock(game_id, *, ttl_seconds, poll_seconds, acquire_timeout)` async CM;
  `InMemorySessionStore`; `RedisSessionStore(redis)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_yolo_session.py
import asyncio

import pytest

from app.backends.yolo.session import CachedSession, InMemorySessionStore, RedisSessionStore


async def test_inmemory_set_get_clear():
    store = InMemorySessionStore()
    assert await store.get(1) is None
    s = CachedSession(cookies={"laravel_session": "abc"}, csrf_token="tok", expires_at=999)
    await store.set(1, s, ttl_seconds=60)
    got = await store.get(1)
    assert got.cookies == {"laravel_session": "abc"} and got.csrf_token == "tok"
    await store.clear(1)
    assert await store.get(1) is None


async def test_inmemory_login_lock_serializes():
    store = InMemorySessionStore()
    order = []

    async def worker(n):
        async with store.login_lock(1, acquire_timeout=2.0):
            order.append(("in", n))
            await asyncio.sleep(0.05)
            order.append(("out", n))

    await asyncio.gather(worker(1), worker(2))
    # No interleaving: each in/out pair is contiguous.
    assert order[0][0] == "in" and order[1][0] == "out"
    assert order[2][0] == "in" and order[3][0] == "out"


async def test_redis_roundtrip_and_lock(fake_redis):
    store = RedisSessionStore(fake_redis)
    s = CachedSession(cookies={"laravel_session": "abc", "XSRF-TOKEN": "x"}, csrf_token="tok", expires_at=123)
    await store.set(7, s, ttl_seconds=60)
    got = await store.get(7)
    assert got == s
    # Lock is exclusive while held.
    async with store.login_lock(7, ttl_seconds=5, acquire_timeout=0.3):
        with pytest.raises(TimeoutError):
            async with store.login_lock(7, ttl_seconds=5, acquire_timeout=0.3):
                pass
    await store.clear(7)
    assert await store.get(7) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_session.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/session.py`**

```python
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    cookies: dict[str, str]
    csrf_token: str
    expires_at: int          # unix seconds; when we consider the cached session stale


class SessionStore(Protocol):
    async def get(self, game_id: int) -> "CachedSession | None": ...
    async def set(self, game_id: int, session: "CachedSession", ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 10,
        poll_seconds: float = 0.1, acquire_timeout: float = 10.0,
    ): ...


class InMemorySessionStore:
    """Process-local store with an in-process asyncio.Lock per game. Tests / single-process."""

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
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 10,
                         poll_seconds: float = 0.1, acquire_timeout: float = 10.0):
        lock = self._locks.setdefault(game_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"yolo login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"yolo_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"yolo_login:{game_id}"


class RedisSessionStore:
    """Redis-backed session store + SET NX login lock. Shared across all workers."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, game_id: int) -> CachedSession | None:
        raw = await self._redis.get(_key_session(game_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedSession(cookies=d["cookies"], csrf_token=d["csrf_token"], expires_at=int(d["expires_at"]))

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        raw = json.dumps({
            "cookies": session.cookies, "csrf_token": session.csrf_token,
            "expires_at": session.expires_at,
        })
        await self._redis.set(_key_session(game_id), raw, ex=max(1, ttl_seconds))

    async def clear(self, game_id: int) -> None:
        await self._redis.delete(_key_session(game_id))

    @asynccontextmanager
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 10,
                         poll_seconds: float = 0.1, acquire_timeout: float = 10.0):
        key = _key_lock(game_id)
        deadline = time.monotonic() + acquire_timeout
        acquired = False
        while True:
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"yolo login lock acquire timeout (game_id={game_id})")
            await asyncio.sleep(poll_seconds)
        try:
            yield
        finally:
            if acquired:
                try:
                    await self._redis.delete(key)
                except Exception:  # noqa: BLE001 - best-effort; TTL backs us up
                    pass
```

Also create empty `app/backends/yolo/__init__.py`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_session.py -v`
Expected: PASS (3 tests). The `fake_redis` fixture is defined in `tests/conftest.py`.

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/__init__.py app/backends/yolo/session.py tests/unit/test_yolo_session.py
git commit -m "feat(yolo): session store (cookies + csrf) with redis lock"
```

### Task 2: Parsers

**Files:**
- Create: `app/backends/yolo/parsers.py`
- Test: `tests/unit/test_yolo_parsers.py`

**Interfaces:**
- Produces: `parse_agent_score(text: str) -> float`; `parse_player_row(html: str, *, account: str) -> tuple[str, float]` (raises `BackendError("yolo:player_not_found")`); `parse_csrf_token(html: str) -> str` (raises `TransientBackendError("yolo:csrf_token_not_found")`); `looks_like_login_page(text: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_yolo_parsers.py
import pytest

from app.backends.base import BackendError, TransientBackendError
from app.backends.yolo.parsers import (
    looks_like_login_page,
    parse_agent_score,
    parse_csrf_token,
    parse_player_row,
)

# Realistic single-row player_list grid fragment. Column order per findings §3:
# Action | Player ID | Account | nickname | AgentAccount | KindName | Player Score | ...
_GRID = """
<table><tbody>
<tr>
  <td><a href="#">edit</a></td>
  <td>922952</td>
  <td><span data-content="apitest102"></span>&nbsp;apitest102</td>
  <td>nick102</td>
  <td>webyolo1</td>
  <td>Member</td>
  <td>123.45</td>
  <td>1000</td><td>900</td><td>5</td><td>3</td><td>2</td>
  <td>Normal</td><td>0.0.0.0</td><td>2026-01-01</td><td>2026-06-01</td>
</tr>
</tbody></table>
"""

_LOGIN_PAGE = """
<html><body><form action="/admin/auth/login" method="post">
<input name="username"><input name="password" type="password">
<script>window.Dcat = {token: "LOGIN_TOK_123"}; Dcat.token = "LOGIN_TOK_123";</script>
</form></body></html>
"""


def test_parse_agent_score():
    assert parse_agent_score("10.00\n") == 10.0
    assert parse_agent_score("  7 ") == 7.0


def test_parse_player_row_match():
    uid, score = parse_player_row(_GRID, account="apitest102")
    assert uid == "922952" and score == 123.45


def test_parse_player_row_no_match_raises():
    with pytest.raises(BackendError, match="player_not_found"):
        parse_player_row(_GRID, account="someone_else")


def test_parse_csrf_token():
    assert parse_csrf_token('foo Dcat.token = "ABC123" bar') == "ABC123"


def test_parse_csrf_token_missing_raises():
    with pytest.raises(TransientBackendError, match="csrf_token_not_found"):
        parse_csrf_token("<html>no token here</html>")


def test_looks_like_login_page():
    assert looks_like_login_page(_LOGIN_PAGE) is True
    assert looks_like_login_page(_GRID) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_parsers.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/parsers.py`**

```python
import re

from app.backends.base import BackendError, TransientBackendError

_CSRF_RE = re.compile(r'Dcat\.token\s*=\s*"([^"]+)"')
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Column indices in the player_list grid (findings §3). 0 = Action.
_COL_PLAYER_ID = 1
_COL_ACCOUNT = 2
_COL_SCORE = 6


def _strip(cell: str) -> str:
    text = _TAG_RE.sub("", cell)
    return text.replace("&nbsp;", " ").strip()


def parse_agent_score(text: str) -> float:
    """`GET /admin/refresh_score` returns the agent balance as a bare number string."""
    return float(text.strip())


def parse_player_row(html: str, *, account: str) -> tuple[str, float]:
    """Find the grid row whose Account column matches `account`; return (player_id, player_score).

    Account cells render as `<span data-content="acct"></span>&nbsp;acct`; we compare the
    visible (tag-stripped) text. Raises BackendError if no row matches.
    """
    for row in _ROW_RE.finditer(html):
        tds = [_strip(m.group(1)) for m in _TD_RE.finditer(row.group(1))]
        if len(tds) <= _COL_SCORE:
            continue
        if tds[_COL_ACCOUNT] == account:
            try:
                score = float(tds[_COL_SCORE])
            except ValueError as exc:
                raise BackendError("yolo:player_score_unparseable") from exc
            return tds[_COL_PLAYER_ID], score
    raise BackendError("yolo:player_not_found")


def parse_csrf_token(html: str) -> str:
    """Scrape the per-session Dcat CSRF token from an admin page."""
    m = _CSRF_RE.search(html)
    if not m:
        raise TransientBackendError("yolo:csrf_token_not_found")
    return m.group(1)


def looks_like_login_page(text: str) -> bool:
    """Heuristic: an unauthenticated response renders the admin login form."""
    return "/admin/auth/login" in text and 'name="password"' in text
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_parsers.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/parsers.py tests/unit/test_yolo_parsers.py
git commit -m "feat(yolo): HTML/text parsers"
```

### Task 3: Error mapping

**Files:**
- Create: `app/backends/yolo/errors.py`
- Test: `tests/unit/test_yolo_errors.py`

**Interfaces:**
- Produces: `map_envelope(http_status: int, body: dict | None) -> dict` (returns success `data` dict, else raises `BackendError`/`TransientBackendError`); `looks_like_auth_failure(status_code: int, location: str, text: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_yolo_errors.py
import pytest

from app.backends.base import BackendError, TransientBackendError
from app.backends.yolo.errors import looks_like_auth_failure, map_envelope


def test_success_returns_data():
    body = {"status": True, "data": {"message": "success", "type": "success"}}
    assert map_envelope(200, body) == {"message": "success", "type": "success"}


def test_business_error_insufficient_terminal():
    body = {"status": False, "data": {"message": "The score is insufficient", "type": "error"}}
    with pytest.raises(BackendError, match="yolo:insufficient_balance"):
        map_envelope(200, body)


def test_validation_account_exists_terminal():
    body = {"status": False, "data": [], "errors": {"Accounts": ["The Accounts has already been taken."]}}
    with pytest.raises(BackendError, match="yolo:account_exists"):
        map_envelope(422, body)


def test_validation_password_too_short_terminal():
    body = {"status": False, "data": [], "errors": {"password": ["The Password must be at least 6 characters."]}}
    with pytest.raises(BackendError, match="yolo:too_short"):
        map_envelope(422, body)


def test_server_error_transient():
    with pytest.raises(TransientBackendError):
        map_envelope(500, None)


def test_non_json_transient():
    with pytest.raises(TransientBackendError):
        map_envelope(200, None)


def test_auth_failure_detection():
    assert looks_like_auth_failure(401, "", "") is True
    assert looks_like_auth_failure(419, "", "") is True
    assert looks_like_auth_failure(302, "https://x/admin/auth/login", "") is True
    assert looks_like_auth_failure(200, "", "") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_errors.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/errors.py`**

```python
from app.backends.base import BackendError, TransientBackendError

# Substring (case-insensitive) -> terminal reason slug. Business (200+status:false) + validation (422).
_PATTERNS: list[tuple[str, str]] = [
    ("score is insufficient", "insufficient_balance"),
    ("already been taken", "account_exists"),
    ("format is invalid", "account_invalid"),
    ("at least 6 characters", "too_short"),
    ("field is required", "field_required"),
]


def _slug(message: str) -> str | None:
    low = (message or "").lower()
    for needle, slug in _PATTERNS:
        if needle in low:
            return slug
    return None


def map_envelope(http_status: int, body: dict | None) -> dict:
    """Classify a YOLO response. Returns the success `data` dict, or raises.

    Three envelopes (findings §7): 200+status:true success; 200+status:false business error
    (`data.message`); 422 validation error (`errors{}`). 5xx / non-JSON -> transient.
    """
    if http_status >= 500:
        raise TransientBackendError(f"yolo:http_{http_status}")
    if body is None:
        raise TransientBackendError("yolo:bad_response")

    if http_status == 422 or "errors" in body:
        errors = body.get("errors") or {}
        field, msgs = next(iter(errors.items()), ("", [""]))
        msg = msgs[0] if isinstance(msgs, list) and msgs else ""
        slug = _slug(msg)
        if slug:
            raise BackendError(f"yolo:{slug}")
        raise BackendError(f"yolo:validation_error: {field}: {msg[:60]}")

    if body.get("status") is True:
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # status:false business error
    data = body.get("data")
    msg = data.get("message", "") if isinstance(data, dict) else ""
    slug = _slug(msg)
    if slug:
        raise BackendError(f"yolo:{slug}")
    raise BackendError(f"yolo:business_error: {msg[:80]}")


def looks_like_auth_failure(status_code: int, location: str, text: str) -> bool:
    """True when a response indicates the admin session/CSRF is no longer valid."""
    if status_code in (401, 419):
        return True
    if status_code in (301, 302) and "/admin/auth/login" in (location or ""):
        return True
    # Lazy import to avoid a parsers<->errors cycle at module load.
    from app.backends.yolo.parsers import looks_like_login_page
    return looks_like_login_page(text or "")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_errors.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/errors.py tests/unit/test_yolo_errors.py
git commit -m "feat(yolo): response-envelope + auth-failure mapping"
```

### Task 4: Passwords

**Files:**
- Create: `app/backends/yolo/passwords.py`
- Test: `tests/unit/test_yolo_passwords.py`

**Interfaces:**
- Produces: `generate_memorable_password() -> str` (re-export; alphanumeric, ≥6 chars).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_yolo_passwords.py
from app.backends.yolo.passwords import generate_memorable_password


def test_password_is_alphanumeric_min6():
    for _ in range(20):
        pw = generate_memorable_password()
        assert len(pw) >= 6 and pw.isalnum()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_passwords.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/passwords.py`**

```python
# YOLO rules: Accounts/LogonPass/reset password all require alphanumeric, min 6 chars.
# The existing memorable generator (word + digits) satisfies this.
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_passwords.py -v`
Expected: PASS.

> If this fails because `generate_memorable_password` can emit a non-alphanumeric or <6-char value, STOP and report — do not weaken the test. (Verified: gamevault's generator is word+digits, ≥6 alnum.)

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/passwords.py tests/unit/test_yolo_passwords.py
git commit -m "feat(yolo): reuse memorable password generator"
```

---

## Phase 2 — Client

### Task 5: YoloClient (session + requests + re-login)

**Files:**
- Create: `app/backends/yolo/client.py`
- Test: `tests/unit/test_yolo_client.py`

**Interfaces:**
- Consumes: `session.{CachedSession,SessionStore,InMemorySessionStore}`, `errors.{map_envelope,looks_like_auth_failure}`, `parsers.{parse_csrf_token,looks_like_login_page}`.
- Produces: `YoloClient(*, base_url, username, password, http_client, session_store, game_id, session_ttl_seconds=1800, login_lock_ttl_seconds=10, login_lock_acquire_timeout_seconds=10.0)` with:
  - `async get_session(*, invalidate: CachedSession | None = None) -> CachedSession`
  - `async post_form(path: str, fields: dict) -> dict` (returns success `data` dict)
  - `async get_text(path: str, params: dict | None = None) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_yolo_client.py
import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import InMemorySessionStore

BASE = "https://yolo.test"
LOGIN_PAGE = '<form action="/admin/auth/login"><input name="password" type="password">' \
             '<script>Dcat.token = "TOK1";</script></form>'
ADMIN_PAGE = '<html>Dcat.token = "TOK1"; <span class="score">10.00</span></html>'


def _make_client(http):
    return YoloClient(
        base_url=BASE, username="webyolo1", password="Web@@1122",
        http_client=http, session_store=InMemorySessionStore(), game_id=1,
        session_ttl_seconds=1800,
    )


@respx.mock
async def test_login_then_post_form_success():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(
        return_value=httpx.Response(200, json={"status": True},
                                    headers={"Set-Cookie": "laravel_session=SESS1; path=/"}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    form = respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {"message": "success"}}))

    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        data = await client.post_form("/admin/dcat-api/form", {"type": 1, "input_score": 5})
    assert data == {"message": "success"}
    body = form.calls.last.request.content.decode()
    assert "_token=TOK1" in body and "input_score=5" in body
    assert form.calls.last.request.headers["X-CSRF-TOKEN"] == "TOK1"
    assert form.calls.last.request.headers["X-Requested-With"] == "XMLHttpRequest"


@respx.mock
async def test_business_error_raises_terminal():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": False, "data": {"message": "The score is insufficient"}}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="insufficient_balance"):
            await _make_client(http).post_form("/admin/dcat-api/form", {"type": 1})


@respx.mock
async def test_session_is_cached_login_happens_once():
    login = respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.post(f"{BASE}/admin/dcat-api/form").mock(
        return_value=httpx.Response(200, json={"status": True, "data": {}}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http)
        await client.post_form("/admin/dcat-api/form", {"type": 1})
        await client.post_form("/admin/dcat-api/form", {"type": 2})
    assert login.call_count == 1  # second call reused the cached session


@respx.mock
async def test_auth_failure_triggers_relogin_and_retry():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    # First write 419 (CSRF expired), second write succeeds.
    respx.post(f"{BASE}/admin/dcat-api/form").mock(side_effect=[
        httpx.Response(419, json={"message": "CSRF token mismatch"}),
        httpx.Response(200, json={"status": True, "data": {"message": "success"}}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).post_form("/admin/dcat-api/form", {"type": 1})
    assert data == {"message": "success"}


@respx.mock
async def test_login_failure_when_creds_rejected():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    # player_list still shows the login page => not authenticated.
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="login_failed"):
            await _make_client(http).get_text("/admin/refresh_score")


@respx.mock
async def test_get_text_returns_body():
    respx.get(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{BASE}/admin/auth/login").mock(return_value=httpx.Response(200, json={"status": True}))
    respx.get(f"{BASE}/admin/player_list").mock(return_value=httpx.Response(200, text=ADMIN_PAGE))
    respx.get(f"{BASE}/admin/refresh_score").mock(return_value=httpx.Response(200, text="10.00"))
    async with httpx.AsyncClient() as http:
        text = await _make_client(http).get_text("/admin/refresh_score")
    assert text.strip() == "10.00"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_client.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/client.py`**

```python
import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.yolo.errors import looks_like_auth_failure, map_envelope
from app.backends.yolo.parsers import looks_like_login_page, parse_csrf_token
from app.backends.yolo.session import CachedSession, SessionStore

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


def _str_map(d: dict) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}


class YoloClient:
    """Session-cookie + CSRF client for the YOLO777 Dcat admin panel."""

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient, session_store: SessionStore, game_id: int,
        session_ttl_seconds: int = 1800,
        login_lock_ttl_seconds: int = 10,
        login_lock_acquire_timeout_seconds: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._game_id = game_id
        self._ttl = session_ttl_seconds
        self._lock_ttl = login_lock_ttl_seconds
        self._lock_timeout = login_lock_acquire_timeout_seconds

    # ---- session ----

    async def get_session(self, *, invalidate: CachedSession | None = None) -> CachedSession:
        cached = await self._store.get(self._game_id)
        if cached and cached != invalidate and not _expired(cached):
            return cached
        async with self._store.login_lock(
            self._game_id, ttl_seconds=self._lock_ttl, acquire_timeout=self._lock_timeout,
        ):
            cached = await self._store.get(self._game_id)
            if cached and cached != invalidate and not _expired(cached):
                return cached
            session = await self._do_login()
            await self._store.set(self._game_id, session, ttl_seconds=self._ttl)
            return session

    async def _do_login(self) -> CachedSession:
        cookies: dict[str, str] = {}
        # 1. GET login page -> scrape _token + collect cookies (XSRF-TOKEN).
        r1 = await self._get(f"{self._base}/admin/auth/login", cookies=cookies)
        cookies.update({k: v for k, v in r1.cookies.items()})
        token = parse_csrf_token(r1.text)
        # 2. POST credentials.
        body = urlencode({"_token": token, "username": self._username, "password": self._password})
        r2 = await self._post(
            f"{self._base}/admin/auth/login", body, cookies=cookies, csrf=token,
        )
        cookies.update({k: v for k, v in r2.cookies.items()})
        # 3. Load an admin page to confirm auth + grab the per-session CSRF token.
        r3 = await self._get(f"{self._base}/admin/player_list", cookies=cookies)
        cookies.update({k: v for k, v in r3.cookies.items()})
        if looks_like_login_page(r3.text):
            raise BackendError("yolo:login_failed")
        csrf = parse_csrf_token(r3.text)
        return CachedSession(cookies=cookies, csrf_token=csrf, expires_at=int(time.time()) + self._ttl)

    # ---- requests ----

    async def post_form(self, path: str, fields: dict) -> dict:
        session = await self.get_session()
        resp = await self._authed_post(path, fields, session)
        if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
            session = await self.get_session(invalidate=session)
            resp = await self._authed_post(path, fields, session)
            if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
                raise BackendError("yolo:auth_failed")
        return map_envelope(resp.status_code, _json_or_none(resp))

    async def get_text(self, path: str, params: dict | None = None) -> str:
        session = await self.get_session()
        resp = await self._authed_get(path, params, session)
        if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
            session = await self.get_session(invalidate=session)
            resp = await self._authed_get(path, params, session)
            if looks_like_auth_failure(resp.status_code, resp.headers.get("Location", ""), _safe_text(resp)):
                raise BackendError("yolo:auth_failed")
        if resp.status_code >= 500:
            raise TransientBackendError(f"yolo:http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"yolo:http_{resp.status_code}")
        return resp.text

    async def _authed_post(self, path: str, fields: dict, session: CachedSession) -> httpx.Response:
        merged = {**_str_map(fields), "_token": session.csrf_token}
        return await self._post(f"{self._base}{path}", urlencode(merged),
                                cookies=session.cookies, csrf=session.csrf_token)

    async def _authed_get(self, path: str, params: dict | None, session: CachedSession) -> httpx.Response:
        return await self._get(f"{self._base}{path}", cookies=session.cookies,
                               params=_str_map(params or {}), csrf=session.csrf_token)

    # ---- transport ----

    async def _get(self, url: str, *, cookies: dict, params: dict | None = None,
                   csrf: str | None = None) -> httpx.Response:
        headers = {**_BROWSER_HEADERS, "X-Requested-With": "XMLHttpRequest"}
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        try:
            return await self._http.get(url, params=params, headers=headers,
                                        cookies=cookies, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"yolo:transport:{type(exc).__name__}") from exc

    async def _post(self, url: str, body: str, *, cookies: dict, csrf: str) -> httpx.Response:
        headers = {
            **_BROWSER_HEADERS,
            "Content-Type": _FORM_CT, "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest", "X-CSRF-TOKEN": csrf,
        }
        try:
            return await self._http.post(url, content=body.encode(), headers=headers,
                                         cookies=cookies, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"yolo:transport:{type(exc).__name__}") from exc


def _json_or_none(resp: httpx.Response) -> dict | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _safe_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001
        return ""
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_client.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/client.py tests/unit/test_yolo_client.py
git commit -m "feat(yolo): client with cached session + best-effort re-login"
```

---

## Phase 3 — Backend

### Task 6: YoloBackend (the six ops)

**Files:**
- Create: `app/backends/yolo/backend.py`
- Test: `tests/unit/test_yolo_backend.py`

**Interfaces:**
- Consumes: `YoloClient`, `passwords.generate_memorable_password`, `parsers.{parse_agent_score,parse_player_row}`, `app.schemas.results.*`, `app.backends.context.BackendContext`.
- Produces: `YoloBackend(client: YoloClient)` implementing `GameBackend`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_yolo_backend.py
import pytest

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.yolo.backend import YoloBackend


class FakeClient:
    """Stand-in for YoloClient: canned get_text + records post_form calls."""

    def __init__(self, *, texts=None, post_result=None):
        self._texts = texts or {}
        self._post_result = post_result if post_result is not None else {"message": "success"}
        self.posts = []

    async def get_text(self, path, params=None):
        for key, val in self._texts.items():
            if key in path:
                # crude param-aware: store last params for assertions
                self.last_params = params
                return val
        raise AssertionError(f"unexpected get_text {path}")

    async def post_form(self, path, fields):
        self.posts.append((path, fields))
        return self._post_result


_GRID = """
<table><tbody><tr>
<td>e</td><td>922952</td><td>apitest102</td><td>nick</td><td>ag</td><td>Member</td>
<td>123.45</td><td>0</td><td>0</td><td>0</td><td>0</td><td>0</td><td>Normal</td>
<td>0.0.0.0</td><td>d</td><td>d</td></tr></tbody></table>
"""


def _ctx(*, account_username=None, username="apitest102", external_user_id=None):
    creds = GameCredentials(
        game_id=1, name="yolo", backend_url="https://yolo.test", login_page_url=None,
        backend_username="webyolo1", backend_password="Web@@1122",
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    account = None
    if username:
        account = AccountIdentity(game_account_id=1, user_id=2, game_id=1,
                                  username=username, external_user_id=external_user_id)
    return BackendContext(credentials=creds, user_id=2, account=account,
                          idempotency_key="k", account_username=account_username)


async def test_agent_balance():
    c = FakeClient(texts={"/admin/refresh_score": "10.00"})
    res = await YoloBackend(c).agent_balance(_ctx())
    assert res.agent_balance == 10.0


async def test_read_balance_by_search():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    res = await YoloBackend(c).read_balance(_ctx(username="apitest102"))
    assert res.balance == 123.45


async def test_recharge_sends_type1_int_dollars():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).recharge(_ctx(external_user_id="922952"), amount=50)
    path, fields = c.posts[-1]
    assert path == "/admin/dcat-api/form"
    assert fields["type"] == 1 and fields["input_score"] == "50" and fields["UserID"] == "922952"
    assert fields["_form_"] == "App\\Admin\\Actions\\UserRecharge"


async def test_redeem_sends_type2():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).redeem(_ctx(external_user_id="922952"), amount=25)
    _path, fields = c.posts[-1]
    assert fields["type"] == 2 and fields["input_score"] == "25"


async def test_reset_password_returns_generated_pw():
    c = FakeClient(texts={"/admin/player_list": _GRID}, post_result={"message": "success"})
    res = await YoloBackend(c).reset_password(_ctx(external_user_id="922952"))
    assert len(res.password) >= 6 and res.password.isalnum()
    _path, fields = c.posts[-1]
    assert fields["_form_"] == "App\\Admin\\Actions\\ResetUserPass"
    assert fields["password"] == res.password


async def test_create_account_generates_and_searches():
    c = FakeClient(texts={"/admin/player_list": _GRID},
                   post_result={"message": "<div>Account: x Password: y</div>"})
    res = await YoloBackend(c).create_account(_ctx(account_username="apitest102", username=None))
    assert res.username == "apitest102" and len(res.password) >= 6
    # external_user_id resolved from the follow-up player_list search
    assert res.external_user_id == "922952"
    create_path, fields = c.posts[0]
    assert create_path == "/admin/player_list"
    assert fields["Accounts"] == "apitest102" and fields["RegisterIP"] == "0.0.0.0"


async def test_recharge_resolves_player_id_via_search_when_uncached():
    c = FakeClient(texts={"/admin/player_list": _GRID})
    await YoloBackend(c).recharge(_ctx(username="apitest102", external_user_id=None), amount=5)
    _path, fields = c.posts[-1]
    assert fields["UserID"] == "922952"  # came from parse_player_row
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_backend.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `app/backends/yolo/backend.py`**

```python
from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.yolo.client import YoloClient
from app.backends.yolo.parsers import parse_agent_score, parse_player_row
from app.backends.yolo.passwords import generate_memorable_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)

_RECHARGE_FORM = "App\\Admin\\Actions\\UserRecharge"
_RESET_FORM = "App\\Admin\\Actions\\ResetUserPass"
_PLAYER_LIST = "/admin/player_list"
_DCAT_FORM = "/admin/dcat-api/form"


class YoloBackend:
    def __init__(self, client: YoloClient) -> None:
        self._client = client

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        text = await self._client.get_text("/admin/refresh_score")
        return AgentBalanceResult(agent_balance=parse_agent_score(text))

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        _uid, score = await self._player(ctx)
        return ReadBalanceResult(balance=score)

    async def recharge(self, ctx: BackendContext, *, amount: int) -> RechargeResult:
        await self._user_recharge(ctx, type_=1, amount=amount)
        return RechargeResult()

    async def redeem(self, ctx: BackendContext, *, amount: int) -> RedeemResult:
        await self._user_recharge(ctx, type_=2, amount=amount)
        return RedeemResult()

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid, account = await self._player_id(ctx)
        pwd = generate_memorable_password()
        await self._client.post_form(_DCAT_FORM, {
            "_form_": _RESET_FORM,
            "UserID": uid, "Accounts": account, "password": pwd,
            "_current_": f"{self._base()}/admin/player_list?",
        })
        return ResetPasswordResult(password=pwd)

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError("yolo:account_username_required")
        pwd = generate_memorable_password()
        await self._client.post_form(_PLAYER_LIST, {
            "Accounts": username, "NickName": username, "LogonPass": pwd,
            "Recharge_Amount": 0, "RegisterIP": "0.0.0.0",
            "_previous_": f"{self._base()}/admin/player_list",
        })
        # Follow-up search to resolve the new player's UserID (best-effort; None if not indexed yet).
        external_user_id: str | None = None
        try:
            external_user_id, _score = await self._search(username)
        except BackendError:
            external_user_id = None
        return CreateAccountResult(username=username, password=pwd, external_user_id=external_user_id)

    # ---- internal ----

    async def _user_recharge(self, ctx: BackendContext, *, type_: int, amount: int) -> None:
        uid, account = await self._player_id(ctx)
        await self._client.post_form(_DCAT_FORM, {
            "_form_": _RECHARGE_FORM,
            "UserID": uid, "Accounts": account, "type": type_,
            "input_score": str(int(amount)), "Score": "", "remark": "",
            "_current_": f"{self._base()}/admin/player_list?",
        })

    async def _player(self, ctx: BackendContext) -> tuple[str, float]:
        """Return (user_id, balance) — searches player_list by account."""
        account = self._account(ctx)
        return await self._search(account)

    async def _player_id(self, ctx: BackendContext) -> tuple[str, str]:
        """Return (user_id, account). Prefer cached external_user_id; else search."""
        account = self._account(ctx)
        if ctx.account and ctx.account.external_user_id:
            return ctx.account.external_user_id, account
        uid, _score = await self._search(account)
        return uid, account

    async def _search(self, account: str) -> tuple[str, float]:
        html = await self._client.get_text(
            _PLAYER_LIST, {"Accounts": account, "_pjax": "#pjax-container"},
        )
        return parse_player_row(html, account=account)

    @staticmethod
    def _account(ctx: BackendContext) -> str:
        if ctx.account and ctx.account.username:
            return ctx.account.username
        raise BackendError("yolo:account_required")

    def _base(self) -> str:
        # _current_/_previous_ echo the panel URL; harmless if the host differs in tests.
        return "https://agent.yolo-777.com"
```

> Note: `_base()` returns the canonical panel URL for the `_current_`/`_previous_` echo fields
> (the server overwrites/ignores them). Keeping it constant avoids threading `backend_url` purely
> for a cosmetic field. If a future finding shows the server validates `_current_`, switch it to
> derive from `ctx.credentials.backend_url`.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_yolo_backend.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/yolo/backend.py tests/unit/test_yolo_backend.py
git commit -m "feat(yolo): backend implementing the six ops"
```

---

## Phase 4 — Wiring

### Task 7: Config settings

**Files:**
- Modify: `app/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `Settings.yolo_session_ttl_seconds: int = 1800`, `yolo_login_lock_ttl_seconds: int = 10`, `yolo_login_lock_acquire_timeout_seconds: float = 10.0`.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_config.py`:

```python
def test_yolo_settings_defaults(monkeypatch):
    monkeypatch.setenv("API_SECRET", "a")
    monkeypatch.setenv("WEBHOOK_SECRET", "b")
    from app.config import Settings
    s = Settings()
    assert s.yolo_session_ttl_seconds == 1800
    assert s.yolo_login_lock_ttl_seconds == 10
    assert s.yolo_login_lock_acquire_timeout_seconds == 10.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py::test_yolo_settings_defaults -v`
Expected: FAIL (AttributeError).

- [ ] **Step 3: Add settings to `app/config.py`** — alongside the other `vpower_*`/`aspnet_*` fields (after `vpower_session_lock_acquire_timeout_seconds`):

```python
    yolo_session_ttl_seconds: int = 1800
    yolo_login_lock_ttl_seconds: int = 10
    yolo_login_lock_acquire_timeout_seconds: float = 10.0
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/unit/test_config.py
git commit -m "feat(yolo): config settings"
```

### Task 8: Registry wiring + non-idempotent

**Files:**
- Modify: `app/backends/registry.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: `YoloBackend`, `YoloClient`, `RedisSessionStore` (as `YoloSessionStore`), `Settings.yolo_*`.
- Produces: `resolve_backend("yolo", …)` returns a `YoloBackend`; `"yolo"` ∈ `NON_IDEMPOTENT_DRIVERS`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_registry.py`:

```python
def test_yolo_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "yolo" in NON_IDEMPOTENT_DRIVERS


def test_resolve_yolo(fake_redis):
    import httpx

    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.backends.yolo.backend import YoloBackend
    from app.config import Settings

    creds = GameCredentials(
        game_id=1, name="yolo", backend_url="https://agent.yolo-777.com", login_page_url=None,
        backend_username="webyolo1", backend_password="Web@@1122",
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    with httpx.Client() as _c:
        pass
    backend = resolve_backend(
        "yolo", credentials=creds, http_client=httpx.AsyncClient(),
        settings=Settings(api_secret="a", webhook_secret="b"),
        session_store=None, redis=fake_redis,
    )
    assert isinstance(backend, YoloBackend)


def test_resolve_yolo_missing_creds_raises(fake_redis):
    import httpx

    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings

    creds = GameCredentials(
        game_id=1, name="yolo", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    with pytest.raises(BackendError, match="missing_yolo_credentials"):
        resolve_backend("yolo", credentials=creds, http_client=httpx.AsyncClient(),
                        settings=Settings(api_secret="a", webhook_secret="b"),
                        session_store=None, redis=fake_redis)
```

(Ensure `import pytest` is present at the top of `tests/unit/test_registry.py`.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -k yolo -v`
Expected: FAIL.

- [ ] **Step 3: Edit `app/backends/registry.py`**

Add imports near the other backend imports:

```python
from app.backends.yolo.backend import YoloBackend
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import RedisSessionStore as YoloSessionStore
```

Add `"yolo"` to the `NON_IDEMPOTENT_DRIVERS` frozenset:

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({
    "gameroom", "goldentreasure",
    "orionstars", "milkyway", "firekirin", "pandamaster",
    "ultrapanda", "vblink",
    "yolo",
})
```

Add the resolve branch immediately before the final `raise BackendError(f"unknown_backend_driver:{driver}")`:

```python
    if key == "yolo":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_yolo_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        return YoloBackend(
            YoloClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=YoloSessionStore(redis),
                game_id=credentials.game_id,
                session_ttl_seconds=settings.yolo_session_ttl_seconds,
                login_lock_ttl_seconds=settings.yolo_login_lock_ttl_seconds,
                login_lock_acquire_timeout_seconds=settings.yolo_login_lock_acquire_timeout_seconds,
            )
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(yolo): registry wiring + non-idempotent driver"
```

### Task 9: Preflight credentials + logging redaction

**Files:**
- Modify: `app/preflight/checks.py`, `app/logging.py`
- Test: `tests/unit/test_preflight.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Produces: preflight raises `missing_yolo_credentials` when a `yolo` game lacks backend_url/username/password; `SECRET_KEYS` includes `_token`, `csrf_token`, `laravel_session`, `xsrf-token`.

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_preflight.py`:

```python
@pytest.mark.asyncio
async def test_yolo_missing_credentials(session_factory):
    from app.db.models import Game
    from app.preflight.checks import PreflightError, build_context
    async with session_factory() as s:
        s.add(Game(id=77, name="yolo", active=True, backend_driver="yolo"))
        await s.commit()
    async with session_factory() as s:
        with pytest.raises(PreflightError, match="missing_yolo_credentials"):
            await build_context(s, type="CREATE_ACCOUNT", backend_name="yolo",
                                username=None, user_id=2, account_username="abc123x")
```

Append to `tests/unit/test_logging.py` (follow the file's existing assertion style):

```python
def test_redacts_yolo_session_secrets():
    from app.logging import _redact_in_place
    d = {"_token": "TOK", "csrf_token": "C", "laravel_session": "S", "keep": "ok"}
    _redact_in_place(d)
    assert d["_token"] == "***" and d["csrf_token"] == "***" and d["laravel_session"] == "***"
    assert d["keep"] == "ok"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py::test_yolo_missing_credentials tests/unit/test_logging.py::test_redacts_yolo_session_secrets -v`
Expected: FAIL.

- [ ] **Step 3: Edit `app/preflight/checks.py`** — add `"yolo"` to the session-family driver set in the credential check:

```python
    if driver in {"gameroom", "goldentreasure", "milkyway", "firekirin", "pandamaster",
                  "orionstars", "ultrapanda", "vblink", "yolo"} and not (
        game.backend_url and game.username and game.password
    ):
        raise PreflightError(f"missing_{driver}_credentials")
```

- [ ] **Step 4: Edit `app/logging.py`** — add to the `SECRET_KEYS` set:

```python
    "_token",
    "csrf_token",
    "laravel_session",
    "xsrf-token",
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py tests/unit/test_logging.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/preflight/checks.py app/logging.py tests/unit/test_preflight.py tests/unit/test_logging.py
git commit -m "feat(yolo): preflight credential check + log redaction"
```

---

## Phase 5 — Integration test + final gate

### Task 10: Live-gated integration test

**Files:**
- Create: `tests/integration/test_yolo_integration.py`

**Interfaces:**
- Consumes: `YoloClient`, `YoloBackend`, `InMemorySessionStore`. Skipped unless `YOLO_LIVE=1` and creds env are set.

- [ ] **Step 1: Write the test** (mirrors the other live-gated provider tests — it self-skips)

```python
# tests/integration/test_yolo_integration.py
import os

import httpx
import pytest

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.yolo.backend import YoloBackend
from app.backends.yolo.client import YoloClient
from app.backends.yolo.session import InMemorySessionStore

_LIVE = os.getenv("YOLO_LIVE") == "1"
pytestmark = pytest.mark.skipif(not _LIVE, reason="set YOLO_LIVE=1 + creds to run")


def _ctx(account: str):
    creds = GameCredentials(
        game_id=1, name="yolo", backend_url=os.environ["YOLO_BASE_URL"], login_page_url=None,
        backend_username=os.environ["YOLO_USER"], backend_password=os.environ["YOLO_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None, binding_key=None,
        backend_driver="yolo",
    )
    acct = AccountIdentity(game_account_id=1, user_id=2, game_id=1,
                           username=account, external_user_id=None)
    return BackendContext(credentials=creds, user_id=2, account=acct,
                          idempotency_key="live", account_username=account)


async def test_live_agent_balance_and_read():
    async with httpx.AsyncClient(timeout=30) as http:
        client = YoloClient(
            base_url=os.environ["YOLO_BASE_URL"], username=os.environ["YOLO_USER"],
            password=os.environ["YOLO_PASS"], http_client=http,
            session_store=InMemorySessionStore(), game_id=1,
        )
        backend = YoloBackend(client)
        agent = await backend.agent_balance(_ctx(os.environ["YOLO_TEST_ACCOUNT"]))
        assert agent.agent_balance >= 0
        bal = await backend.read_balance(_ctx(os.environ["YOLO_TEST_ACCOUNT"]))
        assert bal.balance >= 0
```

- [ ] **Step 2: Run to verify it skips cleanly**

Run: `.venv/bin/python -m pytest tests/integration/test_yolo_integration.py -v`
Expected: SKIPPED (1 skipped).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_yolo_integration.py
git commit -m "test(yolo): live-gated integration test"
```

### Task 11: Full green gate

- [ ] **Step 1: Full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, the YOLO live test skipped. (Baseline was 394 passed / 20 skipped; this adds ~30 unit tests passing + 1 skipped.)

- [ ] **Step 2: Lint + type**

Run: `.venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: `All checks passed!` and `Success: no issues found`. Fix any unused imports / annotations introduced.

- [ ] **Step 3: Commit (only if lint/type fixups were needed)**

```bash
git add -A && git commit -m "chore(yolo): lint/type fixups"
```

---

## Self-Review Notes (verify during execution)

- **Spec coverage:** §4.1 session→T1; §4.3 errors→T3; §4.4 parsers→T2; §4.5 passwords→T4; §4.2 client→T5; §4.6 backend→T6; §4.7 wiring→T7 (config), T8 (registry+non-idempotent), T9 (preflight+logging); §6 testing→T1–T10; §5 money-safety→T8 (NON_IDEMPOTENT_DRIVERS).
- **Type consistency:** `recharge(*, amount: int)`/`redeem(*, amount: int)`; results `balance`/`agent_balance` floats; `CachedSession(cookies, csrf_token, expires_at)`; client `get_session`/`post_form`/`get_text`; `map_envelope(http_status, body)`; `parse_player_row(html, *, account)`.
- **No placeholders:** every code step is complete.
- **Assumptions (from spec §7):** auth-failure trigger is best-effort (401/419/login-redirect); `_payload_` omitted from writes (we send the flat fields the action reads — if a live write needs `_payload_`, add it in `backend.py`); create `external_user_id` may be `None` if the new row isn't indexed immediately (later ops re-search — safe).
</content>
