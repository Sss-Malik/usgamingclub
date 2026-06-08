# Phase 4 — Golden Treasure Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Golden Treasure (`agent.goldentreasure.mobi`) — our second reverse-engineered backend — behind the existing `GameBackend` abstraction, with MD5 body signing + AES-128-ECB login encryption + per-request `x-token` rebuild + a per-game Redis throttle for the strict `code:167` rate limit.

**Architecture:** Pure-functions `crypto.py` (MD5 sign, AES-128-ECB, x-token header) tested against the findings doc's verified oracles. `GoldenTreasureClient` caches a JWT-like session token in Redis (concurrent tokens allowed → no double-checked locking, just a login lock), rebuilds `x-token`/`x-time` per request, and gates mutating ops (`savePlayer`/`enterScore`) behind a `SET NX gtreasure_throttle:{game_id} ex=5` Redis lock. `NON_IDEMPOTENT_DRIVERS += {"goldentreasure"}` so the API endpoint auto-applies `_max_tries=1`. Adds a `redis=None` kwarg to `resolve_backend` for backends that need raw Redis access (Phase 3's `session_store=` plumbing stays untouched).

**Tech Stack:** httpx (async, JSON), `pycryptodome` (AES-128-ECB-PKCS7), `redis.asyncio` + `fakeredis` (cache + throttle), Pydantic v2, SQLAlchemy 2.0 (read-only), pytest + respx.

**Spec:** `docs/superpowers/specs/2026-06-08-phase4-goldentreasure-design.md`
**Findings:** `/Applications/development/goldentreasure-standalone/goldentreasure_api_findings.md`

**Environment:** branch `feat/phase4-goldentreasure` (already checked out); venv at `.venv` (use `.venv/bin/python -m ...`). On this machine `head` is an HTTP tool — never pipe to it; use `sed -n` or your Read tool.

---

## File structure (this phase)

```
Create:
  app/backends/goldentreasure/__init__.py           (empty package marker)
  app/backends/goldentreasure/errors.py             - map_response(code, msg) -> (reason, terminal) (Task 3)
  app/backends/goldentreasure/crypto.py             - pure funcs: sign_body, aes_b64, login_aes_key, xtoken_header (Task 4)
  app/backends/goldentreasure/passwords.py          - re-export memorable alphanumeric (Task 5)
  app/backends/goldentreasure/session.py            - CachedSession + SessionStore + In/Redis impls + login lock (Task 6)
  app/backends/goldentreasure/client.py             - GoldenTreasureClient (Task 7)
  app/backends/goldentreasure/backend.py            - GoldenTreasureBackend: 6 ops (Task 8)

Modify:
  pyproject.toml                                    - add pycryptodome>=3.20 to runtime deps (Task 1)
  tests/conftest.py                                 - seed goldentreasure game + accounts (Task 1)
  app/preflight/checks.py                           - missing_goldentreasure_credentials guard (Task 2)
  app/backends/registry.py                          - NON_IDEMPOTENT_DRIVERS += goldentreasure; redis=None kwarg; goldentreasure branch (Task 9)
  app/operations/executor.py                        - redis=None kwarg threaded to resolve (Task 10)
  app/worker/tasks.py                               - pass redis=ctx["redis_cache"] (Task 11)
  CLAUDE.md, docs/architecture.md, docs/runbook.md  (Task 12)
```

---

## Task 1: Test scaffolding — pycryptodome runtime dep + seeded goldentreasure fixtures

**Files:**
- Modify: `pyproject.toml`, `tests/conftest.py`

- [ ] **Step 1: Add `pycryptodome` to runtime deps**

In `pyproject.toml`, in the top-level `dependencies` list (alongside `httpx`/`pydantic`/etc.):

```toml
    "pycryptodome>=3.20",
```

Install: `.venv/bin/python -m pip install --quiet "pycryptodome>=3.20"` then verify:
`.venv/bin/python -c "from Crypto.Cipher import AES; print('OK')"`
Expected: `OK`.

- [ ] **Step 2: Extend `tests/conftest.py` with goldentreasure seeds**

Inside the existing `seeded` fixture's `async with session_factory() as s:` block, after the existing `s.add(GameAccount(id=3002, ...))` block (the gameroom no-ext account), add:

```python
        s.add(
            Game(
                id=13,
                name="Golden Treasure",
                active=True,
                backend_driver="goldentreasure",
                backend_url="https://gt.test",
                backend_username="Test02Gd1WEB",
                backend_password="Zaeem@1233",
            )
        )
        s.add(
            Game(id=14, name="GT NoCreds", active=True, backend_driver="goldentreasure"),
        )
        s.add(
            GameAccount(
                id=4001, user_id=61, game_id=13, username="apitest01",
                password="x", external_user_id=None,           # gtreasure ops key on username
            )
        )
```

- [ ] **Step 3: Verify the new rows + the existing `fake_redis` fixture both work**

```python
# tests/unit/test_phase4_fixtures.py   (temporary smoke test — deleted in Step 5)
from sqlalchemy import text


async def test_goldentreasure_rows_seeded(seeded):
    async with seeded() as s:
        row = (await s.execute(text(
            "SELECT name, backend_driver, backend_url, backend_username FROM games WHERE id=13"
        ))).first()
    assert row == ("Golden Treasure", "goldentreasure", "https://gt.test", "Test02Gd1WEB")


async def test_pycryptodome_aes_roundtrip(fake_redis):
    # Sanity: pycryptodome's AES-128-ECB is what crypto.py will use.
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    key = b"1231779281935abc"
    cipher = AES.new(key, AES.MODE_ECB)
    ct = cipher.encrypt(pad(b"Test02Gd1WEB", 16))
    pt = unpad(AES.new(key, AES.MODE_ECB).decrypt(ct), 16)
    assert pt == b"Test02Gd1WEB"
```

Run: `.venv/bin/python -m pytest tests/unit/test_phase4_fixtures.py -v`
Expected: `2 passed`.

- [ ] **Step 4: Run the full suite to confirm no regression**

Run: `.venv/bin/python -m pytest -q`
Expected: all previously-passing tests still pass; the 2 new ones pass.

- [ ] **Step 5: Delete the smoke test and commit**

```bash
rm tests/unit/test_phase4_fixtures.py
git add pyproject.toml tests/conftest.py
git commit -m "chore(phase4): pycryptodome runtime dep + seed Golden Treasure test rows

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Preflight goldentreasure credentials guard

**Files:**
- Modify: `app/preflight/checks.py`
- Test: `tests/unit/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_preflight.py`:

```python
async def test_goldentreasure_game_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", idempotency_key="k", user_id=None,
                game_id=14, game_account_id=None,           # game 14: gtreasure, no creds
            )
    assert "missing_goldentreasure_credentials" in ei.value.reason


async def test_goldentreasure_context_carries_credentials(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=61,
            game_id=13, game_account_id=4001,
        )
    assert ctx.credentials.backend_driver == "goldentreasure"
    assert ctx.credentials.backend_url == "https://gt.test"
    assert ctx.credentials.backend_username == "Test02Gd1WEB"
    assert ctx.account.username == "apitest01"
    assert ctx.account.external_user_id is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py::test_goldentreasure_game_missing_credentials_raises -v`
Expected: FAIL — preflight doesn't guard goldentreasure.

- [ ] **Step 3: Add the guard**

In `app/preflight/checks.py`, immediately after the existing `if (game.backend_driver or "").lower() == "gameroom"` block, add:

```python
    if (game.backend_driver or "").lower() == "goldentreasure" and not (
        game.backend_url and game.backend_username and game.backend_password
    ):
        raise PreflightError("missing_goldentreasure_credentials")
