# Python Game Service — Design Spec (Phase 1: Walking Skeleton)

- **Status:** Approved (design) — pending spec review before plan
- **Date:** 2026-06-04
- **Owner:** saud
- **Authoritative wire contract:** `/Applications/development/laravel/casino-app/docs/integrations/python-game-service-api-contract.md`
- **This repo:** `/Applications/development/python/casino-app-automation`

---

## 1. Context & purpose

We run a sweepstakes-style frontend (Laravel `casino-app`) that offers externally hosted games.
Game operations (create account, recharge, redeem, reset password, read balance, read agent
balance) execute against per-game **backends**. Some backends expose official HTTP APIs; others were
reverse-engineered from observed network calls (these may require a captcha at login and return a
persistent session we must store). Either way, every backend is driven over standard HTTP.

Laravel is the **system of record / wallet** and owns the full request → validation → dispatch →
webhook → side-effect cycle. This project is the **Python game service**: the worker that receives a
signed trigger, drives the real game backend, and reports the outcome back via a signed webhook.

This spec covers **Phase 1 — the walking skeleton**: prove the entire control plane end-to-end using
a deterministic **MockBackend**, with no real game integration, captcha, or session persistence yet.

## 2. Goals & non-goals (Phase 1)

**Goals**
- Correct, byte-exact HMAC in both directions (verify inbound trigger, sign outbound webhook).
- `POST /operations` that verifies → validates → dedupes → enqueues → acks `202` fast (< 15s).
- Redis-backed async worker (`arq`) that executes operations off the request path.
- Read-only DB access layer for `games`, `game_accounts`, `game_operations`.
- A pre-flight DB-check layer and a post-flight side-effect seam (the latter a no-op placeholder).
- A `GameBackend` abstraction + registry, with a single `MockBackend` implementation.
- A robust signed webhook client honoring the contract's retry/stop rules.
- Structured (JSON) logs that trace an operation across its lifecycle by `idempotency_key`.
- Docker setup (api + worker + redis) for local dev and production; `.env.example`.
- Unit + integration tests; documentation (README, architecture, runbook); CLAUDE.md.

**Non-goals (deferred to later phases)**
- Any real game backend integration (Phase 2).
- Persistent backend sessions + AntiCaptcha captcha solving (Phase 3).
- Per-backend rate limiting, Prometheus metrics (later).
- The backend-result idempotency **cache** (the *seam* is built in Phase 1; the cache is implemented
  with the first real money backend in Phase 2).
- Full `AGENT_BALANCE` exercise (wire shape is final; Laravel side still pending per contract D1 —
  Python handler is built but cannot be fully driven until Laravel ships its half).

## 3. Phased roadmap

| Phase | Goal | Proves |
|---|---|---|
| **1 — Walking skeleton** *(this spec)* | Structure, config, logging, HMAC, `/operations`→202, arq worker, read-only DB layer, pre-flight layer, `GameBackend` + MockBackend, webhook client, Docker, tests, docs | The whole request → ack → webhook cycle end-to-end |
| 2 — First official-API backend | Integrate a real backend with an official API; implement backend-result idempotency cache | Real game integration + money-op safety |
| 3 — Session + captcha | Persistent session store (Redis) + AntiCaptcha solver + first reverse-engineered backend | The hard path |
| 4+ — Scale & harden | Remaining backends, rate limiting, metrics, ops polish | Production readiness |

Each phase has its own spec → plan → implement → manual test → commit cycle.

## 4. Architecture & container topology

The API process does nothing slow: it verifies, validates, dedupes, enqueues, and acks `202`. The
**worker** owns all game-backend work and webhook delivery. **MySQL is the existing host DB** (shared
with Laravel); we do not manage or migrate it.

```
                 signed POST /operations                 ┌─────────────────────────────┐
   Laravel  ───────────────────────────────────────────▶ │ api (FastAPI/uvicorn :8001) │
   (host)   ◀───────────────  202 Accepted ───────────── │  verify→validate→dedupe→    │
      ▲                                                    │  enqueue→202                │
      │                                                    └──────────────┬──────────────┘
      │                                        enqueue job (job_id = idempotency_key)
      │                                                                    ▼
      │                                                          ┌──────────────────┐
      │     signed POST /webhooks/games/operation                │ redis (queue +   │
      │  ◀───────────────────────────────────────────┐          │ session/captcha  │
      │                                               │          │ + rate-limit)    │
      │                                       ┌───────┴──────────┐           │ pull job
      │  reads (read-only): games,            │ worker (arq)     │◀──────────┘
      └───── game_accounts, game_operations ──│ preflight→backend│
             via shared MySQL ────────────────│ →validate→webhook│
                                              └──────────────────┘
```

