# Phase 2 — GameVault Backend Integration — Design Spec

- **Status:** Approved (design) — pending spec review before plan
- **Date:** 2026-06-04
- **Owner:** saud
- **Depends on:** Phase 1 walking skeleton (merged to `main`)
- **GameVault API doc:** `~/Downloads/gamevault-api-doc.pdf` (18 pp; text at `/tmp/gamevault.txt` during design)
- **Wire contract:** `/Applications/development/laravel/casino-app/docs/integrations/python-game-service-api-contract.md`

---

## 1. Purpose

Integrate **GameVault**, our first real backend with an official HTTP API, behind the existing
`GameBackend` abstraction — replacing the MockBackend for GameVault-tagged games. GameVault is a
**synchronous request/response** API (no captcha, no session, no callback), so this phase also adds
the cross-cutting pieces a real money backend needs: a **Redis backend-result cache** (at-most-once
backend execution, spec D6 from Phase 1) and **driver-based backend selection** via a new
`games.backend_driver` column.

## 2. Goals & non-goals

**Goals**
- A `GameVaultBackend` implementing all six operations against the documented endpoints.
- A GameVault HTTP client: per-request MD5 auth token, `multipart/form-data` POST, `{code,msg,data}`
  envelope parsing, status-code → reason mapping.
- **Unit conversion**: read balances as decimal dollars → integer cents; send `amount` as **whole
  dollars via `ceil(cents/100)`**.
- **Memorable password** generation (word + random number) for CREATE_ACCOUNT and RESET_PASSWORD.
- **Driver selection** from `games.backend_driver` (read-only), routing to GameVault or Mock.
- **Redis result cache**: at-most-once backend execution + at-least-once webhook across worker
  re-runs; guarantees stable generated passwords on replay.
- `getUserID` fallback to resolve GameVault `user_id` when `game_accounts.external_user_id` is null.
- Tests (unit + integration) and docs/CLAUDE.md updates.

**Non-goals (deferred)**
- Reverse-engineered backends, captcha, persistent sessions (Phase 3).
- Rate limiting, metrics (later).
- `getLowDepositUsers` / `playerOffline` endpoints (not part of the six contract operations).
- Laravel-side changes (the `backend_driver` column and `account_username` field are the user's to
  ship; Python is built to match — see §10).

## 3. GameVault API summary (from the doc)

- **Auth (every request):** `agent_id`, `timestamp` (10-digit unix seconds),
  `token = md5(f"{agent_id}:{timestamp}:{secret_key}")` (32-char lowercase hex). Maps to `games`
  columns `api_agent_id`, `api_secret_key`; base URL = `api_base_url`. `binding_key` unused.
  There is an **IP allowlist** (status 5) — the VPS egress IP must be whitelisted by GameVault (ops).
- **Transport:** POST `multipart/form-data` to `{api_base_url}{path}`.
- **Envelope:** `{ "code": <int>, "msg": <str>, "data": <obj|array|null>, "count": <int> }`.
  `code == 0` = success; `code != 0` = failure (status dictionary §7).
- **Endpoints & fields:**

| Path | Request fields | Success `data` |
|---|---|---|
| `/api/external/addUser` | `account`, `login_pwd` | `account_name`, `user_id` |
| `/api/external/recharge` | `user_id`, `amount`, `order_id` | `agent_balance`, `amount`, `pay_order_id`, `transaction_id`, `transaction_time`, `user_balance` |
| `/api/external/withdraw` | `user_id`, `amount`, `order_id` | `agent_balance`, `amount`, `transaction_id`, `transaction_time`, `user_balance`, `wdw_order_id` |
| `/api/external/userBalance` | `user_id` | `user_balance` |
| `/api/external/agentBalance` | — | `agent_balance` |
| `/api/external/getUserID` | `account_name` | `user_id` |
| `/api/external/resetPassword` | `user_id`, `login_pwd` | `null` |

## 4. Decisions (from brainstorming)

| # | Decision | Resolution |
|---|---|---|
| G1 | Money units | **Read** balances as decimal dollars → `round(float(x) * 100)` cents. **Send** `amount` as whole dollars → `ceil(cents / 100)` (string). |
| G2 | `order_id` | Pass the operation's `idempotency_key` as `order_id`; GameVault dedupes duplicates (backstop to the Redis cache). |
| G3 | REDEEM when "user in game" (code 10) | Report `status:"failed"` reason `user_in_game`. |
| G4 | CREATE_ACCOUNT credentials | Laravel sends `account_username`; **Python generates a memorable password** (word + number) and returns it. |
| G5 | CREATE_ACCOUNT when account exists (code 20) | **Report the error as-is** (`account_exists`); no reset-recovery. Same-op re-runs are covered by the result cache (G7). |
| G6 | Driver selection | New **`games.backend_driver`** column (read-only); registry maps the string → backend. |
| G7 | Money-op crash safety | **Redis result cache** keyed by `idempotency_key`: cache *terminal* outcomes; skip the backend on replay; transient errors are not cached (retry-safe via G2). |

