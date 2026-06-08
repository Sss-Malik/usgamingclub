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

## GameVault / Juwa / Juwa2 (same provider)
- Set `games.backend_driver` to `gamevault`, `juwa`, or `juwa2` and the `api_base_url` /
  `api_agent_id` / `api_secret_key` columns (each game has its own credentials, even when the
  driver is shared).
- The VPS egress IP must be on the provider's allowlist (else every call fails
  `gamevault:5:ip_not_whitelisted`; some sandboxes report it as `gamevault:3:invalid_token`).
- CREATE_ACCOUNT receives `account_username` from Laravel (e.g. `saudmalik42`); Python creates the
  backend account with exactly that name and echoes it as `result.username`.
- Common reasons: `gamevault:7:insufficient_user_balance`, `gamevault:10:user_in_game`,
  `gamevault:20:account_exists`. Transient (`12`/`14`/`21`, 5xx, timeout) are retried automatically.
- Error reasons always carry the `gamevault:` prefix even for `juwa` games (same provider, same code
  dictionary) so logs and dashboards group by provider.

## Gameroom (JWT-session reverse-engineered backend)
- Set `games.backend_driver='gameroom'` plus `backend_url` / `backend_username` / `backend_password`
  (the agent's login credentials). No `api_*` columns needed.
- Sessions are cached in Redis (`gameroom_session:{game_id}`) and shared across workers. First op on
  a fresh game lazily logs in; subsequent ops reuse the JWT (TTL = expiry - 60s buffer).
- A worker crash during RECHARGE/REDEEM does NOT retry (per-driver `_max_tries=1`). Laravel's reaper
  fails+refunds the operation at the 10-min mark; if the gameroom call had already applied, the
  operator reconciles via the gameroom dashboard.
- Common reasons: `gameroom:account_exists`, `gameroom:insufficient_agent_balance`,
  `gameroom:insufficient_user_balance`, `gameroom:operation_failed` (opaque, often missing player),
  `gameroom:auth_failed` (creds wrong / session can't be refreshed). Transient: `gameroom:server_error`,
  network/5xx.
- To force a session refresh: `redis-cli DEL gameroom_session:<game_id>`. Next op will re-login.
