# Phase 5 — OrionStars + MilkyWay backends with AntiCaptcha

**Status:** Design (approved, pre-plan)
**Date:** 2026-06-08
**Branch (target):** `feat/phase5-orionstars-milkyway`

## Context

OrionStars (`orionstars.vip:8781`) and MilkyWay (`milkywayapp.xyz:8781`) are two ASP.NET WebForms cashier portals running the same 3.0.303 build with one documented divergence. They are the first backends to require a captcha at login, which forces the AntiCaptcha integration that has been deferred since Phase 3 (the `ANTICAPTCHA_API_KEY` setting already exists in `app/config.py` but no solver code does).

Source of truth for the wire protocol: `/Applications/development/orionstars-standalone/api_findings.md` (reviewed 2026-06-08). Captcha format, session-binding, sentinel strings, error responses for all six operations, and the §4.1 OrionStars-vs-MilkyWay balance-reading divergence are all captured live in that document.

### Empirical findings from the brainstorm worth recording

- **No single-session enforcement.** User reproduced two concurrent valid sessions for the same agent across normal-browser + incognito with no eviction. The `errUser` dictionary entry in §2.6 of the findings doc ("You are logged out because another account has logged in.") is therefore either dead code or only triggers under an admin-override path we have not reverse-engineered. **Consequence:** the session lock in this design is purely an efficiency mechanism (one captcha solve serves all concurrent waiters), not a correctness one — unlike Gameroom.
- **No standalone agent-balance endpoint.** The "Balance:NN" widget on `AccountsList.aspx` and the inline `Balance:<n>` argument of the recharge/redeem success sentinel are the only sources.
- **Create-account does not return UID/GameID.** A follow-up search (the `ctl16` postback) is required to obtain them.

## Goals & scope

In scope (this phase):
1. New top-level captcha adapter (`app/captcha/`) with a `CaptchaSolver` Protocol and an `AntiCaptchaSolver` implementation backed by the official `anticaptchaofficial` PyPI package.
2. New shared helper package `app/backends/_aspnet_cashier/` holding all logic common to OrionStars and MilkyWay (HTTP plumbing, viewstate scraping, sentinel parsing, captcha-aware login, session cache + lock, `tourl`/`param` handshake, search/parse helpers, error mappings, password generator).
3. New backend module `app/backends/orionstars/` (thin: client facade + six op method bodies, including `read_balance` via `getscoreuserid`).
4. New backend module `app/backends/milkyway/` (thin: client facade + six op method bodies, with `read_balance` parsing the `Balance` column from the search-results row).
5. Both drivers registered in `app/backends/registry.py` and added to `NON_IDEMPOTENT_DRIVERS`.
6. Unit tests (target ~250-260 total, up from 231) and live-gated integration tests for both portals.
7. Logging redactions added for new secret-bearing form fields and the session cookie.

Out of scope (intentional, deferred):
- Local OCR / Tesseract fallback for the captcha. AntiCaptcha-only ships now; the Protocol seam keeps swap-in trivial later.
- Transaction Records / Game Records / JP Records dialogs (not in the 6-op contract).
- The `nullityuserid` / `unbinduserid` AccountsList actions (not in the contract).
- A standalone agent-balance endpoint exploration beyond scraping AccountsList.

## Architecture

### Module layout

```
app/captcha/
  __init__.py
  base.py                       # CaptchaSolver Protocol
  anticaptcha.py                # AntiCaptchaSolver (wraps anticaptchaofficial via asyncio.to_thread)

app/backends/_aspnet_cashier/
  __init__.py
  client.py                     # HTTP plumbing: Accept-Language injection, cookie jar, viewstate
                                #   scraper, sentinel parser, tourl/param handshake, ctl16 search,
                                #   AccountsList GET, dialog-page GET/POST helpers, session-death
                                #   detection + retry-once-after-relogin
  session.py                    # CookieSessionStore (Redis + InMemory) + SET-NX login_lock
                                #   (structurally a near-copy of app/backends/gameroom/session.py,
                                #   value = ASP.NET_SessionId cookie string)
  login.py                      # Captcha-aware login flow (GET default.aspx -> solve captcha ->
                                #   POST -> handle 301 errtype dispatching)
  errors.py                     # LOGIN_ERRTYPE_MESSAGES table, sentinel-to-error mappings
  passwords.py                  # Memorable password generator restricted to [A-Za-z0-9_], <=32 chars

app/backends/orionstars/
  __init__.py
  backend.py                    # OrionStarsBackend: 6 ops; read_balance uses getscoreuserid
  client.py                     # Thin facade binding _aspnet_cashier client to orionstars base_url

app/backends/milkyway/
  __init__.py
  backend.py                    # MilkyWayBackend: 6 ops; read_balance parses Balance column from
                                #   search HTML row
  client.py                     # Thin facade binding _aspnet_cashier client to milkyway base_url

app/backends/registry.py        # +2 entries; both added to NON_IDEMPOTENT_DRIVERS

app/logging.py                  # +secret keys (see Logging section)
app/config.py                   # +captcha config knobs (see Config section)
pyproject.toml                  # +anticaptchaofficial dependency
```

