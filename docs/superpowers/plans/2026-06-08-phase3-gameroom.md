# Phase 3 — Gameroom Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Gameroom (`gameroom777.com` agent backend) — our first reverse-engineered backend — behind the existing `GameBackend` abstraction, with a Redis-backed shared session, double-checked-locking refresh for single-session safety, and per-driver `_max_tries=1` to protect non-idempotent money ops.

**Architecture:** A `GameroomClient` holds a JWT session in a Redis-backed `SessionStore` shared across workers (one session per `game_id`). On `status_code:410` (token invalid), `get_token(invalidate=…)` does double-checked-locking under a Redis lock so concurrent workers don't thrash the session under Gameroom's single-session-per-agent enforcement. The API endpoint peeks the game's `backend_driver` before enqueueing and passes `_max_tries=1` for non-idempotent drivers so a worker crash mid-money-op cannot double-apply.

**Tech Stack:** httpx (async, form-urlencoded), `redis.asyncio` (cache + lock), `fakeredis` (tests), Pydantic v2, SQLAlchemy 2.0 (read-only), pytest + respx.

**Spec:** `docs/superpowers/specs/2026-06-08-phase3-gameroom-design.md`
**Findings:** `/Applications/development/gameroom-standalone/gameroom_api_findings.md`

**Environment:** branch `feat/phase3-gameroom` (already checked out); venv at `.venv` (use `.venv/bin/python -m ...`). On this machine `head` is an HTTP tool — never pipe to it; use `sed -n` or your Read tool.

---

## File structure (this phase)

```
Create:
  app/backends/gameroom/__init__.py           (empty package marker)
  app/backends/gameroom/errors.py             - map_response(status_code, message) -> (reason, terminal) (Task 3)
  app/backends/gameroom/passwords.py          - generate_memorable_complex_password (Task 4)
  app/backends/gameroom/session.py            - CachedSession, SessionStore, In/RedisSessionStore + login lock (Task 5)
  app/backends/gameroom/client.py             - GameroomClient: login, double-checked get_token, call (Task 6)
  app/backends/gameroom/backend.py            - GameroomBackend: 6 ops + _player_id fallback (Task 7)

Modify:
  pyproject.toml                              - add fakeredis>=2.21 to dev deps (Task 1)
  tests/conftest.py                           - seed gameroom game + accounts; add fakeredis fixture (Task 1)
  app/db/repositories.py                      - GamesRepository.get_driver(game_id) (Task 2)
  app/preflight/checks.py                     - "missing_gameroom_credentials" guard (Task 2)
  app/backends/registry.py                    - NON_IDEMPOTENT_DRIVERS; resolve_backend(session_store=...) + 'gameroom' branch (Task 8)
  app/operations/executor.py                  - execute_operation accepts + threads session_store (Task 9)
  app/worker/tasks.py                         - pass session_store from ctx (Task 10)
  app/worker/settings.py                      - construct RedisSessionStore in startup; close in shutdown (Task 10)
  app/main.py                                 - lifespan: app.state.session_factory = get_sessionmaker() (Task 11)
  app/api/operations.py                       - per-driver _max_tries via DB peek (Task 11)
  CLAUDE.md, docs/architecture.md, docs/runbook.md (Task 12)
```

---

## Task 1: Test scaffolding — fakeredis dep + seeded gameroom fixtures

**Files:**
- Modify: `pyproject.toml`, `tests/conftest.py`
- (Test verification at the end of the task)

- [ ] **Step 1: Add `fakeredis` to dev deps**

In `pyproject.toml`, in the `dev` extras list (next to `aiosqlite`/`aiomysql`):

```toml
    "fakeredis>=2.21",
```

Install: `.venv/bin/python -m pip install --quiet "fakeredis>=2.21" && .venv/bin/python -c "import fakeredis.aioredis; print('OK')"`
Expected: `OK`.

- [ ] **Step 2: Extend `tests/conftest.py` with gameroom seeds + a fakeredis fixture**

Append to `tests/conftest.py` (alongside the existing `seeded` fixture additions):

Inside the `seeded` fixture's `async with session_factory() as s:` block, after the existing `s.add(Game(id=10, ...))` block, add:

```python
        s.add(
            Game(
                id=11,
                name="Gameroom",
                active=True,
                backend_driver="gameroom",
                backend_url="https://gr.test",
                backend_username="TestGR159",
                backend_password="TestGR1122@",
            )
        )
        s.add(
            Game(id=12, name="Gameroom NoCreds", active=True, backend_driver="gameroom"),
        )
        s.add(
            GameAccount(
                id=3001, user_id=51, game_id=11, username="apifull9983654",
                password="x", external_user_id="2998032",
            )
        )
        s.add(
            GameAccount(
                id=3002, user_id=52, game_id=11, username="user_no_ext",
                password="x", external_user_id=None,
            )
        )
```

At the end of the file, add a new fakeredis fixture:

```python
import pytest_asyncio
# (other imports already at the top of the file)


@pytest_asyncio.fixture
async def fake_redis():
    """In-process fake Redis (full SET NX + TTL semantics) for session-store tests."""
    import fakeredis.aioredis as _f
    r = _f.FakeRedis(decode_responses=False)
    try:
        yield r
    finally:
        await r.aclose()
```

- [ ] **Step 3: Verify seeds and fakeredis fixture both work**

```python
# tests/unit/test_phase3_fixtures.py  (temporary smoke test — delete in Step 5)
from sqlalchemy import text


async def test_gameroom_rows_seeded(seeded):
    async with seeded() as s:
        row = (await s.execute(text("SELECT name, backend_driver, backend_url, backend_username FROM games WHERE id=11"))).first()
    assert row == ("Gameroom", "gameroom", "https://gr.test", "TestGR159")


async def test_fake_redis_setnx_works(fake_redis):
    assert await fake_redis.set("k", "v1", nx=True, ex=10) is True
    assert await fake_redis.set("k", "v2", nx=True, ex=10) is None
    assert await fake_redis.get("k") == b"v1"
```

Run: `.venv/bin/python -m pytest tests/unit/test_phase3_fixtures.py -v`
Expected: `2 passed`.

- [ ] **Step 4: Run the full suite to confirm no regression**

Run: `.venv/bin/python -m pytest -q`
Expected: all previously-passing tests still pass; the 2 new ones pass.

- [ ] **Step 5: Delete the smoke test and commit**

```bash
rm tests/unit/test_phase3_fixtures.py
git add pyproject.toml tests/conftest.py
git commit -m "chore(phase3): seed gameroom test rows + fakeredis dev dep

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: GamesRepository.get_driver + preflight gameroom guard

**Files:**
- Modify: `app/db/repositories.py`, `app/preflight/checks.py`
- Test: `tests/unit/test_repositories.py`, `tests/unit/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_repositories.py`:

```python
async def test_games_repo_get_driver_returns_string(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_driver(11) == "gameroom"


async def test_games_repo_get_driver_returns_none_when_absent(seeded):
    async with seeded() as s:
        assert await GamesRepository(s).get_driver(99999) is None
```

Append to `tests/unit/test_preflight.py`:

```python
async def test_gameroom_game_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", idempotency_key="k", user_id=None,
                game_id=12, game_account_id=None,
            )
    assert "missing_gameroom_credentials" in ei.value.reason


