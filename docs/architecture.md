# Architecture

## Components
- **api** (FastAPI/uvicorn) — verifies HMAC, parses/correlates, dedupes (arq job_id), enqueues, acks `202`.
- **worker** (arq) — runs `execute_operation`: pre-flight → backend → result validation → signed webhook.
- **redis** — arq queue (and, in later phases, session/rate-limit state).
- **MySQL** — shared with Laravel; read-only here.

## Request lifecycle
1. `POST /operations` (signed) → `verify_signature` → parse → `enqueue_job(job_id=idempotency_key)` → `202`.
2. Worker: `build_context` (games + game_accounts) → `get_backend` → `dispatch` by type → result model.
3. `deliver_webhook` (signed, backoff) → Laravel `{APP_URL}/webhooks/games/operation` until `200` / budget.

## Key modules
- `app/security/hmac.py` — the §1 signing scheme (raw-body exact).
- `app/schemas/` — §4 request union, §5 result models.
- `app/backends/` — `GameBackend` protocol, registry, MockBackend. New games add a module + registry entry.
- `app/operations/executor.py` — orchestration and webhook reporting.

## Adding a real backend (Phase 2+)
Implement `GameBackend` in `app/backends/<game>/backend.py`, map it in `app/backends/registry.py`,
read creds from `BackendContext.credentials`. Implement the Redis backend-result cache for
non-idempotent ops (RECHARGE/REDEEM) so a worker restart cannot double-apply.

## Backends & drivers
`games.backend_driver` selects the backend per game (`mock` | `gamevault` | `juwa` | `juwa2`).
`resolve_backend` builds the backend from the game's credentials + the shared httpx client. GameVault
(`app/backends/gamevault/`) is a synchronous official API: MD5 token auth
(`md5(agent_id:timestamp:secret_key)`), multipart POST, a `{code,msg,data}` envelope, and a status-code
dictionary. Money units: send whole dollars (`ceil(cents/100)`), read decimal dollars (`*100` → cents).
**Juwa and Juwa2 are the same provider** as GameVault and share the entire wire protocol; their
drivers route to `GameVaultBackend` with each game's own per-row `api_base_url`/`api_agent_id`/
`api_secret_key`. New sibling games from the same provider are added as a one-line alias in
`app/backends/registry.py`.

## Result cache (money-op safety)
`app/operations/result_cache.py` stores each operation's terminal outcome (success or business failure) in
Redis keyed by `idempotency_key` (TTL `result_cache_ttl_seconds`). The executor replays a cached outcome
without re-calling the backend. Transient failures are NOT cached, so an arq re-run retries the backend;
GameVault's `order_id` dedupe prevents double money movement.

## Webhook diagnostics
Every webhook (success or failure) may carry a top-level `op_id` echo and an optional `diagnostics`
object — a purely observational channel for Arcadia's monitoring module; it never gates a
wallet/credential effect. Assembled in `app/webhook/payload.py::assemble_diagnostics`, populated by
`app/backends/diagnostics.py::DiagnosticsRecorder` (threaded through `BackendContext` into every
client) and by `app/operations/executor.py` at each `except` branch. Full contract:
`docs/superpowers/specs/2026-07-14-webhook-diagnostics-design.md`.

**Shape:** `op_id`, `idempotency_key`, `attempt` (arq `job_try`), `cache_hit` (op-level idempotency
replay only), `duration_ms`, `steps[]` (`name`, `phase`, `http`, `ok`, `ms`, optional
`skipped`/`external`); failure-only `failure_kind`, `reason` (the real internal reason, never the
generic player-facing `message`), `provider` (`http_status`/`code`/`message`, the whole block omitted
when nothing truthful exists); success-side `external_user_id`, `balance_before` (gameroom
recharge/redeem snapshot only), `balance_after` (gamevault + gameroom money ops only — yolo/aspnet
money-op success responses carry no balance, so it stays absent there; every other backend's balance
flows only through the existing `user_data.balance` on `read`, never through diagnostics).

**`failure_kind` taxonomy** — set by which executor branch fired: `retry_blocked` (non-idempotent
driver re-run blocked before any provider call), `preflight` (DB lookup or backend-resolution config
error, no provider call made), `transient` (`TransientBackendError` — timeout/5xx/transient code),
`backend` (`BackendError` from the backend call), `invalid_result` (backend returned a malformed
success payload that failed schema validation), `unexpected` (bare `Exception`).

**`session_reuse` vs `cache_hit`:** two independent axes, never conflated. `session_reuse`
(`hit|fresh|relogin|null`) comes from a session-holding backend's own token/cookie getter (gameroom,
goldentreasure, aspnet, ultrapanda/vblink); it stays `null` on gamevault/mock, which have no session
concept — never `false`. `cache_hit` is strictly the op-level idempotency replay in
`app/operations/result_cache.py`: the executor answered from the cached terminal outcome without
calling the backend at all. A cache hit always reports `steps: []` and `session_reuse: null` —
honest, since no HTTP happened on the replay.

**Omit, don't invent:** the governing rule — a confidently-wrong diagnostic is worse than a missing
one. Fields are absent (never guessed) whenever a backend cannot populate them truthfully:
`provider_txn_id` does not exist anywhere in the payload (no backend returns one, and it is never
synthesized from `idempotency_key`, a CSRF token, or a resolved player id); `provider.message` is
absent for ultrapanda/vblink (the vpower API has no message field); `provider.code` is absent for
yolo and for aspnet business failures (both only classify free-text into an internal slug — aspnet's
one genuine provider code is the login `errtype`, present only on login failures).

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
peeks the game's driver via `GamesRepository.get_driver(game_id)` and **embeds `_max_tries=1` inside
the enqueued payload** (arq has no `_max_tries` kwarg on `enqueue_job`). On a retry, the worker reads
`ctx["job_try"]` against `payload["_max_tries"]` and passes `retry_blocked=True` to the executor,
which delivers a `retry_blocked` failure webhook **without calling the backend**. Laravel finalizes
in seconds; the 10-min reaper is only a secondary safety net. The operator reconciles any in-game
balance change manually via Gameroom's dashboard if the prior attempt had partially applied.

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
embeds `_max_tries=1` in the payload; the worker short-circuits the retry with a `retry_blocked`
failure webhook so Laravel finalizes in seconds (reaper as fallback). Operator reconciles any in-game
balance change via the agent UI if the prior attempt had partially applied.