```

- [ ] **Step 4: Run all preflight tests + full suite**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py -q`
Expected: PASS (new tests + all pre-existing).
Run: `.venv/bin/python -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add app/preflight/checks.py tests/unit/test_preflight.py
git commit -m "feat(preflight): missing_goldentreasure_credentials guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `goldentreasure/errors.py` — code + message mapping

**Files:**
- Create: `app/backends/goldentreasure/__init__.py` (empty), `app/backends/goldentreasure/errors.py`
- Test: `tests/unit/test_goldentreasure_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_errors.py
from app.backends.goldentreasure.errors import map_response


def test_167_is_transient_rate_limited():
    reason, terminal = map_response(167, "high frequency request")
    assert reason == "gtreasure:rate_limited"
    assert terminal is False


def test_known_terminal_codes():
    cases = [
        (8, "gtreasure:account_exists"),
        (21, "gtreasure:operation_refused"),
        (52, "gtreasure:no_permission"),
        (1003, "gtreasure:invalid_password_format"),
        (-3, "gtreasure:token_invalid"),
        (-17, "gtreasure:token_expired"),
        (30100, "gtreasure:system_verify_required"),
        (30200, "gtreasure:google_auth_bind_required"),
        (30201, "gtreasure:google_auth_verify_required"),
    ]
    for code, expected in cases:
        reason, terminal = map_response(code, "msg")
        assert reason == expected, (code, reason)
        assert terminal is True


def test_unknown_code_truncates_message():
    msg = "x" * 200
    reason, terminal = map_response(9999, msg)
    assert reason.startswith("gtreasure:code_9999: ")
    assert len(reason) <= len("gtreasure:code_9999: ") + 80
    assert terminal is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_errors.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/errors.py
GTREASURE_STATUS: dict[int, str] = {
    8: "account_exists",
    21: "operation_refused",        # over-limit / insufficient — misleading "server maintenance" msg
    52: "no_permission",            # surfaces only if relogin retry also fails
    167: "rate_limited",
    1003: "invalid_password_format",
    -3: "token_invalid",
    -17: "token_expired",
    30100: "system_verify_required",
    30200: "google_auth_bind_required",
    30201: "google_auth_verify_required",
}

# Codes the executor should NOT cache (a future system with max_tries>1 would retry these).
# With _max_tries=1, transient still fails the op once and Laravel's reaper handles it.
TRANSIENT_CODES: frozenset[int] = frozenset({167})


def map_response(code: int, message: str) -> tuple[str, bool]:
    """Return (reason_slug, is_terminal). Cache only terminal failures."""
    if code in TRANSIENT_CODES:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", False)
    if code in GTREASURE_STATUS:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", True)
    return (f"gtreasure:code_{code}: {(message or '')[:80]}", True)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_errors.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/__init__.py app/backends/goldentreasure/errors.py tests/unit/test_goldentreasure_errors.py
git commit -m "feat(goldentreasure): map response codes to terminal/transient reasons

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `goldentreasure/crypto.py` — the verified-oracle pure functions

**Files:**
- Create: `app/backends/goldentreasure/crypto.py`
- Test: `tests/unit/test_goldentreasure_crypto.py`

This task carries the **5 doc-verified test oracles**. If they all pass, the crypto matches the live Golden Treasure server byte-for-byte.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_crypto.py
import urllib.parse

from app.backends.goldentreasure.crypto import (
    SIGN_SECRET,
    aes_b64,
    login_aes_key,
    sign_body,
    xtoken_header,
)


# Oracles below come from the findings doc §3, §4, §5 — every value verified against the live API.

def test_secret_matches_findings():
    assert SIGN_SECRET == "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def test_login_aes_key_is_123_stime_abc():
    assert login_aes_key(1779281935) == "1231779281935abc"
    assert len(login_aes_key(1779281935)) == 16     # AES-128 key


def test_aes_b64_login_username_oracle():
    # findings §4
    assert aes_b64("Test02Gd1WEB", "1231779281935abc") == "BXrmQgZgqwThh5+CjFOLFA=="


def test_aes_b64_login_password_oracle():
    # findings §4
    assert aes_b64("Zaeem@1233", "1231779281935abc") == "suyUHuDw+rXOKpJvvW7WsA=="


def test_sign_body_empty_oracle():
    # findings §3 — getLoginNote: empty body, stime=1779281921
    sign, stime = sign_body({}, stime=1779281921)
    assert sign == "1f8aca4093e5002f7481e9d7266b9ceb"
    assert stime == 1779281921


def test_sign_body_save_player_oracle_verifies_empty_skip_and_sort():
    # findings §3 — savePlayer body with empty fields (name/phone/tel_area_code/remark)
    # MUST be skipped during concatenation; keys sorted ascending.
    body = {
        "token": "q5pIWNNzvi%2BpBHDQYDLPnFnckAzxSNbIcEVrTxn%2F%2FTg%3D",
        "account": "apitest01",
        "pwd": "Apitest123",
        "score": "0",
        "name": "",                     # skipped
        "phone": "",                    # skipped
        "tel_area_code": "",            # skipped
        "remark": "",                   # skipped
    }
    sign, _ = sign_body(body, stime=1779282067)
    assert sign == "2fb7d0fb23cce1d967f095352b5bfa3f"


def test_sign_body_skips_none_and_stime_key_itself():
    # `stime` key in the body must be skipped during concat (it's appended as a suffix).
    # `None` values must also be skipped.
    sign, _ = sign_body({"a": "x", "b": None, "stime": 1234567890}, stime=1234567890)
    # Manual: concat = "x"; sign = MD5("x" + "1234567890" + SECRET)
    import hashlib
    expected = hashlib.md5(("x" + "1234567890" + SIGN_SECRET).encode()).hexdigest()
    assert sign == expected


def test_sign_body_defaults_stime_to_now_when_missing(monkeypatch):
    import app.backends.goldentreasure.crypto as crypto_module
    monkeypatch.setattr(crypto_module.time, "time", lambda: 1234567890.5)
    sign, stime = sign_body({})
    assert stime == 1234567890                       # int(time.time())


def test_xtoken_header_oracle():
    # findings §5
    session_token = "q5pIWNNzvi%2BpBHDQYDLPnFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    expected_b64 = (
        "jtSUNgHpXUUdEO+0ksqlndADWqFtaseFwSYCvXZq7l0dwKMicOPagiYFe84+hU6xbU4Xw6kmPKJfwGigrquoJg=="
    )
    expected_url = urllib.parse.quote(expected_b64, safe="")
    assert xtoken_header(session_token, 1779281936505) == expected_url
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_crypto.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/crypto.py
import base64
import hashlib
import time
import urllib.parse

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

SIGN_SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def aes_b64(plaintext: str, key: str) -> str:
    """AES-128-ECB / PKCS7 ciphertext, base64-encoded. Key must be exactly 16 ASCII chars."""
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plaintext.encode(), 16))).decode()


def sign_body(body: dict, *, stime: int | None = None) -> tuple[str, int]:
    """Findings §3: sort body keys ascending, skip the `stime` key + empty-string/None values,
    concatenate values (raw, no separator), append str(stime) + SECRET, MD5-hex.

    Returns (sign, stime). If `stime` is not supplied, uses int(time.time()).
    """
    stime_v = stime if stime is not None else int(time.time())
    concat = "".join(
        str(body[k])
        for k in sorted(body)
        if k != "stime" and body[k] not in ("", None)
    )
    sign = hashlib.md5((concat + str(stime_v) + SIGN_SECRET).encode()).hexdigest()
    return sign, stime_v


def login_aes_key(stime: int) -> str:
    """AES-128 key for encrypting login username/password. Findings §4. MUST equal body.stime."""
    return f"123{stime}abc"


def xtoken_header(session_token: str, x_time_ms: int) -> str:
    """URL-encoded base64 of AES-128-ECB(session_token, key=f"xtu{x_time_ms}"). Findings §5.

    `session_token` is used VERBATIM — including any URL-encoded chars in the token
    string. Do not decode it before encrypting.
    """
    return urllib.parse.quote(aes_b64(session_token, f"xtu{x_time_ms}"), safe="")