async def test_gameroom_context_carries_credentials(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=51,
            game_id=11, game_account_id=3001,
        )
    assert ctx.credentials.backend_driver == "gameroom"
    assert ctx.credentials.backend_url == "https://gr.test"
    assert ctx.credentials.backend_username == "TestGR159"
    assert ctx.account.external_user_id == "2998032"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_repositories.py::test_games_repo_get_driver_returns_string tests/unit/test_preflight.py::test_gameroom_game_missing_credentials_raises -v`
Expected: FAIL — `get_driver` not defined; preflight doesn't guard gameroom.

- [ ] **Step 3: Add `get_driver` to `GamesRepository`**

In `app/db/repositories.py`, in class `GamesRepository`, add:

```python
    async def get_driver(self, game_id: int) -> str | None:
        """Read just the backend_driver column (cheap; used by the API endpoint for per-driver retry policy)."""
        stmt = select(Game.backend_driver).where(Game.id == game_id, Game.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()
```

- [ ] **Step 4: Add the gameroom credentials guard to preflight**

In `app/preflight/checks.py`, just below the existing gamevault credentials guard (the line beginning `if (game.backend_driver or "").lower() == "gamevault"`), add:

```python
    if (game.backend_driver or "").lower() == "gameroom" and not (
        game.backend_url and game.backend_username and game.backend_password
    ):
        raise PreflightError("missing_gameroom_credentials")
```

- [ ] **Step 5: Run all three new tests + full suite**

Run: `.venv/bin/python -m pytest tests/unit/test_repositories.py tests/unit/test_preflight.py -q`
Expected: PASS (new tests + all pre-existing).
Run: `.venv/bin/python -m pytest -q`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add app/db/repositories.py app/preflight/checks.py tests/unit/test_repositories.py tests/unit/test_preflight.py
git commit -m "feat(phase3): GamesRepository.get_driver + preflight gameroom creds guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `gameroom/errors.py` — status-code + message pattern mapping

**Files:**
- Create: `app/backends/gameroom/__init__.py` (empty), `app/backends/gameroom/errors.py`
- Test: `tests/unit/test_gameroom_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gameroom_errors.py
from app.backends.gameroom.errors import map_response


def test_500_is_transient():
    reason, terminal = map_response(500, "Service exception")
    assert reason == "gameroom:server_error"
    assert terminal is False


def test_430_is_terminal_auth_failed():
    reason, terminal = map_response(430, "Username or password error")
    assert reason == "gameroom:auth_failed"
    assert terminal is True


def test_401_is_transient_auth_missing():
    reason, terminal = map_response(401, "Token not provided")
    assert reason == "gameroom:auth_missing"
    assert terminal is False


def test_400_message_patterns_to_terminal_slugs():
    cases = [
        ("Username already exists", "gameroom:account_exists"),
        ("Recharge balance is greater than available balance, please check and recharge again", "gameroom:insufficient_agent_balance"),
        ("Withdrawal amount is greater than customer balance. Please check and withdraw again", "gameroom:insufficient_user_balance"),
        ("Amount must be greater than 0", "gameroom:invalid_amount"),
        ("The balance must be an integer.", "gameroom:invalid_amount"),
        ("The password confirmation does not match.", "gameroom:password_mismatch"),
        ("Operation failed", "gameroom:operation_failed"),
    ]
    for msg, expected in cases:
        reason, terminal = map_response(400, msg)
        assert reason == expected, (msg, reason)
        assert terminal is True


def test_400_unknown_message_is_terminal_business_error_truncated():
    msg = "x" * 200
    reason, terminal = map_response(400, msg)
    assert reason.startswith("gameroom:business_error: ")
    assert len(reason) <= len("gameroom:business_error: ") + 80
    assert terminal is True


def test_other_status_is_terminal_with_slug():
    reason, terminal = map_response(403, "Forbidden")
    assert reason == "gameroom:status_403: Forbidden"
    assert terminal is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_errors.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gameroom/errors.py

# Substring patterns from the findings doc §4.5-4.8. Matched case-insensitively.
_MESSAGE_PATTERNS: list[tuple[str, str]] = [
    ("username already exists", "account_exists"),
    ("recharge balance is greater", "insufficient_agent_balance"),
    ("withdrawal amount is greater", "insufficient_user_balance"),
    ("amount must be greater than 0", "invalid_amount"),
    ("balance must be an integer", "invalid_amount"),
    ("password confirmation does not match", "password_mismatch"),
    ("operation failed", "operation_failed"),
]


def map_response(status_code: int, message: str) -> tuple[str, bool]:
    """Map a Gameroom envelope (status_code + message) to (reason_slug, is_terminal).

    Terminal = same call would fail the same way; the executor caches these so a re-run
    short-circuits. Transient = retry-worthy (network / 5xx / 401-with-no-token).
    """
    msg = message or ""
    if status_code == 500:
        return ("gameroom:server_error", False)
    if status_code == 430:
        return ("gameroom:auth_failed", True)
    if status_code == 401:
        return ("gameroom:auth_missing", False)
    if status_code == 400:
        low = msg.lower()
        for needle, slug in _MESSAGE_PATTERNS:
            if needle in low:
                return (f"gameroom:{slug}", True)
        return (f"gameroom:business_error: {msg[:80]}", True)
    return (f"gameroom:status_{status_code}: {msg[:60]}", True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_errors.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gameroom/__init__.py app/backends/gameroom/errors.py tests/unit/test_gameroom_errors.py
git commit -m "feat(gameroom): map Gameroom envelope to terminal/transient reasons

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `gameroom/passwords.py` — memorable complex password

**Files:**
- Create: `app/backends/gameroom/passwords.py`
- Test: `tests/unit/test_gameroom_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gameroom_passwords.py
import re

from app.backends.gameroom.passwords import (
    generate_memorable_complex_password,
    generate_memorable_password,
)


def test_complex_password_has_all_required_classes():
    for _ in range(50):
        pw = generate_memorable_complex_password()
        assert 6 <= len(pw) <= 12, pw
        assert any(c.isupper() for c in pw), pw
        assert any(c.islower() for c in pw), pw
        assert any(c.isdigit() for c in pw), pw
        assert any(c in "!@#$%&*" for c in pw), pw
        assert " " not in pw


def test_complex_password_format_word_symbol_digits():
    pw = generate_memorable_complex_password()
    assert re.fullmatch(r"[A-Z][a-z]+[!@#$%&*]\d{2}", pw), pw


def test_complex_password_varies():
    assert len({generate_memorable_complex_password() for _ in range(20)}) > 1


def test_memorable_alphanumeric_password_is_re_exported():
    # CREATE_ACCOUNT uses the existing GameVault generator; gameroom re-exports for clarity.
    pw = generate_memorable_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_passwords.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gameroom/passwords.py
import secrets

# Re-export the existing memorable (alphanumeric) generator: Gameroom CREATE_ACCOUNT rule is
# alphanumeric 6-12 chars (same as GameVault), so the existing generator already satisfies it.
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401

# Filter the existing wordlist to 4-7 char words so word+symbol+2 digits stays within 6-12.
from app.backends.gamevault.passwords import _WORDS as _GV_WORDS

_SHORT_WORDS: tuple[str, ...] = tuple(w for w in _GV_WORDS if 4 <= len(w) <= 7)
_SYMBOLS = "!@#$%&*"  # safe set: no quote/space/paren


def generate_memorable_complex_password() -> str:
    """Memorable password satisfying Gameroom's RESET rule:
    upper + lower + special symbol + 6-12 chars. Format: {Word}{symbol}{2 digits}, e.g. 'Tiger@47'.
    """
    word = secrets.choice(_SHORT_WORDS)
    symbol = secrets.choice(_SYMBOLS)
    number = secrets.randbelow(90) + 10  # 10..99
    return f"{word}{symbol}{number}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_passwords.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gameroom/passwords.py tests/unit/test_gameroom_passwords.py
git commit -m "feat(gameroom): memorable-complex password generator (upper+lower+digit+symbol)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `gameroom/session.py` — SessionStore + Redis lock

**Files:**
- Create: `app/backends/gameroom/session.py`
- Test: `tests/unit/test_gameroom_session.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gameroom_session.py
import asyncio

import pytest

from app.backends.gameroom.session import (
    CachedSession,
    InMemorySessionStore,
    RedisSessionStore,
)


async def test_in_memory_set_get_clear():
    store = InMemorySessionStore()
    assert await store.get(1) is None
    await store.set(1, CachedSession(token="t1", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(1)
    assert got is not None and got.token == "t1"
    await store.clear(1)
    assert await store.get(1) is None


async def test_redis_set_get_clear_and_key_prefix(fake_redis):
    store = RedisSessionStore(fake_redis)
    await store.set(7, CachedSession(token="abc", expires_at=9_999_999_999), ttl_seconds=120)
    raw = await fake_redis.get("gameroom_session:7")
    assert raw is not None and b"abc" in raw
    got = await store.get(7)
    assert got is not None and got.token == "abc" and got.expires_at == 9_999_999_999
    await store.clear(7)
    assert await store.get(7) is None


async def test_redis_set_respects_ttl(fake_redis):
    store = RedisSessionStore(fake_redis)
    await store.set(8, CachedSession(token="t", expires_at=9_999_999_999), ttl_seconds=1)
    ttl = await fake_redis.ttl("gameroom_session:8")
    assert 0 < ttl <= 1


async def test_redis_login_lock_serializes_concurrent_acquires(fake_redis):
    """SET NX semantics: first caller takes the lock; second sees None and must wait.

    We don't simulate the poll-and-retry inside the store itself — the client owns retry policy.
    Here we just verify SET NX is what the lock helper uses, by checking the lock key exists
    while held and disappears after release.
    """
    store = RedisSessionStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("gameroom_login:9")) == 1
    assert (await fake_redis.exists("gameroom_login:9")) == 0


async def test_redis_login_lock_setnx_blocks_second_acquire(fake_redis):
    store = RedisSessionStore(fake_redis)
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_lock():
        async with store.login_lock(game_id=10, ttl_seconds=30):
            held.set()
            await release.wait()

    task = asyncio.create_task(hold_lock())
    await held.wait()
    # Second attempt with poll_seconds=0.05 and timeout=0.2 should not acquire (lock held).
    with pytest.raises(TimeoutError):
        async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=0.2):
            raise AssertionError("should not have acquired lock")
    release.set()
    await task
    # After release the next attempt acquires immediately.
    async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=1.0):
        pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_session.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gameroom/session.py
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    token: str
    expires_at: int          # unix seconds (when the JWT itself expires)


class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 10,
        poll_seconds: float = 0.1, acquire_timeout: float = 10.0,
    ):
        """Async context manager that serializes /api/login calls for a game across workers.

        Raises asyncio.TimeoutError if the lock cannot be acquired within acquire_timeout.
        """


