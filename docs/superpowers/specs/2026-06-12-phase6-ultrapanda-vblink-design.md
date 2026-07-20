# Phase 6 — UltraPanda + VBlink backends

**Status:** Design (approved, pre-plan)
**Date:** 2026-06-12
**Branch (target):** `feat/phase6-ultrapanda-vblink`

## Context

UltraPanda (`https://ht.ultrapanda.mobi`) and VBlink (`https://gm.vblink777.club`) are two branded hosts of the same backend application (vendor `vpower`, stack ThinkPHP behind Cloudflare). The reverse-engineering doc verified the JS bundle hashes are **byte-identical** between the two portals, the signing secret is the same, and every live operation returns the same wire shape (one minor wording difference on a permission error). They are aliases of one provider, like the existing `juwa`/`juwa2` → GameVault pair.

This phase is the third reverse-engineered backend family in the project (after Gameroom and Golden Treasure) but the simplest to integrate: one backend module, two registry entries, no captcha.

**Source of truth:** `/Applications/development/ultrapanda-standalone/api_findings.md` (reviewed 2026-06-12).

### Empirical findings worth recording

- **Single active session per account** (§1.4 of findings) — same posture as Gameroom. Re-logging in from a second client kicks the first. Need DCL on re-login (same pattern as `app/backends/gameroom/session.py`).
- **Rate limit on `enterScore`** at `code 167 "high frequency request"` — same code Golden Treasure rate-limits with. ~6s safe spacing reliably avoids it. Same throttle pattern as `app/backends/goldentreasure/` (`SET NX` with TTL).
- **Server-side debug mode is on** (§7.1) — invalid params return HTTP 500 ThinkPHP yellow-pages with framework stack traces. These should be treated as transient (could be a backend hiccup OR a real bug in our request shape).
- **Login does not enumerate users** (§7 "Good") — wrong password and unknown account both return `code 5 / "帐号或密码错误"`.
- **`code 21` is op-ambiguous on the server.** Recharge-over-agent-balance and withdraw-over-player-balance both return the *same* generic `code 21 / "充值失败：服务器维护中"`. We disambiguate at the call site based on which op was running (recharge → `insufficient_agent_funds`, redeem → `insufficient_player_credit`).
- **Scores are fractional** (§3.2) — player balances like `0.11` exist live; integer-only enforcement noted in the frontend dict (msg 1024) is not actually applied server-side. We send `f"{cents/100:.2f}"` (no ceil/floor).
- **Token is stored URL-encoded** (§1.1, §1.3) — the JS keeps the login `token` field exactly as the server returned it (URL-encoded, e.g. ending in `Txn%2F%2FTg%3D`) and uses *that string verbatim* as the plaintext input for `x-token` AES encryption. Decoding before storage or before signing breaks auth.

## Goals & scope

In scope (this phase):
1. New backend module `app/backends/ultrapanda/`:
   - `crypto.py` — three auth primitives (login-credential AES-128-ECB, MD5 body signature, x-token AES-128-ECB)
   - `session.py` — Redis-backed token store + `SET NX vpower_session:{game_id}` login lock + DCL (near-copy of `app/backends/gameroom/session.py`)
   - `client.py` — HTTP client: auto-sign every request, attach `x-token`/`x-time`/`x-fingerprint` headers, throttle `enterScore` calls via `SET NX vpower_throttle:{game_id} ex=6`, retry-once-after-relogin on detected session death
   - `backend.py` — `UltraPandaBackend` with the 6 ops
   - `errors.py` — code→reason mapping
   - `passwords.py` — re-export memorable generator (alphanumeric, no server-side policy)
2. Registry: `"ultrapanda"` and `"vblink"` both resolve to `UltraPandaBackend`, parameterized by `credentials.backend_url`. Both added to `NON_IDEMPOTENT_DRIVERS`. Pattern mirrors `_GAMEVAULT_PROVIDER_DRIVERS`.
3. Unit tests (target ~340 total, up from current 311).
4. Live-gated integration tests for both portals.
5. Logging redactions for new secret-bearing fields.

Out of scope (intentional):
- A separate "shared package" abstraction (`_vpower_panel/`). The doc explicitly says no divergence; promote to a shared-package layout only if/when one appears.
- Frontend error-dictionary mapping for codes the live API doesn't actually return (§5 of findings). We map only observed codes.
- Endpoints beyond the 6 ops (delete player, sub-account management, audits, etc. listed in §8 of findings).

## Architecture

### Module layout

