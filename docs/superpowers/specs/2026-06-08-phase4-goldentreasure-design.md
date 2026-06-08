# Phase 4 — Golden Treasure Backend Integration — Design Spec

- **Status:** Approved (design) — pending spec review before plan
- **Date:** 2026-06-08
- **Owner:** saud
- **Depends on:** Phase 3 (Gameroom) merged to `main`
- **Findings doc:** `/Applications/development/goldentreasure-standalone/goldentreasure_api_findings.md`
  (reverse-engineered from `agent.goldentreasure.mobi`; every algorithm verified end-to-end via a
  pure-Python client at §9 of the findings)
- **Wire contract:** `/Applications/development/laravel/casino-app/docs/integrations/python-game-service-api-contract.md`

---

## 1. Purpose

Integrate **Golden Treasure** (`https://agent.goldentreasure.mobi`), our second reverse-engineered
backend, behind the existing `GameBackend` abstraction. Golden Treasure introduces two new
cross-cutting concerns the project hasn't solved yet:

1. **Heavy per-request crypto** — MD5 sign of sorted body values, AES-128-ECB for login credentials,
   AES-128-ECB for the `x-token` header (rebuilt per request with a fresh millisecond timestamp).
2. **Strict per-agent rate limiting** (`code 167 "high frequency request"`) requiring **≥5-second
   spacing between mutating ops** (`savePlayer` / `enterScore`). We add a per-game Redis throttle
   gate to guarantee we never trip it.

Unlike Gameroom, Golden Treasure **allows multiple concurrent tokens for the same agent** — so we
keep the login lock as cheap insurance against thundering herd, but the double-checked-locking
pattern is unnecessary (no token thrashing under concurrent re-logins).

## 2. Goals & non-goals

**Goals**
- A `GoldenTreasureBackend` implementing all six operations against the documented endpoints.
- A `GoldenTreasureClient`: form-of-JSON POSTs with MD5 sign, AES-encrypted login, per-request
  `x-token`/`x-time` header rebuild, Cloudflare-friendly headers, and transparent re-login on
  `code:-3` / `code:-17` / `code:52`.
- A pure-functions `crypto.py` module testable against the findings doc's verified oracles.
- A duplicated `session.py` (gameroom-style, with `gtreasure_session:` / `gtreasure_login:` keys).
- A **per-game mutating-op throttle** (`SET NX gtreasure_throttle:{game_id} ex=5`) baked into the
  client; engaged for `savePlayer` (CREATE_ACCOUNT) and `enterScore` (RECHARGE/REDEEM) only.