class InMemorySessionStore:
    """Process-local store with an in-process asyncio.Lock per game. For tests / single-process fallback."""

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
            raise TimeoutError(f"gameroom login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"gameroom_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"gameroom_login:{game_id}"


class RedisSessionStore:
    """Redis-backed session store + SET NX login lock. Shared across all workers."""

    def __init__(self, redis) -> None:
        self._redis = redis

    async def get(self, game_id: int) -> CachedSession | None:
        raw = await self._redis.get(_key_session(game_id))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedSession(token=d["token"], expires_at=int(d["expires_at"]))

    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None:
        raw = json.dumps({"token": session.token, "expires_at": session.expires_at})
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
            # SET NX with TTL: only succeeds if the key is absent. Returns True / b'OK' / None.
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"gameroom login lock acquire timeout (game_id={game_id})")
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

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_session.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gameroom/session.py tests/unit/test_gameroom_session.py
git commit -m "feat(gameroom): SessionStore (Redis + in-memory) with SET NX login lock

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `gameroom/client.py` — JWT login, double-checked refresh, re-login-on-410

**Files:**
- Create: `app/backends/gameroom/client.py`
- Test: `tests/unit/test_gameroom_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gameroom_client.py
import asyncio
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.session import CachedSession, InMemorySessionStore

BASE = "https://gr.test"


def _client(http, store=None):
    return GameroomClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store or InMemorySessionStore(), game_id=11,
    )


def _login_ok(token="T1", expires_in=21600):
    return {"status_code": 200, "message": "ok", "token": token,
            "expires_time": int(time.time()) + expires_in, "money": "5.00"}


# --- login ---

@respx.mock
async def test_login_posts_form_urlencoded_without_bearer_and_returns_token():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tabc")))
    async with httpx.AsyncClient() as http:
        token = await _client(http).get_token()
    assert token == "Tabc"
    sent = route.calls.last.request
    body = sent.content.decode()
    assert "username=u" in body and "password=p" in body
    assert "captcha" not in body                                   # captcha intentionally omitted
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "authorization" not in [h.lower() for h in sent.headers.keys()]


@respx.mock
async def test_login_430_is_terminal_auth_failed():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(
        200, json={"status_code": 430, "message": "Username or password error"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).get_token()
    assert ei.value.reason == "gameroom:auth_failed"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_500_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).get_token()


# --- get_token reuse + invalidate ---

@respx.mock
async def test_get_token_returns_cached_when_present_and_fresh():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tx")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="cached", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token()
    assert token == "cached"
    assert route.call_count == 0                                   # NO login happened


@respx.mock
async def test_get_token_with_invalidate_skips_login_if_cache_already_holds_newer():
    # Double-checked-locking regression: another worker already refreshed; we must not re-login.
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("would_be_new")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="T2_already_fresh", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token(invalidate="T1_dead")
    assert token == "T2_already_fresh"
    assert route.call_count == 0


@respx.mock
async def test_get_token_with_invalidate_logs_in_when_cache_still_holds_dead_token():
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tnew")))
    store = InMemorySessionStore()
    await store.set(11, CachedSession(token="T1_dead", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _client(http, store=store).get_token(invalidate="T1_dead")
    assert token == "Tnew"
    assert route.call_count == 1


@respx.mock
async def test_concurrent_get_token_under_lock_logs_in_only_once():
    """Two workers see empty cache simultaneously. The login lock must serialize so only ONE
    /api/login is issued; the second worker reads the freshly-cached token after the lock releases."""
    route = respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tonce")))
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as http:
        c1 = _client(http, store=store)
        c2 = _client(http, store=store)
        tok1, tok2 = await asyncio.gather(c1.get_token(), c2.get_token())
    assert tok1 == tok2 == "Tonce"
    assert route.call_count == 1                                   # crucial: only one login


# --- call() with re-login-on-410 ---

@respx.mock
async def test_call_success_returns_data():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T1")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("POST", "/api/agent/getMoney")
    assert data == {"money": "5.00"}


@respx.mock
async def test_call_410_relogins_and_retries_once_successfully():
    respx.post(f"{BASE}/api/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),               # initial login
        httpx.Response(200, json=_login_ok("Tnew")),               # re-login after 410
    ])
    respx.post(f"{BASE}/api/agent/getMoney").mock(side_effect=[
        httpx.Response(200, json={"status_code": 410, "message": "Please login again"}),
        httpx.Response(200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("POST", "/api/agent/getMoney")
    assert data == {"money": "5.00"}


@respx.mock
async def test_call_410_after_relogin_raises_auth_failed():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 410, "message": "Please login again"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("POST", "/api/agent/getMoney")
    assert ei.value.reason == "gameroom:auth_failed"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_500_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("POST", "/api/agent/getMoney")


@respx.mock
async def test_call_business_400_is_terminal_mapped():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/player/playerInsert").mock(return_value=httpx.Response(
        200, json={"status_code": 400, "message": "Username already exists"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("POST", "/api/player/playerInsert", fields={"username": "x"})
    assert ei.value.reason == "gameroom:account_exists"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_get_uses_query_params_and_bearer_header():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("Tjwt")))
    route = respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"balance": 60}}))
    async with httpx.AsyncClient() as http:
        await _client(http).call("GET", "/api/player/agentMoney", params={"id": 123})
    sent = route.calls.last.request
    assert dict(sent.url.params) == {"id": "123"}
    assert sent.headers["authorization"] == "Bearer Tjwt"


@respx.mock
async def test_call_transport_error_is_transient():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/agent/getMoney").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("POST", "/api/agent/getMoney")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_client.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gameroom/client.py
import time
from urllib.parse import urlencode

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gameroom.errors import map_response
from app.backends.gameroom.session import CachedSession, SessionStore

_FORM_CT = "application/x-www-form-urlencoded; charset=UTF-8"


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class GameroomClient:
    """Form-urlencoded HTTP client for Gameroom with JWT session caching + single-session-safe refresh."""

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient, session_store: SessionStore, game_id: int,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._session = session_store
        self._game_id = game_id

    # ---- session management ----

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid JWT. Double-checked locking so concurrent workers don't both re-login.

        If `invalidate` is given and the cache still holds exactly that value (or is empty/expired),
        force a fresh login. If the cache already holds a different (presumably fresher) token,
        return it without logging in.
        """
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate and not _expired(cached):
            return cached.token
        async with self._session.login_lock(self._game_id, ttl_seconds=10, acquire_timeout=10.0):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate and not _expired(cached):
                return cached.token
            token, expires_at = await self._do_login()
            ttl = max(60, expires_at - int(time.time()) - 60)
            await self._session.set(self._game_id, CachedSession(token=token, expires_at=expires_at), ttl_seconds=ttl)
            return token

    async def _do_login(self) -> tuple[str, int]:
        url = f"{self._base_url}/api/login"
        body = urlencode({"username": self._username, "password": self._password})   # captcha omitted (server ignores)
        try:
            resp = await self._http.post(
                url, content=body.encode(),
                headers={"Content-Type": _FORM_CT, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gameroom:login_transport:{type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:login_http_{resp.status_code}")
        try:
            body_json = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:login_bad_response") from exc
        sc = body_json.get("status_code")
        if sc == 200:
            token = body_json.get("token")
            exp = body_json.get("expires_time")
            if not isinstance(token, str) or not token or not isinstance(exp, int):
                raise TransientBackendError("gameroom:login_missing_token")
            return token, exp
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body_json.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)

    # ---- request ----

    async def call(self, method: str, path: str, *,
                   fields: dict[str, str | int] | None = None,
                   params: dict[str, str | int] | None = None) -> dict:
        """Issue one request, transparently re-login + retry once on status_code:410."""
        token = await self.get_token()
        resp = await self._http_request(method, path, token, fields=fields, params=params)
        if self._is_410(resp):
            fresh = await self.get_token(invalidate=token)
            resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if self._is_410(resp):
                raise BackendError("gameroom:auth_failed")
        return self._classify(resp)

    async def _http_request(self, method: str, path: str, token: str, *,
                            fields=None, params=None) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            if method == "GET":
                return await self._http.get(url, params=_str_map(params or {}), headers=headers)
            body = urlencode(_str_map(fields or {}))
            headers["Content-Type"] = _FORM_CT
            return await self._http.post(url, content=body.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gameroom:transport:{type(exc).__name__}") from exc

    def _classify(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:http_{resp.status_code}")
        if resp.status_code >= 300 and resp.status_code != 200:
            raise BackendError(f"gameroom:http_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:bad_response") from exc
        sc = body.get("status_code")
        if sc == 200:
            # `data` may be missing (e.g. agentWithdraw success). Top-level keys (token, money, etc.)
            # are also exposed so callers can read e.g. login's top-level money / agent/getMoney's
            # money fallback.
            data = body.get("data")
            if isinstance(data, dict):
                return data
            return {k: v for k, v in body.items() if k not in {"status_code", "message", "code", "data"}}
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)

    @staticmethod
    def _is_410(resp: httpx.Response) -> bool:
        if resp.status_code != 200:
            return False
        try:
            return resp.json().get("status_code") == 410
        except ValueError:
            return False


def _str_map(d: dict) -> dict[str, str]:
    return {k: ("" if v is None else str(v)) for k, v in d.items()}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_client.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gameroom/client.py tests/unit/test_gameroom_client.py
git commit -m "feat(gameroom): GameroomClient — JWT login, double-checked refresh, 410-retry

Single-session-safe: get_token(invalidate=...) under a Redis SET-NX lock skips
the actual /api/login if another worker has already cached a fresher token.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `gameroom/backend.py` — the 6 ops

**Files:**
- Create: `app/backends/gameroom/backend.py`
- Test: `tests/unit/test_gameroom_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gameroom_backend.py
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.gameroom.backend import GameroomBackend, _to_cents, _to_dollars
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.session import InMemorySessionStore