**Rounding note (accepted):** GameVault accepts only whole dollars, so `ceil(cents/100)` means RECHARGE
credits ≥ the cents paid (player-favorable) and REDEEM pulls ≥ the cents requested. Exact-cent parity
is impossible with this API; documented and accepted.

## 5. Module layout

```
app/backends/gamevault/
  __init__.py
  client.py        # GameVaultClient: auth token, multipart POST, envelope parse, code->reason
  errors.py        # GAMEVAULT_STATUS dict {code:int -> slug:str}; map_code(code,msg)->reason
  passwords.py     # generate_memorable_password() -> "Word" + digits
  backend.py       # GameVaultBackend(GameBackend): the 6 ops, unit conversion, getUserID fallback
app/operations/result_cache.py   # ResultCache protocol + RedisResultCache + InMemoryResultCache (tests)

# Changed:
app/db/models.py            # Game: add backend_driver column (read-only)
app/backends/context.py     # GameCredentials: add backend_driver
app/preflight/checks.py     # populate credentials.backend_driver
app/backends/registry.py    # resolve_backend(driver, *, credentials, http_client, settings)
app/operations/executor.py  # resolve via driver; wire in the result cache
app/schemas/operations.py   # CreateAccountOp: add optional account_username
app/worker/settings.py      # add a redis client + result cache to the worker ctx
app/config.py               # GameVault knobs (timeouts) if needed
```

## 6. Component designs

### 6.1 `GameVaultClient` (`client.py`)
- Constructed per operation from credentials + the shared `httpx.AsyncClient`:
  `GameVaultClient(base_url, agent_id, secret_key, http_client, timeout=...)`.
- `async def call(path: str, fields: dict[str, str]) -> dict`:
  1. `ts = str(int(time.time()))`; `token = md5(f"{agent_id}:{ts}:{secret_key}").hexdigest()`.
  2. POST `multipart/form-data` with `agent_id`, `timestamp`, `token`, plus `fields`
     (httpx `data=` → urlencoded; GameVault wants multipart, so use `files=`-style multipart or
     `data=` with `Content-Type: multipart/form-data` — implement with httpx `data=fields` and the
     multipart encoding httpx applies when needed; concretely send via `httpx` `data=` works for
     form fields. Use `files={k: (None, v) for k,v in all_fields.items()}` to force multipart).
  3. Parse JSON envelope. If transport/parse fails or HTTP non-2xx → raise
     `BackendError("gamevault_http_<status>")` / `BackendError("gamevault_timeout")` /
     `BackendError("gamevault_bad_response")` — these are **transient** (not cached).
  4. If `code == 0` → return `data` (dict; may be `null`). Else → raise
     `BackendError(map_code(code, msg))` — these are **terminal business failures** (cached).
- The client distinguishes **transient** (raise a marked transient error) vs **terminal** (business
  code) failures so the executor/cache can decide whether to cache. Implement via a subclass:
  `class TransientBackendError(BackendError)` for transport/timeout/5xx; plain `BackendError` for
  business `code != 0`.

### 6.2 `errors.py`
`GAMEVAULT_STATUS: dict[int, str]` for codes 1–23 and 400 (e.g. `6: "insufficient_agent_balance"`,
`7: "insufficient_user_balance"`, `8: "invalid_user_id"`, `10: "user_in_game"`, `20: "account_exists"`,
`23: "password_length"`, `400: "parameter_error"`). `map_code(code, msg) -> f"gamevault:{code}:{slug}"`
(falls back to `gamevault:{code}:{msg}` for unknown codes, truncated).

### 6.3 `passwords.py`
`generate_memorable_password() -> str`: pick a random word from a small **curated** wordlist
(common, inoffensive nouns, ~80 entries, capitalized) + a random 3–4 digit number
(e.g. `Tiger4827`). Guarantees length 6–32 and `[A-Za-z0-9]`. Uses `secrets` for randomness.

### 6.4 `GameVaultBackend` (`backend.py`)
Implements `GameBackend`. Holds a `GameVaultClient`. Per op:

