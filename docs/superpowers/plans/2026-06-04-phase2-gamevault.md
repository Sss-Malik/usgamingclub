# Phase 2 — GameVault Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the GameVault official HTTP API behind the existing `GameBackend` abstraction (all 6 operations), add driver-based backend selection via a new `games.backend_driver` column, and add a Redis at-most-once result cache so money operations survive worker re-runs.

**Architecture:** A `GameVaultClient` (per-request MD5 auth, multipart POST, `{code,msg,data}` envelope, transient-vs-terminal error classification) wraps the shared httpx client. `GameVaultBackend` implements the 6 ops with decimal-dollar↔cents conversion, `ceil`-whole-dollar sends, memorable passwords, and a `getUserID` fallback. The executor resolves the backend from `ctx.credentials.backend_driver` and wraps each operation in a Redis `ResultCache` (cache terminal outcomes; never cache transient failures, which arq safely re-runs thanks to GameVault `order_id` dedupe).

**Tech Stack:** httpx (async, multipart), hashlib MD5, redis.asyncio, Pydantic v2, SQLAlchemy 2.0 (read-only), pytest + respx.

**Spec:** `docs/superpowers/specs/2026-06-04-phase2-gamevault-design.md`
**GameVault doc text:** `/tmp/gamevault.txt` (extracted) — original PDF `~/Downloads/gamevault-api-doc.pdf`

**Environment:** branch `feat/phase2-gamevault` (already checked out); venv at `.venv` (use `.venv/bin/python -m pytest` etc.). On this machine `head` is an HTTP tool — never pipe to it.

---

## File structure (this phase)

```
Create:
  app/backends/gamevault/__init__.py
  app/backends/gamevault/errors.py      # GAMEVAULT_STATUS, TRANSIENT_CODES, map_code (Task 2)
  app/backends/gamevault/passwords.py   # generate_memorable_password (Task 3)
  app/backends/gamevault/client.py      # GameVaultClient (Task 4)
  app/backends/gamevault/backend.py     # GameVaultBackend (Task 5)
  app/operations/result_cache.py        # CachedOutcome, ResultCache, InMemory/Redis (Task 6)

Modify:
  app/db/models.py                      # Game.backend_driver (Task 1)
  app/backends/context.py               # GameCredentials.backend_driver; BackendContext.idempotency_key/account_username (Task 1)
  app/schemas/operations.py             # CreateAccountOp.account_username (Task 1)
  app/preflight/checks.py               # build_context: new params + gamevault creds check (Task 1)
  tests/conftest.py                     # seed a gamevault game + account (Task 1)
  app/backends/base.py                  # add TransientBackendError (Task 4)
  app/backends/registry.py              # resolve_backend(driver, ...) replaces get_backend (Task 7)
  app/operations/executor.py            # driver resolution + result cache (Task 8)
  app/worker/settings.py, app/worker/tasks.py  # redis + result_cache in ctx (Task 9)
  app/config.py                         # result_cache_ttl_seconds (Task 8)
  docs + CLAUDE.md                      # (Task 11)
```

---

## Task 1: Field plumbing (driver column, context fields, account_username, preflight)

**Files:**
- Modify: `app/db/models.py`, `app/backends/context.py`, `app/schemas/operations.py`, `app/preflight/checks.py`, `tests/conftest.py`
- Test: `tests/unit/test_preflight.py`, `tests/unit/test_schemas_operations.py`

- [ ] **Step 1: Extend the conftest seed with a GameVault game + account**

In `tests/conftest.py`, inside the `seeded` fixture's `async with session_factory() as s:` block, after the existing `GameAccount(...)` add:

```python
        s.add(
            Game(
                id=9,
                name="GameVault Demo",
                active=True,
                backend_driver="gamevault",
                api_base_url="https://gv.test",
                api_agent_id="11",
                api_secret_key="gvsecret",
            )
        )
        s.add(
            Game(id=10, name="GameVault NoCreds", active=True, backend_driver="gamevault"),
        )
        s.add(
            GameAccount(
                id=2001, user_id=43, game_id=9, username="user020301",
                password="x", external_user_id="88880212",
            )
        )
        s.add(
            GameAccount(
                id=2002, user_id=44, game_id=9, username="user_no_ext",
                password="x", external_user_id=None,
            )
        )
```

- [ ] **Step 2: Write failing tests**

Add to `tests/unit/test_schemas_operations.py`:

```python
def test_create_account_requires_account_username():
    op = operation_adapter.validate_python(
        {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 9,
         "game_account_id": None, "account_username": "saudmalik42"}
    )
    assert op.account_username == "saudmalik42"


def test_create_account_without_username_is_rejected():
    with pytest.raises(ValidationError):
        operation_adapter.validate_python(
            {"idempotency_key": "k", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 9, "game_account_id": None}
        )
```

Add to `tests/unit/test_preflight.py`:

```python
async def test_context_carries_backend_driver_and_idempotency_key(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="READ_BALANCE", idempotency_key="idem-1", user_id=43,
            game_id=9, game_account_id=2001,
        )
    assert ctx.credentials.backend_driver == "gamevault"
    assert ctx.idempotency_key == "idem-1"
    assert ctx.account.external_user_id == "88880212"


async def test_gamevault_game_missing_credentials_raises(seeded):
    async with seeded() as s:
        with pytest.raises(PreflightError) as ei:
            await build_context(
                s, type="AGENT_BALANCE", idempotency_key="k", user_id=None,
                game_id=10, game_account_id=None,
            )
    assert "missing_gamevault_credentials" in ei.value.reason


async def test_create_account_username_flows_into_context(seeded):
    async with seeded() as s:
        ctx = await build_context(
            s, type="CREATE_ACCOUNT", idempotency_key="k", user_id=43, game_id=9,
            game_account_id=None, account_username="usr_43",
        )
    assert ctx.account_username == "usr_43"
    assert ctx.account is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py tests/unit/test_schemas_operations.py -q`
Expected: FAIL (unexpected keyword `backend_driver`/`idempotency_key`, missing `account_username`).

- [ ] **Step 4: Add `backend_driver` to the `Game` model**

In `app/db/models.py`, in class `Game`, add after `binding_key`:

```python
    backend_driver: Mapped[str | None] = mapped_column(default=None)
```

- [ ] **Step 5: Extend the context dataclasses**