BASE = "https://gr.test"


def _creds():
    return GameCredentials(
        game_id=11, name="Gameroom",
        backend_url=BASE, login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )


def _ctx(*, account=True, external="2998032", username="apifull9983654",
         idem="idem-1", account_username=None, user_id=51):
    acct = AccountIdentity(3001, user_id, 11, username, external) if account else None
    return BackendContext(credentials=_creds(), user_id=user_id, account=acct,
                          idempotency_key=idem, account_username=account_username)


def _backend(http):
    client = GameroomClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=InMemorySessionStore(), game_id=11,
    )
    return GameroomBackend(client)


def _login_ok():
    return {"status_code": 200, "message": "ok", "token": "Tjwt",
            "expires_time": int(time.time()) + 3600, "money": "5.00"}


def _mock_login():
    respx.post(f"{BASE}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))


def test_unit_helpers():
    assert _to_cents("5.00") == 500
    assert _to_cents("3649.0057") == 364901
    assert _to_cents(0) == 0
    assert _to_dollars(500) == "5"
    assert _to_dollars(510) == "6"        # ceil
    assert _to_dollars(3050) == "31"      # ceil


# ---- AGENT_BALANCE ----

@respx.mock
async def test_agent_balance_reads_data_money():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance_cents == 500


@respx.mock
async def test_agent_balance_falls_back_to_top_level_money():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "money": "5.00"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance_cents == 500


@respx.mock
async def test_agent_balance_missing_value_is_terminal():
    _mock_login()
    respx.post(f"{BASE}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).agent_balance(_ctx(account=False))
    assert ei.value.reason == "gameroom:agent_balance_missing"


# ---- READ_BALANCE ----

@respx.mock
async def test_read_balance_uses_external_user_id_and_returns_cents():
    _mock_login()
    route = respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok",
                   "data": {"username": "apifull9983654", "balance": 0, "cusBlance": "4.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx())
    assert r.balance_cents == 0
    assert dict(route.calls.last.request.url.params) == {"id": "2998032"}


# ---- player_id fallback ----

@respx.mock
async def test_player_id_falls_back_to_userList_exact_match():
    _mock_login()
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "count": 2, "data": [
            {"Id": 1, "id": 1, "Account": "user_no_ext_typo", "score": 0},
            {"Id": 99, "id": 99, "Account": "user_no_ext", "score": 0},
        ]}))
    respx.get(f"{BASE}/api/player/agentMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"balance": 5}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert r.balance_cents == 500


@respx.mock
async def test_player_id_no_exact_match_raises_player_not_found():
    _mock_login()
    respx.get(f"{BASE}/api/player/userList").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "count": 1,
                   "data": [{"Id": 99, "id": 99, "Account": "different_user", "score": 0}]}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert ei.value.reason == "gameroom:player_not_found"


# ---- CREATE_ACCOUNT ----

@respx.mock
async def test_create_account_posts_username_and_password_returns_id():
    _mock_login()
    route = respx.post(f"{BASE}/api/player/playerInsert").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Insert successful",
                   "data": {"id": 2998032, "account": "apifull9983654", "password": "Test1122", "balance": "0"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).create_account(_ctx(account=False, account_username="apifull9983654"))
    assert r.username == "apifull9983654" and r.external_user_id == "2998032" and r.password.isalnum()
    body = route.calls.last.request.content.decode()
    assert "username=apifull9983654" in body
    assert "nickname=apifull9983654" in body
    assert "money=0" in body
    assert "password=" in body and "password_confirmation=" in body


@respx.mock
async def test_create_account_requires_account_username():
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


# ---- RECHARGE ----

@respx.mock
async def test_recharge_sends_integer_dollars_and_empty_snapshot_and_remark():
    _mock_login()
    route = respx.post(f"{BASE}/api/player/agentRecharge").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Recharge successful",
                   "data": {"balance": "1", "bonus": 0, "remark": "", "total_balance": "1.00"}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).recharge(_ctx(), amount_cents=5000, bonus_cents=500, total_credit_cents=5510)
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "available_balance=" in body and "available_balance=&" in (body + "&")   # empty
    assert "opera_type=0" in body
    assert "bonus=0" in body
    assert "balance=56" in body                                                     # ceil(5510/100)
    assert "remark=" in body and "remark=&" in (body + "&")                         # empty
    assert r.balance_cents == 100                                                   # round("1.00" * 100)


# ---- REDEEM ----