### Dependency rules

- `orionstars` and `milkyway` depend on `_aspnet_cashier` and `app/captcha`.
- `_aspnet_cashier` depends on `app/captcha` (via the `CaptchaSolver` Protocol injected at login time).
- Nothing in `_aspnet_cashier` depends on the portal modules.
- The captcha Protocol is the seam: backends do not import `AntiCaptchaSolver` directly; the resolver wires the configured solver into the shared client at construction time.

### Registry wiring

```python
# in app/backends/registry.py
NON_IDEMPOTENT_DRIVERS = frozenset({"gameroom", "goldentreasure", "orionstars", "milkyway"})

# resolve_backend:
if key == "orionstars":
    if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
        raise BackendError("missing_orionstars_credentials")
    if redis is None:
        raise BackendError("missing_redis_client")
    if not settings.anticaptcha_api_key:
        raise BackendError("missing_anticaptcha_api_key")
    return OrionStarsBackend(
        OrionStarsClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=CookieSessionStore(redis),
            captcha_solver=AntiCaptchaSolver(api_key=settings.anticaptcha_api_key),
            game_id=credentials.game_id,
        )
    )
# ... same shape for milkyway
```

## Data flow per operation

Every op receives a `BackendContext` (`credentials`, `account` possibly with packed `external_user_id="UID:GID"`, `idempotency_key`). Every op begins with a common preamble:

1. `client.get_or_login()` returns a valid `ASP.NET_SessionId` cookie (reads cache; on miss/death calls `_login()` under the `SET NX` lock).
2. Attach cookie + always-required headers: `Accept-Language: en-US,en;q=0.9`, a normal `User-Agent`, `Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8`.

### create_account (no UID/GID needed)
1. `GET /Module/AccountManager/CreateAccount.aspx?time=<now>` -> scrape `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`.
2. `POST` same URL with `__EVENTTARGET=ctl07` + viewstate trio + `txtAccount=<username>`, `txtNickName=<username>`, `txtLogonPass=<pwd>`, `txtLogonPass2=<pwd>`. Password from `passwords.generate()` (charset `[A-Za-z0-9_]`, <=32, memorable).
3. Parse sentinel. Success = `testAlter("Added successfully")`.
4. **Follow-up search** for the freshly-created account (`POST AccountsList.aspx` with `ctl16` + `txtSearch=<username>`); parse `updateSelect('<UID>,<GID>')`; pack as `"UID:GID"`.
5. Return `CreateAccountResult(username, password, external_user_id="UID:GID")`.

### read_balance — portal-divergent
- **OrionStars** (needs UID): `_player_ids(ctx)` -> `POST AccountsList.aspx` with `getscoreuserid=<UID>` -> parse `"<credit>@<totalwin>|..."` prefix -> `ReadBalanceResult(balance_cents=int(credit*100))`.
- **MilkyWay** (needs a search query, not UID:GID): `POST AccountsList.aspx` with `ctl16 + txtSearch=<account_or_GameID>` -> parse the row matching the account -> read the `Balance` cell (`td[4]`) -> same result shape.
  - MilkyWay's `read_balance` **bypasses `_player_ids` and goes straight to search** — running `_player_ids` first would just duplicate the search round-trip. If `external_user_id` is cached, we use the `GameID` half as the search query (more selective than account; matches §4.8 SQL `WHERE GameID LIKE OR Accounts LIKE`); otherwise we use `ctx.account.username`.