```

- [ ] **Step 4: Run to verify all 9 tests pass**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_crypto.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/crypto.py tests/unit/test_goldentreasure_crypto.py
git commit -m "feat(goldentreasure): pure-function crypto module (5 doc-verified oracles)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `goldentreasure/passwords.py` — one-line re-export

**Files:**
- Create: `app/backends/goldentreasure/passwords.py`
- Test: `tests/unit/test_goldentreasure_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_passwords.py
import re

from app.backends.goldentreasure.passwords import generate_memorable_password


def test_password_satisfies_goldentreasure_rule_letters_and_digits_6_to_16():
    # Golden Treasure rule: 6-16 chars, must combine letters and numbers.
    # The re-exported generator yields "{Word}{4 digits}" (e.g. "Tiger4827") -> 9-11 chars,
    # always has both letters and digits.
    for _ in range(50):
        pw = generate_memorable_password()
        assert 6 <= len(pw) <= 16
        assert any(c.isalpha() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_passwords_vary():
    assert len({generate_memorable_password() for _ in range(20)}) > 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_passwords.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/passwords.py
"""Golden Treasure player passwords.

Server rule (findings §8.3): 6-16 chars, must combine letters AND numbers, may include
!@#$%^/.,(). The existing GameVault memorable generator yields "Tiger4827"-style values which
satisfy the rule (alphanumeric, has letters + digits, length 9-11).
"""
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_passwords.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/passwords.py tests/unit/test_goldentreasure_passwords.py
git commit -m "feat(goldentreasure): re-export memorable alphanumeric password generator

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `goldentreasure/session.py` — duplicated session store + login lock

**Files:**
- Create: `app/backends/goldentreasure/session.py`
- Test: `tests/unit/test_goldentreasure_session.py`

This file is the gameroom session storage adapted with `gtreasure_*` Redis keys (per spec GT5). The shape mirrors `app/backends/gameroom/session.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_session.py
import asyncio

import pytest

from app.backends.goldentreasure.session import (
    CachedSession,
    InMemorySessionStore,
    RedisSessionStore,
)


async def test_in_memory_set_get_clear():
    store = InMemorySessionStore()
    assert await store.get(13) is None
    await store.set(13, CachedSession(token="t", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(13)
    assert got is not None and got.token == "t"
    await store.clear(13)
    assert await store.get(13) is None


async def test_redis_uses_gtreasure_session_key_prefix(fake_redis):
    # Spec GT5: keys must use `gtreasure_session:` / `gtreasure_login:` namespaces, NOT gameroom's.
    store = RedisSessionStore(fake_redis)
    await store.set(7, CachedSession(token="abc", expires_at=9_999_999_999), ttl_seconds=120)
    assert await fake_redis.get("gtreasure_session:7") is not None
    assert await fake_redis.get("gameroom_session:7") is None       # NOT in gameroom's namespace
    got = await store.get(7)
    assert got is not None and got.token == "abc"


async def test_redis_login_lock_uses_gtreasure_login_key_prefix(fake_redis):
    store = RedisSessionStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("gtreasure_login:9")) == 1
        assert (await fake_redis.exists("gameroom_login:9")) == 0
    assert (await fake_redis.exists("gtreasure_login:9")) == 0


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
    with pytest.raises(TimeoutError):
        async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=0.2):
            raise AssertionError("should not have acquired lock")
    release.set()
    await task
    # Lock released — next acquire succeeds immediately.
    async with store.login_lock(game_id=10, ttl_seconds=30, poll_seconds=0.05, acquire_timeout=1.0):
        pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_session.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/session.py
"""Golden Treasure session storage. Duplicates the gameroom session module's shape with
gtreasure_-prefixed Redis keys (per spec GT5). Concurrent tokens are allowed by Golden Treasure
so a simple lock + single cache re-read is sufficient — no double-checked locking needed.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    token: str
    expires_at: int          # unix seconds


class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 10,
        poll_seconds: float = 0.1, acquire_timeout: float = 10.0,
    ):
        """Serializes /api/user/login calls for a game. Raises TimeoutError if not acquired."""


class InMemorySessionStore:
    """Process-local for tests / single-process fallback."""

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
            raise TimeoutError(f"gtreasure login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"gtreasure_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"gtreasure_login:{game_id}"


class RedisSessionStore:
    """Redis-backed; shared across workers. Uses SET NX for the login lock."""

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
            if await self._redis.set(key, b"1", nx=True, ex=ttl_seconds):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"gtreasure login lock acquire timeout (game_id={game_id})")
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

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_session.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/session.py tests/unit/test_goldentreasure_session.py
git commit -m "feat(goldentreasure): SessionStore (gtreasure_ keys) duplicated from gameroom

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `goldentreasure/client.py` — login, signed POST, x-token rebuild, throttle, 410-style retry

**Files:**
- Create: `app/backends/goldentreasure/client.py`
- Test: `tests/unit/test_goldentreasure_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_client.py
import asyncio
import json
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.crypto import xtoken_header
from app.backends.goldentreasure.session import CachedSession, InMemorySessionStore

BASE = "https://gt.test"


def _make_client(http, *, store=None, fake_redis=None):
    return GoldenTreasureClient(
        base_url=BASE, username="Test02Gd1WEB", password="Zaeem@1233",
        http_client=http,
        session_store=store or InMemorySessionStore(),
        redis=fake_redis,
        game_id=13,
    )


def _login_ok(token="Ttok"):
    return {"code": 20000, "name": "Test02Gd1WEB", "token": token,
            "frame": 0, "data": {}}


# ---- login ----

@respx.mock
async def test_login_posts_aes_encrypted_creds_with_matching_stime(monkeypatch):
    monkeypatch.setattr("app.backends.goldentreasure.client.time.time", lambda: 1779281935.0)
    route = respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    async with httpx.AsyncClient() as http:
        token = await _make_client(http).get_token()
    assert token == "Ttok"
    sent_body = json.loads(route.calls.last.request.content.decode())
    # AES oracles from findings §4
    assert sent_body["username"] == "BXrmQgZgqwThh5+CjFOLFA=="
    assert sent_body["password"] == "suyUHuDw+rXOKpJvvW7WsA=="
    assert sent_body["stime"] == 1779281935
    assert sent_body["auth_code"] == ""
    assert "sign" in sent_body
    # No x-token / x-time on login
    headers = {k.lower(): v for k, v in route.calls.last.request.headers.items()}
    assert "x-token" not in headers and "x-time" not in headers
    # Cloudflare-friendly headers present
    assert headers["origin"] == "https://agent.goldentreasure.mobi"
    assert "chrome" in headers["user-agent"].lower()


@respx.mock
async def test_login_30100_is_terminal_operator_action():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(
        200, json={"code": 30100, "message": "Verify code required"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).get_token()
    assert ei.value.reason == "gtreasure:requires_operator_action_system_verify"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_30200_is_terminal_google_auth():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(
        200, json={"code": 30200, "message": "Google Auth required"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).get_token()
    assert ei.value.reason == "gtreasure:requires_operator_action_google_auth_bind"


@respx.mock
async def test_login_5xx_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).get_token()


# ---- get_token reuse + invalidate ----

@respx.mock
async def test_get_token_returns_cached_when_present():
    route = respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("would_be_new")))
    store = InMemorySessionStore()
    await store.set(13, CachedSession(token="cached", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _make_client(http, store=store).get_token()
    assert token == "cached"
    assert route.call_count == 0


@respx.mock
async def test_get_token_with_invalidate_logs_in_when_cache_holds_dead_token():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("Tnew")))
    store = InMemorySessionStore()
    await store.set(13, CachedSession(token="Tdead", expires_at=int(time.time()) + 3600), ttl_seconds=3600)
    async with httpx.AsyncClient() as http:
        token = await _make_client(http, store=store).get_token(invalidate="Tdead")
    assert token == "Tnew"


