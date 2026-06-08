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
- RECHARGE and REDEEM **pre-fetch** the current player+agent balance via `agentMoney?id=<pid>` to
  populate `available_balance` / `customer_balance`. The server validates these against its current
  ledger and rejects mismatches (`"Available balance has changed..."`) — a stale or empty value
  fails. One extra round-trip per money op; reads (READ_BALANCE / AGENT_BALANCE) are unaffected.
- A worker crash during RECHARGE/REDEEM: the endpoint embeds `_max_tries=1` in the payload; the
  worker reads `ctx["job_try"]` on a retry and short-circuits with a `retry_blocked` failure
  webhook (Laravel finalizes in seconds; reaper as fallback). The backend is never re-called. If
  the gameroom call had already applied on the prior attempt, reconcile via the gameroom dashboard.
- Common reasons: `gameroom:account_exists`, `gameroom:insufficient_agent_balance`,
  `gameroom:insufficient_user_balance`, `gameroom:operation_failed` (opaque, often missing player),
  `gameroom:auth_failed` (creds wrong / session can't be refreshed). Transient: `gameroom:server_error`,
  network/5xx.
- To force a session refresh: `redis-cli DEL gameroom_session:<game_id>`. Next op will re-login.

## Golden Treasure (Cloudflare-fronted reverse-engineered backend)
- Set `games.backend_driver='goldentreasure'` plus `backend_url=https://agent.goldentreasure.mobi`,
  `backend_username`, `backend_password` (the agent's login). No `api_*` columns needed.
- **No IP allowlist** (Golden Treasure uses Cloudflare, not IP-based ACLs). Our `_BROWSER_HEADERS_BASE`
  sends the header set CF requires.
- Sessions cached in Redis (`gtreasure_session:{game_id}`, 24h TTL). First op lazy-logs-in; later
  ops reuse the token. To force re-login: `redis-cli DEL gtreasure_session:<game_id>`.
- Mutating ops (savePlayer/enterScore) self-serialize at ≥5s spacing per game via
  `gtreasure_throttle:{game_id}` (TTL 5s, auto-expires). Reads are not throttled.
- A worker crash during RECHARGE/REDEEM: same retry-blocked path as gameroom. The endpoint embeds
  `_max_tries=1`; on retry the worker short-circuits with a `retry_blocked` failure webhook
  (backend never re-called). If Golden Treasure had already applied, reconcile via the agent UI.
- Common reasons: `gtreasure:account_exists`, `gtreasure:operation_refused` (over-limit /
  insufficient), `gtreasure:invalid_password_format`, `gtreasure:auth_failed` (creds wrong / session
  unrecoverable), `gtreasure:rate_limited` (transient — Laravel reaper picks up),
  `gtreasure:requires_operator_action_*` (2FA / verify code — clear via agent UI).
- The agent account must **not have 2FA enabled** — Google Authenticator (`code:30200`/`30201`) and
  system verify codes (`code:30100`) require operator interaction; our automation can't satisfy them.