- `NON_IDEMPOTENT_DRIVERS += {"goldentreasure"}` so the API endpoint passes `_max_tries=1`.
- Memorable alphanumeric password (re-export of GameVault's `generate_memorable_password`) — the
  doc-stated 6-16 letters+digits rule is satisfied by the existing generator.
- Tests (unit + integration) and docs/CLAUDE.md updates.

**Non-goals (deferred / explicitly out)**
- **2FA / verification codes** (login `code:30100` system verify, `30200`/`30201` Google
  Authenticator) — surface as terminal `gtreasure:requires_operator_action_<code>`; the operator
  clears the state via the agent's web UI.
- Token pool rotation across multiple agent credentials (one credential per game).
- `getPlayerList`, `getPlayerInfo`, `search/Account` (not part of our six contract ops).
- Lifting Gameroom's `session.py` into a shared module — explicitly rejected during brainstorming
  ("duplicate, mild tech-debt" was the chosen tradeoff).
- Captcha solving — Golden Treasure's login form has no captcha; not needed.

## 3. Golden Treasure API summary (from the findings doc)

- **Base URL:** per-game (currently `https://agent.goldentreasure.mobi`); slot into
  `games.backend_url`. Per-game agent credentials slot into `games.backend_username` /
  `games.backend_password`. **No new DB columns.**
- **Auth:** `POST /api/user/login` with **AES-128-ECB-encrypted** `username` + `password`
  (key = `f"123{stime}abc"`, exactly 16 chars). The `stime` baked into the AES key MUST equal
  `body.stime`. Captcha (`auth_code`) sent as `""`.
- **Body signing:** every request body is signed:
  `sign = MD5(<sorted-values, skipping empty/none and the stime field itself> + stime + SECRET)`
  with `SECRET = "#s3LEA3RpR6PNmbWtuBCPn!4gS2DNM44"`.
- **`x-token`/`x-time` headers:** mandatory on every authenticated call.
  `x-time` = current millisecond epoch (13 digits); `x-token` = URL-encoded base64 of
  `AES-128-ECB-PKCS7(session_token, key=f"xtu{x_time_ms}")`. **Built fresh per request.**
- **Cloudflare front:** missing realistic browser headers (`User-Agent`, `sec-ch-ua*`,
  `Accept-Language`, `Origin`, `Referer`) → `403` before the request reaches the API.
- **Envelope:** `{"code": <int>, "message"?: <str>, ...payload}`. `code == 20000` is the success
  signal (the JS literally checks `20000 === code`); any other code is an error.
- **Token lifetime:** "each login mints a new token; multiple tokens work concurrently. Expiry
  surfaces as `code -3` / `code -17`." → relogin + retry once.
- **Rate limit:** `code 167 "high frequency request"` on bursts of `savePlayer`/`enterScore`;
  required spacing ≥5s.
- **No `order_id` / idempotency** — same as Gameroom; non-idempotent backend.

### Endpoint → operation mapping (all 6 ops)

| Our op | Golden Treasure call | Notes |
|---|---|---|
| `CREATE_ACCOUNT` | `POST /api/account/savePlayer` | **throttle**; plaintext player password; `external_user_id=None` (response has no uid) |
| `READ_BALANCE` | `POST /api/account/getPlayerScore` | reads `data.curScore` (int) |
| `RESET_PASSWORD` | `POST /api/account/updatePlayer` | not throttled (only savePlayer/enterScore are) |
| `RECHARGE` | `POST /api/account/enterScore` | **throttle**; positive `score` (integer string) |
| `REDEEM` | `POST /api/account/enterScore` | **throttle**; negative `score` (integer string) |
| `AGENT_BALANCE` | `POST /api/user/CurScore` | reads `LimitNum` ("20.00" decimal-dollar string) |

## 4. Decisions (locked during brainstorming)

| # | Decision | Resolution |
|---|---|---|
| GT1 | Money units | Send `score` as integer **whole dollars** via `ceil(cents/100)`. Read `LimitNum`/`curScore` as decimal dollars → `round(float(x) * 100)` cents. Same convention as GameVault/Gameroom. |
| GT2 | Money safety (no `order_id`) | `NON_IDEMPOTENT_DRIVERS += {"goldentreasure"}`. API endpoint's existing DB peek auto-applies `_max_tries=1`. |
| GT3 | Rate limiting (`code 167`) | **Per-game Redis throttle.** Before every mutating op (`savePlayer`/`enterScore`), `SET NX gtreasure_throttle:{game_id} ex=5`; if locked, poll every 500ms up to a 30s cap. Lock auto-expires (no manual release). Reads are NOT throttled. |
| GT4 | `external_user_id` on CREATE_ACCOUNT | **Omit** (`external_user_id=None`). Golden Treasure operations key on `account` (username), not numeric uid; `savePlayer` response has no uid. Contract allows omitting. |
| GT5 | Session storage code organization | **Duplicate** `gameroom/session.py` to `goldentreasure/session.py` with `gtreasure_session:` / `gtreasure_login:` keys. Don't refactor Phase 3. |
| GT6 | Concurrent tokens vs single-session | Golden Treasure allows concurrent tokens (verified in findings doc §10). No double-checked locking required. Login lock still kept as cheap thundering-herd insurance; `get_token` does a single cache read after acquiring the lock. |
| GT7 | RESET_PASSWORD throttling | NOT throttled. The findings only call out rate limiting for `savePlayer` and `enterScore`; `updatePlayer` isn't in that list. |
| GT8 | Crypto dependency | **`pycryptodome>=3.20`** as a runtime dep. Matches the findings reference impl; simple ECB-PKCS7 API. (Alternative `cryptography` rejected — heavier, no API benefit for our use.) |
| GT9 | 2FA | Login `code:30100/30200/30201` → terminal `gtreasure:requires_operator_action_<code>` / `gtreasure:requires_google_auth`. Operator clears via the agent UI. |
| GT10 | Plumbing change to `resolve_backend` | **Add a `redis=None` kwarg** alongside the existing `session_store=None`. Each backend constructs its own SessionStore internally when needed. Existing Phase 3 callers unchanged. |

**Accepted limitations** (locked, see §11):
- `ceil(cents/100)` rounding on send (player-favorable on RECHARGE, agent-favorable on REDEEM when
  amount has cents). Golden Treasure accepts integer dollars only (per the verified examples).
- Orphan window if the worker crashes between a successful Golden Treasure write and our
  `cache.set` — same as Gameroom (`_max_tries=1` blocks retry; Laravel reaper marks failed +
  refunds; operator reconciles via the Golden Treasure dashboard).
- `score` integer-only assumption: the findings examples only show integer `score`; cents-precise
  amounts impossible.

## 5. Module layout

```
app/backends/goldentreasure/
  __init__.py
  errors.py       - GTREASURE_STATUS dict + TRANSIENT_CODES + map_response(code, message) -> (reason, terminal)
  crypto.py       - SIGN_SECRET; aes_b64; sign_body; login_aes_key; xtoken_header — pure functions
  passwords.py    - re-export generate_memorable_password from gamevault/passwords.py
  session.py      - CachedSession; SessionStore; In/RedisSessionStore (gtreasure_session: / gtreasure_login: keys)
  client.py       - GoldenTreasureClient: get_token (lock + single re-read), _do_login, _signed_post,
                    _acquire_throttle, call (relogin-on--3/-17/52, optional throttle)
  backend.py      - GoldenTreasureBackend: 6 ops + ceil-dollar sends

# Changed:
app/backends/registry.py    - NON_IDEMPOTENT_DRIVERS += {"goldentreasure"}; redis=None kwarg; goldentreasure branch
app/preflight/checks.py     - missing_goldentreasure_credentials guard (mirrors gameroom)
app/operations/executor.py  - redis=None kwarg threaded to resolve
app/worker/tasks.py         - pass redis=ctx["redis_cache"]
pyproject.toml              - add pycryptodome>=3.20 to runtime deps
tests/conftest.py           - seed goldentreasure game(s) + account(s)
CLAUDE.md / docs/architecture.md / docs/runbook.md
```

## 6. Component designs

### 6.1 `crypto.py` — pure functions (the most critical part to get exactly right)

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
    """AES-128-ECB / PKCS7; base64 of the ciphertext. Key must be exactly 16 chars (ASCII)."""
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plaintext.encode(), 16))).decode()