In `app/backends/context.py`, add `backend_driver` to `GameCredentials` (end, with default) and the two new fields to `BackendContext` (with defaults, to avoid breaking existing constructions):

```python
@dataclass(frozen=True)
class GameCredentials:
    game_id: int
    name: str
    backend_url: str | None
    login_page_url: str | None
    backend_username: str | None
    backend_password: str | None
    api_base_url: str | None
    api_agent_id: str | None
    api_secret_key: str | None
    binding_key: str | None
    backend_driver: str | None = None


@dataclass(frozen=True)
class AccountIdentity:
    game_account_id: int
    user_id: int
    game_id: int
    username: str
    external_user_id: str | None


@dataclass(frozen=True)
class BackendContext:
    credentials: GameCredentials
    user_id: int | None
    account: AccountIdentity | None
    idempotency_key: str = ""
    account_username: str | None = None
```

- [ ] **Step 6: Add `account_username` to `CreateAccountOp` (required)**

The contract (§4) sends `account_username` on every CREATE_ACCOUNT trigger, so it is required. In
`app/schemas/operations.py`, in class `CreateAccountOp`, add:

```python
    account_username: str = Field(min_length=1)
```

(`Field` is already imported in that module.)

- [ ] **Step 7: Update `build_context`**

Replace the body of `app/preflight/checks.py` `build_context` with this signature + logic (adds `idempotency_key`, `account_username`, the `backend_driver`, and the GameVault creds check):

```python
async def build_context(
    session: AsyncSession,
    *,
    type: str,
    game_id: int,
    game_account_id: int | None,
    user_id: int | None,
    idempotency_key: str = "",
    account_username: str | None = None,
) -> BackendContext:
    game = await GamesRepository(session).get(game_id)
    if game is None:
        raise PreflightError(f"game_not_found: {game_id}")

    credentials = GameCredentials(
        game_id=game.id,
        name=game.name,
        backend_url=game.backend_url,
        login_page_url=game.login_page_url,
        backend_username=game.backend_username,
        backend_password=game.backend_password,
        api_base_url=game.api_base_url,
        api_agent_id=game.api_agent_id,
        api_secret_key=game.api_secret_key,
        binding_key=game.binding_key,
        backend_driver=game.backend_driver,
    )

    if (game.backend_driver or "").lower() == "gamevault" and not (
        game.api_base_url and game.api_agent_id and game.api_secret_key
    ):
        raise PreflightError("missing_gamevault_credentials")

    account: AccountIdentity | None = None
    if type in ACCOUNT_SCOPED_TYPES:
        if game_account_id is None:
            raise PreflightError("missing_game_account_id")
        acct = await GameAccountsRepository(session).get(game_account_id)
        if acct is None:
            raise PreflightError(f"game_account_not_found: {game_account_id}")
        account = AccountIdentity(
            game_account_id=acct.id,
            user_id=acct.user_id,
            game_id=acct.game_id,
            username=acct.username,
            external_user_id=acct.external_user_id,
        )

    return BackendContext(
        credentials=credentials,
        user_id=user_id,
        account=account,
        idempotency_key=idempotency_key,
        account_username=account_username,
    )
```

- [ ] **Step 7b: Echo `account_username` in MockBackend and fix the Phase 1 CREATE_ACCOUNT tests**

The contract (§5) requires CREATE_ACCOUNT to echo the provided `account_username` as `result.username`
for ALL backends, and `account_username` is now required (Step 6) — which breaks the two Phase-1
CREATE_ACCOUNT tests that omit it. Update MockBackend and those tests.