`_player_ids(ctx)` helper (in `_aspnet_cashier/client.py`): if `ctx.account.external_user_id` is present and contains `:`, split and return as `(uid, gid)`; else search by `ctx.account.username` and parse the first `updateSelect`. Raises `BackendError("<driver>:player_not_found")` if the search returns no row. **Used by all OrionStars ops that need UID/GID, and by MilkyWay for everything except `read_balance`.**

### reset_password (UID:GID needed)
1. `POST AccountsList.aspx` `tourl=2&getpassuid=<UID>&getpassgid=<GID>` -> dialog URL + `param`.
2. `GET ResetPassWord.aspx?param=<TOKEN>` -> scrape viewstate trio (this page has `EnableEventValidation` ON).
3. `POST` same URL with `__EVENTTARGET=Button1` + viewstate trio + `txtConfirmPass=<pwd>`, `txtSureConfirmPass=<pwd>` (both = generated password).
4. Success = `Modified success!`. `alert("Inconsistent passwords entered")` = our bug (we generated both halves), surfaced as `BackendError("<driver>:reset_password_mismatch")` — terminal.
5. Return `ResetPasswordResult(password=pwd)`.

### recharge (UID:GID needed)
1. `POST AccountsList.aspx` `tourl=0&getpassuid=<UID>&getpassgid=<GID>` -> dialog URL + `param`.
2. `GET GrantTreasure.aspx?param=<TOKEN>` -> scrape viewstate trio.
3. `POST` same URL with `__EVENTTARGET=Button1` + viewstate trio + `txtAddGold=<ceil(amount_cents/100)>` + `txtReason=""`.
4. Parse sentinel:
   - Success: `showAlter("Confirmed successful","Balance:<n>")` -> extract `<n>` as new agent balance.
   - Insufficient: `showAlter("Sorry, the surplus money is insufficient!")` -> `BackendError("<driver>:insufficient_agent_funds")` — terminal (verified atomic in findings §5.1).
5. Return `RechargeResult(balance_cents=None)`. Player balance is not in the response; caller can re-read if needed (matches Gameroom precedent of omitting when not present).

### redeem (UID:GID needed)
Same shape as recharge: dialog page `ChangeTreasure.aspx`, `tourl=1`. Insufficient sentinel: `showAlter("Sorry, there is not enough gold for the operator!")` -> `BackendError("<driver>:insufficient_player_credit")`. Returns `RedeemResult()`.

### agent_balance (no player context)
1. `GET /Module/AccountManager/AccountsList.aspx`.
2. Regex `Balance:(\d+)` from the HTML widget.
3. Return `AgentBalanceResult(agent_balance_cents=int(n)*100)`.

### Idempotency
Both portals lack any server-side dedupe key. Both are in `NON_IDEMPOTENT_DRIVERS`. The `/operations` endpoint already embeds `_max_tries=1` in the payload for these (per CLAUDE.md and the recent fix in commit 25ec6e6); the worker short-circuits the retry with a `retry_blocked` webhook.

## Login flow, captcha, session-death

### `get_or_login(game_id)` — entry point used by every op

```
1. cached = session_store.get(game_id)
2. if cached and not_expired(cached): return cached.cookie
3. async with session_store.login_lock(game_id, ttl=20s, acquire_timeout=30s):
       # Double-check: another worker may have re-logged in while we waited.
       cached = session_store.get(game_id)
       if cached and not_expired(cached): return cached.cookie
       cookie = await _login(credentials, captcha_solver)
       session_store.set(game_id, CachedSession(cookie, expires_at=now+1800), ttl=1800)
       return cookie
4. On lock acquire timeout: fall through to an unlocked _login() rather than failing the op.
   A wasted captcha is cheaper than a failed money op.
```

The lock is *efficiency-only* (concurrent sessions coexist; we cannot evict ourselves). Its job is to ensure that during a session-death thunderstorm, one re-login serves all waiters and we pay for exactly one captcha.

### `_login()` — captcha-aware, bounded retry

