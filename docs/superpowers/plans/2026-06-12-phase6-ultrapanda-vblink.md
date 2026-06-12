# Phase 6 — UltraPanda + VBlink backends — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate two branded hosts of the same ThinkPHP-backed vendor (UltraPanda + VBlink) as a single backend module with the second driver wired as a registry alias. No captcha. Single active session per agent → Gameroom-style token cache + DCL. `code 167` rate-limit on `enterScore` → Golden-Treasure-style `SET NX` throttle.

**Architecture:** One `app/backends/ultrapanda/` module with `crypto.py` (three AES/MD5 primitives), `session.py` (Redis token store + login lock), `client.py` (auto-signed JSON-RPC client with x-token headers + enterScore throttle + retry-once-after-relogin), `backend.py` (6 ops), `errors.py` (code map), `passwords.py` (re-export). Registry alias frozenset `{"ultrapanda", "vblink"}` resolves to the same backend class with `driver_prefix` taken from the requested key.

**Tech Stack:** Python 3.12, FastAPI/arq, httpx (async, respx for mocking), Redis (fakeredis for tests), pycryptodome (AES, already in `pyproject.toml` from Phase 4 Golden Treasure), hashlib (MD5).

**Spec:** `docs/superpowers/specs/2026-06-12-phase6-ultrapanda-vblink-design.md`
**Findings doc:** `/Applications/development/ultrapanda-standalone/api_findings.md`
**Reference client (read-only):** `/Applications/development/ultrapanda-standalone/up_client.py` — the verified standalone client; the implementer may consult it for confirmation but must not import or copy structurally.
**Branch:** `feat/phase6-ultrapanda-vblink` (already created)

---

## Conventions

- Every task ends with `make lint && make type && make test` and a commit on `feat/phase6-ultrapanda-vblink`.
- All unit tests live in `tests/unit/` flat (`tests/unit/test_<topic>.py`).
- HTTP mocking uses `respx` (existing convention); Redis tests use the existing `fake_redis` fixture in `tests/conftest.py`.
- All raised error reasons are driver-prefixed via `self._client._driver` so the same code surfaces as `"ultrapanda:..."` or `"vblink:..."` depending on which driver was resolved.
- The fingerprint header value is a hardcoded constant taken from the captured traffic; the server doesn't validate it (findings §7.4).

---

## Task 1: Crypto primitives (`crypto.py`)

Three pure functions, fixture-tested against the verified live values from the findings doc.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/__init__.py`
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/crypto.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_crypto.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ultrapanda_crypto.py`:

```python
import re
from urllib.parse import unquote

from app.backends.ultrapanda.crypto import (
    SIGN_SECRET,
    decrypt_xtoken,
    encrypt_login_cred,
    encrypt_xtoken,
    sign_body,
)


# --- encrypt_login_cred ---

def test_encrypt_login_cred_username_fixture():
    """Fixture from findings doc §1.1, verified against captured traffic."""
    assert encrypt_login_cred("TestUP159", 1781187351) == "VhMfl38nq02TCY8sqZu5mg=="


def test_encrypt_login_cred_password_fixture():
    """Fixture from findings doc §1.1."""
    assert encrypt_login_cred("Test1234", 1781187351) == "j9HtJjroTJboYOiA/nGdlQ=="


def test_encrypt_login_cred_uses_different_keys_per_timestamp():
    # Same plaintext + different timestamps → different ciphertexts.
    a = encrypt_login_cred("hello", 1700000000)
    b = encrypt_login_cred("hello", 1700000001)
    assert a != b


# --- sign_body ---

def test_sign_secret_matches_findings_doc():
    assert SIGN_SECRET == "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"


def test_sign_body_login_fixture_matches_captured_md5():
    """The captured login body from findings §3 (Login):
       { username: AES("TestUP159"), password: AES("Test1234"), stime: 1781187351, auth_code: "" }
       produced sign = "fb9013238f78c92d7713fe5523e8b16a" (findings §1.2).
    """
    body = {
        "username": "VhMfl38nq02TCY8sqZu5mg==",
        "password": "j9HtJjroTJboYOiA/nGdlQ==",
        "stime": 1781187351,
        "auth_code": "",
    }
    assert sign_body(body, 1781187351) == "fb9013238f78c92d7713fe5523e8b16a"


def test_sign_body_skips_empty_and_null_values():
    a = sign_body({"a": "x", "b": "", "c": None, "d": "y"}, 1234567890)
    # Identical to a body where b/c are simply absent
    b = sign_body({"a": "x", "d": "y"}, 1234567890)
    assert a == b


def test_sign_body_skips_stime_key_from_concat():
    """The body may carry a 'stime' key; the signer must NOT include it in the concat
    (it's appended as a separate term after the concat)."""
    a = sign_body({"a": "x", "stime": 1234567890}, 1234567890)
    b = sign_body({"a": "x"}, 1234567890)
    assert a == b


def test_sign_body_sorts_keys_alphabetically_before_concat():
    """Key order in the input dict must not affect the signature."""
    a = sign_body({"z": "1", "a": "2", "m": "3"}, 1234567890)
    b = sign_body({"a": "2", "m": "3", "z": "1"}, 1234567890)
    assert a == b


def test_sign_body_returns_lowercase_hex():
    s = sign_body({"a": "x"}, 1234567890)
    assert re.fullmatch(r"[0-9a-f]{32}", s)


# --- encrypt_xtoken ---

def test_encrypt_xtoken_round_trip_with_known_token_and_key():
    """The doc gives the input (URL-encoded token + ms_time) but not the exact ciphertext.
    We verify via round-trip: decrypt(encrypt(t, key), key) == t.
    """
    token = "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    out = encrypt_xtoken(token, 1781187352387)
    assert isinstance(out, str)
    assert "%" in out  # URL-encoded
    # Round-trip via the helper (which un-urlencodes, base64-decodes, AES-decrypts).
    assert decrypt_xtoken(out, 1781187352387) == token


def test_encrypt_xtoken_preserves_token_url_encoded_form():
    """Token must be used VERBATIM as plaintext — no decoding before encryption.
    Findings §1.3: the JS stores sessionStorage['Admin-Token'] in URL-encoded form and
    uses that exact stored string as plaintext for x-token AES.
    """
    token_urlenc = "abc%2Fdef%3D"
    # If our impl wrongly url-decoded before encrypting, decryption would yield "abc/def="
    out = encrypt_xtoken(token_urlenc, 1781187352387)
    assert decrypt_xtoken(out, 1781187352387) == token_urlenc


def test_encrypt_xtoken_uses_xtu_plus_ms_as_key_length_16():
    """ms_time is 13 digits; key = 'xtu' + 13 digits = 16 bytes → AES-128."""
    out = encrypt_xtoken("plaintext_token_value", 1234567890123)
    # Output must be URL-encoded base64 (no raw '/', '+', '=' outside percent-encoding)
    decoded_once = unquote(out)
    # base64 decodes cleanly
    import base64
    decoded_bytes = base64.b64decode(decoded_once)
    assert len(decoded_bytes) % 16 == 0
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_crypto.py -v`
Expected: `ModuleNotFoundError: No module named 'app.backends.ultrapanda'`

- [ ] **Step 3: Implement**

Create `app/backends/ultrapanda/__init__.py`:
```python
```
(empty)

Create `app/backends/ultrapanda/crypto.py`:

```python
"""Three crypto primitives for the UltraPanda/VBlink vpower backend.

All three are reverse-engineered from the vendor's JS bundle and verified byte-for-byte
against captured traffic; see /Applications/development/ultrapanda-standalone/api_findings.md §1.

Primitives:
  encrypt_login_cred(plain, stime_sec) -> base64 ciphertext   (login body fields only)
  sign_body(body, stime_sec) -> lowercase hex MD5             (every request body)
  encrypt_xtoken(admin_token, ms_time) -> URL-encoded b64 ct  (x-token header value)
"""
import base64
import hashlib
from urllib.parse import quote, unquote

from Crypto.Cipher import AES


SIGN_SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"
"""Hardcoded signing secret extracted from the vendor's JS bundle (findings §1.2)."""


def _pkcs7_pad(b: bytes) -> bytes:
    pad = 16 - (len(b) % 16)
    return b + bytes([pad]) * pad


def _pkcs7_unpad(b: bytes) -> bytes:
    pad = b[-1]
    return b[:-pad]


def _aes_ecb_encrypt_b64(plaintext: str, key: str) -> str:
    """AES-128-ECB + PKCS7 padding; returns base64 of the ciphertext."""
    ct = AES.new(key.encode(), AES.MODE_ECB).encrypt(_pkcs7_pad(plaintext.encode()))
    return base64.b64encode(ct).decode()


def _aes_ecb_decrypt_b64(b64: str, key: str) -> str:
    pt = AES.new(key.encode(), AES.MODE_ECB).decrypt(base64.b64decode(b64))
    return _pkcs7_unpad(pt).decode()


def encrypt_login_cred(plain: str, stime_sec: int) -> str:
    """AES-128-ECB + PKCS7 + base64 with key = '123' + str(stime_sec) + 'abc'.

    Used for the `username` and `password` fields on POST /user/login.
    Key length: 3 + 10 + 3 = 16 bytes (AES-128).
    """
    return _aes_ecb_encrypt_b64(plain, "123" + str(stime_sec) + "abc")


def sign_body(body: dict, stime_sec: int) -> str:
    """MD5( ''.join(str(v) for k,v in sorted(body) if k!='stime' and v not in ('', None))
           + str(stime_sec) + SIGN_SECRET ).

    Returns lowercase hex. Injected into the body as `sign` after the body is otherwise complete.
    """
    concat = ""
    for k in sorted(body):
        if k == "stime":
            continue
        v = body[k]
        if v == "" or v is None:
            continue
        concat += str(v)
    return hashlib.md5((concat + str(stime_sec) + SIGN_SECRET).encode()).hexdigest()


def encrypt_xtoken(admin_token: str, ms_time: int) -> str:
    """AES-128-ECB + PKCS7 of `admin_token` (used VERBATIM — URL-encoded as received from
    /user/login), key = 'xtu' + str(ms_time) (16 bytes for a 13-digit ms timestamp).

    Returns urlencode(base64(ciphertext)) — the urlencode is applied to the base64 output,
    matching the JS interceptor exactly (findings §1.3).
    """
    b64 = _aes_ecb_encrypt_b64(admin_token, "xtu" + str(ms_time))
    return quote(b64, safe="")


def decrypt_xtoken(xtoken_value: str, ms_time: int) -> str:
    """Inverse of encrypt_xtoken; used only by tests for round-trip verification."""
    b64 = unquote(xtoken_value)
    return _aes_ecb_decrypt_b64(b64, "xtu" + str(ms_time))
```

- [ ] **Step 4: Run; tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_crypto.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 311 → 322.

- [ ] **Step 6: Commit**

```bash
git add app/backends/ultrapanda/ tests/unit/test_ultrapanda_crypto.py
git commit -m "feat(ultrapanda): three crypto primitives (AES login, MD5 sign, AES x-token)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Token session store (`session.py`)

Near-copy of `app/backends/gameroom/session.py`, parameterized for a token string + Redis key prefix `vpower_session:`.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/session.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_session.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ultrapanda_session.py`:

```python
import asyncio

import pytest

from app.backends.ultrapanda.session import (
    CachedSession,
    InMemoryTokenStore,
    RedisTokenStore,
)


async def test_in_memory_set_get_clear():
    store = InMemoryTokenStore()
    assert await store.get(1) is None
    await store.set(1, CachedSession(token="tok1", expires_at=9_999_999_999), ttl_seconds=60)
    got = await store.get(1)
    assert got is not None and got.token == "tok1"
    await store.clear(1)
    assert await store.get(1) is None


async def test_redis_set_get_clear_and_key_prefix(fake_redis):
    store = RedisTokenStore(fake_redis)
    await store.set(7, CachedSession(token="abc", expires_at=9_999_999_999), ttl_seconds=120)
    raw = await fake_redis.get("vpower_session:7")
    assert raw is not None and b"abc" in raw
    got = await store.get(7)
    assert got is not None and got.token == "abc" and got.expires_at == 9_999_999_999
    await store.clear(7)
    assert await store.get(7) is None


async def test_redis_set_respects_ttl(fake_redis):
    store = RedisTokenStore(fake_redis)
    await store.set(8, CachedSession(token="t", expires_at=9_999_999_999), ttl_seconds=1)
    ttl = await fake_redis.ttl("vpower_session:8")
    assert 0 < ttl <= 1


async def test_redis_login_lock_writes_and_clears_key(fake_redis):
    store = RedisTokenStore(fake_redis)
    async with store.login_lock(game_id=9, ttl_seconds=5):
        assert (await fake_redis.exists("vpower_session_lock:9")) == 1
    assert (await fake_redis.exists("vpower_session_lock:9")) == 0


async def test_redis_login_lock_setnx_blocks_second_acquire(fake_redis):
    store = RedisTokenStore(fake_redis)
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

- [ ] **Step 2: Run; expect ModuleNotFoundError**

- [ ] **Step 3: Implement**

Create `app/backends/ultrapanda/session.py`:

```python
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CachedSession:
    token: str           # the AdminToken string as returned by /user/login (URL-encoded form, verbatim)
    expires_at: int      # unix seconds; when we consider the cache stale (server doesn't tell us)


class TokenStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...

    def login_lock(
        self, game_id: int, *, ttl_seconds: int = 10,
        poll_seconds: float = 0.1, acquire_timeout: float = 10.0,
    ):
        """Async context manager that serializes /user/login calls for a game across workers.

        UltraPanda/VBlink enforce single-active-session-per-account at the application layer
        (findings §1.4); re-logging in invalidates the previous token. The lock prevents two
        workers from racing each other into the kicked-out state.

        Raises TimeoutError if the lock can't be acquired within acquire_timeout.
        """


class InMemoryTokenStore:
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
    async def login_lock(self, game_id: int, *, ttl_seconds: int = 10,
                         poll_seconds: float = 0.1, acquire_timeout: float = 10.0):
        lock = self._locks.setdefault(game_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"vpower login lock acquire timeout (game_id={game_id})") from exc
        try:
            yield
        finally:
            lock.release()


def _key_session(game_id: int) -> str:
    return f"vpower_session:{game_id}"


def _key_lock(game_id: int) -> str:
    return f"vpower_session_lock:{game_id}"


class RedisTokenStore:
    """Redis-backed token store + SET NX login lock. Shared across all workers."""

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
            res = await self._redis.set(key, b"1", nx=True, ex=ttl_seconds)
            if res:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"vpower login lock acquire timeout (game_id={game_id})")
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

- [ ] **Step 4: Run; tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_session.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 322 → 327.

- [ ] **Step 6: Commit**

```bash
git add app/backends/ultrapanda/session.py tests/unit/test_ultrapanda_session.py
git commit -m "feat(ultrapanda): token store + SET-NX login lock (mirrors gameroom)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Error code mapping (`errors.py`)

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/errors.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_errors.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ultrapanda_errors.py`:

```python
from app.backends.ultrapanda.errors import map_code


def test_success_code_returns_none():
    # 20000 is success; the mapper is only called on non-success, but defensively returns None.
    assert map_code(20000, op="recharge") is None


def test_login_bad_credentials():
    slug, terminal = map_code(5, op="login")
    assert slug == "bad_credentials"
    assert terminal is True


def test_create_duplicate_account():
    slug, terminal = map_code(8, op="create_account")
    assert slug == "account_exists"
    assert terminal is True


def test_recharge_insufficient_agent_funds():
    slug, terminal = map_code(21, op="recharge")
    assert slug == "insufficient_agent_funds"
    assert terminal is True


def test_redeem_insufficient_player_credit():
    slug, terminal = map_code(21, op="redeem")
    assert slug == "insufficient_player_credit"
    assert terminal is True


def test_player_not_found_on_recharge():
    slug, terminal = map_code(22, op="recharge")
    assert slug == "player_not_found"
    assert terminal is True


def test_no_permission():
    slug, terminal = map_code(52, op="recharge")
    assert slug == "no_permission"
    assert terminal is True


def test_rate_limit_is_transient():
    slug, terminal = map_code(167, op="recharge")
    assert slug == "rate_limited"
    assert terminal is False


def test_invalid_chars_on_create():
    slug, terminal = map_code(1003, op="create_account")
    assert slug == "account_invalid_chars"
    assert terminal is True


def test_session_expired_is_transient():
    slug, terminal = map_code(1086, op="recharge")
    assert slug == "session_expired"
    assert terminal is False


def test_unknown_code_returns_terminal_unknown_slug():
    slug, terminal = map_code(99999, op="recharge")
    assert slug == "unknown:99999"
    assert terminal is True
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

- [ ] **Step 3: Implement**

Create `app/backends/ultrapanda/errors.py`:

```python
"""Code → (slug, is_terminal) mapping for the vpower (UltraPanda/VBlink) backend.

Only codes actually observed live against the test backends are mapped (see findings §4).
Frontend-dictionary-only codes (§5) are intentionally NOT mapped — they don't appear in
real API responses.
"""


def map_code(code: int, *, op: str) -> tuple[str, bool] | None:
    """Translate a vpower response code into a (reason_slug, is_terminal) pair.

    Returns None for `code == 20000` (success). The `op` argument lets us disambiguate
    code 21 (the server returns the same generic message for over-recharge agent-side and
    over-withdraw player-side).
    """
    if code == 20000:
        return None
    if code == 5:
        return ("bad_credentials", True)
    if code == 8:
        return ("account_exists", True)
    if code == 21:
        if op == "recharge":
            return ("insufficient_agent_funds", True)
        if op == "redeem":
            return ("insufficient_player_credit", True)
        return ("unknown_21", True)
    if code == 22:
        return ("player_not_found", True)
    if code == 52:
        return ("no_permission", True)
    if code == 167:
        return ("rate_limited", False)
    if code == 1003:
        return ("account_invalid_chars", True)
    if code == 1086:
        return ("session_expired", False)
    return (f"unknown:{code}", True)
```

- [ ] **Step 4: Run; tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_errors.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 327 → 338.

- [ ] **Step 6: Commit**

```bash
git add app/backends/ultrapanda/errors.py tests/unit/test_ultrapanda_errors.py
git commit -m "feat(ultrapanda): code → (slug, is_terminal) mapping

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Password generator (`passwords.py`)

The vpower backend has no server-side password policy (findings §3.1 / §3.3), so we reuse the existing memorable generator unchanged.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/passwords.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_passwords.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_ultrapanda_passwords.py`:

```python
import re

from app.backends.ultrapanda.passwords import generate_vpower_password


def test_password_is_memorable_word_plus_digits():
    pw = generate_vpower_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_password_charset_alphanumeric_only():
    for _ in range(50):
        pw = generate_vpower_password()
        assert re.fullmatch(r"[A-Za-z0-9]+", pw), pw


def test_password_varies():
    assert len({generate_vpower_password() for _ in range(20)}) > 1
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

- [ ] **Step 3: Implement**

Create `app/backends/ultrapanda/passwords.py`:

```python
"""Re-export the memorable password generator. The vpower backend has no server-side
password complexity rule (findings §3.1) so the existing GameVault generator is plenty.
"""
from app.backends.gamevault.passwords import generate_memorable_password


def generate_vpower_password() -> str:
    """Memorable password for UltraPanda/VBlink create + reset.

    Format: `{Capitalized word}{4 digits}` (e.g. "Tiger4783"). Alphanumeric ≤12 chars.
    """
    return generate_memorable_password()
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_passwords.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 338 → 341.

- [ ] **Step 6: Commit**

```bash
git add app/backends/ultrapanda/passwords.py tests/unit/test_ultrapanda_passwords.py
git commit -m "feat(ultrapanda): password generator (reuses gamevault)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Config knobs

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/config.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_config.py`:

```python
def test_vpower_defaults():
    from app.config import Settings
    s = Settings()
    assert s.vpower_session_ttl_seconds == 1800
    assert s.vpower_throttle_ttl_seconds == 6
    assert s.vpower_throttle_acquire_timeout_seconds == 10.0
    assert s.vpower_session_lock_ttl_seconds == 10
    assert s.vpower_session_lock_acquire_timeout_seconds == 10.0
```

- [ ] **Step 2: Run; expect failure**

Run: `.venv/bin/pytest tests/unit/test_config.py::test_vpower_defaults -v`
Expected: `AttributeError: 'Settings' object has no attribute 'vpower_session_ttl_seconds'`

- [ ] **Step 3: Add fields**

In `app/config.py`, after the existing `aspnet_lock_acquire_timeout_seconds: float = 30.0` line (or the last existing knob, wherever the trailing line currently sits), add:

```python
    vpower_session_ttl_seconds: int = 1800
    vpower_throttle_ttl_seconds: int = 6
    vpower_throttle_acquire_timeout_seconds: float = 10.0
    vpower_session_lock_ttl_seconds: int = 10
    vpower_session_lock_acquire_timeout_seconds: float = 10.0
```

- [ ] **Step 4: Test passes**

Run: `.venv/bin/pytest tests/unit/test_config.py::test_vpower_defaults -v`
Expected: 1 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 341 → 342.

- [ ] **Step 6: Commit**

```bash
git add app/config.py tests/unit/test_config.py
git commit -m "config(phase6): add vpower session + throttle knobs

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Client (`client.py`)

The biggest task in this phase. Three sub-commits to keep diffs reviewable.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/client.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_client.py`

### Sub-task 6a: Login + auto-sign + auto-headers

- [ ] **Step 1: Write failing tests for login + signed call**

Create `tests/unit/test_ultrapanda_client.py`:

```python
import json
import time
from urllib.parse import unquote

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.ultrapanda.client import FINGERPRINT, UltraPandaClient
from app.backends.ultrapanda.crypto import decrypt_xtoken
from app.backends.ultrapanda.session import (
    CachedSession,
    InMemoryTokenStore,
)

BASE = "https://up.test"


def _client(http, store=None, redis=None) -> UltraPandaClient:
    return UltraPandaClient(
        base_url=BASE,
        username="TestUP159",
        password="Test1234",
        http_client=http,
        session_store=store or InMemoryTokenStore(),
        redis=redis,
        game_id=42,
        session_ttl_seconds=1800,
        throttle_ttl_seconds=6,
        throttle_acquire_timeout_seconds=2.0,
        session_lock_ttl_seconds=10,
        session_lock_acquire_timeout_seconds=2.0,
        driver_prefix="ultrapanda",
    )


# --- login ---