In `app/backends/mock/backend.py`, change `create_account` to echo the username (keep a fallback for
direct unit calls that don't set it):

```python
    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        self._maybe_fail()
        username = ctx.account_username or f"mock_{ctx.user_id}_{ctx.credentials.game_id}"
        return CreateAccountResult(
            username=username,
            password="MockPass123!",
            external_user_id=f"EXT{ctx.user_id}{ctx.credentials.game_id}",
        )
```

In `tests/integration/test_executor.py`, update `test_create_account_includes_username_password` to
send and assert the echoed username:

```python
    await _run(
        {"idempotency_key": "k2", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7,
         "game_account_id": None, "account_username": "saudmalik42"},
        seeded,
    )
    body = route.calls.last.request.content.decode()
    assert '"username":"saudmalik42"' in body and '"password":"' in body
```

In `tests/integration/test_full_loop.py`, update `test_create_account_round_trip`'s body to include
`account_username` and change the final username assertion:

```python
    body = json.dumps(
        {"idempotency_key": "loop-1", "type": "CREATE_ACCOUNT", "user_id": 42, "game_id": 7,
         "game_account_id": None, "account_username": "saudmalik42"},
        separators=(",", ":"),
    )
```
```python
    assert sent["result"]["username"] == "saudmalik42"
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_preflight.py tests/unit/test_schemas_operations.py -q`
Expected: PASS. Then full suite: `.venv/bin/python -m pytest -q` → all green (MockBackend echo + updated CREATE_ACCOUNT tests).

- [ ] **Step 9: Commit**

```bash
git add app/db/models.py app/backends/context.py app/schemas/operations.py app/preflight/checks.py app/backends/mock/backend.py tests/conftest.py tests/unit/test_preflight.py tests/unit/test_schemas_operations.py tests/integration/test_executor.py tests/integration/test_full_loop.py
git commit -m "feat(phase2): plumb backend_driver, idempotency_key, required account_username

CreateAccountOp.account_username is now required (Laravel always sends it per the
updated contract); MockBackend echoes it. Updates the Phase 1 CREATE_ACCOUNT tests.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: GameVault status dictionary (`errors.py`)

**Files:**
- Create: `app/backends/gamevault/__init__.py` (empty), `app/backends/gamevault/errors.py`
- Test: `tests/unit/test_gamevault_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gamevault_errors.py
from app.backends.gamevault.errors import GAMEVAULT_STATUS, TRANSIENT_CODES, map_code


def test_known_code_maps_to_slug():
    assert map_code(6, "Insufficient agent balance") == "gamevault:6:insufficient_agent_balance"
    assert map_code(10, "x") == "gamevault:10:user_in_game"
    assert map_code(20, "x") == "gamevault:20:account_exists"


def test_unknown_code_falls_back_to_msg():
    assert map_code(999, "weird error") == "gamevault:999:weird error"


def test_transient_codes_are_recharge_withdraw_system():
    assert TRANSIENT_CODES == {12, 14, 21}


def test_dictionary_covers_documented_codes():
    for code in list(range(1, 24)) + [400]:
        assert code in GAMEVAULT_STATUS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_errors.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gamevault/errors.py

GAMEVAULT_STATUS: dict[int, str] = {
    1: "invalid_agent_id",
    2: "invalid_request_parameters",
    3: "invalid_token",
    4: "token_expired",
    5: "ip_not_whitelisted",
    6: "insufficient_agent_balance",
    7: "insufficient_user_balance",
    8: "invalid_user_id",
    9: "user_account_frozen",
    10: "user_in_game",
    11: "invalid_amount",
    12: "recharge_failed",
    13: "recharge_permission_denied",
    14: "withdrawal_failed",
    15: "withdrawal_exceeds_daily_limit",
    16: "withdrawal_under_review",
    17: "withdrawal_permission_denied",
    18: "account_name_format_error",
    19: "agent_no_register_permission",
    20: "account_exists",
    21: "system_failed",
    22: "register_ip_limit",
    23: "password_length",
    400: "parameter_error",
}

# Codes treated as transient/retryable (not cached; safe to re-run thanks to order_id dedupe).
TRANSIENT_CODES: frozenset[int] = frozenset({12, 14, 21})


def map_code(code: int, msg: str) -> str:
    slug = GAMEVAULT_STATUS.get(code)
    if slug is not None:
        return f"gamevault:{code}:{slug}"
    return f"gamevault:{code}:{(msg or 'error')[:80]}"
```

> The test asserts `TRANSIENT_CODES == {12, 14, 21}`; `frozenset({12,14,21}) == {12,14,21}` is `True` in Python, so the equality holds.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_errors.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gamevault/__init__.py app/backends/gamevault/errors.py tests/unit/test_gamevault_errors.py
git commit -m "feat(gamevault): status-code dictionary and reason mapping

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Memorable password generator (`passwords.py`)

**Files:**
- Create: `app/backends/gamevault/passwords.py`
- Test: `tests/unit/test_gamevault_passwords.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gamevault_passwords.py
import re

from app.backends.gamevault.passwords import generate_memorable_password


def test_password_format_word_plus_digits():
    pw = generate_memorable_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_password_length_within_gamevault_bounds():
    for _ in range(50):
        pw = generate_memorable_password()
        assert 6 <= len(pw) <= 32
        assert pw.isalnum()


def test_passwords_vary():
    assert len({generate_memorable_password() for _ in range(20)}) > 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_passwords.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gamevault/passwords.py
import secrets

# Curated, inoffensive nouns (capitalized). Kept short so word+4 digits stays well within 6-32.
_WORDS: tuple[str, ...] = (
    "Tiger", "Eagle", "Falcon", "River", "Mountain", "Comet", "Galaxy", "Harbor",
    "Maple", "Cedar", "Willow", "Garnet", "Copper", "Silver", "Marble", "Canyon",
    "Meadow", "Summit", "Lantern", "Compass", "Anchor", "Beacon", "Cobalt", "Crystal",
    "Dolphin", "Ember", "Glacier", "Horizon", "Jasmine", "Juniper", "Kestrel", "Lotus",
    "Mango", "Nebula", "Olive", "Panther", "Quartz", "Raven", "Saffron", "Topaz",
    "Violet", "Walnut", "Yarrow", "Zephyr", "Almond", "Birch", "Cactus", "Dune",
    "Fjord", "Grove", "Hazel", "Indigo", "Lemon", "Onyx", "Pebble", "Reef",
    "Sage", "Thistle", "Umber", "Vetch", "Wren", "Acorn", "Bramble", "Coral",
)


def generate_memorable_password() -> str:
    """A memorable password: capitalized word + 4-digit number (e.g. 'Tiger4827').

    Satisfies GameVault's 6-32 character rule and is alphanumeric only.
    """
    word = secrets.choice(_WORDS)
    number = secrets.randbelow(9000) + 1000  # 1000..9999
    return f"{word}{number}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_passwords.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gamevault/passwords.py tests/unit/test_gamevault_passwords.py
git commit -m "feat(gamevault): memorable password generator (word + digits)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: GameVault client + `TransientBackendError` (`client.py`, `base.py`)

**Files:**
- Modify: `app/backends/base.py` (add `TransientBackendError`)
- Create: `app/backends/gamevault/client.py`
- Test: `tests/unit/test_gamevault_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gamevault_client.py
import hashlib

import httpx
import pytest
import respx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gamevault.client import GameVaultClient

BASE = "https://gv.test"


def _client(http):
    return GameVaultClient(base_url=BASE, agent_id="11", secret_key="gvsecret", http_client=http)


@respx.mock
async def test_call_signs_and_returns_data_on_code_0(monkeypatch):
    monkeypatch.setattr("app.backends.gamevault.client.time.time", lambda: 1709867667.0)
    route = respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "Success", "data": {"user_balance": "60"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        data = await _client(http).call("/api/external/userBalance", {"user_id": "88880212"})
    assert data == {"user_balance": "60"}
    sent = route.calls.last.request
    body = sent.content.decode()
    expected_token = hashlib.md5(b"11:1709867667:gvsecret").hexdigest()  # noqa: S324 - GameVault protocol
    assert "multipart/form-data" in sent.headers["content-type"]
    assert 'name="agent_id"' in body and "11" in body
    assert 'name="timestamp"' in body and "1709867667" in body
    assert expected_token in body
    assert 'name="user_id"' in body and "88880212" in body


@respx.mock
async def test_business_code_raises_backend_error():
    respx.post(f"{BASE}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 10, "msg": "User is in game", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _client(http).call("/api/external/withdraw", {"user_id": "1"})
    assert ei.value.reason == "gamevault:10:user_in_game"
    assert not isinstance(ei.value, TransientBackendError)


@respx.mock
async def test_transient_business_code_raises_transient():
    respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 21, "msg": "System failed", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/recharge", {"user_id": "1"})


@respx.mock
async def test_http_5xx_and_timeout_are_transient():
    respx.post(f"{BASE}/api/external/agentBalance").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/agentBalance", {})

    respx.post(f"{BASE}/api/external/agentBalance").mock(side_effect=httpx.ConnectTimeout("boom"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TransientBackendError):
            await _client(http).call("/api/external/agentBalance", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_client.py -q`
Expected: FAIL — cannot import `TransientBackendError` / `GameVaultClient`.

- [ ] **Step 3: Add `TransientBackendError` to `app/backends/base.py`**

After the existing `BackendError` class, add:

```python
class TransientBackendError(BackendError):
    """A backend failure that is safe to retry (timeout, 5xx, transient business code).

    The executor does NOT cache these, so an arq re-run will retry the backend call.
    """
```

- [ ] **Step 4: Write `app/backends/gamevault/client.py`**

```python
# app/backends/gamevault/client.py
import hashlib
import time

import httpx

from app.backends.base import BackendError, TransientBackendError
from app.backends.gamevault.errors import TRANSIENT_CODES, map_code


class GameVaultClient:
    """Transport for the GameVault HTTP API: MD5 auth, multipart POST, envelope parsing."""

    def __init__(self, *, base_url: str, agent_id: str, secret_key: str, http_client: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_id = str(agent_id)
        self._secret_key = secret_key
        self._http = http_client

    def _auth_fields(self) -> dict[str, str]:
        ts = str(int(time.time()))
        token = hashlib.md5(  # noqa: S324 - MD5 is mandated by the GameVault auth scheme, not security
            f"{self._agent_id}:{ts}:{self._secret_key}".encode()
        ).hexdigest()
        return {"agent_id": self._agent_id, "timestamp": ts, "token": token}

    async def call(self, path: str, fields: dict[str, str]) -> dict:
        form = {**self._auth_fields(), **{k: str(v) for k, v in fields.items()}}
        # Force multipart/form-data with plain form fields (filename=None).
        multipart = {k: (None, v) for k, v in form.items()}
        url = f"{self._base_url}{path}"
        try:
            resp = await self._http.post(url, files=multipart)
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"gamevault_transport:{type(exc).__name__}") from exc

        if resp.status_code >= 500:
            raise TransientBackendError(f"gamevault_http_{resp.status_code}")
        if resp.status_code >= 300:
            raise BackendError(f"gamevault_http_{resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise TransientBackendError("gamevault_bad_response") from exc

        code = body.get("code")
        if code == 0:
            data = body.get("data")
            return data if isinstance(data, dict) else {}
        reason = map_code(code, body.get("msg", ""))
        if code in TRANSIENT_CODES:
            raise TransientBackendError(reason)
        raise BackendError(reason)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_client.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add app/backends/base.py app/backends/gamevault/client.py tests/unit/test_gamevault_client.py
git commit -m "feat(gamevault): HTTP client with MD5 auth, multipart, transient/terminal errors

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: GameVaultBackend (`backend.py`)

**Files:**
- Create: `app/backends/gamevault/backend.py`
- Test: `tests/unit/test_gamevault_backend.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_gamevault_backend.py
import httpx
import pytest
import respx

from app.backends.base import BackendError
from app.backends.context import AccountIdentity, BackendContext, GameCredentials
from app.backends.gamevault.backend import GameVaultBackend, _to_cents, _to_dollars
from app.backends.gamevault.client import GameVaultClient

BASE = "https://gv.test"


def _creds():
    return GameCredentials(
        game_id=9, name="GV", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url=BASE, api_agent_id="11", api_secret_key="gvsecret",
        binding_key=None, backend_driver="gamevault",
    )


def _ctx(*, account=True, external="88880212", username="user020301", idem="idem-1", account_username=None):
    acct = AccountIdentity(2001, 43, 9, username, external) if account else None
    return BackendContext(credentials=_creds(), user_id=43, account=acct,
                          idempotency_key=idem, account_username=account_username)


def _backend(http):
    return GameVaultBackend(GameVaultClient(base_url=BASE, agent_id="11", secret_key="gvsecret", http_client=http))


def test_unit_helpers():
    assert _to_cents("3649.0057") == 364901
    assert _to_cents("60") == 6000
    assert _to_dollars(5500) == "55"
    assert _to_dollars(5510) == "56"   # ceil
    assert _to_dollars(3050) == "31"   # ceil


@respx.mock
async def test_read_balance_converts_dollars_to_cents():
    respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "60"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx())
    assert r.balance_cents == 6000


