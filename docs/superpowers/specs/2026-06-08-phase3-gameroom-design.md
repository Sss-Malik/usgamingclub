# Phase 3 — Gameroom Backend Integration — Design Spec

- **Status:** Approved (design) — pending spec review before plan
- **Date:** 2026-06-08
- **Owner:** saud
- **Depends on:** Phase 2 (GameVault) merged to `main`
- **Gameroom findings doc:** `/Applications/development/gameroom-standalone/gameroom_api_findings.md`
  (reverse-engineered by driving the admin UI in Playwright, then verified via direct `fetch` calls)
- **Wire contract:** `/Applications/development/laravel/casino-app/docs/integrations/python-game-service-api-contract.md`

---

## 1. Purpose

Integrate **Gameroom** (`https://agentserver1.gameroom777.com`), our **first reverse-engineered
backend**, behind the existing `GameBackend` abstraction. Unlike GameVault's per-request MD5 token,
Gameroom uses **JWT bearer auth with a ~6-hour session** that must be re-logged-in before expiry and
on `status_code:410`. Adds two cross-cutting capabilities the project needs for any session-based
backend:

1. A **Redis-backed `SessionStore`** so all workers share one session per `game_id` (the natural pattern
   for any session-holding backend; reused by Phase 4+ captcha-requiring backends).
2. **Per-driver retry policy:** for non-idempotent backends (no `order_id` dedupe), the API endpoint
   passes arq `_max_tries=1` so a worker crash mid-operation cannot double-apply funds.

## 2. Goals & non-goals

**Goals**
- A `GameroomBackend` implementing all six operations against the documented endpoints.
- A `GameroomClient`: form-urlencoded POST, JWT bearer auth, **transparent re-login on `status_code:410`
  with double-checked-locking** (single-session safe — see §6.3), transient-vs-terminal classification.
- A `SessionStore` (Redis + in-memory variants) keyed by `game_id`, with **distributed login lock**.
- **Per-driver `_max_tries=1`** for `gameroom` (and any future non-idempotent driver). Worker crashes
  during a money op → no arq retry → Laravel's 10-min reaper marks failed + refunds.
- `userList` fallback to resolve Gameroom `player.id` when `game_accounts.external_user_id` is null.
- **Memorable-complex password** generator for `RESET_PASSWORD` (upper + lower + digit + symbol, 6–12 chars).
- Tests (unit + integration) and docs/CLAUDE.md updates.

**Non-goals (deferred)**
- AntiCaptcha integration. Gameroom's captcha is client-side only; the server ignores the field. The
  next reverse-engineered backend that actually validates a captcha triggers that phase.
- `playerOffline`, `userList` paging/search, and `agent/logout` (not part of our six contract ops).
- A backend-result idempotency cache *on top of* an `order_id` dedupe — Gameroom has no `order_id`,
  so the existing terminal-outcome cache + `_max_tries=1` is the protection.
- Token-pool rotation across multiple agent credentials (one credential per game).

## 3. Gameroom API summary (from the findings doc)

- **Base URL:** per-game (currently `https://agentserver1.gameroom777.com`); slot into existing
  `games.backend_url`. Per-game agent credentials slot into `games.backend_username` and
  `games.backend_password`. **No new DB columns.**
- **Auth:** `POST /api/login` with form-urlencoded `username` + `password` (captcha **omitted** — server
  ignores it). Response has `token` (JWT, HS256, `exp - iat ≈ 21600s ≈ 6 h`) plus `expires_time`.
- **Transport:** every authenticated request sends `Authorization: Bearer <token>` and
  `Content-Type: application/x-www-form-urlencoded; charset=UTF-8`. We also send
  `Accept: application/json` and `X-Requested-With: XMLHttpRequest` for consistent JSON responses.
- **Envelope:** `{ "status_code": <int>, "message": <str>, "data"?: <obj>, "code"?: <int> }`. **Branch on
  `status_code` only** — `code` is inconsistent (0 on both successes and several 400 errors).