# ---- call() success + x-token + sign ----

@respx.mock
async def test_call_success_attaches_xtoken_xtime_and_signs(monkeypatch):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("Ttok")))
    route = respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    # Freeze time so we can predict x-time / sign.
    monkeypatch.setattr("app.backends.goldentreasure.client.time.time", lambda: 1779281936.505)
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "20.00"
    sent = route.calls.last.request
    headers = {k.lower(): v for k, v in sent.headers.items()}
    # x-time = int(time.time()*1000)
    assert headers["x-time"] == "1779281936505"
    # x-token = url-encoded AES of the session token with key f"xtu{x_time_ms}"
    assert headers["x-token"] == xtoken_header("Ttok", 1779281936505)
    body = json.loads(sent.content.decode())
    assert body["token"] == "Ttok"
    assert "sign" in body and "stime" in body


# ---- call() relogin on -3/-17/52 ----

@respx.mock
async def test_call_code_minus3_relogins_and_retries_once_successfully():
    respx.post(f"{BASE}/api/user/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),
        httpx.Response(200, json=_login_ok("Tnew")),
    ])
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=[
        httpx.Response(200, json={"code": -3, "message": "token invalid"}),
        httpx.Response(200, json={"code": 20000, "LimitNum": "5.00"}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "5.00"


@respx.mock
async def test_call_code_minus3_then_minus3_raises_auth_failed():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": -3, "message": "token invalid"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).call("/api/user/CurScore", {})
    assert ei.value.reason == "gtreasure:auth_failed"


@respx.mock
async def test_call_52_treated_same_as_minus3():
    respx.post(f"{BASE}/api/user/login").mock(side_effect=[
        httpx.Response(200, json=_login_ok("Told")),
        httpx.Response(200, json=_login_ok("Tnew")),
    ])
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=[
        httpx.Response(200, json={"code": 52, "message": "no permission"}),
        httpx.Response(200, json={"code": 20000, "LimitNum": "1.00"}),
    ])
    async with httpx.AsyncClient() as http:
        data = await _make_client(http).call("/api/user/CurScore", {})
    assert data["LimitNum"] == "1.00"


# ---- call() error classification ----

@respx.mock
async def test_call_code_21_is_terminal_operation_refused():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "充值失败：服务器维护中"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _make_client(http).call("/api/account/enterScore", {"score": "1"})
    assert ei.value.reason == "gtreasure:operation_refused"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_call_code_167_is_transient_rate_limited(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 167, "message": "high frequency request"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        with pytest.raises(TransientBackendError) as ei:
            await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)
    assert ei.value.reason == "gtreasure:rate_limited"


@respx.mock
async def test_call_5xx_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).call("/api/user/CurScore", {})


@respx.mock
async def test_call_transport_error_is_transient():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _make_client(http).call("/api/user/CurScore", {})


# ---- throttle ----

@respx.mock
async def test_throttle_acquires_setnx_key_for_mutating_op(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)
    # SET NX with ex=5 means the key exists with TTL > 0 immediately after the call.
    assert await fake_redis.exists("gtreasure_throttle:13") == 1
    ttl = await fake_redis.ttl("gtreasure_throttle:13")
    assert 0 < ttl <= 5


@respx.mock
async def test_non_mutating_call_does_not_touch_throttle_key(fake_redis):
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "5.00"}))
    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        await client.call("/api/user/CurScore", {})        # NO throttle=True
    assert await fake_redis.exists("gtreasure_throttle:13") == 0


@respx.mock
async def test_throttle_serializes_concurrent_mutating_ops(fake_redis, monkeypatch):
    # Two ops on the same game must serialize: the second waits until the first's 5s lock expires.
    # Monkeypatch asyncio.sleep so the test runs fast but still exercises the SETNX poll loop.
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fast_sleep(s):
        sleeps.append(s)
        # Burn the SETNX TTL down so the poll eventually acquires.
        await real_sleep(0)
        await fake_redis.delete("gtreasure_throttle:13")     # simulate TTL expiry

    monkeypatch.setattr("app.backends.goldentreasure.client.asyncio.sleep", fast_sleep)

    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok("T")))
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))

    async with httpx.AsyncClient() as http:
        client = _make_client(http, fake_redis=fake_redis)
        # Manually plant the throttle key as if a prior op held it.
        await fake_redis.set("gtreasure_throttle:13", b"1", nx=True, ex=5)
        await client.call("/api/account/enterScore", {"score": "1"}, throttle=True)

    assert sleeps, "_acquire_throttle should have polled at least once"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_client.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/client.py
import asyncio
import json
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.goldentreasure.crypto import (
    aes_b64,
    login_aes_key,
    sign_body,
    xtoken_header,
)
from app.backends.goldentreasure.errors import map_response
from app.backends.goldentreasure.session import CachedSession, SessionStore

# Cloudflare-friendly browser header set. The findings doc emphasizes that without these the
# request gets HTTP 403 from CF before reaching the API.
_BROWSER_HEADERS_BASE = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://agent.goldentreasure.mobi",
    "Referer": "https://agent.goldentreasure.mobi/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# Authentication-failure codes that trigger a transparent relogin + retry once.
_AUTH_DEAD_CODES = {-3, -17, 52}


