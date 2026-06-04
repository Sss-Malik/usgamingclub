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

## Where things live
- Wire scheme: `app/security/hmac.py` · Schemas: `app/schemas/` · Backends: `app/backends/`
- Orchestration: `app/operations/executor.py` + `dispatch.py` · Worker: `app/worker/`
- API: `app/api/` · Config: `app/config.py`

## Workflow
- TDD: write the failing test first, then the minimal code (see `docs/superpowers/plans/`).
- Per feature: build → user tests manually → commit on approval.
- Integrating a new game backend: request the API-findings doc, confirm it covers success + error
  responses, then add a `GameBackend` module + registry entry.

## Commands
`make install` · `make test` · `make lint` · `make type` · `make up` · `make ping`

## Specs & plans
`docs/superpowers/specs/` (design) · `docs/superpowers/plans/` (implementation).
