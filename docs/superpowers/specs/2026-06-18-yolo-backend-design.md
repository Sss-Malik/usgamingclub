# YOLO777 Backend — Design

**Date:** 2026-06-18
**Status:** Approved design.
**Findings doc:** `/Applications/development/yolo-standalone/yolo_api.md` (reverse-engineered HTTP API,
success + business + validation responses live-verified; session-expiry behavior NOT verified).

## 1. Goal

Add `yolo` as a new game backend driver. YOLO777's agent panel is **Laravel 7 + Dcat Admin**:
session-cookie auth + a scraped CSRF token, form-urlencoded writes, JSON envelopes for writes,
HTML/number bodies for reads. It maps almost exactly onto the existing **gameroom** pattern
(session-holding, form-POST, no server-side `order_id` → non-idempotent). New, self-contained
package; no changes to other backends.

## 2. Decisions (locked)

1. **Session strategy:** Redis-cached session (cookies + CSRF token) with a conservative TTL and
   double-checked-locking login, mirroring gameroom/aspnet. **Best-effort re-login** on any
   auth-failure signal, then retry once.
2. **Money:** dollar-native. Arcadia sends whole-dollar `amount`; YOLO `input_score` accepts
   decimals but we send whole dollars (`str(int(amount))`). Balances read back as float dollars.
3. **Non-idempotent:** YOLO has no `order_id`/dedupe → add to `NON_IDEMPOTENT_DRIVERS`.
4. **No agent-score pre-check** before recharge/redeem (YAGNI) — surface the business error.
5. **Sole driver** `yolo` for now; provider-siblings (if any) become one-line registry aliases later.

## 3. Wire contract (from the findings doc)

Base URL = game's `backend_url` (`https://agent.yolo-777.com`).

### 3.1 Auth
- **Login:** `POST /admin/auth/login` form `_token,username,password` with headers
  `X-Requested-With: XMLHttpRequest`, `X-CSRF-TOKEN: <token>`. `_token` scraped from
  `GET /admin/auth/login` (`Dcat.token = "…"` or page HTML). Success sets cookies
  `laravel_session` (auth) + `XSRF-TOKEN`.
- **Session CSRF token:** `GET /admin/player_list` (any admin page) → scrape
  `Dcat\.token\s*=\s*"([^"]+)"`. Reuse as both `_token` form field and `X-CSRF-TOKEN` header on
  every write. Stable per session.

### 3.2 Operations
| Op | Method/Path | Key body / parse |
|---|---|---|
| Agent score | `GET /admin/refresh_score` | plain number text → float dollars |
| Find/balance | `GET /admin/player_list?Accounts=<acct>` (`&_pjax=#pjax-container`) | parse row: Player ID, Player Score (dollars) |
| Recharge | `POST /admin/dcat-api/form` | `_form_=App\Admin\Actions\UserRecharge type=1 UserID Accounts input_score=<int $>` |
| Redeem | `POST /admin/dcat-api/form` | same, `type=2` |
| Reset pw | `POST /admin/dcat-api/form` | `_form_=App\Admin\Actions\ResetUserPass UserID Accounts password=<pwd>` |
| Create | `POST /admin/player_list` | `Accounts NickName LogonPass Recharge_Amount=0 RegisterIP=0.0.0.0 _token` |

We send the flat form fields the Dcat action actually reads, plus the `_current_` echo field.
The findings doc notes `_payload_` "was sent but the action primarily reads" the flat fields, so
**we omit `_payload_`** (add it only if a live write is observed to require it). Player ID
(`UserID`) is required for recharge/redeem/reset and is obtained by searching `player_list` by
account.

### 3.3 Response envelopes (three)
| Case | HTTP | Body | Mapping |
|---|---|---|---|
| Action success | 200 | `{"status":true,"data":{"message":"success",...}}` | success |
| Create success | 200 | `{"status":true,"data":{"message":"<html acct+pwd>","alert":true}}` | success |
| Business error | 200 | `{"status":false,"data":{"message":"The score is insufficient","type":"error"}}` | terminal `BackendError` |
| Validation error | 422 | `{"status":false,"data":[],"errors":{"<field>":["msg"]}}` | terminal `BackendError` |
| Server/network | ≥500 / exc | — | `TransientBackendError` |
| Auth expired | (unverified) | login redirect / 401 / 419 / missing `Dcat.token` | re-login once, retry; else `BackendError("yolo:auth_failed")` |