- **Critical session-related codes:**
  - `200` — success.
  - `400` — validation/business error; reason in `message`.
  - `401` — `Token not provided` (we should never see this; bug if we do).
  - `410` — token bad or expired (HTTP itself is still 200). **Soft signal to re-login + retry once.**
  - `430` — `Username or password error` on login (terminal — credentials wrong).
  - `500` — server exception (transient).
- **Single-session enforcement (operator knowledge, not in the findings doc):** the agent backend
  allows only **one active session at a time per agent credential**. Issuing a new login invalidates
  the previously-issued token. See §6.3 for how we handle this safely.
- **No `order_id` / idempotency.** The findings explicitly call this out: "Recharge and withdraw are
  not idempotent. No client request ID." This is the gating constraint for §6.5.

### Endpoint → operation mapping (all 6 ops)

| Our op | Gameroom call | Notes |
|---|---|---|
| `CREATE_ACCOUNT` | `POST /api/player/playerInsert` | returns `data.id` → `external_user_id`; we generate the password |
| `READ_BALANCE` | `GET /api/player/agentMoney?id=…` | reads `data.balance` (number, dollars) |
| `RESET_PASSWORD` | `POST /api/player/reset` | **complex** password rule (upper+lower+symbol, 6–12) |
| `RECHARGE` | `POST /api/player/agentRecharge` | integer-dollar `balance`; not idempotent |
| `REDEEM` | `POST /api/player/agentWithdraw` | integer-dollar `balance`; success response has **no `data`** |
| `AGENT_BALANCE` | `POST /api/agent/getMoney` | POST-only; response shape not documented — read `data.money` then top-level `money` |

## 4. Decisions (from brainstorming)

| # | Decision | Resolution |
|---|---|---|
| R1 | Money units | Send `balance` as integer **whole dollars** via `ceil(cents/100)`. Read balances as decimal dollars → `round(float(x) * 100)` cents. Same conventions as Phase 2. |
| R2 | Money safety with no `order_id` | **Per-driver `_max_tries=1`** for non-idempotent drivers (gameroom). API endpoint does a quick `SELECT backend_driver` and passes `_max_tries=1` if driver is in `NON_IDEMPOTENT_DRIVERS`. Other drivers (gamevault/juwa/juwa2) keep `max_tries=3`. |
| R3 | Snapshot fields (`available_balance`, `customer_balance`) | Pass empty strings (`""`). The findings verify the server ignores these values and uses its own ledger. Avoids an extra round-trip per money op. |
| R4 | `remark` on recharge/withdraw | Always empty (`""`). Server requires the field but allows empty. UUIDs contain hyphens which fail Gameroom's `[A-Za-z0-9]` rule. |
| R5 | `REDEEM` result `balance_cents` | Omit (the response has no `data` block; the contract makes `balance_cents` optional on REDEEM). Avoids a wasted `agentMoney` round-trip. |
| R6 | Player ID resolution | Prefer `ctx.account.external_user_id`. If null, `GET userList?account=<username>` and pick the row where `Account == username` (substring filter — must exact-match in code). If not found → `gameroom:player_not_found`. |
| R7 | Single-session safety | **Double-checked-locking refresh** in `GameroomClient.get_token(invalidate=…)`: on `410`, re-read the cache under the login lock and only call `/api/login` if the cache still holds the dead token. Cures the "two workers race-relogin → token thrash" failure mode. See §6.3. |
| R8 | Login concurrency | Distributed login lock via Redis `SET NX gameroom_login:{game_id} ex=10`. Other workers wait by polling the token cache. |
| R9 | Captcha | Omit from `/api/login` requests (server ignores). No AntiCaptcha integration in this phase. |

**Accepted limitations** (locked, see §10):
- `ceil(cents/100)` rounding (player-favorable on RECHARGE, agent-favorable on REDEEM). Gameroom only
  accepts integer dollars.