@respx.mock
async def test_recharge_sends_ceil_dollars_and_order_id():
    route = respx.post(f"{BASE}/api/external/recharge").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "150000"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).recharge(_ctx(), amount_cents=5000, bonus_cents=500, total_credit_cents=5510)
    body = route.calls.last.request.content.decode()
    assert 'name="amount"' in body and "56" in body          # ceil(5510/100)
    assert 'name="order_id"' in body and "idem-1" in body
    assert r.balance_cents == 15000000


@respx.mock
async def test_redeem_user_in_game_raises_backend_error():
    respx.post(f"{BASE}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 10, "msg": "User is in game", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).redeem(_ctx(), amount_cents=3000)
    assert ei.value.reason == "gamevault:10:user_in_game"


@respx.mock
async def test_reset_password_returns_generated_password():
    route = respx.post(f"{BASE}/api/external/resetPassword").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": None, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).reset_password(_ctx())
    assert r.password and r.password.isalnum()
    assert 'name="login_pwd"' in route.calls.last.request.content.decode()


@respx.mock
async def test_create_account_requires_username():
    async with httpx.AsyncClient() as http:
        with pytest.raises(BackendError) as ei:
            await _backend(http).create_account(_ctx(account=False, account_username=None))
    assert ei.value.reason == "account_username_required"


@respx.mock
async def test_create_account_posts_username_and_returns_user_id():
    route = respx.post(f"{BASE}/api/external/addUser").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"account_name": "usr_43", "user_id": "88886468"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).create_account(_ctx(account=False, account_username="usr_43"))
    assert r.username == "usr_43" and r.external_user_id == "88886468" and r.password.isalnum()
    body = route.calls.last.request.content.decode()
    assert 'name="account"' in body and "usr_43" in body