```
app/backends/ultrapanda/
  __init__.py
  crypto.py                     # three auth primitives:
                                #   encrypt_login_cred(plaintext, stime_sec) -> b64 ciphertext
                                #   sign_body(body_dict, stime_sec) -> hex md5
                                #   encrypt_xtoken(admin_token, ms_time) -> urlencoded b64 ciphertext
  session.py                    # CachedSession (token + expires_at) + InMemoryTokenStore +
                                #   RedisTokenStore (Redis-backed) + SET-NX vpower_session login_lock
                                #   Near-copy of app/backends/gameroom/session.py
  client.py                     # UltraPandaClient: signed POST, x-token headers, throttle around
                                #   enterScore, session-death detection + retry-once-after-relogin
  backend.py                    # UltraPandaBackend: 6 ops
  errors.py                     # code -> short slug mapping (5/8/21/22/52/167/1003 + HTTP 500)
  passwords.py                  # re-export generate_memorable_password from gamevault
```

Registry adds:
```python
_VPOWER_PROVIDER_DRIVERS = frozenset({"ultrapanda", "vblink"})
# ... and in resolve_backend:
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
            session_store=RedisTokenStore(redis),
            redis=redis,
            game_id=credentials.game_id,
            session_ttl_seconds=settings.vpower_session_ttl_seconds,
            throttle_ttl_seconds=settings.vpower_throttle_ttl_seconds,
            driver_prefix=key,
        )
    )
```

Both drivers also added to `NON_IDEMPOTENT_DRIVERS`.

### Dependency rules

- `ultrapanda` module depends on `pycryptodome` (already in `pyproject.toml` from Golden Treasure) for AES, plus standard library `hashlib`/`hmac` for MD5.
- Registry imports `UltraPandaBackend` and `UltraPandaClient` and `RedisTokenStore` from `ultrapanda/`.

## Auth primitives (`crypto.py`)

All three primitives reverse-engineered from `static/js/app.057e5ccf.js` modules `413c`/`5f87`. Each has byte-for-byte test fixtures from the findings doc.

### 1. Login-credential AES-128-ECB

```python
def encrypt_login_cred(plaintext: str, stime_sec: int) -> str:
    """AES-128-ECB + PKCS7 encrypt of utf-8 plaintext; key = ('123' + str(stime_sec) + 'abc') as 16 bytes.

    Used for `username` and `password` fields on /user/login.
    Verified fixture: encrypt_login_cred("TestUP159", 1781187351) == "VhMfl38nq02TCY8sqZu5mg=="
    """
```

### 2. MD5 body signature

```python
_SIGN_SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"

def sign_body(body: dict, stime_sec: int) -> str:
    """MD5(concat + stime + SECRET) where concat = ''.join(str(value) for key in sorted(body)
       if key != 'stime' and value not in ('', None)).

    Used on EVERY request body, after which `sign` and `stime` are added to the body itself.
    Verified fixture: matches the captured login body's `fb9013238f78c92d7713fe5523e8b16a`.
    """
```

### 3. x-token header AES-128-ECB

```python
def encrypt_xtoken(admin_token: str, ms_time: int) -> str:
    """AES-128-ECB + PKCS7 encrypt of `admin_token` (URL-encoded form as received from /user/login,
    stored verbatim); key = ('xtu' + str(ms_time)) as 16 bytes.

    Returns: urlencode(base64(ciphertext)) — the urlencode is applied to the base64 output (the
    JS does this; verified live).

    Used on EVERY post-login request as the `x-token` header value.
    Verified fixture: encrypt_xtoken("l9oh…Txn%2F%2FTg%3D", 1781187352387) matches the captured x-token.
    """
```