```
for attempt in 1..settings.captcha_login_max_attempts (default 3):
    1. GET /default.aspx (fresh cookie jar) -> scrape __VIEWSTATE, __VIEWSTATEGENERATOR,
       __EVENTVALIDATION, and the captcha <img src="Tools/VerifyImagePage.aspx?<rand>"> URL.
    2. GET /Tools/VerifyImagePage.aspx?<rand> (same cookie jar) -> JPEG bytes.
    3. captcha_text = await captcha_solver.solve_numeric_image(jpeg_bytes)
    4. POST /default.aspx (form-encoded, follow_redirects=False) with:
         __VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION, __EVENTTARGET="", __EVENTARGUMENT="",
         __LASTFOCUS="", ddlRole=0, txtLoginName=<creds.username>, txtLoginPass=<creds.password>,
         txtVerifyCode=<captcha_text>, btnLogin="Login in"
    5. Inspect the 301 Location header:
         - "Cashier.aspx"                                  -> SUCCESS; return ASP.NET_SessionId cookie value.
         - "default.aspx?errtype=verifycode"               -> captcha wrong; continue loop (restart at step 1 with a new jar).
         - "default.aspx?errtype=overtime&errInfo=AE0"     -> BackendError("<driver>:login_failed:session_overtime") (shouldn't fire on fresh login).
         - any other errtype                                -> look up via LOGIN_ERRTYPE_MESSAGES; raise BackendError("<driver>:login_failed:<code>") (TERMINAL).
raise BackendError("<driver>:captcha_failed_max_attempts")
```

Critical detail: on captcha-retry we restart from a **fresh cookie jar and a fresh GET /default.aspx**. The viewstate and the captcha are both single-use and session-rotated; reusing either fails.

### `AntiCaptchaSolver` (`app/captcha/anticaptcha.py`)

Wraps the official `anticaptchaofficial.imagecaptcha.imagecaptcha` class via `asyncio.to_thread`. Configures `set_numeric(2)` (digits only — matches our 5-digit JPEG). The library accepts a file path, not bytes, so we write to a `NamedTemporaryFile` for each solve, unlink in a `finally`. On `solve_and_return_solution() == 0`, raise `TransientBackendError(f"anticaptcha:{solver.error_code}")`.

**Reference for implementation:** `https://github.com/anti-captcha/anticaptcha-python` (the official Python repo). The exact API surface is verified against the library version pinned in `pyproject.toml` during the implementation phase.

### Session-death detection (during a normal op, not login)

After any module-page request, *before* parsing:

| Symptom | Meaning | Action |
|---|---|---|
| HTTP 500 with body containing `Server Error in '/' Application` | Dead session (or missing `Accept-Language` — our bug; fail loudly with a distinct error code if header was actually sent) | `session_store.clear()`, call `get_or_login()`, retry the op exactly once |
| HTTP 200 with body containing `name="txtLoginName"` | Login page returned -> session dead | Same as above |
| HTTP 301 to `default.aspx?errtype=overtime` | Soft session expiry | Same as above |
| Other 4xx | `TransientBackendError` | Op fails; surfaced via webhook |

The retry-once-after-relogin lives inside the shared client wrapper. Money ops that hit a dead session get exactly one re-attempt within the same arq job; only the *login* (read-only against the casino) is repeated, never the money POST itself. Compatible with `NON_IDEMPOTENT_DRIVERS`.

## Error model & sentinel classification

The shared sentinel parser pattern-matches `showAlter\(...\)`, `testAlter\(...\)`, and `alert\(...\)`. Returns `("success", args)`, `("business_failure", message)`, or `("unknown", raw)`. Unknown sentinels are raised as `BackendError("<driver>:unknown_sentinel:<truncated>")` so they show up in logs rather than being silently miscategorized.

### Per-op sentinel map

| Op | Success | Business failures (terminal) | Mapped error code |
|---|---|---|---|
| create_account | `testAlter("Added successfully")` | `"The account number already exists..."` | `account_exists` |
| | | `"account name should be compose..."` | `account_invalid_chars` |
| | | `"entered passwords differ..."` | `password_mismatch` (our bug) |
| read_balance | `<credit>@<totalwin>\|` prefix (OS) / row parse (MW) | — | — |
| reset_password | `showAlter("Modified success!")` | `alert("Inconsistent passwords entered")` | `password_mismatch` (our bug) |
| recharge | `showAlter("Confirmed successful","Balance:<n>")` | `showAlter("Sorry, the surplus money is insufficient!")` | `insufficient_agent_funds` |
| redeem | `showAlter("Confirmed successful","Balance:<n>")` | `showAlter("Sorry, there is not enough gold for the operator!")` | `insufficient_player_credit` |
| agent_balance | regex `Balance:(\d+)` from HTML | — | — |