@respx.mock
async def test_redeem_succeeds_with_no_data_block():
    _mock_login()
    route = respx.post(f"{BASE}/api/player/agentWithdraw").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Withdraw successful"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).redeem(_ctx(), amount_cents=3050)
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "customer_balance=" in body and "customer_balance=&" in (body + "&")
    assert "opera_type=1" in body
    assert "balance=31" in body                                                     # ceil(3050/100)
    assert r.balance_cents is None                                                  # response has no data


@respx.mock
async def test_redeem_insufficient_user_balance_is_terminal():
    _mock_login()
    respx.post(f"{BASE}/api/player/agentWithdraw").mock(return_value=httpx.Response(
        200, json={"status_code": 400,
                   "message": "Withdrawal amount is greater than customer balance. Please check and withdraw again"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).redeem(_ctx(), amount_cents=100)
    assert ei.value.reason == "gameroom:insufficient_user_balance"


# ---- RESET_PASSWORD ----

@respx.mock
async def test_reset_password_posts_complex_password_and_returns_it():
    import re
    _mock_login()
    route = respx.post(f"{BASE}/api/player/reset").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "Reset successful"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http).reset_password(_ctx())
    assert re.fullmatch(r"[A-Z][a-z]+[!@#$%&*]\d{2}", r.password), r.password
    body = route.calls.last.request.content.decode()
    assert "id=2998032" in body
    assert "password=" in body and "password_confirmation=" in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_backend.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Add `call_raw` to `GameroomClient` (for endpoints with `data:[…]` lists)**

`GameroomClient.call()` (Task 6) unwraps the envelope's `data` field when it's a dict. The
`userList` endpoint returns `data: [<rows>]` (a list), so we need an escape hatch that returns the
full envelope. Add this method to `app/backends/gameroom/client.py` (immediately after `call`):

```python
    async def call_raw(self, method: str, path: str, *,
                       fields: dict[str, str | int] | None = None,
                       params: dict[str, str | int] | None = None) -> dict:
        """Like .call() but returns the full envelope (use when `data` is a list, e.g. userList)."""
        token = await self.get_token()
        resp = await self._http_request(method, path, token, fields=fields, params=params)
        if self._is_410(resp):
            fresh = await self.get_token(invalidate=token)
            resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if self._is_410(resp):
                raise BackendError("gameroom:auth_failed")
        if resp.status_code >= 500:
            raise TransientBackendError(f"gameroom:http_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gameroom:bad_response") from exc
        sc = body.get("status_code")
        if sc == 200:
            return body
        reason, terminal = map_response(int(sc) if isinstance(sc, int) else 0, str(body.get("message", "")))
        if not terminal:
            raise TransientBackendError(reason)
        raise BackendError(reason)
```

- [ ] **Step 4: Write `app/backends/gameroom/backend.py`**

```python
# app/backends/gameroom/backend.py
import math

from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.gameroom.client import GameroomClient
from app.backends.gameroom.passwords import (
    generate_memorable_complex_password,
    generate_memorable_password,
)
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _to_cents(value) -> int:
    return round(float(value) * 100)


def _to_cents_opt(value) -> int | None:
    return None if value is None else _to_cents(value)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


class GameroomBackend:
    def __init__(self, client: GameroomClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        # /api/agent/getMoney response shape isn't pinned in the findings doc; .call() unwraps
        # `data` if it's a dict, else returns the top-level keys (where login's `money` lives).
        data = await self._client.call("POST", "/api/agent/getMoney")
        value = data.get("money")
        if value is None:
            raise BackendError("gameroom:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=_to_cents(value))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        pid = await self._player_id(ctx)
        data = await self._client.call("GET", "/api/player/agentMoney", params={"id": pid})
        return ReadBalanceResult(balance_cents=_to_cents(data.get("balance", 0)))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        pid = await self._player_id(ctx)
        pwd = generate_memorable_complex_password()
        await self._client.call(
            "POST", "/api/player/reset",
            fields={"id": pid, "password": pwd, "password_confirmation": pwd},
        )
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        pid = await self._player_id(ctx)
        # available_balance: server ignores the value but the field is required (empty OK).
        # bonus=0: we already credit `total_credit_cents` via balance; bonus is on top per the doc.
        # remark="": UUIDs have hyphens which fail [A-Za-z0-9]; empty is allowed.
        data = await self._client.call(
            "POST", "/api/player/agentRecharge",
            fields={
                "id": pid,
                "available_balance": "",
                "opera_type": 0,
                "bonus": 0,
                "balance": _to_dollars(total_credit_cents),
                "remark": "",
            },
        )
        return RechargeResult(balance_cents=_to_cents_opt(data.get("total_balance")))

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        pid = await self._player_id(ctx)
        # agentWithdraw success returns no `data` block; treat as success and omit balance_cents.
        await self._client.call(
            "POST", "/api/player/agentWithdraw",
            fields={
                "id": pid,
                "customer_balance": "",
                "opera_type": 1,
                "balance": _to_dollars(amount_cents),
                "remark": "",
            },
        )
        return RedeemResult()

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()  # alphanumeric 6-12 (satisfies the create rule)
        data = await self._client.call(
            "POST", "/api/player/playerInsert",
            fields={
                "username": ctx.account_username,
                "nickname": ctx.account_username,
                "money": 0,                   # send defensively; missing money triggers a server bug
                "password": pwd,
                "password_confirmation": pwd,
            },
        )
        new_id = data.get("id")
        if new_id is None:
            raise BackendError("gameroom:playerInsert_missing_id")
        return CreateAccountResult(
            username=ctx.account_username,
            password=pwd,
            external_user_id=str(new_id),
        )

    # ---- internal: player_id resolution ----

    async def _player_id(self, ctx: BackendContext) -> str:
        """Prefer cached external_user_id; else exact-match the player via userList."""
        if ctx.account and ctx.account.external_user_id:
            return ctx.account.external_user_id
        if ctx.account and ctx.account.username:
            envelope = await self._client.call_raw(
                "GET", "/api/player/userList",
                params={"page": 1, "limit": 20, "account": ctx.account.username},
            )
            rows = envelope.get("data") or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("Account") == ctx.account.username:
                        rid = row.get("id") or row.get("Id")
                        if rid is not None:
                            return str(rid)
            raise BackendError("gameroom:player_not_found")
        raise BackendError("gameroom:player_not_found")
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gameroom_backend.py -q`
Expected: PASS (13 tests).

- [ ] **Step 6: Commit (both files together — the client gained `call_raw`)**

```bash
git add app/backends/gameroom/backend.py app/backends/gameroom/client.py tests/unit/test_gameroom_backend.py
git commit -m "feat(gameroom): GameroomBackend — 6 ops + userList player_id fallback

Adds GameroomClient.call_raw for endpoints that return data:[] (userList).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Registry — `NON_IDEMPOTENT_DRIVERS` + `gameroom` driver branch

**Files:**
- Modify: `app/backends/registry.py`
- Test: `tests/unit/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_registry.py`:

```python
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.session import InMemorySessionStore
from app.backends.registry import NON_IDEMPOTENT_DRIVERS, resolve_backend as resolve


def _gameroom_creds():
    return GameCredentials(
        game_id=11, name="g",
        backend_url="https://gr.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )


def test_non_idempotent_drivers_contains_gameroom():
    assert "gameroom" in NON_IDEMPOTENT_DRIVERS
    # gamevault family is deliberately NOT in this set (order_id dedupe makes retries safe)
    assert {"gamevault", "juwa", "juwa2"}.isdisjoint(NON_IDEMPOTENT_DRIVERS)


def test_gameroom_driver_routes_to_gameroom_backend():
    s = _settings()
    backend = resolve_backend(
        "gameroom", credentials=_gameroom_creds(),
        http_client=object(), settings=s, session_store=InMemorySessionStore(),
    )
    assert isinstance(backend, GameroomBackend)


def test_gameroom_missing_session_store_raises():
    s = _settings()
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "gameroom", credentials=_gameroom_creds(),
            http_client=object(), settings=s, session_store=None,
        )
    assert ei.value.reason == "missing_session_store"


def test_gameroom_missing_credentials_raises():
    s = _settings()
    creds = GameCredentials(
        game_id=11, name="g",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="gameroom",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "gameroom", credentials=creds,
            http_client=object(), settings=s, session_store=InMemorySessionStore(),
        )
    assert ei.value.reason == "missing_gameroom_credentials"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: FAIL (4 new tests: NON_IDEMPOTENT_DRIVERS not defined; resolve_backend doesn't accept session_store; gameroom branch missing).

- [ ] **Step 3: Update `app/backends/registry.py`**

Replace the file's contents with:

```python
# app/backends/registry.py
from app.backends.base import BackendError, GameBackend
from app.backends.context import GameCredentials
from app.backends.gameroom.backend import GameroomBackend
from app.backends.gameroom.client import GameroomClient
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.gamevault.client import GameVaultClient
from app.backends.mock.backend import MockBackend
from app.config import Settings

# Driver strings that share the GameVault provider's wire protocol (auth, endpoints, envelope).
_GAMEVAULT_PROVIDER_DRIVERS = frozenset({"gamevault", "juwa", "juwa2"})

# Drivers with no server-side idempotency (no order_id/dedupe). The API endpoint passes
# arq _max_tries=1 for these so a worker crash mid-money-op cannot double-apply funds.
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({"gameroom"})


def resolve_backend(
    driver: str | None, *,
    credentials: GameCredentials,
    http_client,
    settings: Settings,
    session_store=None,
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend; `gamevault`/`juwa`/`juwa2` -> GameVaultBackend (same provider,
    per-game creds); `gameroom` -> GameroomBackend (requires session_store). Unknown -> BackendError.
    """
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
    if key in _GAMEVAULT_PROVIDER_DRIVERS:
        if not (credentials.api_base_url and credentials.api_agent_id and credentials.api_secret_key):
            raise BackendError("missing_gamevault_credentials")
        return GameVaultBackend(
            GameVaultClient(
                base_url=credentials.api_base_url,
                agent_id=credentials.api_agent_id,
                secret_key=credentials.api_secret_key,
                http_client=http_client,
            )
        )
    if key == "gameroom":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_gameroom_credentials")
        if session_store is None:
            raise BackendError("missing_session_store")
        return GameroomBackend(
            GameroomClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=session_store,
                game_id=credentials.game_id,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: PASS.

- [ ] **Step 5: Run full suite (ensure existing gamevault/mock tests still pass with the new signature)**

Run: `.venv/bin/python -m pytest -q`
Expected: all green (existing callers still work because `session_store` defaults to `None`).

- [ ] **Step 6: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(backends): driver-aware resolve_backend with gameroom + NON_IDEMPOTENT_DRIVERS

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Executor — accept and thread `session_store`

**Files:**
- Modify: `app/operations/executor.py`
- Test: `tests/integration/test_executor.py` (existing tests must keep passing)

- [ ] **Step 1: Update `execute_operation` signature**

In `app/operations/executor.py`:

1. Change the signature to accept `session_store`:

```python
async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    result_cache: ResultCache | None = None,
    session_store=None,
    resolve=_resolve_backend,
) -> None:
```

2. In the backend resolution block, pass `session_store` through:

```python
    try:
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver,
            credentials=ctx.credentials,
            http_client=http_client,
            settings=settings,
            session_store=session_store,
        )
    except BackendError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, exc.reason))
        return