- A worker crash between a successful backend call and our `cache.set` leaves an "orphaned" money move
  with no retry (because `_max_tries=1`). Laravel's reaper at 10 min marks the op failed + refunds the
  wallet, **but the in-game balance change is real**. Operator reconciles manually via Gameroom's
  dashboard. This is the price of no backend-side `order_id`; alternatives were rejected per R2.

## 5. Module layout

```
app/backends/gameroom/
  __init__.py
  errors.py     - GAMEROOM_STATUS dict + message-pattern matchers + map_response(status_code, message) -> reason
  passwords.py  - generate_memorable_complex_password() (upper+lower+digit+symbol, 6-12)
  session.py    - CachedSession dataclass + SessionStore protocol + InMemorySessionStore + RedisSessionStore
                  + LoginLock (Redis SET NX) helper
  client.py     - GameroomClient: get_token (double-checked), call (re-login-on-410-retry-once),
                  classification (transient vs terminal)
  backend.py    - GameroomBackend: 6 ops + _player_id() fallback + unit conversion + password gen

# Changed:
app/backends/registry.py    - NON_IDEMPOTENT_DRIVERS = {"gameroom"}; resolve_backend branch for 'gameroom'
app/preflight/checks.py     - missing_gameroom_credentials guard (mirrors gamevault)
app/api/operations.py       - per-driver _max_tries via SELECT backend_driver before enqueue
app/main.py                 - lifespan: expose session_factory on app.state for the endpoint lookup
app/worker/settings.py      - construct + inject SessionStore into ctx["session_store"]
app/operations/executor.py  - pass http_client + session_store down to the registry (for the gameroom client)
app/backends/registry.py    - resolve_backend now also takes session_store kwarg
tests/conftest.py           - seed a gameroom game (id=11) + accounts (with/without external_user_id)
```

## 6. Component designs

### 6.1 `errors.py`

```python
# Patterns are documented in the findings doc §4.5-4.8.
_MESSAGE_PATTERNS: list[tuple[str, str]] = [
    ("Username already exists", "account_exists"),
    ("Recharge balance is greater", "insufficient_agent_balance"),
    ("Withdrawal amount is greater", "insufficient_user_balance"),
    ("Amount must be greater than 0", "invalid_amount"),
    ("balance must be an integer", "invalid_amount"),
    ("password confirmation does not match", "password_mismatch"),
    ("Operation failed", "operation_failed"),  # opaque catch-all — usually missing player
]

def map_response(status_code: int, message: str) -> tuple[str, bool]:
    """Return (reason_slug, is_terminal). The executor caches terminal failures only."""
    if status_code == 500:
        return ("gameroom:server_error", False)            # transient
    if status_code == 430:
        return ("gameroom:auth_failed", True)              # terminal (creds wrong)
    if status_code == 401:
        return ("gameroom:auth_missing", False)            # transient — our bug; let max_tries handle
    if status_code == 400:
        for needle, slug in _MESSAGE_PATTERNS:
            if needle.lower() in (message or "").lower():
                return (f"gameroom:{slug}", True)
        return (f"gameroom:business_error: {(message or '')[:80]}", True)
    return (f"gameroom:status_{status_code}: {(message or '')[:60]}", True)
```

The `410` code does **not** appear here — `GameroomClient.call()` handles it internally (re-login +
retry once) before any error surfaces. If 410 survives the retry, the client itself raises
`BackendError("gameroom:auth_failed")` (terminal).

### 6.2 `passwords.py`

Add to the existing GameVault `passwords` style. Reuse the `_WORDS` list (capitalised, 4–7 chars). For
Gameroom **reset** the rule is upper + lower + special, 6–12 chars, no spaces.