All raised codes are prefixed with the driver name: `BackendError("orionstars:insufficient_agent_funds")` / `BackendError("milkyway:insufficient_player_credit")`.

### Transient vs terminal

Per CLAUDE.md golden rule ("Cache terminal outcomes... never cache transient errors"):

**Transient** (`TransientBackendError`, not cached):
- httpx timeout or connection error.
- HTTP 5xx (except dead-session NRE, which triggers relogin-and-retry).
- AntiCaptcha task-create failure or poll timeout.
- The "retried once after relogin and still failed for transient reasons" case.

**Terminal** (`BackendError`, cached as failed):
- All business-failure sentinels above.
- All non-`verifycode` login `errtype` codes (bad creds, banned IP, banned account, IP-bound mismatch).
- `captcha_failed_max_attempts` after N attempts (treat as terminal: the image class is consistently unsolvable in this window).
- Unknown sentinel.

### Login error code mapping (`_aspnet_cashier/errors.py`)

```python
LOGIN_ERRTYPE_MESSAGES = {
    "verifycode":          "captcha_wrong",         # handled inside _login loop, not raised
    "overtime":            "session_overtime",
    # Best-effort mappings from §2.6 — exact errtype query values for these weren't individually
    # exercised by the findings doc; confirm as we encounter them in the wild.
    "errorNamePassowrd":   "bad_credentials",
    "errorUserName":       "bad_username",
    "errorBlockIPErr":     "ip_blocked",
    "errorBindIP":         "ip_not_bound",
    "errorNullity":        "account_banned",
    "errorLogonTimeout":   "logon_timeout",
    "errorAuthParam":      "auth_param",
    "errorUnknown":        "server_unknown",
    "frequent":            "rate_limited",
    "errUser":             "session_stolen",        # see open question below
}
```

## Configuration

New fields on `app/config.py:Settings`:

```python
anticaptcha_poll_interval_seconds: float = 2.0
anticaptcha_max_poll_seconds: float = 120.0
captcha_login_max_attempts: int = 3
aspnet_session_ttl_seconds: int = 1800       # 30 minutes; refresh on use
aspnet_lock_ttl_seconds: int = 20
aspnet_lock_acquire_timeout_seconds: float = 30.0
```

`anticaptcha_api_key` already exists. The `.env` stub is already present per the session-start note.

## Logging redaction

Add to `app/logging.py:SECRET_KEYS`:
- `txtLoginPass`
- `txtLogonPass`, `txtLogonPass2`
- `txtConfirmPass`, `txtSureConfirmPass`
- `ASP.NET_SessionId` (cookie)
- `anticaptcha_api_key` (defensive; config-only)