@respx.mock
async def test_login_posts_aes_encrypted_creds_and_caches_token(fake_redis):
    """The login body must carry AES-encrypted username/password, stime, auth_code='',
    and a valid `sign`. On success (code 20000), the returned token is cached verbatim.
    """
    captured: dict = {}

    def login_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(200, json={
            "code": 20000,
            "name": "TestUP159",
            "token": "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D",
            "data": {},
        })

    respx.post(f"{BASE}/user/login").mock(side_effect=login_handler)
    store = InMemoryTokenStore()
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        token = await c.get_or_login()

    assert token == "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"
    # Body shape
    assert set(captured.keys()) >= {"username", "password", "stime", "auth_code", "sign"}
    # AES-encrypted (base64 with possible '/', '+', '='); definitely not plain
    assert captured["username"] != "TestUP159"
    assert captured["password"] != "Test1234"
    assert captured["auth_code"] == ""
    # Cached
    cached = await store.get(42)
    assert cached is not None
    assert cached.token == "Ul%2Ba9iVUvWnqlti2VP%2BatFnckAzxSNbIcEVrTxn%2F%2FTg%3D"


@respx.mock
async def test_login_bad_credentials_raises_terminal_backend_error(fake_redis):
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 5, "message": "帐号或密码错误"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(BackendError) as ei:
            await c.get_or_login()
    assert ei.value.reason == "ultrapanda:bad_credentials"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_login_5xx_is_transient(fake_redis):
    respx.post(f"{BASE}/user/login").mock(return_value=httpx.Response(500, text="boom"))
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, redis=fake_redis)
        with pytest.raises(TransientBackendError):
            await c.get_or_login()


@respx.mock
async def test_get_or_login_returns_cached_token_when_fresh(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="cached_tok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        token = await c.get_or_login()
    assert token == "cached_tok"
    # No login call happened
    assert len(respx.calls) == 0


# --- signed call ---

@respx.mock
async def test_signed_call_injects_stime_sign_and_headers(fake_redis):
    """Every non-login POST gets `sign` + `stime` in the body and `x-time`, `x-token`,
    `x-fingerprint` headers."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="testtok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    route = respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call("/user/CurScore", {"token": "testtok"})
    assert body == {"code": 20000, "LimitNum": "3.00"}
    sent = route.calls.last.request
    sent_body = json.loads(sent.content)
    assert "stime" in sent_body and isinstance(sent_body["stime"], int)
    assert "sign" in sent_body and len(sent_body["sign"]) == 32
    # Headers
    ms_time = int(sent.headers["x-time"])
    assert len(sent.headers["x-time"]) == 13
    assert sent.headers["x-fingerprint"] == FINGERPRINT
    # x-token decrypts back to the cached token
    assert decrypt_xtoken(sent.headers["x-token"], ms_time) == "testtok"
    # Content type
    assert sent.headers["content-type"] == "application/json;charset=UTF-8"
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_client.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement minimal client (login + call, no throttle/relogin yet)**

Create `app/backends/ultrapanda/client.py`:

```python
import json
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.ultrapanda.crypto import (
    encrypt_login_cred,
    encrypt_xtoken,
    sign_body,
)
from app.backends.ultrapanda.errors import map_code
from app.backends.ultrapanda.session import CachedSession, TokenStore


FINGERPRINT = "45657e48dc42985f3e021fc065112c22"
"""Constant device fingerprint. Server doesn't validate (findings §7.4)."""

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "x-fingerprint": FINGERPRINT,
}


def _expired(session: CachedSession | None, *, skew_seconds: int = 60) -> bool:
    return session is None or session.expires_at - skew_seconds <= int(time.time())