**Division of responsibility (locked by contract §0):** Laravel is the sole writer of all
money/account state; Python is a worker that **reads anything, writes nothing money**, and reports
outcomes via the signed webhook only. Python must never write the money/account tables and never log
secret columns.

## 5. Tech stack (decided)

| Concern | Choice | Rationale |
|---|---|---|
| HTTP framework | **FastAPI + uvicorn** | Async, typed, great fit for concurrent outbound calls |
| HTTP client | **httpx (async)** | Backend calls + webhook delivery |
| Validation | **Pydantic v2** | Validate §4 request payloads and §5 result payloads |
| DB access | **SQLAlchemy 2.0 (async) + asyncmy** | Read-only; asyncmy has manylinux wheels (clean in Docker) |
| Async queue | **arq** (Redis) | Async-native, lightweight; `job_id` dedupe; better fit than Celery for httpx/async |
| Cache/queue/session store | **Redis** | Queue, dedupe, and (later) session + rate-limit state |
| Logging | **structlog** (JSON) | Lifecycle tracing bound by `idempotency_key` |
| Tests | **pytest + respx** | respx fakes the Laravel webhook receiver / backend HTTP |
| Lint/type | **ruff + mypy** | Quality gates (CI later) |
| Packaging | **pyproject.toml** (uv or pip-tools) | Reproducible builds |

**Decisions made on the user's behalf (approved):** `arq` over Celery; `asyncmy` as the DB driver;
add `/health` + `/ready` endpoints (not contract-required, used for Docker healthchecks).

## 6. Project structure

```
casino-app-automation/
├── app/
│   ├── main.py                  # FastAPI factory, lifespan (db/redis pools), route mount
│   ├── config.py                # Pydantic Settings (env-driven)
│   ├── logging.py               # structlog JSON; bind idempotency_key/type/game_id/phase
│   ├── security/hmac.py         # sign() / verify() — §1 scheme, raw-body exact
│   ├── api/
│   │   ├── deps.py              # HMAC verify dependency (reads RAW body)
│   │   ├── operations.py        # POST /operations
│   │   └── health.py            # GET /health, GET /ready
│   ├── schemas/
│   │   ├── operations.py        # §4 request models, discriminated union on `type`
│   │   └── results.py           # §5 result models (validate before sending)
│   ├── db/
│   │   ├── engine.py            # async engine + session factory (read-only)
│   │   ├── models.py            # Game, GameAccount, GameOperation (read-only mappings)
│   │   └── repositories.py      # GamesRepo, GameAccountsRepo, GameOperationsRepo
│   ├── preflight/checks.py      # load creds + account, dedupe; typed pass/fail
│   ├── postflight/effects.py    # no-op seam (side effects are Laravel's)
│   ├── backends/
│   │   ├── base.py              # GameBackend protocol (the 6 operations)
│   │   ├── registry.py          # game_id/type → backend resolver
│   │   ├── context.py           # BackendContext (creds + account; secrets never logged)
│   │   └── mock/backend.py      # Phase-1 MockBackend: deterministic, contract-valid results
│   ├── webhook/client.py        # signed POST + backoff retry honoring §3 stop rules
│   ├── operations/executor.py   # orchestrator: preflight→backend→validate→webhook (pure/testable)
│   └── worker/
│       ├── settings.py          # arq WorkerSettings, Redis config, job timeouts
│       └── tasks.py             # thin arq job wrapper around executor
├── tests/
│   ├── unit/                    # hmac, schemas, preflight, mock backend, webhook retry
│   ├── integration/             # /operations 202, full executor loop (respx), dedupe
│   └── conftest.py
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml       # prod-ish: api, worker, redis
│   └── docker-compose.dev.yml   # local dev (hot reload)
├── docs/
│   ├── architecture.md
│   ├── runbook.md
│   └── superpowers/specs/...    # this file
├── .env.example  .gitignore  .dockerignore
├── pyproject.toml  README.md  CLAUDE.md
└── Makefile                     # make ping / test / up / lint shortcuts
```

**Design principle:** each unit has one responsibility and a clear interface. A new game integration
only implements `GameBackend` and adds a registry entry; `executor.py` never knows which backend it
called.

## 7. Wire protocol Python must honor (from the contract)