| Op | Steps |
|---|---|
| `read_balance(ctx)` | uid = `_user_id(ctx)`; `userBalance{user_id}`; `ReadBalanceResult(balance_cents=_to_cents(data["user_balance"]))` |
| `agent_balance(ctx)` | `agentBalance{}`; `AgentBalanceResult(agent_balance_cents=_to_cents(data["agent_balance"]))` |
| `reset_password(ctx)` | pwd = `generate_memorable_password()`; uid = `_user_id(ctx)`; `resetPassword{user_id, login_pwd:pwd}`; `ResetPasswordResult(password=pwd)` |
| `recharge(ctx, amount_cents, bonus_cents, total_credit_cents)` | uid; `recharge{user_id, amount:_to_dollars(total_credit_cents), order_id:idempotency_key}`; `RechargeResult(balance_cents=_to_cents(data.get("user_balance")))` |
| `redeem(ctx, amount_cents)` | uid; `withdraw{user_id, amount:_to_dollars(amount_cents), order_id:idempotency_key}`; `RedeemResult(balance_cents=_to_cents(data.get("user_balance")))` — code 10 surfaces as `gamevault:10:user_in_game` failure automatically |
| `create_account(ctx)` | require `account_username` (else `BackendError("account_username_required")`); pwd = memorable; `addUser{account:account_username, login_pwd:pwd}`; `CreateAccountResult(username=account_username, password=pwd, external_user_id=str(data["user_id"]))` — code 20 surfaces as `gamevault:20:account_exists` |

- `_user_id(ctx)`: `ctx.account.external_user_id` if set, else `getUserID{account_name: ctx.account.username}` → `data["user_id"]`; cache within the op. For AGENT_BALANCE no user is needed; for CREATE_ACCOUNT no account exists yet.
- `_to_cents(x)`: `round(float(x) * 100)` (handles `"150000"`, `"3649.0057"`, `None`→omit).
- `_to_dollars(cents)`: `str(math.ceil(cents / 100))`.
- **The backend needs `idempotency_key`** for `order_id`. It is added to `BackendContext` (new field
  `idempotency_key: str`) so backends can reference it without a wider signature change.
- **The backend needs `account_username`** for create. Added to `BackendContext`
  (`account_username: str | None`), populated by preflight from the validated op when present.

### 6.5 Driver selection (`registry.py`)
```python
def resolve_backend(driver: str | None, *, credentials, http_client, settings) -> GameBackend:
    key = (driver or "mock").lower()
    if key == "mock":
        return MockBackend(fail=settings.mock_force_fail, fail_reason=settings.mock_force_fail_reason)
    if key == "gamevault":
        return GameVaultBackend(GameVaultClient(
            base_url=credentials.api_base_url, agent_id=credentials.api_agent_id,
            secret_key=credentials.api_secret_key, http_client=http_client))
    raise BackendError(f"unknown_backend_driver:{driver}")
```
The executor calls `resolve_backend(ctx.credentials.backend_driver, credentials=ctx.credentials,
http_client=http_client, settings=settings)` after preflight. Missing GameVault creds
(`api_base_url`/`api_agent_id`/`api_secret_key` null) → preflight raises
`missing_gamevault_credentials`.

### 6.6 Result cache (`result_cache.py`)
```python
@dataclass
class CachedOutcome:
    status: str            # "succeeded" | "failed"
    result: dict | None    # for succeeded
    reason: str | None     # for failed

class ResultCache(Protocol):
    async def get(self, key: str) -> CachedOutcome | None: ...
    async def set(self, key: str, outcome: CachedOutcome, ttl_seconds: int) -> None: ...
```
- `RedisResultCache` (JSON value, key `opresult:{idempotency_key}`, TTL 900s).
- `InMemoryResultCache` for tests.
- **Executor flow** becomes:
  1. validate payload (invalid → webhook failed, as today).
  2. **cache.get(key)** → if hit, **skip preflight+backend**, deliver the cached outcome via webhook, return.
  3. preflight → resolve backend → dispatch.
     - On success or **terminal** `BackendError` (business `code != 0`, incl. account_exists,
       user_in_game): build outcome, **cache.set(...)**, deliver webhook.
     - On `TransientBackendError` / preflight error / unexpected: deliver webhook `failed`
       **without caching** (so an arq re-run retries the backend; GameVault `order_id` dedupe makes
       money ops safe to retry).
- Cache is written **immediately** after the backend returns, before webhook delivery, minimizing the
  crash window. (Edge: a crash between GameVault success and cache.set leaves an orphaned account/
  charge; GameVault `order_id` dedupe covers recharge/withdraw, and CREATE_ACCOUNT would then fail
  with `account_exists` on retry — a rare, admin-resolvable case, accepted per G5.)