def sign_body(body: dict, *, stime: int | None = None) -> tuple[str, int]:
    """MD5 sign per findings doc §3.

    Concatenates body values in ascending key order, skipping the `stime` key itself and any
    empty-string / None values. Then appends str(stime) + SECRET and MD5-hex-digests.
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
    """AES key for login credential encryption. MUST equal body.stime."""
    return f"123{stime}abc"

def xtoken_header(session_token: str, x_time_ms: int) -> str:
    """Returns the URL-encoded x-token header value for an authenticated request."""
    return urllib.parse.quote(aes_b64(session_token, f"xtu{x_time_ms}"), safe="")
```

**Test oracles (every one verified in the findings doc):**

| Function | Input | Expected output |
|---|---|---|
| `aes_b64` | `("Test02Gd1WEB", "1231779281935abc")` | `"BXrmQgZgqwThh5+CjFOLFA=="` |
| `aes_b64` | `("Zaeem@1233", "1231779281935abc")` | `"suyUHuDw+rXOKpJvvW7WsA=="` |
| `sign_body` | `({}, stime=1779281921)` | `("1f8aca4093e5002f7481e9d7266b9ceb", 1779281921)` |
| `sign_body` (savePlayer example) | `({token:"q5pIWNNzvi%2B…Tg%3D", account:"apitest01", pwd:"Apitest123", score:"0", name:"", phone:"", tel_area_code:"", remark:""}, stime=1779282067)` | `("2fb7d0fb23cce1d967f095352b5bfa3f", 1779282067)` (verifies empty-skip + sort) |
| `xtoken_header` | `("q5pIWNNzvi%2BpBHDQYDLPnFnckAzxSNbIcEVrTxn%2F%2FTg%3D", 1779281936505)` | URL-encoded form of `"jtSUNgHpXUUdEO+0ksqlndADWqFtaseFwSYCvXZq7l0dwKMicOPagiYFe84+hU6xbU4Xw6kmPKJfwGigrquoJg=="` |

If these 5 unit tests pass, the crypto matches the live server byte-for-byte.

### 6.2 `errors.py`

```python
# Patterns from findings doc §7. terminal_or_transient gates the result cache.
GTREASURE_STATUS: dict[int, str] = {
    8: "account_exists",
    21: "operation_refused",       # over-limit / insufficient (misleading msg "server maintenance")
    52: "no_permission",
    167: "rate_limited",
    1003: "invalid_password_format",
    -3:  "token_invalid",
    -17: "token_expired",
    30100: "system_verify_required",
    30200: "google_auth_bind_required",
    30201: "google_auth_verify_required",
}

# Codes the executor should NOT cache (still surface as failures, but eligible for legitimate retry
# if max_tries > 1; with _max_tries=1 they end up as one-shot failures + reaper).
TRANSIENT_CODES: frozenset[int] = frozenset({167})

def map_response(code: int, message: str) -> tuple[str, bool]:
    """Return (reason_slug, is_terminal)."""
    if code in TRANSIENT_CODES:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", False)
    if code in GTREASURE_STATUS:
        return (f"gtreasure:{GTREASURE_STATUS[code]}", True)
    return (f"gtreasure:code_{code}: {(message or '')[:80]}", True)
```

`-3` / `-17` / `52` are listed here for completeness, but in normal flow they don't reach
`map_response` — `client.call()` intercepts them and does a relogin retry. They only surface if
the retry also fails.

### 6.3 `passwords.py` (one line — re-export)

```python
# app/backends/goldentreasure/passwords.py
# Golden Treasure password rule: 6-16 chars, must combine letters and numbers, may include
# !@#$%^/.,(). The existing alphanumeric generator (e.g. "Tiger4827") satisfies this.
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401
```

### 6.4 `session.py` (duplicated from gameroom)

Same code structure as `app/backends/gameroom/session.py`. Differences:
- Key prefixes: `gtreasure_session:{game_id}` and `gtreasure_login:{game_id}`.

`get_token` in the Golden Treasure client uses this store but doesn't need double-checked-locking
because tokens are concurrent (decision GT6).

### 6.5 `client.py` (the heart of the phase)

```python
class GoldenTreasureClient:
    def __init__(
        self, *,
        base_url: str, username: str, password: str,
        http_client: httpx.AsyncClient,
        session_store: SessionStore,    # gtreasure-flavored
        redis,                          # raw redis client for the throttle
        game_id: int,
        fingerprint: str = "db3bb59096022abb85b4612d53387101",  # any 32-hex; server doesn't validate
    ): ...

    # ---- session ----

    async def get_token(self, *, invalidate: str | None = None) -> str:
        """Return a valid token. Concurrent tokens allowed → no double-check needed."""
        cached = await self._session.get(self._game_id)
        if cached and cached.token != invalidate:
            return cached.token
        async with self._session.login_lock(self._game_id):
            cached = await self._session.get(self._game_id)
            if cached and cached.token != invalidate:
                return cached.token
            token = await self._do_login()
            # No exp returned; pick an arbitrary long TTL (24h) — relogin happens on -3/-17 anyway.
            await self._session.set(
                self._game_id,
                CachedSession(token=token, expires_at=int(time.time()) + 86400),
                ttl_seconds=86400,
            )
            return token

    async def _do_login(self) -> str:
        """POST /api/user/login. No x-token (no session yet). AES-encrypted creds."""
        stime = int(time.time())
        key = login_aes_key(stime)
        body = {
            "username": aes_b64(self._username.strip(), key),
            "password": aes_b64(self._password, key),
            "stime": stime,
            "auth_code": "",
        }
        sign, _ = sign_body(body, stime=stime)
        full = {**body, "sign": sign}
        body_json = await self._post_raw("/api/user/login", full, authenticated=False)
        code = body_json.get("code")
        if code == 20000:
            token = body_json.get("token")
            if not isinstance(token, str) or not token:
                raise TransientBackendError("gtreasure:login_missing_token")
            return token
        if code in (30100, 30200, 30201):
            slug = {30100: "system_verify", 30200: "google_auth_bind", 30201: "google_auth_verify"}[code]
            raise BackendError(f"gtreasure:requires_operator_action_{slug}")
        reason, terminal = map_response(int(code) if isinstance(code, int) else 0, str(body_json.get("message", "")))
        raise (BackendError if terminal else TransientBackendError)(reason)

    # ---- throttle (mutating ops only) ----

    async def _acquire_throttle(self) -> None:
        """SET NX gtreasure_throttle:{game_id} ex=5. Poll if locked, up to 30s."""
        key = f"gtreasure_throttle:{self._game_id}"
        deadline = time.monotonic() + 30.0
        while True:
            if await self._redis.set(key, b"1", nx=True, ex=5):
                return
            if time.monotonic() >= deadline:
                raise TransientBackendError("gtreasure:throttle_wait_timeout")
            await asyncio.sleep(0.5)

    # ---- HTTP + sign + x-token ----

    async def _post_raw(self, path: str, body: dict, *, authenticated: bool) -> dict:
        raw = json.dumps(body, separators=(",", ":"))
        headers = self._cf_headers()
        if authenticated:
            x_time_ms = int(time.time() * 1000)
            token = body.get("token")  # body must already contain a token for auth calls
            headers["x-token"] = xtoken_header(str(token), x_time_ms)
            headers["x-time"] = str(x_time_ms)
        try:
            resp = await self._http.post(f"{self._base_url}{path}", content=raw.encode(), headers=headers)
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

    def _cf_headers(self) -> dict[str, str]:
        return {
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
            "x-fingerprint": self._fingerprint,
        }

    # ---- authenticated call (relogin on -3/-17/52 + optional throttle) ----

    async def call(self, path: str, params: dict, *, throttle: bool = False) -> dict:
        if throttle:
            await self._acquire_throttle()
        token = await self.get_token()
        body = {**params, "token": token}
        sign, stime = sign_body(body)
        full = {**body, "sign": sign, "stime": stime}
        body_json = await self._post_raw(path, full, authenticated=True)
        if body_json.get("code") in (-3, -17, 52):
            # Session dead — relogin transparently, retry once.
            fresh = await self.get_token(invalidate=token)
            body["token"] = fresh
            sign, stime = sign_body(body)
            full = {**body, "sign": sign, "stime": stime}
            body_json = await self._post_raw(path, full, authenticated=True)
            if body_json.get("code") in (-3, -17, 52):
                raise BackendError("gtreasure:auth_failed")
        if body_json.get("code") == 20000:
            return body_json
        reason, terminal = map_response(
            int(body_json.get("code", 0)) if isinstance(body_json.get("code"), int) else 0,
            str(body_json.get("message", "")),
        )
        raise (BackendError if terminal else TransientBackendError)(reason)
```

### 6.6 `backend.py`

```python
def _to_cents(value) -> int: return round(float(value) * 100)
def _to_dollars(cents: int) -> str: return str(math.ceil(cents / 100))


class GoldenTreasureBackend:
    def __init__(self, client: GoldenTreasureClient) -> None:
        self._client = client

    async def agent_balance(self, ctx):
        data = await self._client.call("/api/user/CurScore", {})
        v = data.get("LimitNum")
        if v is None:
            raise BackendError("gtreasure:agent_balance_missing")
        return AgentBalanceResult(agent_balance_cents=_to_cents(v))

    async def read_balance(self, ctx):
        data = await self._client.call(
            "/api/account/getPlayerScore", {"account": ctx.account.username},
        )
        return ReadBalanceResult(balance_cents=_to_cents(data.get("curScore", 0)))

    async def create_account(self, ctx):
        if not ctx.account_username:
            raise BackendError("account_username_required")
        pwd = generate_memorable_password()
        await self._client.call(
            "/api/account/savePlayer",
            {
                "account": ctx.account_username,
                "pwd": pwd, "score": "0",
                "name": "", "phone": "", "tel_area_code": "", "remark": "",
            },
            throttle=True,
        )
        return CreateAccountResult(
            username=ctx.account_username, password=pwd, external_user_id=None,
        )

    async def reset_password(self, ctx):
        pwd = generate_memorable_password()
        await self._client.call(
            "/api/account/updatePlayer",
            {
                "account": ctx.account.username,
                "pwd": pwd, "name": "", "phone": "", "remark": "", "tel_area_code": "",
            },
        )
        return ResetPasswordResult(password=pwd)

    async def recharge(self, ctx, *, amount_cents, bonus_cents, total_credit_cents):
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": ctx.account.username,
                "score": _to_dollars(total_credit_cents),
                "remark": "", "user_type": "player",
            },
            throttle=True,
        )
        return RechargeResult()                         # no balance returned

    async def redeem(self, ctx, *, amount_cents):
        dollars = math.ceil(amount_cents / 100)
        await self._client.call(
            "/api/account/enterScore",
            {
                "account": ctx.account.username,
                "score": str(-dollars),                 # negative = withdraw
                "remark": "", "user_type": "player",
            },
            throttle=True,
        )
        return RedeemResult()
```

### 6.7 `registry.py` changes

```python
NON_IDEMPOTENT_DRIVERS: frozenset[str] = frozenset({"gameroom", "goldentreasure"})

def resolve_backend(
    driver, *, credentials, http_client, settings,
    session_store=None,                                 # Phase 3 — unchanged
    redis=None,                                         # NEW — used by goldentreasure for throttle + own session store
) -> GameBackend:
    ...
    if key == "goldentreasure":
        if not (credentials.backend_url and credentials.backend_username and credentials.backend_password):
            raise BackendError("missing_goldentreasure_credentials")
        if redis is None:
            raise BackendError("missing_redis_client")
        from app.backends.goldentreasure.session import RedisSessionStore as GTSessionStore
        gt_session = GTSessionStore(redis)
        return GoldenTreasureBackend(GoldenTreasureClient(
            base_url=credentials.backend_url,
            username=credentials.backend_username,
            password=credentials.backend_password,
            http_client=http_client,
            session_store=gt_session,
            redis=redis,
            game_id=credentials.game_id,
        ))
    raise BackendError(f"unknown_backend_driver:{driver}")
```

### 6.8 Executor + worker — minimal one-kwarg threads

`execute_operation` adds `redis: object | None = None` kwarg, passed to `resolve(...)`.

`worker/tasks.py`'s `execute_operation_task` passes `redis=ctx["redis_cache"]`. The worker's
`startup` already creates `ctx["redis_cache"]` for Phase 2's result cache — we reuse it.

Phase 3's `session_store=ctx["session_store"]` plumbing stays untouched (gameroom flavored).

## 7. Status / error mapping summary

| Trigger | Reason | Cached? |
|---|---|---|
| `/api/user/login` `code:20000` | success | — |
| `/api/user/login` `code:30100` | `gtreasure:requires_operator_action_system_verify` | terminal |
| `/api/user/login` `code:30200` | `gtreasure:requires_operator_action_google_auth_bind` | terminal |
| `/api/user/login` `code:30201` | `gtreasure:requires_operator_action_google_auth_verify` | terminal |
| `/api/user/login` other non-20000 | `gtreasure:code_<n>: <msg>` | terminal |
| Any auth call `code:-3` / `-17` / `52` after one relogin retry | `gtreasure:auth_failed` | terminal |
| Any call `code:8` (account exists) | `gtreasure:account_exists` | terminal |
| Any call `code:21` (operation refused) | `gtreasure:operation_refused` | terminal |
| Any call `code:1003` (password format) | `gtreasure:invalid_password_format` | terminal |
| Any call `code:167` (rate limit) | `gtreasure:rate_limited` | **transient** (not cached) |
| HTTP 5xx / timeout / bad JSON | `gtreasure:...` | transient |
| `gtreasure:throttle_wait_timeout` (we couldn't get the throttle in 30s) | (transient) | transient |

With `_max_tries=1`, transient outcomes still effectively fail the op (Laravel reaper handles it).
The transient classification matters so we don't cache a recoverable condition.

## 8. Error handling matrix (operational)

| Situation | Behavior |
|---|---|
| First op on a fresh game_id | Lazy login under lock; cache token (24h TTL); proceed |
| Cached token in Redis | All ops use it directly; no login (concurrent tokens allowed) |
| Mutating op arrives within 5s of a previous mutating op (same game) | Throttle gate polls until SETNX succeeds (~5s wait), then proceeds |
| 30s throttle wait elapsed | `gtreasure:throttle_wait_timeout` (transient) — Laravel reaper handles |
| `code:-3` / `-17` / `52` on a call | Drop our copy → `get_token(invalidate=dead)` → if cache still holds dead, relogin under lock; else use cached fresh → retry once |
| Second `-3`/`-17`/`52` | `gtreasure:auth_failed` (terminal); cached so replays short-circuit |
| Login `code:30100`/`30200`/`30201` | Terminal `gtreasure:requires_operator_action_*`; operator clears via UI |
| Worker crash mid-money op | No arq retry (`_max_tries=1`); Laravel reaper at 10 min → failed + refunded; operator reconciles any orphan via Golden Treasure dashboard |
| Cloudflare 403 (missing browser headers) | Our `_cf_headers()` sends the full set; if it still fails we'd see HTTP 403 → `gtreasure:http_403` (terminal — config bug worth surfacing) |

## 9. Testing

- **Crypto unit tests (`tests/unit/test_goldentreasure_crypto.py`):** the 5 doc-verified oracles in
  §6.1 must pass exactly. Plus `sign_body` skipping empty strings and `None`; `sign_body` excluding
  the `stime` key itself.
- **Errors unit tests:** each documented code → expected `(reason, terminal)`. Unknown code → truncate msg ≤80.
- **Passwords unit test:** the re-export works (the password matches the GameVault regex; also
  passes the 6-16 letters+digits rule).
- **Session unit tests:** mirror gameroom's session tests (in-memory + fakeredis SET/GET/clear,
  login lock SET NX serialization, lock auto-expiry). Plus a key-prefix test to lock in the
  `gtreasure_session:` / `gtreasure_login:` namespaces.
- **Client unit tests:**
  - `_do_login` posts AES-encrypted creds + the matching stime in the body + sign; no Bearer; no
    `x-token`. Success returns token; 30100 → terminal; 5xx → transient.
  - `get_token` returns cached; logs in when cache missing; honors `invalidate` only when cache
    still holds that token.
  - `call` builds `x-token`/`x-time` headers from current `time.time()*1000`; sends compact JSON;
    `code:-3` triggers a relogin + retry; second `-3` → `gtreasure:auth_failed`; `code:20000`
    returns the envelope; `code:21` → terminal `operation_refused`; `code:167` → transient
    `rate_limited`; HTTP 5xx → transient.
  - **Throttle:** mutating call acquires the gate (assert `gtreasure_throttle:<id>` key exists with
    TTL≤5); non-mutating call does NOT touch the throttle key; concurrent mutating calls serialize
    via SETNX (use `asyncio.gather` with two clients + fakeredis); throttle wait timeout raises
    `gtreasure:throttle_wait_timeout`.
- **Backend unit tests:** each of the 6 ops via respx — correct path/body/`score` units, correct
  result model, `external_user_id=None` on CREATE_ACCOUNT, REDEEM sends negative `score`,
  RechargeResult/RedeemResult have no balance, AGENT_BALANCE missing `LimitNum` → terminal
  `gtreasure:agent_balance_missing`.
- **Registry unit tests:** `'goldentreasure'` driver routes to `GoldenTreasureBackend`; missing
  creds → terminal `missing_goldentreasure_credentials`; missing redis → terminal
  `missing_redis_client`; `NON_IDEMPOTENT_DRIVERS` now contains `goldentreasure`.
- **Integration tests:** routing via `backend_driver='goldentreasure'`; terminal-failure cache
  replay (a code:8 doesn't re-call backend on a second run); transient (code:167) NOT cached;
  session is reused across ops (one login for two AGENT_BALANCE ops); mutating ops on the same
  game serialize via the throttle gate.
- **API endpoint test:** a `'goldentreasure'` game's enqueue carries `_max_tries=1` (existing test
  pattern auto-extends because `NON_IDEMPOTENT_DRIVERS` now contains it).
- **Update existing tests** for the new `redis=None` kwarg on `resolve_backend`,
  `execute_operation`, and `execute_operation_task` (defaults to `None`; existing callers untouched).

## 10. Laravel-side dependencies

**Nothing new to ship.** Golden Treasure uses the existing reverse-engineered credential columns
(`backend_url`, `backend_username`, `backend_password`) and the existing `backend_driver` column.

Operator setup for a Golden Treasure game:
1. In Filament: add the game with `backend_driver='goldentreasure'`, `backend_url=https://agent.goldentreasure.mobi`,
   `backend_username=<agent login>`, `backend_password=<agent password>`. If `backend_driver` is
   enum-restricted on the Laravel side, add `'goldentreasure'` to the enum (one-line migration).
2. Trigger any op from Laravel. First op lazily logs in; subsequent ops share the cached token
   from Redis. Mutating ops self-serialize at ≥5s spacing.
3. **No IP allowlist** required (Golden Treasure doesn't restrict by IP — it's Cloudflare-fronted).
   The `Origin`/`Referer` Cloudflare-friendly header set we send is what gets us past CF.

## 11. Deferred / accepted limitations

- **Orphan-on-crash window** during RECHARGE/REDEEM: same as Gameroom. `_max_tries=1` + no
  `order_id` → a worker crash *after* a successful Golden Treasure write but *before* our
  `cache.set` leaves an in-game change Laravel has already refunded (reaper). Operator
  reconciles via the Golden Treasure dashboard.
- **Whole-dollar `ceil` rounding** on send. Golden Treasure's verified examples only show integer
  `score`; we don't risk cents-precision experimentation. Player-favorable on RECHARGE,
  agent-favorable on REDEEM when amount has cents.
- **No `order_id` / idempotency.** Documented. Mitigated by `_max_tries=1` + result cache for
  terminal outcomes.
- **2FA not supported.** Login `code:30100`/`30200`/`30201` → terminal. The agent must not have
  2FA enabled for our automation; if it ever does, ops fail with a clear reason and the operator
  intervenes via the agent UI.
- **No `score`-decimal experimentation.** If cent-precise amounts ever matter, we'd need to verify
  the server's behavior empirically (the findings doc didn't try).
- **`getPlayerList`/`getPlayerInfo`/`search/Account`** not implemented (not part of our six
  contract ops).

## 12. Resolved review items

(None pending — all design questions resolved during brainstorming; spec self-review done inline.)