```python
import secrets

_SYMBOLS = "!@#$%&*"           # safe symbols (no quote/paren/space)

def generate_memorable_complex_password() -> str:
    """{Word}{symbol}{2 digits}, e.g. 'Tiger@47'. Always upper+lower+symbol+digit, 7-10 chars.

    Satisfies the Gameroom RESET rule: upper+lower+special, 6-12 chars, no spaces.
    """
    word = secrets.choice(_SHORT_WORDS)         # filter to 4-7 chars
    symbol = secrets.choice(_SYMBOLS)
    number = secrets.randbelow(90) + 10         # 10..99
    return f"{word}{symbol}{number}"
```

(CREATE_ACCOUNT uses the existing alphanumeric generator from `gamevault/passwords.py` —
**re-exported** through `gameroom/passwords.py` for clarity, no duplication.)

### 6.3 `session.py` (the heart of this phase)

```python
@dataclass(frozen=True)
class CachedSession:
    token: str
    expires_at: int           # unix seconds

class SessionStore(Protocol):
    async def get(self, game_id: int) -> CachedSession | None: ...
    async def set(self, game_id: int, session: CachedSession, ttl_seconds: int) -> None: ...
    async def clear(self, game_id: int) -> None: ...
    # Distributed login lock for single-session safety. Returns an async context manager.
    def login_lock(self, game_id: int, *, ttl_seconds: int = 10): ...
```

- `RedisSessionStore`: key `gameroom_session:{game_id}` → JSON `{token, expires_at}`. TTL set to
  `expires_at - now - 60s` buffer. Login lock = `SET NX gameroom_login:{game_id} ex=10`; release on
  context exit (no-op if expired). On lock contention, raise/poll is the caller's concern (see client).
- `InMemorySessionStore` for tests: in-process dict + `asyncio.Lock` per game_id.

### 6.4 `client.py` — single-session safe

```python
class GameroomClient:
    def __init__(self, *, base_url, username, password, http_client, session_store, game_id): ...

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid token. If `invalidate` is given and the cache still holds that exact value,
        force a fresh login. Otherwise reuse whatever is cached, or log in if nothing is cached.

        Double-checked locking: re-read the cache under the lock before issuing a login, so two
        workers that both saw the same dead token don't both re-login (which would invalidate each
        other's session under Gameroom's single-session rule)."""
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate and not _expired(cached):
            return cached.token
        async with self._session.login_lock(self._game_id):
            cached = await self._session.get(self._game_id)     # someone may have refreshed while we waited
            if cached and cached.token != invalidate and not _expired(cached):
                return cached.token
            token, expires_at = await self._login()             # one POST /api/login
            await self._session.set(
                self._game_id,
                CachedSession(token=token, expires_at=expires_at),
                ttl_seconds=max(60, expires_at - int(time.time()) - 60),
            )
            return token

    async def call(self, method, path, fields=None, params=None) -> dict:
        token = await self.get_token()
        resp = await self._http_request(method, path, token, fields=fields, params=params)
        if _is_410(resp):
            fresh = await self.get_token(invalidate=token)      # only re-logs in if no one else has
            resp = await self._http_request(method, path, fresh, fields=fields, params=params)
            if _is_410(resp):
                raise BackendError("gameroom:auth_failed")      # second 410 -> creds problem; terminal
        return self._classify(resp)                              # 200 -> data dict; non-200 -> raise
```

- **Login (`_login`)**: `POST /api/login` (no Bearer, no captcha). On `status_code == 200`: return
  `(token, expires_time)`. On `status_code == 430`: raise `BackendError("gameroom:auth_failed")`
  (terminal). Anything else → `TransientBackendError("gameroom:login_failed:<status>")`.
- **`_http_request`**: form-urlencoded body for POST; query string for GET. Always sets the four
  headers from the findings doc. On `httpx.HTTPError` (timeout/conn/5xx) → `TransientBackendError`.
- **`_classify`**: success returns `body.get("data") or {}`. Non-200 `status_code` → `map_response(...)`
  → raise `BackendError` (terminal) or `TransientBackendError` (transient).
- **Login lock contention**: `login_lock()` waits for the lock; if another worker holds it and
  finishes, our second cache read inside the lock returns the fresh token without doing our own
  login. If the lock holder dies, the TTL (10s) releases it and we take over.