**`x-fingerprint` is a static string** (server doesn't validate per §7.4). We hardcode a known-good value from the captured traffic.

## Session lifecycle

### Token cache

Keyed by `game_id`. Cached value: the `token` string from `/user/login` (URL-encoded form, verbatim).

- **TTL:** start at 30 minutes (configurable via `vpower_session_ttl_seconds`, default 1800). Server doesn't return an expiry; refresh-on-use.
- **Storage:** Redis at key `vpower_session:{game_id}` (mirrors Gameroom's `gameroom_session:`).
- **Login lock:** `SET NX vpower_session_lock:{game_id} ex=10` for DCL re-login (Gameroom-pattern).

### `get_or_login()` (entry point used by every op)

Same shape as Gameroom's `get_token`:
1. Read cache; if fresh, return.
2. Acquire `SET NX` login lock (10s TTL, 10s acquire timeout).
3. Re-read cache (DCL — another worker may have re-logged in meanwhile).
4. POST `/user/login` with AES-encrypted creds + `stime` + `sign` + `auth_code:""`.
5. Cache the returned token.

On lock acquire timeout: fall back to an unlocked login (same posture as Gameroom — efficiency lock, not correctness).

### Session-death detection

UltraPanda doesn't have a documented "session expired" code on the call path (frontend dict says `1086 "Not logged in"`, but not directly observed live). Defensive strategy:
- Any auth-related failure on a call after a successful initial login (specifically: `code 1086` if observed, OR `code 5` "wrong creds" on a non-login endpoint, OR HTTP 401/403) triggers cache-clear + re-login + retry-once.
- 500 ThinkPHP NRE pages are NOT treated as session-death — they're transient (could be our bug or backend hiccup). No retry-once on 500; let arq's policy handle retries (per `NON_IDEMPOTENT_DRIVERS` rules, money ops won't retry).

## Throttle for `enterScore`

Findings §6: `code 167` triggers on rapid sequential `enterScore` calls. ~6s spacing reliably avoids it. Same risk profile as Golden Treasure's `code:167`.

Implementation:
- Before every `enterScore` POST: `SET NX vpower_throttle:{game_id} b"1" ex=6`.
- If the `SET NX` returns false (key already exists): block-and-poll until the key is gone (with a deadline ~10s), then proceed.
- If the deadline expires while still blocked: raise `TransientBackendError("ultrapanda:throttle_acquire_timeout")` (Laravel sees a clean failed-with-retryable webhook).
- The throttle applies to recharge and redeem. Other ops (read_balance, agent_balance, create, reset_password) are not throttled.

The throttle is best-effort — it doesn't prevent a 167 from happening if two backends share the same agent across processes without the lock, but it gives one process clean behavior. Falls back to surfacing 167 as `TransientBackendError("ultrapanda:rate_limited")` if it still fires.

## Data flow per operation

Every op precondition: `client.get_or_login()` produces a valid token; the client signs the body and attaches `x-token`/`x-time`/`x-fingerprint` headers automatically.

### `create_account`
- `POST /account/savePlayer` body `{account, pwd}` (auto-signed).
- Success: `code 20000` → `CreateAccountResult(username=account, password=pwd, external_user_id=None)`.
- Errors:
  - `code 8` → `BackendError("ultrapanda:account_exists")`
  - `code 1003` → `BackendError("ultrapanda:account_invalid_chars")`
  - HTTP 500 (e.g. too-long account name) → `TransientBackendError("ultrapanda:http_500")` (debug-mode leak; let the worker decide)
- The doc notes empty/short passwords are accepted by the server — we still generate a memorable alphanumeric password via the existing generator to avoid surprising downstream systems.

### `read_balance`
- `POST /account/getPlayerScore` body `{account}`.
- Success: `code 20000` → `ReadBalanceResult(balance_cents=round(float(curScore) * 100))`.
- Errors: any non-`20000` → surface via error mapping (likely `22` for unknown account; `52` for cross-agent — both terminal).
- `account` is taken from `ctx.account.username` (already populated by the executor). `external_user_id` is not used on this provider (the server keys on `account` name).

### `reset_password`
- `POST /account/updatePlayer` body `{account, pwd, name, tel_area_code, phone, remark}` with the empty-string defaults the doc specifies for the other required fields.
- Generated `pwd` from the memorable generator.
- Success: `code 20000` → `ResetPasswordResult(password=pwd)`.
- Errors: HTTP 500 for missing/extra fields (defensive — we always send the full required set).

### `recharge`
- Throttle: `SET NX vpower_throttle:{game_id} ex=6`; on collision, block-and-poll.
- `POST /account/enterScore` body `{account, score: f"{total_credit_cents/100:.2f}", user_type: 0}`.
  - **Uses `total_credit_cents`** (principal + bonus), not `amount_cents`, per the OrionStars phase 5 lesson — the form has a single amount field.
- Success: `code 20000` ("进分成功") → `RechargeResult(balance_cents=None)` (server response doesn't include the new player balance; caller can re-fetch if needed).
- Errors:
  - `code 21` → `BackendError("ultrapanda:insufficient_agent_funds")` (op-disambiguated; server returns generic message)
  - `code 22` → `BackendError("ultrapanda:player_not_found")`
  - `code 167` → `TransientBackendError("ultrapanda:rate_limited")`

### `redeem`
- Same shape as recharge with negated score.
- `POST /account/enterScore` body `{account, score: f"-{amount_cents/100:.2f}", user_type: 0}`.
- Success: `code 20000` ("下分成功") → `RedeemResult()`.
- Errors:
  - `code 21` → `BackendError("ultrapanda:insufficient_player_credit")` (op-disambiguated)
  - `code 22` → `BackendError("ultrapanda:player_not_found")`
  - `code 167` → `TransientBackendError("ultrapanda:rate_limited")`

### `agent_balance`
- `POST /user/CurScore` body `{token: <admin_token>}` (the cached token; gets signed like any other body).
- Success: `code 20000` → `AgentBalanceResult(agent_balance_cents=round(float(LimitNum) * 100))`.

### Idempotency
No server-side dedupe key on `enterScore`. Both drivers added to `NON_IDEMPOTENT_DRIVERS`. `/operations` already embeds `_max_tries=1` in the payload (per CLAUDE.md), worker short-circuits the retry with a `retry_blocked` webhook.

## Error model

Single mapping function `map_code(code: int, op: str) -> tuple[str, bool]` returns `(reason_slug, is_terminal)`:

| code | recharge → | redeem → | other ops → | terminal? |
|---|---|---|---|---|
| 5 (login only) | — | — | `bad_credentials` | yes |
| 8 | — | — | `account_exists` | yes |
| 21 | `insufficient_agent_funds` | `insufficient_player_credit` | `unknown_21` | yes |
| 22 | `player_not_found` | `player_not_found` | `player_not_found` | yes |
| 52 | `no_permission` | `no_permission` | `no_permission` | yes |
| 167 | `rate_limited` | `rate_limited` | `rate_limited` | **no** (transient) |
| 1003 | `invalid_chars` | `invalid_chars` | `invalid_chars` | yes |
| 1086 (frontend dict) | `session_expired` | `session_expired` | `session_expired` | **no** (transient, triggers relogin path) |
| other 20000+1..29999 | `unknown:{code}` | `unknown:{code}` | `unknown:{code}` | yes |
| HTTP 500 (ThinkPHP) | `http_500` | `http_500` | `http_500` | **no** (transient) |
| HTTP 5xx other / network / timeout | `transport:{type}` | `transport:{type}` | `transport:{type}` | **no** (transient) |

Errors are raised as `BackendError("ultrapanda:<slug>")` (driver-prefixed via `client._driver` so the VBlink alias surfaces `vblink:<slug>` instead).

## Configuration

New fields on `app/config.py:Settings`:

```python
vpower_session_ttl_seconds: int = 1800     # token cache TTL; refresh on use
vpower_throttle_ttl_seconds: int = 6        # SET NX TTL for enterScore throttle
vpower_throttle_acquire_timeout_seconds: float = 10.0
vpower_session_lock_ttl_seconds: int = 10
vpower_session_lock_acquire_timeout_seconds: float = 10.0
```

## Logging redaction

Add to `app/logging.py:SECRET_KEYS` (lowercase):
- `pwd` (already there)
- `admin-token`
- `auth_code`
- The login body fields `username`/`password` are AES-encrypted on the wire, but the *plaintext* values appear in the call to `encrypt_login_cred()` — the client must never log the pre-encryption values. We rely on never passing raw creds through `structlog` calls; defensive only.

## Testing strategy

### Unit tests (default `make test`)

**`app/backends/ultrapanda/crypto.py`** (~8 tests)
- `encrypt_login_cred("TestUP159", 1781187351) == "VhMfl38nq02TCY8sqZu5mg=="` (fixture from §1.1)
- `encrypt_login_cred("Test1234", 1781187351) == "j9HtJjroTJboYOiA/nGdlQ=="`
- `sign_body(...)` matches the captured `fb9013238f78c92d7713fe5523e8b16a` (fixture from §1.2)
- `sign_body` skips empty/null values and the `stime` key when concatenating
- `sign_body` sorts keys before concatenating
- `encrypt_xtoken(<admin_token>, 1781187352387)` matches the captured live x-token (fixture from §1.3)
- `encrypt_xtoken` URL-encodes the base64 output
- Round-trip: a value encrypted with `encrypt_xtoken` and decrypted with the same key yields the original

**`app/backends/ultrapanda/session.py`** (~6 tests, mirror Gameroom's session tests)
- get/set/clear on in-memory store
- get/set/clear on Redis with key prefix `vpower_session:`
- TTL respected
- Login lock acquire/release with `vpower_session_lock:` prefix
- Lock contention blocks second acquirer
- Lock auto-releases on timeout

**`app/backends/ultrapanda/client.py`** (~12 tests)
- Login flow: sends AES-encrypted creds + signed body; stores returned token in cache
- Auto-sign: every non-login POST has `sign` and `stime` injected
- Auto-headers: every non-login POST carries `x-time`, `x-token`, `x-fingerprint`, `Content-Type: application/json;charset=UTF-8`
- Token URL-encoded form preserved (no decode applied)
- Throttle: `SET NX` taken before `enterScore`; second call within TTL blocks
- Throttle acquire timeout → `TransientBackendError`
- Session-death detection: code 1086 triggers cache-clear + relogin + retry-once
- Session-death detection: subsequent failure raises `TransientBackendError("...:session_dead_after_relogin")`
- HTTP 500 → transient (not session-death)
- Network error → transient with `transport:` prefix

**`app/backends/ultrapanda/backend.py`** (~10 tests)
- One per op (create, read_balance, reset_password, recharge, redeem, agent_balance)
- Recharge uses `total_credit_cents` (regression guard from Phase 5)
- Recharge insufficient → `BackendError("ultrapanda:insufficient_agent_funds")`
- Redeem insufficient → `BackendError("ultrapanda:insufficient_player_credit")`
- Recharge unknown account → `BackendError("ultrapanda:player_not_found")`

**`app/backends/ultrapanda/errors.py`** (~5 tests)
- Per-code mapping table coverage including the op-disambiguation for code 21

**Registry tests** (~4 tests)
- `resolve_backend("ultrapanda", ...)` returns `UltraPandaBackend` with `client._driver == "ultrapanda"`
- `resolve_backend("vblink", ...)` returns `UltraPandaBackend` with `client._driver == "vblink"`
- Both in `NON_IDEMPOTENT_DRIVERS`
- Missing credentials → driver-specific error message

### Live-gated integration tests (opt-in)

`tests/integration/test_ultrapanda_integration.py` and `tests/integration/test_vblink_integration.py`, env-gated:
- `ULTRAPANDA_TEST_BASE_URL`, `_AGENT_USER`, `_AGENT_PASS`, `_PLAYER`
- `VBLINK_TEST_BASE_URL`, `_AGENT_USER`, `_AGENT_PASS`, `_PLAYER`

Coverage:
- Login → agent_balance round-trip
- create → read_balance on a fresh account
- recharge $1 → read_balance → redeem $1 → read_balance (with ~7s spacing to respect the throttle)
- reset_password against the test player

### Coverage target

~340 total tests (current 311). Most concentrated in `client.py` (~12) and `crypto.py` (~8) where the genuinely new logic lives.

## Open questions (not blocking)

1. **Exact "session expired" code on a call** is inferred (frontend dict says 1086) but not directly observed. Implementation treats 1086 + `code 5` on non-login + HTTP 401/403 as session-death; we may need to add more triggers after first incident.
   - **RESOLVED (2026-07, first incident).** The live server signals a dead session on a call with **`code 52` ("no permission")**, *not* 1086 (never observed live). vblink ops ran green immediately after a fresh login and returned `no_permission` on the same cached token ~10-20 min later, in ~10-30 min bursts bounded by the Redis cache TTL — i.e. the server session dies well within our cache window. Fix: `client.call()` now treats **52 as session-death too** (clear → re-login → retry-once); a *persistent* 52 on a fresh session falls through to the terminal `no_permission` mapping (it is then a genuine permission error, not session death). Retry is safe on the non-idempotent score endpoint because a 52 is a rejection (the op never executed) — same posture as the 1086 retry.
2. **Nonexistent-player on `getPlayerScore`** — untested by the doc. Will surface whatever code the backend returns; expected to be `code 22` or HTTP 500.
3. **Throttle scope** — applied per `game_id` (one Laravel game → one agent → one throttle key). If two distinct games share an agent login, throttles run independently and could collide. The findings doc doesn't surface this; flag for first incident.
4. **Token expiry** — the server doesn't return an expiry timestamp. 30-min cache TTL is conservative; if production sessions actually live longer or shorter, tighten/loosen based on logs.
   - **RESOLVED (2026-07).** Sessions live well under 30 min (observed good at +6 min, dead by +20 min). `vpower_session_ttl_seconds` default lowered **1800 → 300** so the cached token stays inside the provider's real session lifetime — belt-and-braces with the code-52 relogin recovery above.

## Build sequence (informs the plan)

1. `crypto.py` + tests (pure functions, byte-exact fixtures)
2. `session.py` + tests (mirror Gameroom's session)
3. `errors.py` + tests (code map)
4. `passwords.py` (re-export)
5. `client.py` + tests (login + auto-sign + auto-headers + throttle + session-death)
6. `backend.py` + tests (6 ops)
7. Registry wiring (alias frozenset) + `NON_IDEMPOTENT_DRIVERS` update + tests
8. Config knobs
9. Logging redactions
10. Live-gated integration test scaffolding
11. Manual verification on `feat/phase6-ultrapanda-vblink` against real agent accounts (one UltraPanda game, one VBlink game). Merge to main only after manual sign-off.