```

- [ ] **Step 2: Run existing executor tests to confirm no regression**

Run: `.venv/bin/python -m pytest tests/integration/test_executor.py tests/integration/test_executor_cache.py tests/integration/test_full_loop.py -q`
Expected: PASS (existing tests don't pass `session_store`; the default `None` keeps mock/gamevault paths intact).

- [ ] **Step 3: Add a regression test that gameroom routing fails cleanly without a session_store**

Append to `tests/integration/test_executor_cache.py`:

```python
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
```

- [ ] **Step 4: Run + commit**

Run: `.venv/bin/python -m pytest tests/integration/test_executor_cache.py -q`
Expected: PASS (3 cache tests + 1 new).

Run the full gate: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all green.

```bash
git add app/operations/executor.py tests/integration/test_executor_cache.py
git commit -m "feat(operations): executor threads session_store to the backend registry

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Worker — construct `RedisSessionStore`, inject into `ctx`

**Files:**
- Modify: `app/worker/settings.py`, `app/worker/tasks.py`
- Test: `tests/unit/test_worker_tasks.py`

- [ ] **Step 1: Update the worker task test**

Replace `tests/unit/test_worker_tasks.py`:

```python
# tests/unit/test_worker_tasks.py
import app.worker.tasks as tasks


async def test_task_delegates_to_executor_with_all_resources(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)

    class FakeClient: ...
    class FakeCache: ...
    class FakeSessionStore: ...

    ctx = {
        "http_client": FakeClient(),
        "session_factory": seeded,
        "result_cache": FakeCache(),
        "session_store": FakeSessionStore(),
    }
    payload = {"idempotency_key": "k", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
    assert captured["kwargs"]["result_cache"] is ctx["result_cache"]
    assert captured["kwargs"]["session_store"] is ctx["session_store"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: FAIL (task doesn't pass `session_store`).

- [ ] **Step 3: Update `app/worker/tasks.py`**

```python
# app/worker/tasks.py
from app.config import get_settings
from app.operations.executor import execute_operation


async def execute_operation_task(ctx: dict, payload: dict) -> None:
    await execute_operation(
        payload,
        session_factory=ctx["session_factory"],
        http_client=ctx["http_client"],
        settings=get_settings(),
        result_cache=ctx["result_cache"],
        session_store=ctx["session_store"],
    )
```

- [ ] **Step 4: Update `app/worker/settings.py`**

```python
# app/worker/settings.py
import httpx
import redis.asyncio as redis_asyncio
from arq.connections import RedisSettings

from app.backends.gameroom.session import RedisSessionStore
from app.config import get_settings, require_runtime_settings
from app.db.engine import get_sessionmaker
from app.logging import configure_logging
from app.operations.result_cache import RedisResultCache
from app.worker.tasks import execute_operation_task