@respx.mock
async def test_user_id_falls_back_to_getUserID_when_external_missing():
    respx.post(f"{BASE}/api/external/getUserID").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_id": "88880212"}, "count": 0})
    )
    bal = respx.post(f"{BASE}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "5"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).read_balance(_ctx(external=None, username="user_no_ext"))
    assert r.balance_cents == 500
    assert 'name="user_id"' in bal.calls.last.request.content.decode()  # resolved id used downstream


@respx.mock
async def test_agent_balance_converts():
    respx.post(f"{BASE}/api/external/agentBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"agent_balance": "3649.0057"}, "count": 0})
    )
    async with httpx.AsyncClient() as http:
        r = await _backend(http).agent_balance(_ctx(account=False))
    assert r.agent_balance_cents == 364901
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_backend.py -q`
Expected: FAIL — cannot import `GameVaultBackend`.

- [ ] **Step 3: Write the implementation**

```python
# app/backends/gamevault/backend.py
import math

from app.backends.base import BackendError
from app.backends.context import BackendContext
from app.backends.gamevault.client import GameVaultClient
from app.backends.gamevault.passwords import generate_memorable_password
from app.schemas.results import (
    AgentBalanceResult,
    CreateAccountResult,
    ReadBalanceResult,
    RechargeResult,
    RedeemResult,
    ResetPasswordResult,
)


def _to_cents(value: str | int | float) -> int:
    return round(float(value) * 100)


def _to_cents_opt(value: str | int | float | None) -> int | None:
    return None if value is None else _to_cents(value)


def _to_dollars(cents: int) -> str:
    return str(math.ceil(cents / 100))


class GameVaultBackend:
    def __init__(self, client: GameVaultClient) -> None:
        self._client = client

    async def _user_id(self, ctx: BackendContext) -> str:
        if ctx.account and ctx.account.external_user_id:
            return ctx.account.external_user_id
        if ctx.account and ctx.account.username:
            data = await self._client.call(
                "/api/external/getUserID", {"account_name": ctx.account.username}
            )
            return str(data["user_id"])
        raise BackendError("user_id_unresolved")

    async def create_account(self, ctx: BackendContext) -> CreateAccountResult:
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()
        data = await self._client.call(
            "/api/external/addUser", {"account": ctx.account_username, "login_pwd": pwd}
        )
        return CreateAccountResult(
            username=ctx.account_username, password=pwd, external_user_id=str(data["user_id"])
        )

    async def read_balance(self, ctx: BackendContext) -> ReadBalanceResult:
        uid = await self._user_id(ctx)
        data = await self._client.call("/api/external/userBalance", {"user_id": uid})
        return ReadBalanceResult(balance_cents=_to_cents(data["user_balance"]))

    async def reset_password(self, ctx: BackendContext) -> ResetPasswordResult:
        uid = await self._user_id(ctx)
        pwd = generate_memorable_password()
        await self._client.call("/api/external/resetPassword", {"user_id": uid, "login_pwd": pwd})
        return ResetPasswordResult(password=pwd)

    async def recharge(
        self, ctx: BackendContext, *, amount_cents: int, bonus_cents: int, total_credit_cents: int
    ) -> RechargeResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/recharge",
            {"user_id": uid, "amount": _to_dollars(total_credit_cents), "order_id": ctx.idempotency_key},
        )
        return RechargeResult(balance_cents=_to_cents_opt(data.get("user_balance")))

    async def redeem(self, ctx: BackendContext, *, amount_cents: int) -> RedeemResult:
        uid = await self._user_id(ctx)
        data = await self._client.call(
            "/api/external/withdraw",
            {"user_id": uid, "amount": _to_dollars(amount_cents), "order_id": ctx.idempotency_key},
        )
        return RedeemResult(balance_cents=_to_cents_opt(data.get("user_balance")))

    async def agent_balance(self, ctx: BackendContext) -> AgentBalanceResult:
        data = await self._client.call("/api/external/agentBalance", {})
        return AgentBalanceResult(agent_balance_cents=_to_cents(data["agent_balance"]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_gamevault_backend.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/gamevault/backend.py tests/unit/test_gamevault_backend.py
git commit -m "feat(gamevault): GameVaultBackend implementing the 6 operations

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Result cache (`result_cache.py`)

**Files:**
- Create: `app/operations/result_cache.py`
- Test: `tests/unit/test_result_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_result_cache.py
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, RedisResultCache


async def test_in_memory_get_set():
    cache = InMemoryResultCache()
    assert await cache.get("k") is None
    await cache.set("k", CachedOutcome("succeeded", {"balance_cents": 1}, None), 900)
    got = await cache.get("k")
    assert got.status == "succeeded" and got.result == {"balance_cents": 1}


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


async def test_redis_roundtrip_and_ttl_and_key():
    fake = FakeRedis()
    cache = RedisResultCache(fake)
    await cache.set("idem-9", CachedOutcome("failed", None, "backend_error: x"), 900)
    assert "opresult:idem-9" in fake.store
    assert fake.ttls["opresult:idem-9"] == 900
    got = await cache.get("idem-9")
    assert got.status == "failed" and got.reason == "backend_error: x" and got.result is None
    assert await cache.get("missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_result_cache.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```python
# app/operations/result_cache.py
import json
from dataclasses import dataclass
from typing import Protocol


@dataclass
class CachedOutcome:
    status: str               # "succeeded" | "failed"
    result: dict | None       # present when succeeded
    reason: str | None        # present when failed


class ResultCache(Protocol):
    async def get(self, key: str) -> CachedOutcome | None: ...
    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None: ...


class InMemoryResultCache:
    """Process-local cache for tests / single-process fallback."""

    def __init__(self) -> None:
        self._store: dict[str, CachedOutcome] = {}

    async def get(self, key: str) -> CachedOutcome | None:
        return self._store.get(key)

    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None:
        self._store[key] = outcome


class RedisResultCache:
    """Redis-backed at-most-once outcome cache keyed by idempotency_key."""

    def __init__(self, redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(key: str) -> str:
        return f"opresult:{key}"

    async def get(self, key: str) -> CachedOutcome | None:
        raw = await self._redis.get(self._key(key))
        if raw is None:
            return None
        d = json.loads(raw)
        return CachedOutcome(status=d["status"], result=d.get("result"), reason=d.get("reason"))

    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None:
        raw = json.dumps(
            {"status": outcome.status, "result": outcome.result, "reason": outcome.reason}
        )
        await self._redis.set(self._key(key), raw, ex=ttl_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_result_cache.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add app/operations/result_cache.py tests/unit/test_result_cache.py
git commit -m "feat(operations): Redis/in-memory result cache (at-most-once backend exec)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Driver-based registry (`registry.py`)

**Files:**
- Modify: `app/backends/registry.py` (replace `get_backend` with `resolve_backend`)
- Test: `tests/unit/test_registry.py` (rewrite)

- [ ] **Step 1: Rewrite the test**

Replace the entire contents of `tests/unit/test_registry.py`:

```python
# tests/unit/test_registry.py
import pytest

from app.backends.base import BackendError
from app.backends.context import GameCredentials
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.mock.backend import MockBackend
from app.backends.registry import resolve_backend
from app.config import Settings


def _creds(driver):
    return GameCredentials(
        game_id=9, name="g", backend_url=None, login_page_url=None,
        backend_username=None, backend_password=None,
        api_base_url="https://gv.test", api_agent_id="11", api_secret_key="s",
        binding_key=None, backend_driver=driver,
    )


def _settings():
    return Settings(python_signing_secret="s")


def test_none_or_mock_returns_mock_backend():
    s = _settings()
    assert isinstance(resolve_backend(None, credentials=_creds(None), http_client=None, settings=s), MockBackend)
    assert isinstance(resolve_backend("mock", credentials=_creds("mock"), http_client=None, settings=s), MockBackend)


def test_gamevault_driver_returns_gamevault_backend():
    s = _settings()
    backend = resolve_backend("gamevault", credentials=_creds("gamevault"), http_client=object(), settings=s)
    assert isinstance(backend, GameVaultBackend)


def test_unknown_driver_raises():
    s = _settings()
    with pytest.raises(BackendError):
        resolve_backend("nope", credentials=_creds("nope"), http_client=None, settings=s)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: FAIL — cannot import `resolve_backend`.

- [ ] **Step 3: Write the implementation**

Replace the entire contents of `app/backends/registry.py`:

```python
# app/backends/registry.py
from app.backends.base import BackendError, GameBackend
from app.backends.context import GameCredentials
from app.backends.gamevault.backend import GameVaultBackend
from app.backends.gamevault.client import GameVaultClient
from app.backends.mock.backend import MockBackend
from app.config import Settings


def resolve_backend(
    driver: str | None, *, credentials: GameCredentials, http_client, settings: Settings
) -> GameBackend:
    """Resolve the backend for an operation from its game's backend_driver.

    `null`/`mock` -> MockBackend; `gamevault` -> GameVaultBackend. Unknown -> BackendError.
    """
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
    if key == "gamevault":
        return GameVaultBackend(
            GameVaultClient(
                base_url=credentials.api_base_url or "",
                agent_id=credentials.api_agent_id or "",
                secret_key=credentials.api_secret_key or "",
                http_client=http_client,
            )
        )
    raise BackendError(f"unknown_backend_driver:{driver}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/test_registry.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/backends/registry.py tests/unit/test_registry.py
git commit -m "feat(backends): driver-based resolve_backend (mock | gamevault)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Executor — driver resolution + result cache (`executor.py`, `config.py`)

**Files:**
- Modify: `app/config.py` (add `result_cache_ttl_seconds`), `app/operations/executor.py` (rewrite)
- Test: `tests/integration/test_executor_cache.py` (new); existing `tests/integration/test_executor.py` and `test_full_loop.py` keep passing unchanged.

- [ ] **Step 1: Add the TTL setting**

In `app/config.py`, in `Settings`, add near the webhook knobs:

```python
    result_cache_ttl_seconds: int = 900
```

- [ ] **Step 2: Write the failing cache-behavior test**

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/integration/test_executor_cache.py -q`
Expected: FAIL — `execute_operation` has no `result_cache` kwarg.

- [ ] **Step 4: Rewrite `app/operations/executor.py`**

```python
# app/operations/executor.py
import httpx
from pydantic import BaseModel, ValidationError

from app.backends.base import BackendError, GameBackend, TransientBackendError
from app.backends.context import BackendContext
from app.backends.registry import resolve_backend as _resolve_backend
from app.config import Settings
from app.logging import get_logger
from app.operations.dispatch import dispatch
from app.operations.result_cache import CachedOutcome, InMemoryResultCache, ResultCache
from app.postflight.effects import apply_post_effects
from app.preflight.checks import PreflightError, build_context
from app.schemas.operations import operation_adapter
from app.webhook.client import deliver_webhook

logger = get_logger(__name__)


async def execute_operation(
    payload: dict,
    *,
    session_factory,
    http_client: httpx.AsyncClient,
    settings: Settings,
    result_cache: ResultCache | None = None,
    resolve=_resolve_backend,
) -> None:
    if result_cache is None:
        result_cache = InMemoryResultCache()
    key = str(payload.get("idempotency_key", ""))
    log = logger.bind(idempotency_key=key, phase="received")

    # 1. Validate (invalid payloads are reported, never cached).
    try:
        op = operation_adapter.validate_python(payload)
    except ValidationError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"invalid_payload: {_summarize(exc)}"))
        return

    log = log.bind(type=op.type, game_id=op.game_id)

    # 2. Replay short-circuit.
    cached = await result_cache.get(key)
    if cached is not None:
        log.bind(phase="cache_hit").info("operation_replay_from_cache", status=cached.status)
        await _deliver(http_client, settings, key, cached)
        return

    # 3. Pre-flight (not cached on failure).
    try:
        async with session_factory() as session:
            ctx: BackendContext = await build_context(
                session,
                type=op.type,
                game_id=op.game_id,
                game_account_id=getattr(op, "game_account_id", None),
                user_id=getattr(op, "user_id", None),
                idempotency_key=key,
                account_username=getattr(op, "account_username", None),
            )
    except PreflightError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"preflight_failed: {exc.reason}"))
        return

    # 4. Resolve backend (config error -> failure, not cached).
    try:
        backend: GameBackend = resolve(
            ctx.credentials.backend_driver, credentials=ctx.credentials, http_client=http_client, settings=settings
        )
    except BackendError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, exc.reason))
        return

    # 5. Backend call.
    log = log.bind(phase="backend_call")
    try:
        result: BaseModel = await dispatch(backend, op, ctx)
    except TransientBackendError as exc:
        log.warning("operation_backend_transient", reason=exc.reason)
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"backend_error: {exc.reason}"))
        return  # not cached -> arq re-run retries (order_id dedupe keeps money ops safe)
    except BackendError as exc:
        outcome = CachedOutcome("failed", None, f"backend_error: {exc.reason}")
        await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
        log.warning("operation_backend_failed", reason=exc.reason)
        await _deliver(http_client, settings, key, outcome)
        return
    except ValidationError as exc:
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, f"invalid_result_payload: {_summarize(exc)}"))
        return
    except Exception:  # noqa: BLE001 - any unexpected error is reported, not cached
        log.exception("operation_unexpected_error")
        await _deliver(http_client, settings, key, CachedOutcome("failed", None, "backend_error: unexpected"))
        return

    outcome = CachedOutcome("succeeded", result.model_dump(exclude_none=True), None)
    await result_cache.set(key, outcome, settings.result_cache_ttl_seconds)
    log.bind(phase="backend_result").info("operation_succeeded", result_keys=sorted((outcome.result or {}).keys()))
    await _deliver(http_client, settings, key, outcome)
    await apply_post_effects(key, op.type, outcome.result or {})