## 4. Architecture — `app/backends/yolo/`

```
preflight (creds) → registry.resolve_backend("yolo") → YoloBackend(YoloClient(YoloSessionStore(redis)))
dispatch → YoloBackend.<op> → YoloClient.post_form/get_text (ensure_session → re-login-on-auth-fail)
                                   → parsers + errors.map_envelope → result model (dollars)
```

### 4.1 `session.py`
- `@dataclass CachedSession(cookies: dict[str,str], csrf_token: str, expires_at: int)`.
- `SessionStore` Protocol: `get/set/clear(game_id)` + `login_lock(game_id, ...)` async CM.
- `InMemorySessionStore` (asyncio.Lock per game; tests) and `RedisSessionStore`
  (`yolo_session:{game_id}` JSON value; `SET NX` lock `yolo_login:{game_id}`). Direct analog of
  gameroom's session store (store cookies+csrf instead of a JWT).

### 4.2 `client.py` — `YoloClient`
- `__init__(*, base_url, username, password, http_client, session_store, game_id,
  session_ttl_seconds, login_lock_ttl_seconds, login_lock_acquire_timeout_seconds)`.
- `get_session(*, invalidate: CachedSession | None = None) -> CachedSession`: double-checked
  locking; returns cached unless expired or equal to `invalidate`; else logs in under the lock.
- `_do_login() -> CachedSession`: GET login page → scrape `_token` → POST credentials → on success
  GET `/admin/player_list` → scrape `Dcat.token` → build `CachedSession(cookies, csrf, now+ttl)`.
  Login transport/5xx → Transient; a login page that still shows after POST → `BackendError("yolo:login_failed")`.
- `post_form(path, fields) -> dict`: ensure session, POST form (cookies + `_token` + `X-CSRF-TOKEN`
  + `X-Requested-With`), classify via `errors.map_envelope`. On auth-failure signal:
  `get_session(invalidate=current)` then retry once; still failing → `BackendError("yolo:auth_failed")`.
- `get_text(path, params) -> str`: ensure session, GET, return text; same auth-failure retry.
- Auth-failure detection (`errors.looks_like_auth_failure(resp, text)`): HTTP 401/419, a redirect to
  `/admin/auth/login`, or a body that `looks_like_login_page`.

### 4.3 `errors.py`
- `map_envelope(http_status: int, body_json: dict | None) -> dict | None`: returns the success
  `data` dict, or raises `BackendError`/`TransientBackendError`. Branches: 200+`status:true`→data;
  200+`status:false`→ business error from `data.message` via substring map; 422→ first `errors{}`
  message via substring map; ≥500→ Transient; non-JSON→ Transient.
- Substring → slug maps (terminal): `"score is insufficient"→insufficient_balance`,
  `"already been taken"→account_exists`, `"format is invalid"→account_invalid`,
  `"at least 6 characters"→too_short`, `"required"→field_required`; fallback
  `yolo:business_error: <msg[:80]>` / `yolo:validation_error: <field>: <msg[:60]>`.
- `looks_like_auth_failure(...)`, `looks_like_login_page(text)`.

### 4.4 `parsers.py`
- `parse_agent_score(text) -> float` — strip, `float()`.
- `parse_player_row(html, *, account) -> tuple[str, float]` — locate the grid `<tr>` whose Account
  cell matches; return `(user_id, player_score)`. Account cells render as
  `data-content="<acct>"...&nbsp;<acct>`; numeric cells are plain. Raise
  `BackendError("yolo:player_not_found")` if no row.
- `parse_csrf_token(html) -> str` — regex `Dcat\.token\s*=\s*"([^"]+)"`; raise transient if absent.
- `looks_like_login_page(text) -> bool` — presence of the login form / `auth/login` markers.

### 4.5 `passwords.py`
- Re-export `gamevault.generate_memorable_password` (alphanumeric, ≥6) — satisfies YOLO `Accounts`,
  `LogonPass`, and reset `password` rules.

### 4.6 `backend.py` — `YoloBackend`
Implements `GameBackend`:
- `agent_balance(ctx)` → `get_text("/admin/refresh_score")` → `AgentBalanceResult(agent_balance=float)`.
- `read_balance(ctx)` → `_player(ctx)` → `ReadBalanceResult(balance=score)`.
- `recharge(ctx, *, amount)` → `uid = _player_id(ctx)`; `post_form("/admin/dcat-api/form", {…
  _form_=UserRecharge, type:1, UserID:uid, Accounts:acct, input_score:str(int(amount)), …})`;
  `RechargeResult()`.