### 7.1 HMAC (identical both directions)
- Shared secret: `PYTHON_SIGNING_SECRET` (env, same value both sides).
- Headers: `X-Timestamp` (unix epoch **seconds** as string), `X-Signature`, `Content-Type: application/json`.
- Signing string: `timestamp + "." + rawBody` (literal dot).
- `X-Signature = "sha256=" + lowercase_hex(HMAC_SHA256(secret, signingString))`.
- **Sign/verify over the exact raw bytes on the wire** — never re-serialize. Verify uses the raw
  request body; sign hashes the exact serialized bytes we send.
- Replay window: 300s; reject if `abs(now - ts) > 300`. Compare with `hmac.compare_digest`.
- Validate signing first against `POST {APP_URL}/webhooks/_ping` → expect `200 {"ok":true}`.

### 7.2 Inbound trigger — `POST /operations`
- Body = the operation's `request_payload` (§8 shapes).
- **Must ack HTTP `202`** (empty body fine) within 5s connect / 15s total. Anything else makes
  Laravel mark the op `FAILED (dispatch_failed)` and refund — so reserve non-202 for genuine auth
  failure.
- At-most-once (Laravel never retries). Still **dedupe defensively** by `idempotency_key`.
- On receipt: verify signature → dedupe → enqueue → `202`. Game work runs async afterward.

### 7.3 Outbound webhook — `POST {APP_URL}/webhooks/games/operation`
- Signed (§7.1). Body:
  - success: `{ "idempotency_key": "...", "status": "succeeded", "result": { ...§9 } }`
  - failure: `{ "idempotency_key": "...", "status": "failed", "reason": "..." }` (reason ≤ 255 chars)
- Laravel responses & Python action:
  - `200 {"ok":true}` → done, stop.
  - `401` → **signing bug**; fix, do not blind-retry.
  - `422` → `idempotency_key` missing/empty; **payload bug**, do not retry.
  - `404` → no op for that key; likely transient — retry a few times then alert.
