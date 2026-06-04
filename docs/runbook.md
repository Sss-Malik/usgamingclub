# Runbook

## Health
- `GET /health` — liveness (always 200 if the process is up).
- `GET /ready` — checks MySQL + Redis; 503 if either is down.

## Verify signing against Laravel
`make ping` → expects `200 {"ok":true}` from `{APP_URL}/webhooks/_ping`. A `401` means the shared
secret or clock (NTP, 300s window) is wrong.

## Common failures (reported as webhook status:failed)
- `invalid_payload: ...` — trigger body failed §4 validation.
- `preflight_failed: game_not_found|game_account_not_found|missing_game_account_id` — DB lookups.
- `backend_error: ...` — the game backend call failed.

## Force a failure for testing
Set `MOCK_FORCE_FAIL=true` (and optional `MOCK_FORCE_FAIL_REASON`) and restart the worker.

## Webhook delivery
Retries on conn-error/5xx/404 with backoff up to `WEBHOOK_MAX_BUDGET_SECONDS` (default 600s).
`401`/`422` are sender bugs and are not retried — check signing / payload.
