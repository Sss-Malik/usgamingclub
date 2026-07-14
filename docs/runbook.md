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

## Webhook diagnostics — operator field reference
Every webhook (success or failure) may carry `op_id` (top-level) and a `diagnostics` object — see
`docs/architecture.md` ("Webhook diagnostics") for the shape overview and
`docs/superpowers/specs/2026-07-14-webhook-diagnostics-design.md` for the full contract. Every field
is optional — **absent**, not `null`, whenever a backend can't populate it truthfully.

| Field | Meaning | What to check when set |
|---|---|---|
| `op_id` | Echo of Arcadia's request-side ULID | Correlate this webhook to the originating `/operations` POST — the only correlation id `create` has |
| `idempotency_key` | The op's internal dedupe key | Cross-reference the result cache / worker logs for this op |
| `attempt` | arq `job_try` | `>1` means a crash/job-loss re-ran this op — check for an earlier `retry_blocked` delivery if the driver is non-idempotent (the `NON_IDEMPOTENT_DRIVERS` set: gameroom, goldentreasure, orionstars, milkyway, firekirin, pandamaster, ultrapanda, vblink, yolo — i.e. everything except the gamevault/juwa/juwa2 family and mock) |
| `cache_hit: true` | This delivery replayed a cached terminal outcome; the backend was NOT called again | `steps` is `[]` and `session_reuse` is `null` by design on a replay — expected, not a bug |
| `session_reuse: hit` | A cached session/token was reused; no login this call | Normal steady-state path |
| `session_reuse: fresh` | First login performed this call | Normal on a cold game or after natural session expiry |
| `session_reuse: relogin` | A mid-call re-login fired (dead-session / auth-fail code) | Session churn or contention — check for concurrent workers hammering the same game, or an evicted Redis session key |
| `session_reuse: null` | No session concept (mock, gamevault) or a cache-hit replay | Not itself a signal |
| `duration_ms` | Wall-clock time for the whole operation | Compare against the backend's typical latency; large values often correlate with `session_reuse: relogin` or a provider that's slow to respond |
| `steps[]` | Ordered timeline of internal/HTTP steps (`name`, `phase`, `http`, `ok`, `ms`, optional `skipped`/`external`) | `ok` reflects the step's own round-trip at the **transport** level: `ok:false` marks a step whose HTTP call failed to complete (connection error / timeout / a raised transport error). A provider that *answered* but rejected the op (business error, HTTP 4xx/5xx envelope) is identified by `failure_kind` + `provider` + `reason`, **not** by `ok` — the request completed, so its step stays `ok:true`. Use the step `name`/`phase`/`ms` sequence to see how far the flow got and where the time went. `skipped:true` marks a conditional step not taken this call (e.g. `login.submit` on a session hit); `external:true` marks a call to a third party (the captcha solver), not the game provider |
| `failure_kind: retry_blocked` | A non-idempotent driver's re-run was blocked before any provider call | Backend was NOT re-invoked this attempt; reconcile manually only if the *prior* attempt may have partially applied |
| `failure_kind: preflight` | DB lookup or backend config error — no provider call made | Check `games` / `game_accounts` rows and `games.backend_driver` |
| `failure_kind: transient` | Provider 5xx / timeout / transient code | Laravel already treats this delivery as final (arq will not retry the backend call); Laravel has already refunded/reverted — safe to let the player retry |
| `failure_kind: backend` | Provider returned a terminal business error | Read `reason` together with `provider.code` / `provider.message` |
| `failure_kind: invalid_result` | Backend returned a "success" our schema rejected | Likely a provider response-shape drift — check the backend module against a fresh sample response |
| `failure_kind: unexpected` | Unhandled exception | Check worker logs (`operation_unexpected_error`) for the traceback |
| `reason` | The real internal reason (never the generic player-facing `message`) | The actionable string, e.g. `gamevault:7:insufficient_user_balance` |
| `provider.http_status` | True transport status (honestly `200` even when the error rode a 200-body envelope) | Distinguishes a transport failure from a business-logic failure |
| `provider.code` | The provider's own business/envelope code, when one exists | Look up in the backend's error-code table (per-backend sections above); absent for yolo and for aspnet business failures (no numeric code exists there) |
| `provider.message` | Raw, untruncated provider error text | The exact string that drove classification — useful when `reason`'s slug is ambiguous; absent for ultrapanda/vblink (no message field in that API) |
| `external_user_id` | The resolved backend account id actually used this call | Should match the game account's stored external id; absent on a non-create op can mean it genuinely couldn't be resolved |
| `balance_before` | Pre-operation balance from a snapshot the backend already took | Gameroom recharge/redeem only (`agentMoney` snapshot) — absent everywhere else |
| `balance_after` | Post-operation balance the provider's own response carried | Gamevault + gameroom money ops only; absent for yolo/aspnet/ultrapanda/goldentreasure (their money-op success responses carry no balance) — compare against Laravel's ledger if a mismatch is suspected |