- **Retry** (exponential backoff + jitter) on conn-error / `5xx` / `404` until `200`; give up after
  ~10 min (Laravel's reaper has failed the op by then; late callbacks are harmless no-ops).

## 8. Read schema (read-only; filter `deleted_at IS NULL` where present)

### `games` (by `game_id`) — per-game backend credentials
`id, name, active, is_hot, thumbnail_path, backend_url, login_page_url, game_url,
backend_username, backend_password, api_base_url, api_agent_id, api_secret_key, binding_key,
created_at, updated_at, deleted_at`

- **Official-API set:** `api_base_url, api_agent_id, api_secret_key, binding_key`.
- **Reverse-engineered set:** `backend_url, login_page_url, backend_username, backend_password`.
- A game uses whichever set its backend module needs. **Secret columns are never logged.**

### `game_accounts` (by `game_account_id`) — who to act on at the backend
`id, user_id, game_id, username, password, external_user_id, balance_cents, balance_synced_at,
last_recharge_cents, last_recharge_bonus_cents, last_recharge_at, created_at, updated_at, deleted_at`
- The trigger only carries the internal `game_account_id`; Python reads `username` /
  `external_user_id` / `password` here to identify the player on the backend.
- Unique `(user_id, game_id)`.

### `game_operations` (by `idempotency_key`) — defensive dedupe & context
`id, user_id, game_id, game_account_id, wallet_transaction_id, type, status, idempotency_key (unique),
request_payload (json), result_payload (json), failure_reason, dispatched_at, completed_at,
expires_at, created_at, updated_at`
- Status lifecycle is Laravel's (`PENDING → SUCCEEDED | FAILED`). Python reads, never writes.

## 9. Operation types — request → backend method → result

Common request keys: `idempotency_key` (uuid), `type`, `user_id`, `game_id`, `game_account_id`.
Money is integer **cents**.

| `type` | Extra request keys | `game_account_id` | Backend method | Result (§5) — Laravel validates |
|---|---|---|---|---|
| `CREATE_ACCOUNT` | — | `null` | `create_account` | `username` (req, non-empty), `password` (req, non-empty), `external_user_id` (non-empty or omit/null) |
| `READ_BALANCE` | — | set | `read_balance` | `balance_cents` (req, int ≥ 0) |
| `RESET_PASSWORD` | — | set | `reset_password` | `password` (req, non-empty) |
| `RECHARGE` | `amount_cents`, `bonus_cents`, `total_credit_cents` | set | `recharge` | `balance_cents` (optional, int ≥ 0) |
| `REDEEM` | `amount_cents` | set | `redeem` | `balance_cents` (optional, int ≥ 0) |
| `AGENT_BALANCE` | *(no `user_id`/`game_account_id`)*, `game_id` only | — | `agent_balance` | `agent_balance_cents` (req, int ≥ 0) |

**Amount semantics (locked):**
- RECHARGE: `amount_cents` = what the player paid; `bonus_cents` = incentive;
  `total_credit_cents = amount_cents + bonus_cents`. **Python credits `total_credit_cents`** into the
  game account. (Normalize if a backend applies its own promo so net credit = `total_credit_cents`.)
- REDEEM: `amount_cents` = the *actual pull* Python removes from the game account. Wallet math is
  entirely Laravel's.
- `password` (CREATE_ACCOUNT/RESET_PASSWORD): set on the account by Laravel, then stripped — Python
  must **never** persist or log it.

**MockBackend (Phase 1)** returns deterministic, contract-valid results per type (e.g.
`username = f"mock_{user_id}_{game_id}"`, fixed `balance_cents`, generated `external_user_id`), plus a
configurable failure mode to exercise the `status:"failed"` path.

## 10. Operation lifecycle & robustness

1. **Trigger received** → verify HMAC over raw body. Bad/expired/missing signature → `401` (the only
   non-202 path). Signature OK → continue.
2. **Validate** body against the typed `type` union. If signature-valid but schema-invalid →
   **ack `202`, then immediately webhook `status:"failed"`** (graceful: Laravel records a real
   failure rather than a `dispatch_failed`). *(See Decision D5 — to confirm in spec review.)*
3. **Dedupe** → enqueue arq job with `job_id = idempotency_key` (duplicate trigger within TTL is a
   no-op by construction). Defensive cross-check against `game_operations`.
4. **`202`** returned immediately.
5. **Worker (`execute_operation`)**:
   a. **Pre-flight:** load game creds (`games`), load account (`game_accounts`) for account-scoped
      types; on missing creds/account/inconsistency, fail fast with a structured reason → webhook
      `status:"failed"`.
   b. **Resolve backend** via registry; call the type's method.
   c. **Validate result** against §9 schema (so we never send Laravel something it'll reject).
   d. **Deliver webhook** with exponential backoff + jitter honoring §7.3 stop rules until `200` or
      the ~10-min budget elapses.
6. **Post-flight:** no-op seam (`postflight/effects.py`) — side effects are Laravel's.

**Money-op safety (seam now, cache later):** for non-idempotent ops (RECHARGE/REDEEM) a worker crash
mid-webhook must not re-run the backend call. `executor.py` exposes a seam to cache the backend
result in Redis keyed by `idempotency_key` (→ at-most-once backend exec + at-least-once webhook).
Phase 1's MockBackend is idempotent, so the **cache is implemented in Phase 2** with the first real
money backend; Phase 1 builds the seam only.

## 11. Error handling matrix

| Situation | Python behavior |
|---|---|
| Bad/expired/missing inbound signature | `401` (matches Laravel's `VerifyHmacSignature` codes) |
| Signature OK, schema-invalid body | `202`, then webhook `status:"failed"` (reason = validation summary) |
| Duplicate trigger (same `idempotency_key`) | `202`, no duplicate job (arq job_id dedupe) |
| Missing game creds / account / inconsistency (pre-flight) | webhook `status:"failed"` (structured reason) |
| Backend call error/timeout | webhook `status:"failed"` (reason) |
| Backend returns result failing §9 validation | webhook `status:"failed"` (`invalid_result_payload: ...`) |
| Webhook `200` | stop (success path) |
| Webhook `401`/`422` | stop + log as **our bug** (no blind retry) |
| Webhook `5xx`/`404`/conn-error | retry w/ backoff until `200` or ~10-min budget, then give up |

## 12. Logging

- **structlog** JSON to stdout (Docker-friendly).
- Every log line in an operation's lifecycle is bound with: `idempotency_key`, `type`, `game_id`,
  `user_id` (when present), and `phase` ∈ {`received`, `validated`, `enqueued`, `preflight`,
  `backend_call`, `backend_result`, `webhook_attempt`, `webhook_delivered`, `failed`}.
- A per-request `request_id` for the inbound `/operations` call.
- **Never log** secret columns (`backend_password`, `api_secret_key`, `binding_key`, etc.) or any
  account/result `password`. A redaction helper enforces this centrally.

## 13. Configuration (env)

| Var | Meaning |
|---|---|
| `PYTHON_SIGNING_SECRET` | Shared HMAC secret (must match Laravel) |
| `APP_URL` | Laravel base URL; webhook → `{APP_URL}/webhooks/games/operation`, ping → `{APP_URL}/webhooks/_ping` |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | Shared MySQL (read-only user recommended) |
| `REDIS_URL` | arq queue + (later) session/rate-limit store |
| `LOG_LEVEL` / `ENV` | Logging + environment toggles |
| `WEBHOOK_MAX_BUDGET_SECONDS` | Webhook retry deadline (default ~600) |
| `WEBHOOK_BACKOFF_*` | Backoff base / max / jitter knobs |
| `ANTICAPTCHA_API_KEY` | Phase 3 (present in `.env.example`, unused in Phase 1) |

Service listens on `:8001` (contract dev default `http://127.0.0.1:8001`).

## 14. Phase-1 build list

1. Config (`config.py`) + structured logging (`logging.py`).
2. HMAC module (`security/hmac.py`) + the `/webhooks/_ping` self-check (`make ping`).
3. FastAPI app: `POST /operations`, `GET /health`, `GET /ready`; HMAC verify dependency.
4. Pydantic schemas: §4 request union + §5 result models.
5. Read-only DB layer: engine, models, repositories (games/accounts/operations).
6. Pre-flight checks layer.
7. `GameBackend` protocol + registry + `BackendContext` + `MockBackend`.
8. Webhook client with backoff/stop rules.
9. `operations/executor.py` orchestrator (pure, DI'd) + post-flight no-op seam.
10. arq worker (`worker/settings.py`, `worker/tasks.py`).
11. Docker: Dockerfile, `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example`, Makefile.
12. Tests (unit + integration).
13. Docs: README, `architecture.md`, `runbook.md`, CLAUDE.md.

## 15. Phase-1 acceptance — manual end-to-end test (real Laravel ↔ our Python)

1. `docker compose -f docker/docker-compose.dev.yml up` → api + worker + redis. Set
   `PYTHON_SIGNING_SECRET` to match Laravel; `APP_URL` → local Laravel.
2. **Signing self-check:** `make ping` (and a startup check) → `POST {APP_URL}/webhooks/_ping` →
   expect `200 {"ok":true}`.
3. Point Laravel `PYTHON_BASE_URL` at our service.
4. Trigger a real operation from Laravel (CREATE_ACCOUNT / READ_BALANCE): Laravel triggers
   `/operations` → we `202` → worker runs MockBackend → we sign+POST the webhook → **Laravel
   finalizes the op to SUCCEEDED** with MockBackend data.
5. Force MockBackend failure → Laravel records FAILED + refunds.
6. Send a duplicate trigger → exactly one job runs.

This proves the whole control plane before any real game is wired.

## 16. Testing strategy

- **Unit:** HMAC sign/verify (raw-body exactness, replay window, tamper); each Pydantic schema
  (valid + invalid); pre-flight pass/fail; MockBackend determinism; webhook client retry/stop logic
  (`respx`).
- **Integration:** signed `POST /operations` → `202`; full executor loop with a `respx`-faked Laravel
  receiver asserting the exact signed webhook payload per type; dedupe (duplicate trigger → one job).
- Quality gates (`ruff`, `mypy`, coverage) wired in CI later.

## 17. Deployment (Hostinger VPS)

- `docker-compose.yml`: `api` (uvicorn), `worker` (arq), `redis`. MySQL is the **host** DB — the
  containers reach it via the host (e.g. `extra_hosts: host.docker.internal` or the host IP);
  finalized in the Docker task.
- `api` bound to `127.0.0.1:8001` (Laravel calls it locally; optionally fronted by the existing
  nginx). Healthchecks use `/health` + `/ready`.
- Secrets via env/`.env` (never committed). Read-only MySQL user for Python.

## 18. Decisions log

| # | Decision | Resolution |
|---|---|---|
| D1 | Execution model | Redis + async worker (**arq**); api only acks 202 |
| D2 | Tech stack | FastAPI + httpx + Pydantic v2 + SQLAlchemy 2.0 async + structlog + pytest |
| D3 | DB driver | **asyncmy** (manylinux wheels; clean Docker build) |
| D4 | Health endpoints | Add `/health` + `/ready` (ops hygiene; not contract-required) |
| D5 | Invalid trigger handling | `401` for bad signature; for signature-valid-but-schema-invalid, ack `202` then webhook `status:"failed"` *(confirm in spec review)* |
| D6 | Money-op idempotency | Build the Redis backend-result cache **seam** in Phase 1; implement the cache in Phase 2 with the first real money backend |
| D7 | Backend abstraction | Single `GameBackend` protocol; per-game module + registry entry; MockBackend for Phase 1 |

## 19. Items to confirm in spec review

- **D5**: Confirm the graceful "ack 202 then webhook-failed" path for signature-valid but
  schema-invalid bodies (alternative: return a non-202 and let Laravel mark `dispatch_failed`). The
  spec assumes the graceful path.