async def startup(ctx: dict) -> None:
    configure_logging()
    settings = get_settings()
    require_runtime_settings(settings)
    ctx["http_client"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    ctx["session_factory"] = get_sessionmaker()
    ctx["redis_cache"] = redis_asyncio.from_url(settings.redis_url)
    ctx["result_cache"] = RedisResultCache(ctx["redis_cache"])
    ctx["session_store"] = RedisSessionStore(ctx["redis_cache"])     # shares the redis client


async def shutdown(ctx: dict) -> None:
    await ctx["http_client"].aclose()
    await ctx["redis_cache"].aclose()


class WorkerSettings:
    functions = [execute_operation_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Default job timeout: must exceed the webhook retry budget so a still-retrying job is not killed.
    job_timeout = int(get_settings().webhook_max_budget_seconds) + 60
    # Default backstop for worker crashes. SAFE for idempotent drivers (GameVault family: order_id
    # dedupe). For non-idempotent drivers (e.g. gameroom), the API endpoint passes _max_tries=1
    # on enqueue, overriding this default per-job.
    max_tries = 3
    keep_result = 0
```

- [ ] **Step 5: Run worker task test + verify import without live Redis**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: PASS.
Run: `.venv/bin/python -c "import app.worker.settings; print('OK')"`
Expected: `OK` (importing does not connect to Redis).

- [ ] **Step 6: Commit**

```bash
git add app/worker/settings.py app/worker/tasks.py tests/unit/test_worker_tasks.py
git commit -m "feat(worker): inject RedisSessionStore into ctx (shares the existing redis client)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: API endpoint — per-driver `_max_tries` via DB peek

**Files:**
- Modify: `app/main.py`, `app/api/operations.py`
- Test: `tests/integration/test_operations_endpoint.py`

- [ ] **Step 1: Update the endpoint test**

Append to `tests/integration/test_operations_endpoint.py`:

```python
async def test_idempotent_driver_uses_default_max_tries(monkeypatch, seeded, app):
    # Wire a real session_factory so the endpoint can peek the driver.
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gv-1", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"func": func, "payload": payload, "_job_id": _job_id, "_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    assert app.state.arq.jobs[0]["_max_tries"] is None or app.state.arq.jobs[0]["_max_tries"] == 3


async def test_gameroom_driver_uses_max_tries_1(seeded, app):
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "gr-1", "type": "AGENT_BALANCE", "game_id": 11},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    assert app.state.arq.jobs[0]["_max_tries"] == 1


async def test_unknown_game_id_falls_back_to_default(seeded, app):
    app.state.session_factory = seeded
    body = json.dumps(
        {"idempotency_key": "u-1", "type": "AGENT_BALANCE", "game_id": 99999},
        separators=(",", ":"),
    )
    headers = sign("s", body)

    class CapturingArq:
        def __init__(self): self.jobs = []
        async def enqueue_job(self, func, payload, _job_id=None, _max_tries=None):
            self.jobs.append({"_max_tries": _max_tries})
    app.state.arq = CapturingArq()

    async with await _client(app) as c:
        resp = await c.post("/operations", content=body, headers=headers)
    assert resp.status_code == 202
    # Default policy (None / 3); preflight in the worker will fail with game_not_found later.
    assert app.state.arq.jobs[0]["_max_tries"] in (None, 3)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/integration/test_operations_endpoint.py -q`
Expected: FAIL on the gameroom case (default max_tries used; no DB peek).

- [ ] **Step 3: Expose `session_factory` on `app.state` in `main.py`**

In `app/main.py`, in the `lifespan` function, after `app.state.arq = await create_pool(...)` add:

```python
    from app.db.engine import get_sessionmaker
    app.state.session_factory = get_sessionmaker()
```

(Top-level import is also fine: `from app.db.engine import get_sessionmaker` near the other imports.)

- [ ] **Step 4: Update `app/api/operations.py`**

Replace the file's contents with:

```python
# app/api/operations.py
import json

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import verify_signature
from app.backends.registry import NON_IDEMPOTENT_DRIVERS
from app.db.repositories import GamesRepository
from app.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/operations")
async def receive_operation(
    request: Request, raw: bytes = Depends(verify_signature)
) -> Response:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("operation_unparseable_body", phase="received")
        return Response(status_code=400)

    key = data.get("idempotency_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or key == "":
        logger.warning("operation_missing_idempotency_key", phase="received")
        return Response(status_code=400)

    # Per-driver retry policy: peek the game's driver to decide arq's _max_tries.
    # Default (3) is safe for idempotent drivers (GameVault family). Non-idempotent
    # drivers (gameroom) get _max_tries=1 so a worker crash can't double-apply funds.
    max_tries: int | None = None
    game_id = data.get("game_id") if isinstance(data, dict) else None
    if isinstance(game_id, int):
        try:
            session_factory = getattr(request.app.state, "session_factory", None)
            if session_factory is not None:
                async with session_factory() as session:
                    driver = await GamesRepository(session).get_driver(game_id)
                if driver and driver.lower() in NON_IDEMPOTENT_DRIVERS:
                    max_tries = 1
        except Exception:  # noqa: BLE001 - DB blip: fall back to default; preflight surfaces the real error
            logger.exception("driver_peek_failed", idempotency_key=key, phase="received")

    try:
        await request.app.state.arq.enqueue_job(
            "execute_operation_task", data, _job_id=key, _max_tries=max_tries,
        )
    except Exception:  # noqa: BLE001 - any enqueue failure must surface as a non-202
        logger.exception("operation_enqueue_failed", idempotency_key=key, phase="enqueued")
        return Response(status_code=500)
    logger.bind(idempotency_key=key, phase="enqueued").info("operation_enqueued", max_tries=max_tries)
    return Response(status_code=202)
```

- [ ] **Step 5: Run the endpoint tests + full gate**

Run: `.venv/bin/python -m pytest tests/integration/test_operations_endpoint.py -q`
Expected: PASS (existing 4 + 3 new).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/main.py app/api/operations.py tests/integration/test_operations_endpoint.py
git commit -m "feat(api): per-driver arq _max_tries (gameroom -> 1 via DB peek)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 12: Gameroom integration test + docs

**Files:**
- Create: `tests/integration/test_gameroom_integration.py`
- Modify: `CLAUDE.md`, `docs/architecture.md`, `docs/runbook.md`

- [ ] **Step 1: Write the integration test**

```python
# tests/integration/test_gameroom_integration.py
import json
import time

import httpx
import respx

from app.config import Settings
from app.backends.gameroom.session import InMemorySessionStore
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GR = "https://gr.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


def _login_ok():
    return {"status_code": 200, "message": "ok", "token": "Tjwt",
            "expires_time": int(time.time()) + 3600, "money": "5.00"}


@respx.mock
async def test_gameroom_agent_balance_end_to_end(seeded):
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gr-ab-1", "type": "AGENT_BALANCE", "game_id": 11}
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded" and sent["result"]["agent_balance_cents"] == 500


@respx.mock
async def test_gameroom_terminal_failure_cached_and_not_recalled(seeded):
    # 430 (wrong creds) is terminal: cached so a re-run does NOT re-call gameroom.
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(
        200, json={"status_code": 430, "message": "Username or password error"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = {"idempotency_key": "gr-430", "type": "AGENT_BALANCE", "game_id": 11}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
        assert login.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
    assert login.call_count == 1                          # cache hit -> no second login
    cached = await cache.get("gr-430")
    assert cached and cached.status == "failed" and "auth_failed" in cached.reason


@respx.mock
async def test_gameroom_transient_failure_not_cached_recalls_backend(seeded):
    respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    gm = respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(500))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    payload = {"idempotency_key": "gr-500", "type": "AGENT_BALANCE", "game_id": 11}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, session_store=store)
    assert gm.call_count == 2                              # transient -> not cached -> called twice
    assert await cache.get("gr-500") is None


@respx.mock
async def test_gameroom_session_is_reused_across_ops(seeded):
    """Two ops in a row should issue exactly ONE /api/login (shared via the session store)."""
    login = respx.post(f"{GR}/api/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GR}/api/agent/getMoney").mock(return_value=httpx.Response(
        200, json={"status_code": 200, "message": "ok", "data": {"money": "5.00"}}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    store = InMemorySessionStore()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            {"idempotency_key": "gr-share-1", "type": "AGENT_BALANCE", "game_id": 11},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
        await execute_operation(
            {"idempotency_key": "gr-share-2", "type": "AGENT_BALANCE", "game_id": 11},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, session_store=store,
        )
    assert login.call_count == 1                            # shared session -> only one login
```

- [ ] **Step 2: Run the integration test + full gate**

Run: `.venv/bin/python -m pytest tests/integration/test_gameroom_integration.py -q`
Expected: PASS (4 tests).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all green.

- [ ] **Step 3: Update `CLAUDE.md`**

In `CLAUDE.md`, edit the "Backend selection" bullet to add `gameroom`:

Replace:
```markdown
- Backend selection comes from `games.backend_driver` (read-only): `mock` | `gamevault` | `juwa` |
  `juwa2`. New backends add a module + a `resolve_backend` branch; sibling games on an existing
  provider (e.g. `juwa`/`juwa2` share GameVault's API) are added as an alias in the registry.
```

With:
```markdown
- Backend selection comes from `games.backend_driver` (read-only): `mock` | `gamevault` | `juwa` |
  `juwa2` | `gameroom`. New backends add a module + a `resolve_backend` branch; sibling games on an
  existing provider (e.g. `juwa`/`juwa2` share GameVault's API) are added as an alias in the registry.
- Non-idempotent drivers (no server-side `order_id` dedupe — currently `gameroom`) are listed in
  `NON_IDEMPOTENT_DRIVERS`; the `/operations` endpoint passes arq `_max_tries=1` for these so a
  worker crash can't double-apply funds. Reaper at Laravel's 10-min mark handles the orphan.
- Gameroom: JWT bearer auth (~6h sessions) cached in Redis via `app/backends/gameroom/session.py`.
  Re-login on `status_code:410` uses **double-checked locking** (`get_token(invalidate=...)`) to
  stay safe under Gameroom's single-session-per-agent enforcement.
```

In the "Where things live" section, append:

```markdown
- Gameroom backend: `app/backends/gameroom/` (client, backend, errors, passwords, session).
```

- [ ] **Step 4: Update `docs/architecture.md`**

Append:

```markdown
## Reverse-engineered backends (Gameroom)
Gameroom (`app/backends/gameroom/`) is the first session-holding backend: form-urlencoded POST, JWT
bearer auth with ~6h sessions, and a `{status_code, message, data?}` envelope (`status_code` is the
real status, not HTTP). The captcha is client-side only and the server ignores it; we omit the field.

**Session storage:** `RedisSessionStore` (`gameroom_session:{game_id}`) is shared across all workers
so they reuse one JWT per game. **Login lock** (`SET NX gameroom_login:{game_id} ex=10`) serializes
concurrent logins — important because Gameroom allows only one active session per agent. On
`status_code:410`, `GameroomClient.get_token(invalidate=<dead_token>)` does double-checked-locking:
if the cache already holds a different (presumably fresher) token, no login happens. This prevents
two workers from re-logging-in simultaneously and invalidating each other's session.

**Money safety on non-idempotent backends:** Gameroom has no `order_id` / dedupe. We register
`gameroom` in `NON_IDEMPOTENT_DRIVERS` (`app/backends/registry.py`). The `/operations` endpoint
peeks the game's driver via `GamesRepository.get_driver(game_id)` and passes arq `_max_tries=1` for
non-idempotent drivers, so a worker crash mid-money-op cannot retry and double-apply. Laravel's
10-min reaper marks the op failed + refunds the wallet; the operator reconciles any in-game balance
change manually via Gameroom's dashboard.
```

- [ ] **Step 5: Update `docs/runbook.md`**

Append:

```markdown
## Gameroom (JWT-session reverse-engineered backend)
- Set `games.backend_driver='gameroom'` plus `backend_url` / `backend_username` / `backend_password`
  (the agent's login credentials). No `api_*` columns needed.
- Sessions are cached in Redis (`gameroom_session:{game_id}`) and shared across workers. First op on
  a fresh game lazily logs in; subsequent ops reuse the JWT (TTL = expiry - 60s buffer).
- A worker crash during RECHARGE/REDEEM does NOT retry (per-driver `_max_tries=1`). Laravel's reaper
  fails+refunds the operation at the 10-min mark; if the gameroom call had already applied, the
  operator reconciles via the gameroom dashboard.
- Common reasons: `gameroom:account_exists`, `gameroom:insufficient_agent_balance`,
  `gameroom:insufficient_user_balance`, `gameroom:operation_failed` (opaque, often missing player),
  `gameroom:auth_failed` (creds wrong / session can't be refreshed). Transient: `gameroom:server_error`,
  network/5xx.
- To force a session refresh: `redis-cli DEL gameroom_session:<game_id>`. Next op will re-login.
```

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_gameroom_integration.py CLAUDE.md docs/architecture.md docs/runbook.md
git commit -m "test+docs(gameroom): integration tests + CLAUDE.md/architecture/runbook updates

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase-3 acceptance (manual, after the suite is green)

Against the real Gameroom test agent from the findings doc (`TestGR159 / TestGR1122@`):

1. In Filament, add a Gameroom game: `backend_driver='gameroom'`,
   `backend_url='https://agentserver1.gameroom777.com'`, `backend_username='TestGR159'`,
   `backend_password='TestGR1122@'`. (If `backend_driver` is enum-restricted, add `gameroom` to the
   enum.)
2. From the host with the venv (`/Applications/development/python/casino-app-automation`):
   - `cp .env.example .env` (if not done) and ensure `PYTHON_SIGNING_SECRET` matches Laravel,
     `APP_URL` points at local Laravel, `REDIS_URL` is reachable, `DB_DRIVER=aiomysql`.
   - `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001` (one shell)
   - `.venv/bin/arq app.worker.settings.WorkerSettings` (another shell)
   - `.venv/bin/python -m app.tools.ping` → expect `200 {"ok":true}`.
3. Trigger ops from Laravel against the new Gameroom game:
   - **AGENT_BALANCE** → SUCCEEDED with the real money figure. Verify cents match GameRoom UI.
   - **CREATE_ACCOUNT** (Laravel-generated `account_username`) → SUCCEEDED; player visible in
     Gameroom's player list with the generated memorable password.
   - **READ_BALANCE** on that player → SUCCEEDED with `balance_cents:0`.
   - **RECHARGE** small amount → SUCCEEDED; player balance increases by ceil(cents/100) dollars;
     agent balance decreases by the same.
   - **REDEEM** → SUCCEEDED (or `gameroom:insufficient_user_balance` if the player is broke).
   - **RESET_PASSWORD** → SUCCEEDED; new memorable-complex password (e.g. `Tiger@47`) stored Laravel-side.
4. **Session reuse:** trigger a second AGENT_BALANCE; check the worker log shows no `login_*` event
   between calls. To force a re-login: `redis-cli DEL gameroom_session:<game_id>`, trigger again,
   verify a fresh login happens and the op still succeeds.
5. **410 path (advanced):** manually write a bogus token to Redis
   (`redis-cli SET gameroom_session:<game_id> '{"token":"junk","expires_at":9999999999}' EX 3600`)
   and trigger an op — confirm the worker handles 410 transparently (re-login + retry once → success).

---

## Self-review (completed by plan author)

**Spec coverage:**
- §3 API summary → Tasks 3 (errors), 6 (client), 7 (backend).
- §4 R1 money units → Task 7 (`_to_cents` / `_to_dollars`).
- §4 R2 per-driver `_max_tries=1` → Task 8 (registry constant) + Task 11 (endpoint).
- §4 R3-R5 snapshot/remark/redeem result → Task 7 backend tests assert empty snapshot/remark and `RedeemResult()` (no balance).
- §4 R6 player_id userList fallback → Task 7 `_player_id` + tests.
- §4 R7 double-checked-locking → Task 6 `get_token(invalidate=...)` + dedicated regression test.
- §4 R8 login lock → Task 5 `RedisSessionStore.login_lock` + Task 6 concurrent-login test.
- §4 R9 captcha omitted → Task 6 `_do_login` test asserts no `captcha` field.
- §6.1 errors → Task 3.
- §6.2 passwords → Task 4 (complex + re-export alphanumeric).
- §6.3 session store + lock → Task 5.
- §6.4 client → Task 6.
- §6.5 backend → Task 7.
- §6.6 registry → Task 8.
- §6.7 executor → Task 9.
- §6.8 endpoint + main.py → Task 11.
- §7-§8 error/operational matrix → Tasks 3 + 6 cover; integration test (Task 12) exercises the
  cache-hit-on-terminal-failure semantic.
- §9 testing → covered by Tasks 1-12.
- §10 Laravel deps (none new) → Task 12 runbook documents the operator setup.
- §11 deferred → noted in CLAUDE.md (max_tries=1 caveat) and runbook (operator reconcile).

**Placeholder scan:** No TBD/TODO. Every code step shows complete, runnable code. Task 7 splits
across two files (a `call_raw` helper added to the client + the new `backend.py`) and commits both
together in Step 6, since the backend's `_player_id` depends on `call_raw`.

**Type consistency:**
- `BackendContext(credentials, user_id, account, idempotency_key, account_username)` (unchanged from Phase 2).
- `GameCredentials(..., backend_driver)` (unchanged).
- `resolve_backend(driver, *, credentials, http_client, settings, session_store=None)` — used consistently across Tasks 8-9 and 11.
- `GameroomClient.get_token(invalidate=None)`, `.call(method, path, *, fields=None, params=None)`,
  `.call_raw(...)` — same signatures across Tasks 6 and 7.
- `CachedSession(token, expires_at)`, `SessionStore.{get,set,clear,login_lock}` — same across Tasks 5, 6, 7.
- `NON_IDEMPOTENT_DRIVERS` (Task 8) read by Task 11 endpoint.
- `GamesRepository.get_driver(game_id) -> str | None` (Task 2) used by Task 11 endpoint.
- `execute_operation(..., session_store=None)` (Task 9) wired by Task 10 worker tasks.