class UltraPandaClient:
    """Auto-signed JSON-RPC client for the vpower (UltraPanda/VBlink) backend.

    Responsibilities: (1) token cache + DCL around login, (2) auto-inject `stime`+`sign`
    into every request body, (3) auto-inject `x-time`+`x-token`+`x-fingerprint` headers,
    (4) throttle `enterScore` calls via SET NX, (5) detect session-expired and retry-once.
    """

    def __init__(
        self, *, base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: TokenStore,
        redis,
        game_id: int,
        session_ttl_seconds: int,
        throttle_ttl_seconds: int,
        throttle_acquire_timeout_seconds: float,
        session_lock_ttl_seconds: int,
        session_lock_acquire_timeout_seconds: float,
        driver_prefix: str,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http_client
        self._store = session_store
        self._redis = redis
        self._game_id = game_id
        self._session_ttl = session_ttl_seconds
        self._throttle_ttl = throttle_ttl_seconds
        self._throttle_acquire = throttle_acquire_timeout_seconds
        self._lock_ttl = session_lock_ttl_seconds
        self._lock_acquire = session_lock_acquire_timeout_seconds
        self._driver = driver_prefix

    # ---- session ----

    async def get_or_login(self) -> str:
        cached = await self._store.get(self._game_id)
        if not _expired(cached):
            return cached.token       # type: ignore[union-attr]
        try:
            async with self._store.login_lock(
                self._game_id, ttl_seconds=self._lock_ttl,
                acquire_timeout=self._lock_acquire,
            ):
                cached = await self._store.get(self._game_id)
                if not _expired(cached):
                    return cached.token       # type: ignore[union-attr]
                return await self._do_login()
        except TimeoutError:
            # Lock contention is rare; an unlocked re-login is the right fallback (the JS
            # also doesn't lock; worst case we kick our own previous session out).
            return await self._do_login()

    async def _do_login(self) -> str:
        stime = int(time.time())
        body: dict = {
            "username": encrypt_login_cred(self._username, stime),
            "password": encrypt_login_cred(self._password, stime),
            "stime": stime,
            "auth_code": "",
        }
        body["sign"] = sign_body(body, stime)
        try:
            resp = await self._http.post(
                f"{self._base}/user/login",
                content=json.dumps(body).encode(),
                headers=_BASE_HEADERS,
            )
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self._driver}:login_transport:{type(exc).__name__}"
            ) from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"{self._driver}:login_http_{resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise TransientBackendError(f"{self._driver}:login_bad_response") from exc
        code = data.get("code")
        if code == 20000:
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise TransientBackendError(f"{self._driver}:login_missing_token")
            await self._store.set(
                self._game_id,
                CachedSession(token=token, expires_at=int(time.time()) + self._session_ttl),
                ttl_seconds=self._session_ttl,
            )
            return token
        # Failed login. Map the code.
        mapped = map_code(int(code) if isinstance(code, int) else 0, op="login")
        if mapped is None:
            raise BackendError(f"{self._driver}:login_failed")
        slug, terminal = mapped
        if terminal:
            raise BackendError(f"{self._driver}:{slug}")
        raise TransientBackendError(f"{self._driver}:{slug}")

    # ---- signed call ----

    async def call(self, path: str, params: dict | None = None) -> dict:
        """Make one signed POST. Body gets auto-signed; headers carry x-time/x-token/x-fingerprint.

        Returns the parsed JSON body. Caller is responsible for inspecting `code` and any
        op-specific fields.
        """
        token = await self.get_or_login()
        return await self._do_call(path, params or {}, token=token)

    async def _do_call(self, path: str, params: dict, *, token: str) -> dict:
        body: dict = dict(params)
        stime = int(time.time())
        body["stime"] = stime
        body["sign"] = sign_body(body, stime)
        ms_time = int(time.time() * 1000)
        x_token = encrypt_xtoken(token, ms_time)
        headers = {
            **_BASE_HEADERS,
            "x-time": str(ms_time),
            "x-token": x_token,
        }
        url = f"{self._base}{path}" if path.startswith("/") else f"{self._base}/{path}"
        try:
            resp = await self._http.post(url, content=json.dumps(body).encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self._driver}:transport:{type(exc).__name__}"
            ) from exc
        if resp.status_code >= 500:
            raise TransientBackendError(f"{self._driver}:http_{resp.status_code}")
        if resp.status_code >= 400:
            raise TransientBackendError(f"{self._driver}:http_{resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise TransientBackendError(f"{self._driver}:bad_response") from exc
```

(Note: `quote` is not imported here because `encrypt_xtoken` from `crypto.py` already URL-encodes its output. Don't add `from urllib.parse import quote` or ruff will flag F401.)

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_client.py -v`
Expected: 5 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 342 → 347.

- [ ] **Step 6: Commit (sub-commit 6a)**

```bash
git add app/backends/ultrapanda/client.py tests/unit/test_ultrapanda_client.py
git commit -m "feat(ultrapanda): client login + auto-signed call + x-token headers

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

### Sub-task 6b: Throttle for enterScore + session-death detection + retry-once-after-relogin

- [ ] **Step 7: Append throttle + session-death tests**

Append to `tests/unit/test_ultrapanda_client.py`:

```python
# --- throttle ---

@respx.mock
async def test_throttle_blocks_second_enter_score_within_ttl(fake_redis):
    """SET NX vpower_throttle:{game_id} ex=6 — second enterScore inside the TTL must wait."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "ok"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        # Pre-warm the throttle so it's "held"
        await fake_redis.set("vpower_throttle:42", b"1", ex=6, nx=True)
        # Now the call should fail-fast with throttle timeout because acquire_timeout=2.0
        with pytest.raises(TransientBackendError, match="throttle_acquire_timeout"):
            await c.call_throttled("/account/enterScore", {"account": "x", "score": "1", "user_type": 0})


@respx.mock
async def test_throttle_allows_call_when_key_absent(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call_throttled("/account/enterScore",
                                     {"account": "x", "score": "1", "user_type": 0})
    assert body["code"] == 20000
    # Throttle key was set
    assert await fake_redis.exists("vpower_throttle:42") == 1


@respx.mock
async def test_throttle_rate_limit_code_167_is_transient(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="t", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        # Throttle slot is available; the server still 167s us (overlap with another worker)
        with pytest.raises(TransientBackendError, match="rate_limited"):
            await c.call_throttled("/account/enterScore",
                                  {"account": "x", "score": "1", "user_type": 0},
                                  op="recharge")


# --- session-death detection + retry-once-after-relogin ---

@respx.mock
async def test_call_retries_once_after_session_expired_1086(fake_redis):
    """Code 1086 (Not logged in) triggers cache-clear → re-login → retry the original call."""
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    route = respx.post(f"{BASE}/user/CurScore").mock(
        side_effect=[
            httpx.Response(200, json={"code": 1086, "message": "Not logged in"}),
            httpx.Response(200, json={"code": 20000, "LimitNum": "1.50"}),
        ]
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        body = await c.call("/user/CurScore", {"token": "DEAD"})
    assert body == {"code": 20000, "LimitNum": "1.50"}
    assert route.call_count == 2
    # Cache holds the fresh token
    cached = await store.get(42)
    assert cached is not None and cached.token == "FRESH"


@respx.mock
async def test_call_does_not_retry_more_than_once_on_repeated_1086(fake_redis):
    store = InMemoryTokenStore()
    await store.set(42, CachedSession(token="DEAD", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)
    respx.post(f"{BASE}/user/login").mock(
        return_value=httpx.Response(200, json={"code": 20000, "token": "FRESH"})
    )
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 1086, "message": "Not logged in"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        c = _client(http, store=store, redis=fake_redis)
        with pytest.raises(TransientBackendError, match="session_dead_after_relogin"):
            await c.call("/user/CurScore", {"token": "DEAD"})
```

- [ ] **Step 8: Extend the client with `call_throttled` + retry-once-after-relogin**

Append to `app/backends/ultrapanda/client.py` — replace the `call` method and add helpers:

```python
    # ---- session-death-aware call ----

    async def call(self, path: str, params: dict | None = None, *, op: str = "") -> dict:
        """Signed POST with session-death detection. If the response is `code 1086`
        (Not logged in), clear the cached token, re-login, and retry the call once.
        """
        params = dict(params or {})
        token = await self.get_or_login()
        body = await self._do_call(path, params, token=token)
        if body.get("code") == 1086:
            await self._store.clear(self._game_id)
            token = await self.get_or_login()
            body = await self._do_call(path, params, token=token)
            if body.get("code") == 1086:
                raise TransientBackendError(f"{self._driver}:session_dead_after_relogin")
        return body

    # ---- throttled call (enterScore only) ----

    async def call_throttled(
        self, path: str, params: dict | None = None, *, op: str = "",
    ) -> dict:
        """Like `.call()` but acquires `SET NX vpower_throttle:{game_id} ex={throttle_ttl}`
        before issuing the request. Used for /account/enterScore (recharge + redeem).

        If the SET NX is contended, blocks-and-polls up to throttle_acquire_timeout seconds;
        on timeout raises TransientBackendError(...:throttle_acquire_timeout).

        After the call returns, if the server STILL says code 167 (overlap with another
        process), raise TransientBackendError(...:rate_limited).
        """
        await self._acquire_throttle()
        body = await self.call(path, params, op=op)
        if body.get("code") == 167:
            raise TransientBackendError(f"{self._driver}:rate_limited")
        return body

    async def _acquire_throttle(self) -> None:
        import asyncio
        key = f"vpower_throttle:{self._game_id}"
        deadline = time.monotonic() + self._throttle_acquire
        while True:
            ok = await self._redis.set(key, b"1", nx=True, ex=self._throttle_ttl)
            if ok:
                return
            if time.monotonic() >= deadline:
                raise TransientBackendError(
                    f"{self._driver}:throttle_acquire_timeout"
                )
            await asyncio.sleep(0.5)
```

(Note: the original `call` from sub-task 6a is replaced; the new version is identical but adds the 1086-retry layer.)

- [ ] **Step 9: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_client.py -v`
Expected: 10 passed (5 from 6a + 5 from 6b).

- [ ] **Step 10: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 347 → 352.

- [ ] **Step 11: Commit (sub-commit 6b)**

```bash
git add app/backends/ultrapanda/client.py tests/unit/test_ultrapanda_client.py
git commit -m "feat(ultrapanda): enterScore throttle + 1086 retry-once-after-relogin

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Backend (`backend.py`)

The 6 ops over the client.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/app/backends/ultrapanda/backend.py`
- Create: `/Applications/development/python/casino-app-automation/tests/unit/test_ultrapanda_backend.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_ultrapanda_backend.py`:

```python
import time

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import CachedSession, InMemoryTokenStore

BASE = "https://up.test"


def _credentials() -> GameCredentials:
    return GameCredentials(
        game_id=42, name="UP Test",
        backend_url=BASE, login_page_url=None,
        backend_username="TestUP159", backend_password="Test1234",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )


def _account(username: str = "userup01") -> AccountIdentity:
    return AccountIdentity(
        game_account_id=1, user_id=1, game_id=42,
        username=username, external_user_id=None,
    )


def _ctx(*, account=None, username=None) -> BackendContext:
    return BackendContext(
        credentials=_credentials(), user_id=1, account=account,
        idempotency_key="idem", account_username=username,
    )


def _make_backend(http, fake_redis):
    store = InMemoryTokenStore()
    client = UltraPandaClient(
        base_url=BASE, username="u", password="p",
        http_client=http, session_store=store, redis=fake_redis,
        game_id=42, session_ttl_seconds=1800,
        throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=2.0,
        session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=2.0,
        driver_prefix="ultrapanda",
    )
    return UltraPandaBackend(client), store


async def _seed_session(store):
    await store.set(42, CachedSession(token="testtok", expires_at=int(time.time()) + 3600),
                    ttl_seconds=3600)


# --- agent_balance ---

@respx.mock
async def test_agent_balance_returns_LimitNum_as_cents(fake_redis):
    respx.post(f"{BASE}/user/CurScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "LimitNum": "3.00"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents == 300


# --- read_balance ---

@respx.mock
async def test_read_balance_returns_curScore_as_cents(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "curScore": 1.50})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.read_balance(_ctx(account=_account("u01")))
    assert result.balance_cents == 150


@respx.mock
async def test_read_balance_unknown_account_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/getPlayerScore").mock(
        return_value=httpx.Response(200, json={"code": 22, "message": "转入账号不存在"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.read_balance(_ctx(account=_account("nope")))
    assert ei.value.reason == "ultrapanda:player_not_found"


# --- create_account ---

@respx.mock
async def test_create_account_posts_savePlayer_and_returns_credentials(fake_redis):
    route = respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "新增玩家成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.create_account(_ctx(username="newuser01"))
    assert result.username == "newuser01"
    assert result.password
    sent_body = route.calls.last.request.content.decode()
    assert '"account": "newuser01"' in sent_body or '"account":"newuser01"' in sent_body


@respx.mock
async def test_create_account_duplicate_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/savePlayer").mock(
        return_value=httpx.Response(200, json={"code": 8, "message": "该帐号已被使用"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.create_account(_ctx(username="existing"))
    assert ei.value.reason == "ultrapanda:account_exists"


# --- reset_password ---

@respx.mock
async def test_reset_password_sends_all_required_fields_and_returns_pwd(fake_redis):
    route = respx.post(f"{BASE}/account/updatePlayer").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "编辑玩家成功",
                                               "info": {"Account": "u01"}})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        result = await backend.reset_password(_ctx(account=_account("u01")))
    assert result.password
    body = route.calls.last.request.content.decode()
    for k in ("account", "pwd", "name", "tel_area_code", "phone", "remark"):
        assert f'"{k}"' in body


# --- recharge ---

@respx.mock
async def test_recharge_sends_total_credit_cents_as_score(fake_redis):
    """Regression guard: must send total_credit_cents (principal + bonus), not amount_cents."""
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "进分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.recharge(
            _ctx(account=_account("u01")),
            amount_cents=1200, bonus_cents=1200, total_credit_cents=2400,
        )
    body = route.calls.last.request.content.decode()
    assert '"score": "24.00"' in body or '"score":"24.00"' in body
    # Regression: principal alone (12.00) must NOT be the value sent
    assert '"score": "12.00"' not in body and '"score":"12.00"' not in body
    assert '"user_type": 0' in body or '"user_type":0' in body


@respx.mock
async def test_recharge_insufficient_agent_funds_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中",
                                               "test": 21})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.recharge(
                _ctx(account=_account("u01")),
                amount_cents=100, bonus_cents=0, total_credit_cents=100,
            )
    assert ei.value.reason == "ultrapanda:insufficient_agent_funds"


# --- redeem ---

@respx.mock
async def test_redeem_sends_negative_score(fake_redis):
    route = respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 20000, "message": "下分成功"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        await backend.redeem(_ctx(account=_account("u01")), amount_cents=150)
    body = route.calls.last.request.content.decode()
    assert '"score": "-1.50"' in body or '"score":"-1.50"' in body


@respx.mock
async def test_redeem_insufficient_player_credit_raises_terminal(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 21, "message": "充值失败：服务器维护中"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(BackendError) as ei:
            await backend.redeem(_ctx(account=_account("u01")), amount_cents=999)
    assert ei.value.reason == "ultrapanda:insufficient_player_credit"


# --- rate-limit ---

@respx.mock
async def test_recharge_167_rate_limit_is_transient(fake_redis):
    respx.post(f"{BASE}/account/enterScore").mock(
        return_value=httpx.Response(200, json={"code": 167, "message": "high frequency request"})
    )
    async with httpx.AsyncClient(base_url=BASE) as http:
        backend, store = _make_backend(http, fake_redis)
        await _seed_session(store)
        with pytest.raises(TransientBackendError, match="rate_limited"):
            await backend.recharge(
                _ctx(account=_account("u01")),
                amount_cents=100, bonus_cents=0, total_credit_cents=100,
            )
```

- [ ] **Step 2: Run; expect ModuleNotFoundError**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_backend.py -v`

- [ ] **Step 3: Implement**

Create `app/backends/ultrapanda/backend.py`:

```python
from app.backends.base import BackendError, TransientBackendError
from app.backends.context import BackendContext
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.errors import map_code
from app.backends.ultrapanda.passwords import generate_vpower_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _cents_to_score(cents: int) -> str:
    """Format integer cents as a 2-decimal-place dollar string for the `score` field."""
    return f"{cents / 100:.2f}"


def _raise_for_code(body: dict, *, op: str, driver: str) -> None:
    """If `body['code']` isn't 20000, map it and raise the right BackendError variant."""
    code = body.get("code")
    if code == 20000:
        return
    mapped = map_code(int(code) if isinstance(code, int) else 0, op=op)
    if mapped is None:
        # 20000 fell through to here only if code is missing — treat as transient.
        raise TransientBackendError(f"{driver}:malformed_response")
    slug, terminal = mapped
    if terminal:
        raise BackendError(f"{driver}:{slug}")
    raise TransientBackendError(f"{driver}:{slug}")


class UltraPandaBackend:
    """6 ops over the vpower JSON-RPC client. Used for both UltraPanda and VBlink
    (registry alias); driver_prefix on the underlying client distinguishes them.
    """

    def __init__(self, client: UltraPandaClient) -> None:
        self._client = client

    # ---- AGENT_BALANCE ----

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        token = await self._client.get_or_login()
        body = await self._client.call("/user/CurScore", {"token": token})
        _raise_for_code(body, op="agent_balance", driver=self._client._driver)
        limit = body.get("LimitNum")
        if limit is None:
            raise BackendError(f"{self._client._driver}:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=round(float(limit) * 100))

    # ---- READ_BALANCE ----

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        account = self._account_name(ctx)
        body = await self._client.call("/account/getPlayerScore", {"account": account})
        _raise_for_code(body, op="read_balance", driver=self._client._driver)
        cur = body.get("curScore", 0)
        return ReadBalanceResult(balance_cents=round(float(cur) * 100))

    # ---- RESET_PASSWORD ----

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        account = self._account_name(ctx)
        pwd = generate_vpower_password()
        body = await self._client.call(
            "/account/updatePlayer",
            {
                "account": account,
                "pwd": pwd,
                "name": "",
                "tel_area_code": "",
                "phone": "",
                "remark": "",
            },
        )
        _raise_for_code(body, op="reset_password", driver=self._client._driver)
        return ResetPasswordResult(password=pwd)

    # ---- RECHARGE ----

    async def recharge(
        self, ctx: BackendContext, *,
        amount_cents: int, bonus_cents: int, total_credit_cents: int,
    ) -> RechargeResult:
        account = self._account_name(ctx)
        body = await self._client.call_throttled(
            "/account/enterScore",
            {
                "account": account,
                "score": _cents_to_score(total_credit_cents),     # principal + bonus
                "user_type": 0,
            },
            op="recharge",
        )
        _raise_for_code(body, op="recharge", driver=self._client._driver)
        return RechargeResult(balance_cents=None)   # server doesn't return new player balance

    # ---- REDEEM ----

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        account = self._account_name(ctx)
        body = await self._client.call_throttled(
            "/account/enterScore",
            {
                "account": account,
                "score": f"-{_cents_to_score(amount_cents)}",
                "user_type": 0,
            },
            op="redeem",
        )
        _raise_for_code(body, op="redeem", driver=self._client._driver)
        return RedeemResult()

    # ---- CREATE_ACCOUNT ----

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        username = ctx.account_username
        if not username:
            raise BackendError(f"{self._client._driver}:account_username_required")
        pwd = generate_vpower_password()
        body = await self._client.call(
            "/account/savePlayer",
            {"account": username, "pwd": pwd},
        )
        _raise_for_code(body, op="create_account", driver=self._client._driver)
        # The vpower backend doesn't return a player ID on create; we leave external_user_id
        # unset — subsequent reads key on `account` (the username) directly.
        return CreateAccountResult(username=username, password=pwd, external_user_id=None)

    # ---- internal ----

    def _account_name(self, ctx: BackendContext) -> str:
        """The vpower backend keys on `account` (the username). external_user_id is unused."""
        if ctx.account and ctx.account.username:
            return ctx.account.username
        if ctx.account_username:
            return ctx.account_username
        raise BackendError(f"{self._client._driver}:account_name_required")
```

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_ultrapanda_backend.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 352 → 363.

- [ ] **Step 6: Commit**

```bash
git add app/backends/ultrapanda/backend.py tests/unit/test_ultrapanda_backend.py
git commit -m "feat(ultrapanda): backend with all 6 ops over UltraPandaClient

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Registry wiring

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/backends/registry.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_registry.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_registry.py`:

```python
def test_ultrapanda_and_vblink_in_non_idempotent_drivers():
    from app.backends.registry import NON_IDEMPOTENT_DRIVERS
    assert "ultrapanda" in NON_IDEMPOTENT_DRIVERS
    assert "vblink" in NON_IDEMPOTENT_DRIVERS


async def test_resolve_ultrapanda_returns_ultrapanda_backend(fake_redis):
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.backends.ultrapanda.backend import UltraPandaBackend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url="https://up.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    settings = Settings()
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "ultrapanda", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, UltraPandaBackend)
    assert b._client._driver == "ultrapanda"


async def test_resolve_vblink_returns_ultrapanda_backend_with_vblink_prefix(fake_redis):
    """VBlink is a registry alias: same class, different driver_prefix."""
    import httpx
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.backends.ultrapanda.backend import UltraPandaBackend
    from app.config import Settings
    creds = GameCredentials(
        game_id=100, name="VB",
        backend_url="https://vb.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="vblink",
    )
    settings = Settings()
    async with httpx.AsyncClient() as http:
        b = resolve_backend(
            "vblink", credentials=creds, http_client=http,
            settings=settings, redis=fake_redis,
        )
    assert isinstance(b, UltraPandaBackend)
    assert b._client._driver == "vblink"


async def test_resolve_ultrapanda_requires_credentials(fake_redis):
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_ultrapanda_credentials"):
            resolve_backend(
                "ultrapanda", credentials=creds, http_client=http,
                settings=Settings(), redis=fake_redis,
            )


async def test_resolve_ultrapanda_requires_redis():
    import httpx
    import pytest
    from app.backends.base import BackendError
    from app.backends.context import GameCredentials
    from app.backends.registry import resolve_backend
    from app.config import Settings
    creds = GameCredentials(
        game_id=99, name="UP",
        backend_url="https://up.test", login_page_url=None,
        backend_username="u", backend_password="p",
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError, match="missing_redis_client"):
            resolve_backend(
                "ultrapanda", credentials=creds, http_client=http,
                settings=Settings(), redis=None,
            )
```

- [ ] **Step 2: Run; expect failures**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v`
Expected: 5 new failures.

- [ ] **Step 3: Implement**

Edit `app/backends/registry.py`. Add imports near the top with the other backend imports:

```python
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import RedisTokenStore as VPowerTokenStore
```

Add a new alias frozenset above the `NON_IDEMPOTENT_DRIVERS` line:

```python
# Driver strings that share the vpower provider (UltraPanda + VBlink). Same wire protocol,
# only the host differs. Verified byte-identical per the Phase 6 findings doc §10.
_VPOWER_PROVIDER_DRIVERS = frozenset({"ultrapanda", "vblink"})
```

Extend `NON_IDEMPOTENT_DRIVERS`:

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({
    "gameroom", "goldentreasure", "orionstars", "milkyway", "ultrapanda", "vblink",
})
```

Add a new branch inside `resolve_backend`, before the trailing `raise BackendError("unknown_backend_driver:...")`:

```python
    if key in _VPOWER_PROVIDER_DRIVERS:
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError(f"missing_{key}_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        return UltraPandaBackend(
            UltraPandaClient(
                base_url=credentials.backend_url,
                username=credentials.backend_username,
                password=credentials.backend_password,
                http_client=http_client,
                session_store=VPowerTokenStore(redis),
                redis=redis,
                game_id=credentials.game_id,
                session_ttl_seconds=settings.vpower_session_ttl_seconds,
                throttle_ttl_seconds=settings.vpower_throttle_ttl_seconds,
                throttle_acquire_timeout_seconds=settings.vpower_throttle_acquire_timeout_seconds,
                session_lock_ttl_seconds=settings.vpower_session_lock_ttl_seconds,
                session_lock_acquire_timeout_seconds=settings.vpower_session_lock_acquire_timeout_seconds,
                driver_prefix=key,
            )
        )
```

Update the `resolve_backend` docstring to mention the new drivers and the alias relationship.

- [ ] **Step 4: Tests pass**

Run: `.venv/bin/pytest tests/unit/test_registry.py -v`
Expected: previously-passing + 5 new tests pass.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 363 → 368.

- [ ] **Step 6: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(registry): wire ultrapanda + vblink (alias frozenset)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Logging redaction

**Files:**
- Modify: `/Applications/development/python/casino-app-automation/app/logging.py`
- Modify: `/Applications/development/python/casino-app-automation/tests/unit/test_logging.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/test_logging.py`:

```python
def test_vpower_token_and_admin_token_are_redacted():
    from app.logging import _redact_in_place
    d = {
        "admin-token": "secret1",
        "Admin-Token": "secret2",
        "auth_code": "code123",
        "x-time": "1700000000000",   # NOT secret — should NOT be redacted
        "other": "visible",
    }
    _redact_in_place(d)
    assert d["admin-token"] == "***"
    assert d["Admin-Token"] == "***"
    assert d["auth_code"] == "***"
    # x-time is not a secret
    assert d["x-time"] == "1700000000000"
    assert d["other"] == "visible"
```

- [ ] **Step 2: Run; expect failure**

- [ ] **Step 3: Extend `SECRET_KEYS`**

Edit `app/logging.py`. Find the `SECRET_KEYS` set and add the two new lowercase keys at the end of the existing additions:

```python
    # Phase 6: UltraPanda/VBlink (vpower)
    "admin-token",
    "auth_code",
```

(`pwd`, `password`, `token`, `x-token` are already redacted by earlier phases.)

- [ ] **Step 4: Test passes**

Run: `.venv/bin/pytest tests/unit/test_logging.py -v`
Expected: all pass.

- [ ] **Step 5: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 368 → 369.

- [ ] **Step 6: Commit**

```bash
git add app/logging.py tests/unit/test_logging.py
git commit -m "log(phase6): redact vpower admin-token + auth_code

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Live-gated integration tests

Same pattern as Phase 5: two files, env-var gated, skipped without creds.

**Files:**
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_ultrapanda_integration.py`
- Create: `/Applications/development/python/casino-app-automation/tests/integration/test_vblink_integration.py`

- [ ] **Step 1: Create UltraPanda live test**

Create `tests/integration/test_ultrapanda_integration.py`:

```python
"""Live-gated end-to-end test against the real UltraPanda portal.

Skipped unless all of these are set:
  ULTRAPANDA_TEST_BASE_URL     e.g. https://ht.ultrapanda.mobi/api
  ULTRAPANDA_TEST_AGENT_USER   e.g. TestUP159
  ULTRAPANDA_TEST_AGENT_PASS   e.g. Test1234
  ULTRAPANDA_TEST_PLAYER       must already exist under the agent

Costs: no captcha. The vpower backend rate-limits enterScore at ~6s; this test inserts
sleep(7) between recharge and redeem to respect the throttle.
"""
import asyncio
import os

import fakeredis.aioredis as _fr
import httpx
import pytest
import pytest_asyncio

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import RedisTokenStore

_required = [
    "ULTRAPANDA_TEST_BASE_URL", "ULTRAPANDA_TEST_AGENT_USER",
    "ULTRAPANDA_TEST_AGENT_PASS", "ULTRAPANDA_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["ULTRAPANDA_TEST_BASE_URL"]
    user = os.environ["ULTRAPANDA_TEST_AGENT_USER"]
    pwd = os.environ["ULTRAPANDA_TEST_AGENT_PASS"]
    redis = _fr.FakeRedis(decode_responses=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = UltraPandaClient(
                base_url=base, username=user, password=pwd,
                http_client=http, session_store=RedisTokenStore(redis), redis=redis,
                game_id=9999, session_ttl_seconds=1800,
                throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=15.0,
                session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=10.0,
                driver_prefix="ultrapanda",
            )
            yield UltraPandaBackend(client)
    finally:
        await redis.aclose()


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9999, name="UP Live",
        backend_url=os.environ["ULTRAPANDA_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["ULTRAPANDA_TEST_AGENT_USER"],
        backend_password=os.environ["ULTRAPANDA_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="ultrapanda",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    # Respect the 6s server-side throttle on enterScore
    await asyncio.sleep(7)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    await asyncio.sleep(7)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents


async def test_live_reset_password_then_login_unaffected(backend):
    """Destructive — only run with a disposable test player."""
    player = os.environ["ULTRAPANDA_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9999,
        username=player, external_user_id=None,
    )
    result = await backend.reset_password(_ctx(account=account))
    assert result.password and len(result.password) >= 5
```

- [ ] **Step 2: Create VBlink live test**

Create `tests/integration/test_vblink_integration.py` — same shape but using `VBLINK_TEST_*` env vars and `driver_prefix="vblink"`. Full content:

```python
"""Live-gated end-to-end test against the real VBlink portal.

Skipped unless all of these are set:
  VBLINK_TEST_BASE_URL     e.g. https://gm.vblink777.club/api
  VBLINK_TEST_AGENT_USER   e.g. TestVB159
  VBLINK_TEST_AGENT_PASS   e.g. Test12345
  VBLINK_TEST_PLAYER       must already exist under the agent

VBlink runs the same backend application as UltraPanda — verified byte-identical per
the findings doc §10. This test confirms the alias wiring works end-to-end against the
real host. ~6s sleeps between enterScore calls respect the rate limit.
"""
import asyncio
import os

import fakeredis.aioredis as _fr
import httpx
import pytest
import pytest_asyncio

from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.ultrapanda.backend import UltraPandaBackend
from app.backends.ultrapanda.client import UltraPandaClient
from app.backends.ultrapanda.session import RedisTokenStore

_required = [
    "VBLINK_TEST_BASE_URL", "VBLINK_TEST_AGENT_USER",
    "VBLINK_TEST_AGENT_PASS", "VBLINK_TEST_PLAYER",
]

pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in _required),
    reason=f"set {', '.join(_required)} to run",
)


@pytest_asyncio.fixture
async def backend():
    base = os.environ["VBLINK_TEST_BASE_URL"]
    user = os.environ["VBLINK_TEST_AGENT_USER"]
    pwd = os.environ["VBLINK_TEST_AGENT_PASS"]
    redis = _fr.FakeRedis(decode_responses=False)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            client = UltraPandaClient(
                base_url=base, username=user, password=pwd,
                http_client=http, session_store=RedisTokenStore(redis), redis=redis,
                game_id=9998, session_ttl_seconds=1800,
                throttle_ttl_seconds=6, throttle_acquire_timeout_seconds=15.0,
                session_lock_ttl_seconds=10, session_lock_acquire_timeout_seconds=10.0,
                driver_prefix="vblink",
            )
            yield UltraPandaBackend(client)
    finally:
        await redis.aclose()


def _ctx(*, account=None, username=None) -> BackendContext:
    creds = GameCredentials(
        game_id=9998, name="VB Live",
        backend_url=os.environ["VBLINK_TEST_BASE_URL"],
        login_page_url=None,
        backend_username=os.environ["VBLINK_TEST_AGENT_USER"],
        backend_password=os.environ["VBLINK_TEST_AGENT_PASS"],
        api_base_url=None, api_agent_id=None, api_secret_key=None,
        binding_key=None, backend_driver="vblink",
    )
    return BackendContext(
        credentials=creds, user_id=1, account=account,
        idempotency_key="live-test", account_username=username,
    )


async def test_live_agent_balance(backend):
    result = await backend.agent_balance(_ctx())
    assert result.agent_balance_cents >= 0


async def test_live_read_balance_for_existing_player(backend):
    player = os.environ["VBLINK_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    result = await backend.read_balance(_ctx(account=account))
    assert result.balance_cents >= 0


async def test_live_recharge_one_dollar_then_redeem_one_dollar(backend):
    player = os.environ["VBLINK_TEST_PLAYER"]
    account = AccountIdentity(
        game_account_id=1, user_id=1, game_id=9998,
        username=player, external_user_id=None,
    )
    ctx = _ctx(account=account)
    before = await backend.read_balance(ctx)
    await backend.recharge(ctx, amount_cents=100, bonus_cents=0, total_credit_cents=100)
    await asyncio.sleep(7)
    after_recharge = await backend.read_balance(ctx)
    assert after_recharge.balance_cents == before.balance_cents + 100
    await backend.redeem(ctx, amount_cents=100)
    await asyncio.sleep(7)
    after_redeem = await backend.read_balance(ctx)
    assert after_redeem.balance_cents == before.balance_cents
```

- [ ] **Step 3: Run; both should skip**

Run: `.venv/bin/pytest tests/integration/test_ultrapanda_integration.py tests/integration/test_vblink_integration.py -v`
Expected: 7 skipped (4 + 3), 0 failed.

- [ ] **Step 4: Lint, type, full suite**

Run: `make lint && make type && make test`
Expected: all green; test count 369 → 369 passing + 7 newly-skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_ultrapanda_integration.py tests/integration/test_vblink_integration.py
git commit -m "test(phase6): live-gated integration scaffolding for ultrapanda + vblink

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Final task: Manual verification and merge

This is the user's gate — same as Phase 5.

- [ ] **Step 1: Verify the suite is fully green**

Run: `make lint && make type && make test`
Expected: 369 passing, 14 skipped (7 from Phase 5 + 7 from Phase 6).

- [ ] **Step 2: Push the branch**

Run: `git push -u origin feat/phase6-ultrapanda-vblink`

- [ ] **Step 3: Hand off to the user**

Tell the user:
> Phase 6 is implemented on `feat/phase6-ultrapanda-vblink`. Suite: 369 tests passing + 14 skipped. Live integration tests scaffolded but skipped (set `ULTRAPANDA_TEST_*` / `VBLINK_TEST_*` env vars to exercise the real portals). Both drivers added to NON_IDEMPOTENT_DRIVERS. Ready for manual verification against real agent accounts — let me know when to merge to `main`.

Do **not** merge without explicit go-ahead.

---

## Self-review checklist

- [ ] **Spec coverage:** Walk each section of `docs/superpowers/specs/2026-06-12-phase6-ultrapanda-vblink-design.md` and confirm a task covers it: crypto (Task 1), session (Task 2), errors (Task 3), passwords (Task 4), config (Task 5), client (Task 6), backend (Task 7), registry+NON_IDEMPOTENT_DRIVERS (Task 8), logging (Task 9), live tests (Task 10).
- [ ] **Driver-prefix propagation:** every raised error in `client.py` and `backend.py` uses `self._driver` / `self._client._driver` — not hardcoded `"ultrapanda:"`. Confirmed by reading.
- [ ] **No silent type drift:** `CachedSession.token` is a `str` throughout; `map_code(code, op=...)` signature consistent in client + backend usage; `_cents_to_score` returns a `str` consistent with the test assertions.
- [ ] **VBlink wiring works:** Task 8 verifies `resolve_backend("vblink", ...)._client._driver == "vblink"`.
- [ ] **No placeholders.**