### 6.5 `backend.py`

Implements `GameBackend`. Wraps a `GameroomClient`. The 6 ops:

| Op | Implementation |
|---|---|
| `create_account(ctx)` | `pwd = generate_memorable_password()` (alphanumeric); `POST playerInsert` with `username=ctx.account_username`, `nickname=ctx.account_username`, `money=0`, `password=pwd`, `password_confirmation=pwd`; return `CreateAccountResult(username=ctx.account_username, password=pwd, external_user_id=str(data["id"]))` |
| `read_balance(ctx)` | `pid = await self._player_id(ctx)`; `GET agentMoney?id=pid`; return `ReadBalanceResult(balance_cents=_to_cents(data["balance"]))` |
| `reset_password(ctx)` | `pid`; `pwd = generate_memorable_complex_password()`; `POST reset` with `id=pid, password=pwd, password_confirmation=pwd`; return `ResetPasswordResult(password=pwd)` |
| `recharge(ctx, *, total_credit_cents, ...)` | `pid`; `POST agentRecharge` with `id=pid, available_balance="", opera_type=0, bonus=0, balance=_to_dollars(total_credit_cents), remark=""`; return `RechargeResult(balance_cents=_to_cents_opt(data.get("total_balance")))` |
| `redeem(ctx, *, amount_cents)` | `pid`; `POST agentWithdraw` with `id=pid, customer_balance="", opera_type=1, balance=_to_dollars(amount_cents), remark=""`; return `RedeemResult()` (no balance — response has no `data`) |
| `agent_balance(ctx)` | `POST agent/getMoney`; read `data.money` first, fall back to top-level `money` (shape not documented); return `AgentBalanceResult(agent_balance_cents=_to_cents(value))` |

`_player_id(ctx)`:
- If `ctx.account and ctx.account.external_user_id`: return that string.
- Else if `ctx.account and ctx.account.username`: `GET userList?account=<username>&page=1&limit=20`,
  scan `data` for the row where `Account == username` (substring filter — exact-match in code), return
  `str(row["id"])`.
- Else: `BackendError("gameroom:player_not_found")`.

Unit helpers (mirror `gamevault/backend.py`):
- `_to_cents(x) = round(float(x) * 100)`
- `_to_cents_opt(x) = None if x is None else _to_cents(x)`
- `_to_dollars(cents) = str(math.ceil(cents / 100))`

### 6.6 `registry.py` changes

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({"gameroom"})
_GAMEVAULT_PROVIDER_DRIVERS = frozenset({"gamevault", "juwa", "juwa2"})

def resolve_backend(driver, *, credentials, http_client, settings, session_store=None):
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(...)
    if key in _GAMEVAULT_PROVIDER_DRIVERS:
        ...                                  # unchanged
    if key == "gameroom":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_gameroom_credentials")
        if session_store is None:
            raise BackendError("missing_session_store")     # config bug — surface loudly
        return GameroomBackend(GameroomClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=session_store,
            game_id=credentials.game_id,
        ))
    raise BackendError(f"unknown_backend_driver:{driver}")