async def _deliver(client, settings: Settings, key: str, outcome: CachedOutcome) -> None:
    if outcome.status == "succeeded":
        body = {"idempotency_key": key, "status": "succeeded", "result": outcome.result or {}}
    else:
        body = {"idempotency_key": key, "status": "failed", "reason": (outcome.reason or "failed")[:255]}
    await deliver_webhook(
        client,
        settings.webhook_url,
        settings.python_signing_secret,
        body,
        max_budget_seconds=settings.webhook_max_budget_seconds,
        backoff_base=settings.webhook_backoff_base,
        backoff_max=settings.webhook_backoff_max,
    )


def _summarize(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "validation error"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    return f"{loc}: {first.get('msg', 'invalid')}"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/integration/test_executor_cache.py tests/integration/test_executor.py tests/integration/test_full_loop.py -q`
Expected: PASS (new cache tests + the existing executor/full-loop tests, which still work via the `result_cache=None` default and `resolve` default).

- [ ] **Step 6: Commit**

```bash
git add app/config.py app/operations/executor.py tests/integration/test_executor_cache.py
git commit -m "feat(operations): executor resolves by driver and caches terminal outcomes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Worker wiring (`worker/settings.py`, `worker/tasks.py`)

**Files:**
- Modify: `app/worker/settings.py`, `app/worker/tasks.py`
- Test: `tests/unit/test_worker_tasks.py` (update)

- [ ] **Step 1: Update the worker task test**

Replace `tests/unit/test_worker_tasks.py` contents:

```python
# tests/unit/test_worker_tasks.py
import app.worker.tasks as tasks


async def test_task_delegates_to_executor(monkeypatch, seeded):
    captured = {}

    async def fake_execute(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs

    monkeypatch.setattr(tasks, "execute_operation", fake_execute)

    class FakeClient: ...
    class FakeCache: ...

    ctx = {"http_client": FakeClient(), "session_factory": seeded, "result_cache": FakeCache()}
    payload = {"idempotency_key": "k", "type": "READ_BALANCE", "user_id": 42, "game_id": 7, "game_account_id": 1001}
    await tasks.execute_operation_task(ctx, payload)

    assert captured["payload"] == payload
    assert captured["kwargs"]["http_client"] is ctx["http_client"]
    assert captured["kwargs"]["session_factory"] is seeded
    assert captured["kwargs"]["result_cache"] is ctx["result_cache"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: FAIL — task does not pass `result_cache`.

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
    )
```

- [ ] **Step 4: Update `app/worker/settings.py`**

```python
# app/worker/settings.py
import httpx
import redis.asyncio as redis_asyncio
from arq.connections import RedisSettings

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


async def shutdown(ctx: dict) -> None:
    await ctx["http_client"].aclose()
    await ctx["redis_cache"].aclose()


class WorkerSettings:
    functions = [execute_operation_task]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Job timeout must exceed the webhook retry budget so a still-retrying job is not killed.
    job_timeout = int(get_settings().webhook_max_budget_seconds) + 60
    # Backstop for worker crashes. The result cache makes re-runs safe (cached terminal outcomes are
    # replayed without re-calling the backend); transient failures are re-tried and GameVault dedupes
    # by order_id, so money ops cannot double-apply.
    max_tries = 3
    keep_result = 0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/test_worker_tasks.py -q`
Expected: PASS. Then `.venv/bin/python -c "import app.worker.settings"` → no error (no live Redis needed at import).

- [ ] **Step 6: Commit**

```bash
git add app/worker/settings.py app/worker/tasks.py tests/unit/test_worker_tasks.py
git commit -m "feat(worker): inject Redis result cache into the executor

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 10: GameVault integration test + suite green

**Files:**
- Create: `tests/integration/test_gamevault_integration.py`

- [ ] **Step 1: Write the integration test (driver routing + cache replay over respx)**

```python
# tests/integration/test_gamevault_integration.py
import json

import httpx
import respx

from app.config import Settings
from app.operations.executor import execute_operation
from app.operations.result_cache import InMemoryResultCache

WEBHOOK = "https://laravel.test/webhooks/games/operation"
GV = "https://gv.test"


def _settings():
    return Settings(python_signing_secret="s", app_url="https://laravel.test", webhook_max_budget_seconds=600)


@respx.mock
async def test_gamevault_read_balance_routes_and_reports(seeded):
    # game 9 has backend_driver='gamevault'; account 2001 has external_user_id 88880212
    respx.post(f"{GV}/api/external/userBalance").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"user_balance": "60"}, "count": 0})
    )
    hook = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    payload = {"idempotency_key": "gv-1", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=InMemoryResultCache())
    sent = json.loads(hook.calls.last.request.content.decode())
    assert sent["status"] == "succeeded" and sent["result"]["balance_cents"] == 6000