Captcha image bytes are never logged (they're large and embedded inside the AntiCaptcha library calls, not in our request log path).

## Testing strategy

### Unit tests (default `make test`)

**`app/captcha/`** (~6-8 tests)
- `AntiCaptchaSolver.solve_numeric_image()` happy path returns digits.
- `error_code != 0` raises `TransientBackendError("anticaptcha:<code>")`.
- `asyncio.to_thread` wrapping doesn't block the loop (smoke test).
- `FakeCaptchaSolver` fixture in `tests/conftest.py` for reuse across the suite.

**`app/backends/_aspnet_cashier/`** (~20-25 tests; this is where the cross-portal logic concentrates)
- Sentinel parser: table-driven across every success and failure string from findings §5/§5.1, plus a few unknown-sentinel cases.
- Viewstate scraper: fixture HTML; asserts extraction of all four hidden fields; verifies `EnableEventValidation=false` page (AccountsList) does NOT include `__EVENTVALIDATION`.
- Login flow happy path (`httpx.MockTransport` + `FakeCaptchaSolver`): GET /default.aspx -> captcha image -> success 301 -> cookie is in jar.
- Login retry on `errtype=verifycode`: first POST returns wrong-captcha, second POST succeeds; verifies fresh image+viewstate are fetched on retry.
- Login terminal failure: `errtype=errorNamePassowrd` raises `BackendError(":login_failed:bad_credentials")`.
- Captcha exhaustion: every POST returns `verifycode` -> after N attempts raises `BackendError(":captcha_failed_max_attempts")`.
- Session-death detection: module GET returns 500 NRE -> cache cleared, second attempt logs in afresh.
- `tourl`/`param` handshake parsing.
- `updateSelect` parsing across realistic search HTML fragments.
- `Accept-Language` always present on every emitted request (asserted via MockTransport inspection).

**`app/backends/_aspnet_cashier/session.py`** (~6 tests)
- Mirror Gameroom's session tests against `fakeredis`: get/set/clear, lock acquire/release, lock contention (two coroutines, one waits), TTL behavior.

**`app/backends/orionstars/`** (~10 tests)
- One test per op using `httpx.MockTransport` + fixture responses; assert request shape and result.
- Create -> follow-up search -> packed `external_user_id` flow.
- Insufficient-funds business failure -> `BackendError("orionstars:insufficient_agent_funds")`.

**`app/backends/milkyway/`** (~6 tests)
- Same op coverage; `read_balance` test asserts the MilkyWay-specific path (no `getscoreuserid`, parses `Balance` column from search row).
- Registry resolution test.

**Registry tests** (~3 tests)
- `resolve_backend("orionstars", ...)` returns `OrionStarsBackend`.
- `resolve_backend("milkyway", ...)` returns `MilkyWayBackend`.
- Both in `NON_IDEMPOTENT_DRIVERS`.

### Live-gated integration tests (opt-in)

`tests/live/test_orionstars_live.py`, `tests/live/test_milkyway_live.py`, gated by both `ANTICAPTCHA_API_KEY` and `<PORTAL>_TEST_AGENT_USER` env vars (matching the pattern used by the other live-backend test files). Coverage: full login (real AntiCaptcha solve), create -> read_balance -> recharge $1 -> read_balance -> redeem $1 -> reset_password, agent_balance round-trip. Not run in CI; for manual verification after deploy.

### Coverage target

~250-260 total tests (current 231). Bulk in `_aspnet_cashier` where the genuinely new shared logic lives; portal modules thin with proportionally fewer tests.

## Open questions (not blocking)

1. **When does `errUser` actually fire?** User's empirical observation: two concurrent sessions for the same agent coexist. So `errUser` is either dead code or triggers under an admin-override path. Worth verifying before treating as a real runtime signal.
2. **Idle session TTL is unmeasured.** Findings observed expiry within ~3 days. We start at 30-min cache TTL with refresh-on-use, tighten/loosen based on production.
3. **MilkyWay parity beyond §4.1** is spot-checked, not exhaustively per-op verified. Implementation mirrors OrionStars; we surface any second-order divergences during manual testing.
4. **`txtReason` charset** — Gameroom hit charset rejection on UUID hyphens; OrionStars showed "api test" worked. We send empty string by default (documented working).

## Build sequence (for the implementation plan)

Logical ordering (the plan skill will refine):

1. `app/captcha/` Protocol + `AntiCaptchaSolver` + tests + `FakeCaptchaSolver` fixture + `pyproject` dep.
2. `app/backends/_aspnet_cashier/session.py` (cookie store + lock) + tests.
3. `app/backends/_aspnet_cashier/client.py` HTTP plumbing + viewstate scraper + sentinel parser + login flow + session-death handling + tests.
4. `app/backends/_aspnet_cashier/passwords.py` + tests.
5. `app/backends/orionstars/` backend + client + tests.
6. `app/backends/milkyway/` backend + client + tests (focus on `read_balance` divergence).
7. Registry entries + `NON_IDEMPOTENT_DRIVERS` update + tests.
8. Logging redactions + config knobs.
9. Live-gated integration test scaffolding.
10. Manual verification on `feat/phase5-orionstars-milkyway` against real agent accounts (one OrionStars game, one MilkyWay game). Merge to main only after manual sign-off, matching the prior-phase workflow.