```

### 6.7 `executor.py` change (one line — thread session_store through)

```python
backend = resolve(
    ctx.credentials.backend_driver,
    credentials=ctx.credentials,
    http_client=http_client,
    settings=settings,
    session_store=session_store,        # NEW: pulled from worker ctx
)
```

`execute_operation` gains a `session_store: SessionStore | None = None` kwarg. Defaults to `None`
(works for mock/gamevault tests that don't need it). Worker passes the real one.

### 6.8 `api/operations.py` — per-driver `_max_tries`

Add `app.state.session_factory = get_sessionmaker()` in `main.py`'s lifespan. Then in the endpoint:

```python
@router.post("/operations")
async def receive_operation(request, raw=Depends(verify_signature)) -> Response:
    try: data = json.loads(raw)
    except json.JSONDecodeError: return Response(status_code=400)
    key = data.get("idempotency_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key: return Response(status_code=400)

    # Cheap driver peek to decide per-job retry policy. NOT a full preflight — that runs in the worker.
    max_tries = 3
    game_id = data.get("game_id")
    if isinstance(game_id, int):
        try:
            async with request.app.state.session_factory() as session:
                driver = await GamesRepository(session).get_driver(game_id)
            if driver and driver.lower() in NON_IDEMPOTENT_DRIVERS:
                max_tries = 1
        except Exception:  # DB blip — fall through with default; preflight will surface the real error
            logger.exception("driver_peek_failed", idempotency_key=key, phase="received")

    try:
        await request.app.state.arq.enqueue_job("execute_operation_task", data, _job_id=key, _max_tries=max_tries)
    except Exception:
        logger.exception("operation_enqueue_failed", idempotency_key=key, phase="enqueued")
        return Response(status_code=500)
    logger.bind(idempotency_key=key, phase="enqueued").info("operation_enqueued", max_tries=max_tries)
    return Response(status_code=202)
```

Adds `GamesRepository.get_driver(game_id) -> str | None` (a thin one-column read so we don't load the
secret columns at the API tier).

## 7. Status / error mapping (`gameroom:` prefix)

| Trigger | Reason slug | Terminal? |
|---|---|---|
| `/api/login` `status_code:200` | (success — no error) | — |
| `/api/login` `status_code:430` | `gameroom:auth_failed` | terminal |
| `/api/login` other non-200 | `gameroom:login_failed:<status>` | transient |
| Any call `status_code:200` | (success) | — |
| Any call `status_code:410` after one re-login retry | `gameroom:auth_failed` | terminal |
| Any call `status_code:430` (shouldn't happen post-login) | `gameroom:auth_failed` | terminal |
| Any call `status_code:401` | `gameroom:auth_missing` | transient |
| Any call `status_code:400` + matching pattern | `gameroom:<slug>` (see §6.1) | terminal |
| Any call `status_code:400` other | `gameroom:business_error: <msg ≤80>` | terminal |
| Any call `status_code:500` | `gameroom:server_error` | transient |
| HTTP 5xx / timeout / conn error / bad JSON | (TransientBackendError) | transient |

With `_max_tries=1`, "transient" failures still don't trigger an arq retry — they surface as the
op's webhook failure and Laravel's reaper picks up the operation when it expires. We keep the
transient/terminal distinction in the client to (a) avoid caching transient outcomes in the result
cache (which would block a later legitimate replay of a transient error), and (b) preserve the same
semantic across drivers so the executor logic stays uniform.

## 8. Error handling matrix (operational summary)

| Situation | Behavior |
|---|---|
| First op on a fresh game_id | Lazy login under lock; cache token; proceed |
| Cached token still valid | All ops use it; no login |
| Token expired (TTL elapsed) | Cache miss → lazy login under lock |
| 410 on a call | Drop local view → `get_token(invalidate=dead_token)` → if cache still holds dead, re-login under lock; else use the fresher cached token → retry once |
| 410 after retry | `gameroom:auth_failed` (terminal); cached so no re-call on replay |
| Login concurrency (two ops, cache miss) | First takes lock and logs in; second waits, then reads the fresh token from cache and skips its own login |
| Worker crash mid-money-op | No arq retry (`_max_tries=1`); Laravel reaper at 10 min → failed + refunded; operator reconciles any orphan |
| Player ID missing in our DB | `userList?account=<username>` exact-match fallback; else `gameroom:player_not_found` |
| Empty / null `data` on success (e.g. agentWithdraw) | Treat as success; omit optional `balance_cents` |

## 9. Testing

- **Unit — session/lock:**
  - `RedisSessionStore.set/get/clear` (round-trip JSON, TTL respected) — use `fakeredis` or a thin
    fake to keep tests in-process.
  - Login lock: two concurrent acquires serialize correctly; auto-release on TTL.
- **Unit — client:**
  - `_login` posts form-urlencoded, no Bearer, parses `token`/`expires_time` on 200; raises
    `BackendError("gameroom:auth_failed")` on 430; `TransientBackendError` on 500/timeout.
  - `get_token` returns cached when present + valid; logs in when cache missing; honors `invalidate`
    only when cache still holds that token; **does not** log in if the cache already holds a newer
    token (double-checked-locking regression test).
  - `call`: re-logs in on 410 once and retries; raises `gameroom:auth_failed` if second 410; HTTP
    5xx/timeout → `TransientBackendError`.
- **Unit — errors:** `map_response` for each documented `status_code:400` message → expected slug;
  unknown messages truncate to ≤80.
- **Unit — passwords:** complex generator yields strings with upper, lower, digit, symbol, 6–12
  chars, no spaces.
- **Unit — backend:**
  - Each of the 6 ops via `respx` against the mocked Gameroom; verify request fields (incl.
    integer-dollar `ceil` on recharge/withdraw, empty `available_balance`/`customer_balance`/`remark`)
    and the returned result model.
  - `_player_id` fallback via `userList` exact-match; mismatch row → `gameroom:player_not_found`.
- **Unit — registry:** `'gameroom'` driver → `GameroomBackend`; missing creds → terminal
  `missing_gameroom_credentials`; missing `session_store` → `missing_session_store`.
- **Integration — executor + cache:**
  - Driver routing: a seeded gameroom game routes to `GameroomBackend`; terminal failures are cached
    (replay doesn't re-call gameroom); transient failures are not cached.
  - End-to-end signed webhook for one success and one terminal failure.
- **Integration — API endpoint:**
  - With a gameroom game_id, the enqueued job carries `_max_tries=1`; with a gamevault game_id, it
    carries `_max_tries=3`. (Assert via the `FakeArq.jobs` capture from Phase 1's endpoint tests.)
- **Update existing tests** for the new `resolve_backend(session_store=...)` kwarg and the
  `execute_operation(session_store=...)` kwarg (default `None` keeps Phase 1/2 callers unchanged).

## 10. Laravel-side dependencies

**Nothing new to ship.** Gameroom uses the existing reverse-engineered credential columns
(`backend_url`, `backend_username`, `backend_password`) and the existing `backend_driver` column.

What you (operator) do to enable a gameroom game:
1. In Filament: add the gameroom game with `backend_driver='gameroom'`, `backend_url=<provider URL>`,
   `backend_username=<agent login>`, `backend_password=<agent password>`. If `backend_driver` is an
   enum-restricted select, add `'gameroom'` as an option (a one-line Laravel migration/enum bump).
2. Trigger any op from Laravel. First op lazily logs in; subsequent ops share the cached JWT.

## 11. Deferred / accepted limitations

- **Orphan-on-crash window** during RECHARGE/REDEEM: with `_max_tries=1`, a crash *after* the
  Gameroom call succeeded but *before* our `cache.set` leaves an applied in-game change that Laravel
  has already refunded (reaper). Operator reconciles via Gameroom dashboard. Accepted per R2 — the
  only alternative on a backend with no `order_id` is the in-flight-marker pattern, which was
  considered and explicitly rejected during brainstorming.
- **Whole-dollar `ceil` rounding** on send (player-favorable on recharge, agent-favorable on redeem).
  Gameroom only accepts integer dollars.
- **No `agent/getMoney` response shape documented.** We read `data.money` then fall back to top-level
  `money` (the login response uses the latter). If neither is present → `gameroom:business_error:
  agent_balance_missing` (terminal). To be verified empirically on the first real call.
- **No captcha solving.** Gameroom doesn't validate captchas server-side. Phase 4+ when needed.
- **One credential per game.** Token-pool rotation across multiple agent logins is not in scope.

## 12. Resolved review items

(None pending — all design questions resolved in brainstorming; spec self-review done inline.)