@respx.mock
async def test_terminal_failure_cached_and_not_recalled(seeded):
    # First call: GameVault returns business failure (code 7). Should be cached.
    gv = respx.post(f"{GV}/api/external/withdraw").mock(
        return_value=httpx.Response(200, json={"code": 7, "msg": "Insufficient user balance", "data": None, "count": 0})
    )
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gv-2", "type": "REDEEM", "user_id": 43, "game_id": 9, "game_account_id": 2001, "amount_cents": 3000}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
        assert gv.call_count == 1
        # Second run (simulated arq re-run): cache hit -> GameVault NOT called again.
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    assert gv.call_count == 1  # still 1 -> backend not re-called
    cached = await cache.get("gv-2")
    assert cached.status == "failed" and "insufficient_user_balance" in cached.reason


@respx.mock
async def test_transient_failure_not_cached_recalls_backend(seeded):
    gv = respx.post(f"{GV}/api/external/userBalance").mock(return_value=httpx.Response(503))
    respx.post(WEBHOOK).mock(return_value=httpx.Response(200, json={"ok": True}))
    cache = InMemoryResultCache()
    payload = {"idempotency_key": "gv-3", "type": "READ_BALANCE", "user_id": 43, "game_id": 9, "game_account_id": 2001}
    async with httpx.AsyncClient() as client:
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
        await execute_operation(payload, session_factory=seeded, http_client=client, settings=_settings(), result_cache=cache)
    assert gv.call_count == 2  # transient -> not cached -> backend called both runs
    assert await cache.get("gv-3") is None