## 7. GameVault status-code dictionary (codes → reason slugs)
1 invalid_agent_id · 2 invalid_request_parameters · 3 invalid_token · 4 token_expired ·
5 ip_not_whitelisted · 6 insufficient_agent_balance · 7 insufficient_user_balance · 8 invalid_user_id ·
9 user_account_frozen · 10 user_in_game · 11 invalid_amount · 12 recharge_failed ·
13 recharge_permission_denied · 14 withdrawal_failed · 15 withdrawal_exceeds_daily_limit ·
16 withdrawal_under_review · 17 withdrawal_permission_denied · 18 account_name_format_error ·
19 agent_no_register_permission · 20 account_exists · 21 system_failed · 22 register_ip_limit ·
23 password_length · 400 parameter_error.

Codes that are **transient/retryable** (treated like infra errors, not cached): `12` recharge_failed,
`14` withdrawal_failed, `21` system_failed. All others are terminal business failures (cached).

## 8. Error handling matrix (additions to Phase 1)

| Situation | Behavior |
|---|---|
| GameVault `code == 0` | success → result per §6.4 |
| GameVault business `code` (e.g. 6,7,8,10,20) | webhook `failed`, reason `gamevault:<code>:<slug>`, **cached** |
| GameVault transient (`12`/`14`/`21`, HTTP 5xx, timeout, bad JSON) | webhook `failed`, reason set, **not cached** → arq re-run retries |
| Missing `external_user_id` and `getUserID` finds none | webhook `failed`, `gamevault:8:invalid_user_id` (or `user_id_unresolved`) |
| CREATE_ACCOUNT without `account_username` | webhook `failed`, `account_username_required` |
| Unknown `backend_driver` | webhook `failed`, `unknown_backend_driver:<x>` |
| Cache hit on replay | deliver cached outcome; no backend call |

## 9. Testing

- **Unit:**
  - `client`: token = md5(agent:ts:secret); multipart fields include agent_id/timestamp/token; envelope
    `code==0` returns data; business code raises `BackendError` with mapped reason; HTTP 5xx/timeout/bad
    JSON raise `TransientBackendError`. (`respx` + frozen time.)
  - `errors`: `map_code` for known + unknown codes.
  - `passwords`: charset `[A-Za-z0-9]`, length 6–32, format word+digits, varies across calls.
  - `backend`: each of the 6 ops via `respx`-mocked GameVault — assert exact request fields (incl.
    `order_id`, `amount` ceil, `user_id` resolution), and the returned result model
    (`_to_cents` rounding incl. `"3649.0057"`, `"150000"`); code 10 redeem → failure; code 20 create →
    failure; `getUserID` fallback when `external_user_id` null; create without username → failure.
  - `result_cache`: in-memory get/set/ttl; Redis impl round-trips JSON (fakeredis or respx-free unit).
  - `registry`: driver → correct backend; unknown → BackendError; mock when null.
- **Integration:**
  - executor + `GameVaultBackend` over `respx`: success per op asserts the exact signed webhook;
    terminal failure cached (second run with a `respx` route that would 500 still delivers the cached
    success → proves no second backend call); transient failure NOT cached (re-run calls backend
    again).
  - driver routing: a seeded game with `backend_driver='gamevault'` routes to GameVault.
- Update Phase 1 preflight/executor/worker tests for the new `BackendContext` fields
  (`idempotency_key`, `account_username`), the `resolve_backend` signature, and the injected cache.
- Gates: full suite green, `ruff`, `mypy` clean.

## 10. Laravel-side dependencies (the user ships these)

1. **`games.backend_driver`** — migration adding `string('backend_driver', 32)->nullable()`; Filament
   editable field (`mock` | `gamevault`); set GameVault games to `gamevault`. Read-only in Python.
2. **`account_username`** on the CREATE_ACCOUNT `request_payload` (string, 6–32, `[A-Za-z0-9_]`).
   Until shipped, GameVault CREATE_ACCOUNT fails with `account_username_required` (build-ahead).

## 11. Deferred / accepted limitations
- Whole-dollar rounding (`ceil`) — exact cents impossible via GameVault (§4 note).
- Rare orphan window if the worker crashes between a successful GameVault write and `cache.set`
  (mitigated by `order_id` dedupe; accepted per G5/G7).
- `playerOffline`, `getLowDepositUsers` not implemented (out of contract scope).
