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