- `redeem(ctx, *, amount)` → same, `type:2`; `RedeemResult()`.
- `reset_password(ctx)` → `pwd = generate_memorable_password()`; `post_form(… ResetUserPass,
  password:pwd …)`; `ResetPasswordResult(password=pwd)`.
- `create_account(ctx)` → require `ctx.account_username`; `pwd = generate_memorable_password()`;
  `post_form("/admin/player_list", {Accounts:user, NickName:user, LogonPass:pwd,
  Recharge_Amount:0, RegisterIP:"0.0.0.0", _token:…})`; follow-up `player_list` search to get the
  new `UserID`; `CreateAccountResult(username=user, password=pwd, external_user_id=uid_or_None)`.
- `_player(ctx) -> tuple[str,float]` / `_player_id(ctx) -> str`: prefer cached
  `ctx.account.external_user_id` (the YOLO `UserID`); else `get_text("/admin/player_list",
  {"Accounts": ctx.account.username, "_pjax": "#pjax-container"})` → `parse_player_row`.

### 4.7 Wiring
- `registry.py`: add `key == "yolo"` branch — require `backend_url+username+password` and `redis`;
  build `YoloBackend(YoloClient(..., session_store=YoloSessionStore(redis), game_id=...,
  session_ttl_seconds=settings.yolo_session_ttl_seconds, ...))`. Add `"yolo"` to
  `NON_IDEMPOTENT_DRIVERS`.
- `preflight/checks.py`: add `"yolo"` to the session-family credential set (needs
  `backend_url+username+password`).
- `config.py`: add `yolo_session_ttl_seconds: int = 1800`,
  `yolo_login_lock_ttl_seconds: int = 10`, `yolo_login_lock_acquire_timeout_seconds: float = 10.0`.
- `logging.py`: `_token`, `csrf`, cookie values are sensitive — ensure `SECRET_KEYS` covers
  `_token`/`csrf_token`/`laravel_session`/`xsrf-token` (add if missing).

## 5. Money safety & idempotency
- `yolo` in `NON_IDEMPOTENT_DRIVERS` → `/operations`-equivalent API path embeds `_max_tries=1`.
- A worker crash mid recharge/redeem → `retry_blocked` → `error` webhook; operator reconciles via
  the YOLO panel. Terminal business/validation failures are cached (executor); transient/auth
  failures are not, so an arq re-run (capped at 1) is safe.

## 6. Testing (TDD)
- `parsers`: agent score, player row (match/no-match, copyable-cell format), csrf token, login-page detection.
- `errors`: each envelope (200 true / 200 false business / 422 validation / 5xx / non-JSON) → correct raise/slug.
- `client`: session cache hit; double-checked login (concurrent → one login); auth-failure → re-login + retry once; second auth-failure → `yolo:auth_failed`; cookie+`_token`+`X-CSRF-TOKEN` present on writes.
- `backend`: every op success; business error (insufficient); validation error (account_exists/too_short); transient (5xx); player-id resolution from cache vs search; create follow-up search.
- `registry`: `yolo` resolves; missing creds/redis raise; `yolo` ∈ `NON_IDEMPOTENT_DRIVERS`.
- `tests/integration/test_yolo_integration.py`: live-gated (env creds), skipped by default, mirroring the other provider integration tests.

## 7. Open items / assumptions
- **Session-expiry trigger unverified.** Best-effort detection (401/419/login-redirect/missing token)
  → confirm against a live expired session and tighten `looks_like_auth_failure` if needed.
- **`_payload_` necessity** unconfirmed (doc says the action reads flat fields). We send it verbatim
  to match the browser; if writes fail without it being exact, revisit.
- **Create `external_user_id`**: relies on a follow-up search resolving the new account; if YOLO
  indexes the new row with a delay, `external_user_id` may be `None` (later ops re-search by account — safe).

## 8. Out of scope
- No changes to other backends or the Arcadia integration boundary.
- `agent_balance` has no Arcadia endpoint (implemented for parity, unreachable via dispatch).
- Arcadia-side: operator adds the `games` row (`backend_driver='yolo'`, `backend_url`, agent
  `username`/`password`).
</content>