class GoldenTreasureClient:
    """Golden Treasure HTTP client: AES-encrypted login, signed JSON POSTs, per-request
    x-token rebuild, and per-game mutating-op throttle."""

    def __init__(
        self, *,
        base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: SessionStore,
        redis,                                             # raw redis client for the throttle
        game_id: int,
        fingerprint: str = "db3bb59096022abb85b4612d53387101",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._session = session_store
        self._redis = redis
        self._game_id = game_id
        self._fingerprint = fingerprint

    # ---- session management ----

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid session token. Concurrent tokens are allowed by Golden Treasure
        (findings §10) — no double-checked locking needed; one cache re-read under the lock
        is enough to prevent thundering-herd logins.
        """
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate:
            return cached.token
        async with self._session.login_lock(self._game_id, ttl_seconds=10, acquire_timeout=10.0):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate:
                return cached.token
            token = await self._do_login()
            # Token expiry isn't returned; pick a generous TTL (24h). Relogin on -3/-17 anyway.
            await self._session.set(
                self._game_id,
                CachedSession(token=token, expires_at=int(time.time()) + 86400),
                ttl_seconds=86400,
            )
            return token

    async def _do_login(self) -> str:
        """POST /api/user/login with AES-encrypted credentials. No x-token (no session yet)."""
        stime = int(time.time())
        key = login_aes_key(stime)
        body = {
            "username": aes_b64(self._username.strip(), key),
            "password": aes_b64(self._password, key),
            "stime": stime,
            "auth_code": "",
        }
        sign, _ = sign_body(body, stime=stime)
        body_json = await self._post_raw("/api/user/login", {**body, "sign": sign}, authenticated=False)
        code = body_json.get("code")
        if code == 20000:
            token = body_json.get("token")
            if not isinstance(token, str) or not token:
                raise TransientBackendError("gtreasure:login_missing_token")
            return token
        if code in (30100, 30200, 30201):
            slug = {30100: "system_verify", 30200: "google_auth_bind", 30201: "google_auth_verify"}[code]
            raise BackendError(f"gtreasure:requires_operator_action_{slug}")
        reason, terminal = map_response(
            int(code) if isinstance(code, int) else 0,
            str(body_json.get("message", "")),
        )
        raise (BackendError if terminal else TransientBackendError)(reason)

    # ---- throttle (mutating ops only) ----

    async def _acquire_throttle(self) -> None:
        """SET NX gtreasure_throttle:{game_id} ex=5. Poll until acquired or 30s timeout."""
        key = f"gtreasure_throttle:{self._game_id}"
        deadline = time.monotonic() + 30.0
        while True:
            if await self._redis.set(key, b"1", nx=True, ex=5):
                return
            if time.monotonic() >= deadline:
                raise TransientBackendError("gtreasure:throttle_wait_timeout")
            await asyncio.sleep(0.5)

    # ---- HTTP transport ----

    async def _post_raw(self, path: str, body: dict, *, authenticated: bool) -> dict:
        """Send one POST. authenticated=True adds the x-token/x-time headers from body['token']."""
        raw = json.dumps(body, separators=(",", ":"))
        headers = dict(_BROWSER_HEADERS_BASE)
        headers["x-fingerprint"] = self._fingerprint
        if authenticated:
            x_time_ms = int(time.time() * 1000)
            token = body.get("token")
            headers["x-token"] = xtoken_header(str(token), x_time_ms)
            headers["x-time"] = str(x_time_ms)
        try:
            resp = await self._http.post(
                f"{self._base_url}{path}", content=raw.encode(), headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gtreasure:transport:{type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"gtreasure:http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"gtreasure:http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError("gtreasure:bad_response") from exc

    # ---- authenticated call (relogin on -3/-17/52 + optional throttle) ----

    async def call(self, path: str, params: dict, *, throttle: bool = False) -> dict:
        if throttle:
            await self._acquire_throttle()
        token = await self.get_token()
        body = {**params, "token": token}
        sign, stime = sign_body(body)
        body_json = await self._post_raw(path, {**body, "sign": sign, "stime": stime}, authenticated=True)
        if body_json.get("code") in _AUTH_DEAD_CODES:
            fresh = await self.get_token(invalidate=token)
            body["token"] = fresh
            sign, stime = sign_body(body)
            body_json = await self._post_raw(path, {**body, "sign": sign, "stime": stime}, authenticated=True)
            if body_json.get("code") in _AUTH_DEAD_CODES:
                raise BackendError("gtreasure:auth_failed")
        if body_json.get("code") == 20000:
            return body_json
        reason, terminal = map_response(
            int(body_json.get("code", 0)) if isinstance(body_json.get("code"), int) else 0,
            str(body_json.get("message", "")),
        )
        raise (BackendError if terminal else TransientBackendError)(reason)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_client.py -q`
Expected: PASS (15 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/client.py tests/unit/test_goldentreasure_client.py
git commit -m "feat(goldentreasure): GoldenTreasureClient — AES login, x-token rebuild, throttle, relogin

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `goldentreasure/backend.py` — the 6 ops

**Files:**
- Create: `app/backends/goldentreasure/backend.py`
- Test: `tests/unit/test_goldentreasure_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_goldentreasure_backend.py
import json
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.goldentreasure.backend import GoldenTreasureBackend, _to_cents, _to_dollars
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.session import InMemorySessionStore

BASE = "https://gt.test"


def _creds():
    return GameCredentials(
        game_id=13, name="GT",
        backend_url=BASE, login_page_url=None,
        backend_username="Test02Gd1WEB", backend_password="Zaeem@1233",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )


def _ctx(*, account=True, username="apitest01", idem="idem-1",
         account_username=None, user_id=61):
    acct = AccountIdentity(4001, user_id, 13, username, None) if account else None
    return BackendContext(credentials=_creds(), user_id=user_id, account=acct,
                          idempotency_key=idem, account_username=account_username)


def _backend(http, fake_redis):
    client = GoldenTreasureClient(
        base_url=BASE, username="Test02Gd1WEB", password="Zaeem@1233",
        http_client=http, session_store=InMemorySessionStore(),
        redis=fake_redis, game_id=13,
    )
    return GoldenTreasureBackend(client)


def _login_ok():
    return {"code": 20000, "token": "Ttok", "name": "Test02Gd1WEB", "data": {}}


def _mock_login():
    respx.post(f"{BASE}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))


def test_unit_helpers():
    # _to_cents matches gamevault/gameroom's decimal-dollar->cents convention.
    assert _to_cents("5.00") == 500
    assert _to_cents("20.00") == 2000
    assert _to_cents(0) == 0
    assert _to_cents(5) == 500                       # int player score "5" -> 500 cents
    # _to_dollars matches ceil-dollar-on-send convention.
    assert _to_dollars(500) == "5"
    assert _to_dollars(510) == "6"                   # ceil
    assert _to_dollars(3050) == "31"                 # ceil


# ---- AGENT_BALANCE ----

@respx.mock
async def test_agent_balance_reads_LimitNum(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).agent_balance(_ctx(account=False))
    assert r.agent_balance_cents == 2000


@respx.mock
async def test_agent_balance_missing_LimitNum_is_terminal(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).agent_balance(_ctx(account=False))
    assert ei.value.reason == "gtreasure:agent_balance_missing"


# ---- READ_BALANCE ----

@respx.mock
async def test_read_balance_posts_account_and_returns_cents(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/getPlayerScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "curScore": 5}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).read_balance(_ctx())
    assert r.balance_cents == 500
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["token"] == "Ttok"


# ---- CREATE_ACCOUNT ----

@respx.mock
async def test_create_account_posts_username_and_zero_score(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "新增玩家成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).create_account(
            _ctx(account=False, account_username="apitestnew")
        )
    assert r.username == "apitestnew"
    assert r.password and r.password.isalnum()
    assert r.external_user_id is None                # spec GT4
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitestnew"
    assert body["score"] == "0"
    assert body["name"] == "" and body["phone"] == "" and body["tel_area_code"] == "" and body["remark"] == ""


@respx.mock
async def test_create_account_requires_account_username(fake_redis):
    _mock_login()
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


@respx.mock
async def test_create_account_throttles(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "ok"}))
    async with httpx.AsyncClient() as http:
        await _backend(http, fake_redis).create_account(_ctx(account=False, account_username="apitestthr"))
    assert await fake_redis.exists("gtreasure_throttle:13") == 1


@respx.mock
async def test_create_account_code_8_is_account_exists(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/savePlayer").mock(return_value=httpx.Response(
        200, json={"code": 8, "message": "该帐号已被使用"}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).create_account(
                _ctx(account=False, account_username="taken")
            )
    assert ei.value.reason == "gtreasure:account_exists"


# ---- RECHARGE ----

@respx.mock
async def test_recharge_sends_positive_ceil_score_and_throttles(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "进分成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).recharge(
            _ctx(), amount_cents=5000, bonus_cents=500, total_credit_cents=5510,
        )
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["score"] == "56"                     # ceil(5510/100)
    assert body["user_type"] == "player"
    assert body["remark"] == ""
    assert r.balance_cents is None                   # RechargeResult() with no balance
    assert await fake_redis.exists("gtreasure_throttle:13") == 1


# ---- REDEEM ----

@respx.mock
async def test_redeem_sends_negative_ceil_score_and_throttles(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "下分成功"}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).redeem(_ctx(), amount_cents=3050)
    body = json.loads(route.calls.last.request.content.decode())
    assert body["score"] == "-31"                    # negative ceil(3050/100)
    assert body["account"] == "apitest01"
    assert r.balance_cents is None                   # RedeemResult() with no balance


@respx.mock
async def test_redeem_code_21_is_operation_refused(fake_redis):
    _mock_login()
    respx.post(f"{BASE}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "充值失败：服务器维护中", "test": 21}))
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http, fake_redis).redeem(_ctx(), amount_cents=100)
    assert ei.value.reason == "gtreasure:operation_refused"


# ---- RESET_PASSWORD ----

@respx.mock
async def test_reset_password_posts_to_updatePlayer_and_does_not_throttle(fake_redis):
    _mock_login()
    route = respx.post(f"{BASE}/api/account/updatePlayer").mock(return_value=httpx.Response(
        200, json={"code": 20000, "message": "编辑玩家成功", "info": {}}))
    async with httpx.AsyncClient() as http:
        r = await _backend(http, fake_redis).reset_password(_ctx())
    assert r.password and r.password.isalnum()
    body = json.loads(route.calls.last.request.content.decode())
    assert body["account"] == "apitest01"
    assert body["pwd"] == r.password
    # RESET_PASSWORD is NOT throttled (spec GT7) — throttle key must NOT be set.
    assert await fake_redis.exists("gtreasure_throttle:13") == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_backend.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/goldentreasure/backend.py
import math

from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.passwords import generate_memorable_password
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


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


class GoldenTreasureBackend:
    def __init__(self, client: GoldenTreasureClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        data = await self._client.call("/api/user/CurScore", {})
        v = data.get("LimitNum")
        if v is None:
            raise BackendError("gtreasure:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=_to_cents(v))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        data = await self._client.call(
            "/api/account/getPlayerScore", {"account": ctx.account.username},
        )
        return ReadBalanceResult(balance_cents=_to_cents(data.get("curScore", 0)))

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()       # alphanumeric (satisfies 6-16 letters+digits rule)
        await self._client.call(
            "/api/account/savePlayer",
            {
                "account": ctx.account_username,
                "pwd": pwd,
                "score": "0",
                "name": "", "phone": "", "tel_area_code": "", "remark": "",
            },
            throttle=True,
        )
        # savePlayer doesn't return a uid (spec GT4) -> external_user_id=None.
        return CreateAccountResult(
            username=ctx.account_username, password=pwd, external_user_id=None,
        )

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        pwd = generate_memorable_password()
        await self._client.call(
            "/api/account/updatePlayer",
            {
                "account": ctx.account.username,
                "pwd": pwd,
                "name": "", "phone": "", "remark": "", "tel_area_code": "",
            },
            # NOT throttled (spec GT7).
        )
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": ctx.account.username,
                "score": _to_dollars(total_credit_cents),
                "remark": "",
                "user_type": "player",
            },
            throttle=True,
        )
        # enterScore success has no balance; we omit it (contract makes it optional).
        return RechargeResult()

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        dollars = math.ceil(amount_cents / 100)
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": ctx.account.username,
                "score": str(-dollars),               # negative score = withdraw
                "remark": "",
                "user_type": "player",
            },
            throttle=True,
        )
        return RedeemResult()
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_goldentreasure_backend.py -q`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/goldentreasure/backend.py tests/unit/test_goldentreasure_backend.py
git commit -m "feat(goldentreasure): GoldenTreasureBackend — 6 ops with throttle gating

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Registry — add `goldentreasure` branch + `redis=None` kwarg + extend `NON_IDEMPOTENT_DRIVERS`

**Files:**
- Modify: `app/backends/registry.py`
- Test: `tests/unit/test_registry.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_registry.py`:

```python
from app.backends.goldentreasure.backend import GoldenTreasureBackend


def _gt_creds():
    return GameCredentials(
        game_id=13, name="g",
        backend_url="https://gt.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )


def test_non_idempotent_drivers_contains_goldentreasure():
    assert "goldentreasure" in NON_IDEMPOTENT_DRIVERS
    assert "gameroom" in NON_IDEMPOTENT_DRIVERS              # Phase 3
    # gamevault family is deliberately NOT in this set (order_id dedupe makes retries safe)
    assert {"gamevault", "juwa", "juwa2"}.isdisjoint(NON_IDEMPOTENT_DRIVERS)


def test_goldentreasure_driver_routes_to_goldentreasure_backend(fake_redis):
    s = _settings()
    backend = resolve_backend(
        "goldentreasure", credentials=_gt_creds(),
        http_client=object(), settings=s, redis=fake_redis,
    )
    assert isinstance(backend, GoldenTreasureBackend)


def test_goldentreasure_missing_credentials_raises():
    s = _settings()
    creds = GameCredentials(
        game_id=13, name="g",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="goldentreasure",
    )
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "goldentreasure", credentials=creds,
            http_client=object(), settings=s, redis=object(),
        )
    assert ei.value.reason == "missing_goldentreasure_credentials"


def test_goldentreasure_missing_redis_raises():
    s = _settings()
    with pytest.raises(BackendError) as ei:
        resolve_backend(
            "goldentreasure", credentials=_gt_creds(),
            http_client=object(), settings=s, redis=None,
        )
    assert ei.value.reason == "missing_redis_client"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: FAIL (4 new tests — driver branch missing; `redis` kwarg not accepted; `NON_IDEMPOTENT_DRIVERS` doesn't contain `goldentreasure`).

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
from app.backends.goldentreasure.backend import GoldenTreasureBackend
from app.backends.goldentreasure.client import GoldenTreasureClient
from app.backends.goldentreasure.session import RedisSessionStore as GTSessionStore
from app.backends.mock.backend import MockBackend
from app.config import Settings

# Driver strings that share the GameVault provider's wire protocol (auth, endpoints, envelope).
_GAMEVAULT_PROVIDER_DRIVERS = frozenset({"gamevault", "juwa", "juwa2"})

# Drivers with no server-side idempotency (no order_id/dedupe). The API endpoint passes
# arq _max_tries=1 for these so a worker crash mid-money-op cannot double-apply funds.
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({"gameroom", "goldentreasure"})


def resolve_backend(
    driver: str | None, *,
    credentials: GameCredentials,
    http_client,
    settings: Settings,
    session_store=None,                           # Phase 3 — used by gameroom
    redis=None,                                   # Phase 4 — used by goldentreasure (throttle + own session store)
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend.
    `gamevault`/`juwa`/`juwa2` -> GameVaultBackend (same provider, per-game creds).
    `gameroom` -> GameroomBackend (requires session_store).
    `goldentreasure` -> GoldenTreasureBackend (requires redis client; constructs its own SessionStore).
    Unknown -> BackendError.
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
    if key == "goldentreasure":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_goldentreasure_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        return GoldenTreasureBackend(
            GoldenTreasureClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=GTSessionStore(redis),
                redis=redis,
                game_id=credentials.game_id,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: PASS (all old + 4 new).

- [ ] **Step 5: Run full suite (existing callers should be unaffected — `redis` defaults to None)**

Run: `.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(backends): driver-aware resolve_backend with goldentreasure + redis kwarg

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: Executor — accept and thread `redis` kwarg

**Files:**
- Modify: `app/operations/executor.py`
- Test: `tests/integration/test_executor_cache.py` (existing tests unchanged; add a new regression test)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_executor_cache.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_executor_cache.py::test_goldentreasure_without_redis_reports_failure -q`
Expected: FAIL — `execute_operation` doesn't accept `redis` kwarg.

- [ ] **Step 3: Update `execute_operation`**

In `app/operations/executor.py`:

1. Add `redis=None` to the signature (between `session_store` and `resolve`):

```python
async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    result_cache: ResultCache | None = None,
    session_store=None,
    redis=None,
    resolve=_resolve_backend,
) -> None:
```

2. In the resolve call (step 4 of the function — backend resolution), add `redis=redis`:

```python
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver,
            credentials=ctx.credentials,
            http_client=http_client,
            settings=settings,
            session_store=session_store,
            redis=redis,
        )
```

- [ ] **Step 4: Run the new test + the existing executor/full-loop tests**

Run: `.venv/bin/python -m pytest tests/integration/test_executor_cache.py tests/integration/test_executor.py tests/integration/test_full_loop.py -q`
Expected: PASS (all old + 1 new).

- [ ] **Step 5: Run the full gate**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add app/operations/executor.py tests/integration/test_executor_cache.py
git commit -m "feat(operations): executor threads redis kwarg to resolve_backend

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Worker — pass `redis` from ctx to executor

**Files:**
- Modify: `app/worker/tasks.py`
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
    class FakeRedis: ...

    ctx = {
        "http_client": FakeClient(),
        "session_factory": seeded,
        "result_cache": FakeCache(),
        "session_store": FakeSessionStore(),
        "redis_cache": FakeRedis(),
    }
    payload = {"idempotency_key": "k", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
    assert captured["kwargs"]["result_cache"] is ctx["result_cache"]
    assert captured["kwargs"]["session_store"] is ctx["session_store"]
    assert captured["kwargs"]["redis"] is ctx["redis_cache"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: FAIL — task doesn't pass `redis`.

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
        redis=ctx["redis_cache"],
    )
```

- [ ] **Step 4: Run + verify import without live Redis**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: PASS.
Run: `.venv/bin/python -c "import app.worker.settings; print('OK')"`
Expected: `OK` (no live Redis required).

- [ ] **Step 5: Commit**

```bash
git add app/worker/tasks.py tests/unit/test_worker_tasks.py
git commit -m "feat(worker): pass redis client from ctx to executor

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 12: Integration test + docs

**Files:**
- Create: `tests/integration/test_goldentreasure_integration.py`
- Modify: `CLAUDE.md`, `docs/architecture.md`, `docs/runbook.md`

- [ ] **Step 1: Write the integration test**

```python
# tests/integration/test_goldentreasure_integration.py
import json
import time

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GT = "https://gt.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


def _login_ok():
    return {"code": 20000, "token": "Ttok", "name": "Test02Gd1WEB", "data": {}}


@respx.mock
async def test_goldentreasure_agent_balance_end_to_end(seeded, fake_redis):
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "20.00"}))
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gt-ab-1", "type": "AGENT_BALANCE", "game_id": 13}
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            payload, session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded"
    assert sent["result"]["agent_balance_cents"] == 2000      # "20.00" -> 2000 cents


@respx.mock
async def test_goldentreasure_terminal_failure_cached_and_not_recalled(seeded, fake_redis):
    # Login succeeds; CurScore returns code:21 (terminal). Second run should NOT re-call CurScore.
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    cs = respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 21, "message": "服务器维护中"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-21", "type": "AGENT_BALANCE", "game_id": 13}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
        assert cs.call_count == 1
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    assert cs.call_count == 1                                  # cache hit -> no second call
    cached = await cache.get("gt-21")
    assert cached and cached.status == "failed" and "operation_refused" in cached.reason


@respx.mock
async def test_goldentreasure_rate_limited_167_is_not_cached(seeded, fake_redis):
    respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    es = respx.post(f"{GT}/api/account/enterScore").mock(return_value=httpx.Response(
        200, json={"code": 167, "message": "high frequency request"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-167", "type": "RECHARGE", "user_id": 61, "game_id": 13,
               "game_account_id": 4001, "amount_cents": 100, "bonus_cents": 0, "total_credit_cents": 100}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    assert es.call_count == 1
    assert await cache.get("gt-167") is None                   # transient -> not cached


@respx.mock
async def test_goldentreasure_session_is_reused_across_ops(seeded, fake_redis):
    """Two AGENT_BALANCE ops on the same game must issue exactly ONE /api/user/login."""
    login = respx.post(f"{GT}/api/user/login").mock(return_value=httpx.Response(200, json=_login_ok()))
    respx.post(f"{GT}/api/user/CurScore").mock(return_value=httpx.Response(
        200, json={"code": 20000, "LimitNum": "5.00"}))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    async with httpx.AsyncClient() as client:
        await execute_operation(
            {"idempotency_key": "gt-share-1", "type": "AGENT_BALANCE", "game_id": 13},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
        await execute_operation(
            {"idempotency_key": "gt-share-2", "type": "AGENT_BALANCE", "game_id": 13},
            session_factory=seeded, http_client=client, settings=_settings(),
            result_cache=cache, redis=fake_redis,
        )
    assert login.call_count == 1                              # shared Redis session -> one login


@respx.mock
async def test_goldentreasure_recharge_relogin_on_minus3_then_success(seeded, fake_redis):
    """RECHARGE returns code:-3 -> client relogs in transparently -> retry once -> success."""
    respx.post(f"{GT}/api/user/login").mock(side_effect=[
        httpx.Response(200, json={"code": 20000, "token": "T1", "name": "x", "data": {}}),
        httpx.Response(200, json={"code": 20000, "token": "T2", "name": "x", "data": {}}),
    ])
    respx.post(f"{GT}/api/account/enterScore").mock(side_effect=[
        httpx.Response(200, json={"code": -3, "message": "token invalid"}),
        httpx.Response(200, json={"code": 20000, "message": "进分成功"}),
    ])
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gt-relogin", "type": "RECHARGE", "user_id": 61, "game_id": 13,
               "game_account_id": 4001, "amount_cents": 100, "bonus_cents": 0, "total_credit_cents": 100}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(),
                                result_cache=cache, redis=fake_redis)
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded"
```

- [ ] **Step 2: Run the integration test + full gate**

Run: `.venv/bin/python -m pytest tests/integration/test_goldentreasure_integration.py -q`
Expected: PASS (5 tests).
Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all green.

- [ ] **Step 3: Update `CLAUDE.md`**

In `CLAUDE.md`, edit the backend-selection bullet to include `goldentreasure`:

Replace:
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

With:
```markdown
- Backend selection comes from `games.backend_driver` (read-only): `mock` | `gamevault` | `juwa` |
  `juwa2` | `gameroom` | `goldentreasure`. New backends add a module + a `resolve_backend` branch;
  sibling games on an existing provider (e.g. `juwa`/`juwa2` share GameVault's API) are added as an
  alias in the registry.
- Non-idempotent drivers (no server-side `order_id` dedupe — currently `gameroom`, `goldentreasure`)
  are listed in `NON_IDEMPOTENT_DRIVERS`; the `/operations` endpoint passes arq `_max_tries=1` for
  these so a worker crash can't double-apply funds. Reaper at Laravel's 10-min mark handles the orphan.
- Gameroom: JWT bearer auth (~6h sessions) cached in Redis via `app/backends/gameroom/session.py`.
  Re-login on `status_code:410` uses **double-checked locking** (`get_token(invalidate=...)`) to
  stay safe under Gameroom's single-session-per-agent enforcement.
- Golden Treasure: MD5-signed JSON bodies + AES-128-ECB login creds + per-request `x-token` header
  built from the cached token. Cloudflare-fronted (a full browser header set is mandatory). Multi-
  token concurrency (no single-session) -> no double-checked locking, just a login lock. Mutating
  ops (savePlayer/enterScore) are gated by `SET NX gtreasure_throttle:{game_id} ex=5` to stay
  under the strict `code:167` rate limit.
```

In the "Where things live" section, append:

```markdown
- Golden Treasure backend: `app/backends/goldentreasure/` (crypto, client, backend, errors, passwords, session).
```

- [ ] **Step 4: Update `docs/architecture.md`**

Append a section:

```markdown
## Reverse-engineered backends (Golden Treasure)
Golden Treasure (`app/backends/goldentreasure/`) is the second session-holding backend, with much
heavier crypto than Gameroom: every body is MD5-signed (`MD5(sorted-values + stime + SECRET)`),
login credentials are **AES-128-ECB encrypted** (key = `f"123{stime}abc"`, must match `body.stime`),
and `x-token`/`x-time` headers are rebuilt per authenticated request (`AES(token, key=f"xtu{ms}")`,
URL-encoded). The Cloudflare front rejects requests without a realistic browser header set, so
`_BROWSER_HEADERS_BASE` sends `User-Agent`, `sec-ch-ua*`, `Origin`, `Referer`, `Accept-Language`.

**Sessions:** `RedisSessionStore` (`gtreasure_session:{game_id}`) shared across workers. Concurrent
tokens are allowed (no single-session enforcement) so `get_token` uses a simple lock + one cache
re-read — no double-checked locking. On `code:-3`/`-17`/`52`, `client.call` re-logs in transparently
and retries once; a second auth-dead code raises terminal `gtreasure:auth_failed`.

**Rate limit:** `code:167` ("high frequency request") fires on bursts of `savePlayer`/`enterScore`
with required ≥5s spacing. The client guards mutating ops with `SET NX gtreasure_throttle:{game_id}
ex=5` (TTL = the spacing window; never released — lets the lock auto-expire). Reads bypass the
throttle. Hitting 167 anyway surfaces as transient (not cached).

**Money safety:** `goldentreasure` is in `NON_IDEMPOTENT_DRIVERS` (no `order_id`). API endpoint
passes `_max_tries=1`; a worker crash mid-money-op is failed+refunded by Laravel's reaper, with
manual reconcile via the agent UI.
```

- [ ] **Step 5: Update `docs/runbook.md`**

Append:

```markdown
## Golden Treasure (Cloudflare-fronted reverse-engineered backend)
- Set `games.backend_driver='goldentreasure'` plus `backend_url=https://agent.goldentreasure.mobi`,
  `backend_username`, `backend_password` (the agent's login). No `api_*` columns needed.
- **No IP allowlist** (Golden Treasure uses Cloudflare, not IP-based ACLs). Our `_BROWSER_HEADERS_BASE`
  sends the header set CF requires.
- Sessions cached in Redis (`gtreasure_session:{game_id}`, 24h TTL). First op lazy-logs-in; later
  ops reuse the token. To force re-login: `redis-cli DEL gtreasure_session:<game_id>`.
- Mutating ops (savePlayer/enterScore) self-serialize at ≥5s spacing per game via
  `gtreasure_throttle:{game_id}` (TTL 5s, auto-expires). Reads are not throttled.
- A worker crash during RECHARGE/REDEEM does NOT retry (per-driver `_max_tries=1`). Laravel reaper
  fails+refunds at 10 min; if Golden Treasure had already applied, reconcile via the agent dashboard.
- Common reasons: `gtreasure:account_exists`, `gtreasure:operation_refused` (over-limit /
  insufficient), `gtreasure:invalid_password_format`, `gtreasure:auth_failed` (creds wrong / session
  unrecoverable), `gtreasure:rate_limited` (transient — Laravel reaper picks up),
  `gtreasure:requires_operator_action_*` (2FA / verify code — clear via agent UI).
- The agent account must **not have 2FA enabled** — Google Authenticator (`code:30200`/`30201`) and
  system verify codes (`code:30100`) require operator interaction; our automation can't satisfy them.
```

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_goldentreasure_integration.py CLAUDE.md docs/architecture.md docs/runbook.md
git commit -m "test+docs(goldentreasure): integration tests + CLAUDE.md/architecture/runbook updates

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase-4 acceptance (manual, after the suite is green)

Against the real Golden Treasure test agent from the findings doc (`Test02Gd1WEB / Zaeem@1233`):

1. In Filament, add a Golden Treasure game: `backend_driver='goldentreasure'`,
   `backend_url='https://agent.goldentreasure.mobi'`, `backend_username='Test02Gd1WEB'`,
   `backend_password='Zaeem@1233'`. If `backend_driver` is enum-restricted, add `goldentreasure` to
   the enum.
2. **Confirm the agent account is not 2FA-enabled** (the findings doc tested with 2FA off; if it's
   been enabled since, ops will fail with `gtreasure:requires_operator_action_*` until cleared).
3. Restart the local stack:
   - `pkill -f "uvicorn app.main" ; pkill -f "arq app.worker"`
   - `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 &`
   - `.venv/bin/arq app.worker.settings.WorkerSettings &`
   - `.venv/bin/python -m app.tools.ping` → expect `200 {"ok":true}`.
4. Trigger ops from Laravel against the new Golden Treasure game:
   - **AGENT_BALANCE** → SUCCEEDED with the real agent balance (validate against the agent dashboard).
   - **CREATE_ACCOUNT** (Laravel sends `account_username`) → SUCCEEDED; player visible in the agent
     UI's player list; `result.username` echoes the Laravel-sent name; `result.password` is a
     memorable alphanumeric.
   - **READ_BALANCE** on that player → `balance_cents: 0`.
   - **RECHARGE** a small amount → SUCCEEDED; balances in the agent UI move by `ceil(cents/100)` dollars.
   - **REDEEM** → SUCCEEDED (or `gtreasure:operation_refused` if over the player's balance).
   - **RESET_PASSWORD** → SUCCEEDED; new memorable password stored Laravel-side.
5. **Session reuse:** trigger a second AGENT_BALANCE — worker logs show no `_do_login` between calls.
6. **Throttle:** trigger two RECHARGEs back-to-back on the same game — the second one should be
   delayed ~5s by the throttle gate (worker logs show the gap; both still succeed).
7. **(Optional) Force a re-login** by deleting the session: `redis-cli DEL gtreasure_session:<game_id>`,
   trigger again — confirm a fresh login + the op succeeds.

---

## Self-review (completed by plan author)

**Spec coverage:**
- §3 API summary → Tasks 3 (errors), 4 (crypto), 7 (client), 8 (backend).
- §4 GT1 money units → Task 8 (_to_cents/_to_dollars + tests).
- §4 GT2 per-driver `_max_tries=1` → Task 9 (`NON_IDEMPOTENT_DRIVERS` includes goldentreasure).
- §4 GT3 throttle → Task 7 (`_acquire_throttle`) + Task 8 (mutating ops use `throttle=True`).
- §4 GT4 omit external_user_id → Task 8 `create_account` returns `external_user_id=None`.
- §4 GT5 duplicated session.py → Task 6.
- §4 GT6 concurrent tokens (no double-check) → Task 7 `get_token` single re-read + concurrent-login test.
- §4 GT7 RESET_PASSWORD not throttled → Task 8 test `test_reset_password_posts_to_updatePlayer_and_does_not_throttle`.
- §4 GT8 pycryptodome runtime dep → Task 1.
- §4 GT9 2FA codes → Task 7 `_do_login` raises `gtreasure:requires_operator_action_*` for 30100/30200/30201.
- §4 GT10 `redis=None` kwarg → Tasks 9 (registry), 10 (executor), 11 (worker).
- §6.1 crypto with 5 oracles → Task 4 (all 5 + extra coverage of empty-skip + stime default).
- §6.2 errors → Task 3.
- §6.3 passwords re-export → Task 5.
- §6.4 session.py → Task 6.
- §6.5 client → Task 7.
- §6.6 backend → Task 8.
- §6.7 registry → Task 9.
- §6.8 executor + worker → Tasks 10, 11.
- §7-§8 error/operational matrix → Tasks 3 + 7 cover; integration tests (Task 12) exercise terminal cache + transient + relogin retry.
- §9 testing → covered by Tasks 1-12.
- §10 Laravel deps (none new) → Task 12 runbook documents operator setup.
- §11 deferred → CLAUDE.md / runbook note the orphan-on-crash window + 2FA caveat.

**Placeholder scan:** No TBD/TODO. Every code step shows complete, runnable code.

**Type consistency:**
- `BackendContext(credentials, user_id, account, idempotency_key, account_username)` — unchanged.
- `resolve_backend(driver, *, credentials, http_client, settings, session_store=None, redis=None)` — Task 9 + Task 10 + Task 11 + integration tests use the same name.
- `GoldenTreasureClient(base_url, username, password, http_client, session_store, redis, game_id, fingerprint=...)` — Tasks 7, 8, 9.
- `GoldenTreasureClient.get_token(invalidate=None)`, `.call(path, params, *, throttle=False)` — Tasks 7, 8.
- `CachedSession(token, expires_at)`, `SessionStore.{get,set,clear,login_lock}` — Task 6, used in Tasks 7+9.
- `NON_IDEMPOTENT_DRIVERS` (Task 9) — Phase 3's existing API endpoint logic auto-applies `_max_tries=1` once goldentreasure is added.
- `execute_operation(..., redis=None)` — Task 10, threaded by Task 11.
- `_to_cents` / `_to_dollars` — Task 8.
