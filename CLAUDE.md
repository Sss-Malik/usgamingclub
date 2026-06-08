# CLAUDE.md — Casino Game Service (Python)

## What this is
Worker service driving external game backends for the Laravel `casino-app`. Receives a signed
`POST /operations`, acks `202`, runs work on an arq/Redis worker, and reports via a signed webhook.
Laravel owns all money/account writes; this service reads the shared MySQL only.

## Golden rules
- **Never write** money/account tables. Read-only DB access (`games`, `game_accounts`, `game_operations`).
- **Never log secrets**: `backend_password`, `api_secret_key`, `binding_key`, account/result `password`.
  Logging redaction is in `app/logging.py` (`SECRET_KEYS`); keep it current.
- HMAC must be byte-exact over the raw body; re-sign on every webhook retry (300s replay window).
- Always return `202` for a correlatable trigger; report real failures via the webhook (`status:"failed"`).
  Reserve non-`202` for bad signatures (401) and uncorrelatable bodies (400).
- Backend selection comes from `games.backend_driver` (read-only): `mock` | `gamevault` | `juwa` |
  `juwa2` | `gameroom`. New backends add a module + a `resolve_backend` branch; sibling games on an
  existing provider (e.g. `juwa`/`juwa2` share GameVault's API) are added as an alias in the registry.
- Non-idempotent drivers (no server-side `order_id` dedupe — currently `gameroom`) are listed in
  `NON_IDEMPOTENT_DRIVERS`; the `/operations` endpoint passes arq `_max_tries=1` for these so a
  worker crash can't double-apply funds. Reaper at Laravel's 10-min mark handles the orphan.
- Gameroom: JWT bearer auth (~6h sessions) cached in Redis via `app/backends/gameroom/session.py`.
  Re-login on `status_code:410` uses **double-checked locking** (`get_token(invalidate=...)`) to
  stay safe under Gameroom's single-session-per-agent enforcement.
- GameVault: amounts are sent as whole dollars via `ceil(cents/100)`; balances read as decimal dollars `*100`.
  Pass `idempotency_key` as `order_id` (GameVault dedupes). Generated passwords are memorable (word+digits).
- Cache terminal outcomes (success + business failures) in the result cache; never cache transient errors
  (timeout/5xx/codes 12,14,21) so re-runs retry safely.

## Where things live
- Wire scheme: `app/security/hmac.py` · Schemas: `app/schemas/` · Backends: `app/backends/`
- Orchestration: `app/operations/executor.py` + `dispatch.py` · Worker: `app/worker/`
- API: `app/api/` · Config: `app/config.py`
- GameVault backend: `app/backends/gamevault/` (client, backend, errors, passwords). Result cache: `app/operations/result_cache.py`.
- Gameroom backend: `app/backends/gameroom/` (client, backend, errors, passwords, session).

## Workflow
- TDD: write the failing test first, then the minimal code (see `docs/superpowers/plans/`).
- Per feature: build → user tests manually → commit on approval.
- Integrating a new game backend: request the API-findings doc, confirm it covers success + error
  responses, then add a `GameBackend` module + registry entry.

## Commands
`make install` · `make test` · `make lint` · `make type` · `make up` · `make ping`

## Specs & plans
`docs/superpowers/specs/` (design) · `docs/superpowers/plans/` (implementation).
