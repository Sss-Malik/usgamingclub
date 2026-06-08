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
peeks the game's driver via `GamesRepository.get_driver(game_id)` and passes arq `_max_tries=1` for
non-idempotent drivers, so a worker crash mid-money-op cannot retry and double-apply. Laravel's
10-min reaper marks the op failed + refunds the wallet; the operator reconciles any in-game balance
change manually via Gameroom's dashboard.