```

- [ ] **Step 2: Run the integration test**

Run: `.venv/bin/python -m pytest tests/integration/test_gamevault_integration.py -q`
Expected: PASS (3 tests).

- [ ] **Step 3: Full suite + gates**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: all tests pass; ruff clean; mypy clean. Fix any issues inline and re-run until green.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_gamevault_integration.py
git commit -m "test(gamevault): driver routing, terminal-cache replay, transient re-call

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md`, `docs/architecture.md`, `docs/runbook.md`

- [ ] **Step 1: Update `CLAUDE.md`**

Under "Where things live", add a line:

```markdown
- GameVault backend: `app/backends/gamevault/` (client, backend, errors, passwords). Result cache: `app/operations/result_cache.py`.
```

Under "Golden rules", add:

```markdown
- Backend selection comes from `games.backend_driver` (read-only): `mock` | `gamevault`. New backends add a
  module + a `resolve_backend` branch.
- GameVault: amounts are sent as whole dollars via `ceil(cents/100)`; balances read as decimal dollars `*100`.
  Pass `idempotency_key` as `order_id` (GameVault dedupes). Generated passwords are memorable (word+digits).
- Cache terminal outcomes (success + business failures) in the result cache; never cache transient errors
  (timeout/5xx/codes 12,14,21) so re-runs retry safely.
```

- [ ] **Step 2: Update `docs/architecture.md`**

Append a section:

```markdown
## Backends & drivers
`games.backend_driver` selects the backend per game (`mock` | `gamevault`). `resolve_backend` builds the
backend from the game's credentials + the shared httpx client. GameVault (`app/backends/gamevault/`) is a
synchronous official API: MD5 token auth (`md5(agent_id:timestamp:secret_key)`), multipart POST, a
`{code,msg,data}` envelope, and a status-code dictionary. Money units: send whole dollars (`ceil(cents/100)`),
read decimal dollars (`*100` → cents).

## Result cache (money-op safety)
`app/operations/result_cache.py` stores each operation's terminal outcome (success or business failure) in
Redis keyed by `idempotency_key` (TTL `result_cache_ttl_seconds`). The executor replays a cached outcome
without re-calling the backend. Transient failures are NOT cached, so an arq re-run retries the backend;
GameVault's `order_id` dedupe prevents double money movement.
```

- [ ] **Step 3: Update `docs/runbook.md`**

Append:

```markdown
## GameVault
- Set `games.backend_driver='gamevault'` and the `api_base_url` / `api_agent_id` / `api_secret_key` columns.
- The VPS egress IP must be on GameVault's allowlist (else every call fails `gamevault:5:ip_not_whitelisted`).
- CREATE_ACCOUNT receives `account_username` from Laravel (e.g. `saudmalik42`); Python creates the
  GameVault account with exactly that name and echoes it as `result.username`.
- Common reasons: `gamevault:7:insufficient_user_balance`, `gamevault:10:user_in_game`,
  `gamevault:20:account_exists`. Transient (`12`/`14`/`21`, 5xx, timeout) are retried automatically.
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/architecture.md docs/runbook.md
git commit -m "docs(phase2): document GameVault backend, drivers, and the result cache

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase-2 acceptance (manual, after the suite is green)

Against the real Laravel + real GameVault (sandbox if available):
1. Laravel has shipped `games.backend_driver` + `account_username` (per the 2026-06-04 contract). Set your GameVault game's `backend_driver='gamevault'` and its `api_base_url`/`api_agent_id`/`api_secret_key`; ensure the VPS IP is allowlisted with GameVault.
2. `make up`; `make ping` → 200.
3. From Laravel, trigger **READ_BALANCE** / **AGENT_BALANCE** on the GameVault game → op SUCCEEDED with a real balance. **Verify the cents value matches GameVault's dashboard** (validates the decimal-dollar `*100` rule against a real response).
4. **RECHARGE** a small amount → op SUCCEEDED; confirm the in-game balance increased by `ceil` whole dollars; re-trigger the same op (same idempotency_key) → no double credit (cache replay + GameVault `order_id`).
5. **REDEEM** → SUCCEEDED (or `user_in_game` failure if applicable).
6. **RESET_PASSWORD** → SUCCEEDED, memorable password stored by Laravel.
7. **CREATE_ACCOUNT** (once Laravel sends `account_username`) → SUCCEEDED with the username + memorable password + external_user_id.

---

## Self-review (completed by plan author)

**Spec coverage:** client/auth/envelope (§6.1) → Task 4; status dictionary (§7) → Task 2; passwords (§6.3)
→ Task 3; the 6 ops + unit conversion + getUserID (§6.4, G1) → Task 5; driver selection (§6.5, G6) →
Tasks 1+7; result cache (§6.6, G7) → Tasks 6+8; CreateAccount build-ahead + account_username (G4) →
Tasks 1+5; code-20 error-as-is (G5) → covered by Task 5 (client raises `gamevault:20:account_exists`,
executor caches+reports); redeem code-10 (G3) → Task 5 test; order_id dedupe (G2) → Task 5 (order_id sent);
worker wiring → Task 9; integration + gates → Task 10; docs + Laravel deps (§10) → Task 11 + runbook.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. `# noqa: S324` annotations on MD5 are
intentional (GameVault-mandated, not a security hash) — harmless even if the S-rules aren't enabled.

**Type consistency:** `BackendContext(credentials, user_id, account, idempotency_key, account_username)`,
`GameCredentials(..., backend_driver)`, `resolve_backend(driver, *, credentials, http_client, settings)`,
`GameVaultClient(base_url, agent_id, secret_key, http_client).call(path, fields)`, `CachedOutcome(status,
result, reason)`, `ResultCache.get/set`, `execute_operation(..., result_cache=None, resolve=...)`,
`_to_cents/_to_cents_opt/_to_dollars` are used consistently across tasks.
```
