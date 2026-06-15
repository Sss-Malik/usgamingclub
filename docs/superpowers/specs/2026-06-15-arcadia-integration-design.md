# Arcadia Integration — Design

**Date:** 2026-06-15
**Status:** Approved design. Replaces the original `casino-app` integration contract with
the Arcadia (Laravel) contract at `/Applications/development/laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md`.

## 1. Goal

Switch this worker service from the original `casino-app` integration boundary to the
**Arcadia** Laravel app. The backend driver **transport** (clients, sessions, crypto) and
all money-safety machinery (result cache, `_max_tries=1` for non-idempotent drivers,
retry-blocked webhooks, transient-vs-terminal handling) **stay intact**. Two things change:
the **integration boundary** (API, HMAC, schemas, preflight, webhook, DB models, config),
and the **money representation** — the service becomes **dollar-native** (see §2.6 / §5.13),
removing the internal cents conversions that existed only to adapt the old cents-based
contract.

## 2. Decisions (locked)

1. **Driver routing:** Arcadia adds a `backend_driver` column to `games`; Python reads it
   exactly as before.
2. **Provider scope:** full parity, including the GameVault/Juwa HTTP-API family →
   Arcadia adds `api_base_url`, `api_agent_id`, `api_secret_key`, `binding_key` columns.
3. **Direction:** replace the `/operations` contract entirely (no dual-support).
4. **Failure `message`:** provider/business-failure text where available (user-readable),
   generic fallback for unexpected/internal errors.
5. **Create username:** derived from `full_name` (slugified + random digits).
6. **Dollar-native money:** Arcadia owns all cents↔dollar conversion (Eloquent
   accessors/mutators) and sends/receives finalized **dollars**. This service stops
   converting: it accepts dollar `amount`s, passes them straight to the providers (which
   use dollars), and returns provider balances as dollars. All `_to_cents`/`_to_dollars`/
   `_cents_to_score` helpers and `*_cents` fields are removed. Recharge collapses to a
   single `amount` (the old `amount_cents`/`bonus_cents`/`total_credit_cents` split was dead
   — every backend only used the total).

## 3. Contract summary (Arcadia side, source of truth)

### 3.1 Inbound (Arcadia → Python), all `POST application/json`
Auth: `X-Request-Signature = hash_hmac('sha256', "{ts}.{body}", API_SECRET)` (plain hex,
**no `sha256=` prefix**), `X-Request-Timestamp = {unix_ts}`.

| Endpoint | Body fields | Correlation id |
|---|---|---|
| `/create` | `user_id`, `full_name`, `backend_name` | — (none) |
| `/recharge` | `user_id`, `backend_name`, `username`, `amount` (int $), `transaction_id` (uuid) | `transaction_id` |
| `/withdraw` | `user_id`, `backend_name`, `username`, `amount` (int $), `redeem_id` | `redeem_id` |
| `/reset-password` | `user_id`, `backend_name`, `username`, `reset_password_id` | `reset_password_id` |
| `/freeplay` | `user_id`, `backend_name`, `username`, `amount` (int $), `freeplay_id` | `freeplay_id` |
| `/read` | `user_id`, `backend_name`, `username`, `read_id` | `read_id` |

**Amounts are whole dollars** on the wire (Arcadia stores cents in DB but the Eloquent
accessors divide by 100, then `(int) ceil(...)` is sent). Python passes the dollar `amount`
straight to the providers — **no cents conversion** (see §2.6 / §5.13).

Python responds **`202`** to acknowledge (Arcadia only logs on `$response->failed()`,
i.e. status ≥ 400; any 2xx is fine). `401` for bad signature, `400` for an uncorrelatable
body.

### 3.2 Outbound webhook (Python → Arcadia), `POST /api/automation/webhook`
Auth: `X-Webhook-Signature = hash_hmac('sha256', raw_body, WEBHOOK_SECRET)` (plain hex).
Freshness: `data.timestamp` (unix seconds, **inside** the signed body) must be within
**60s** of Arcadia's clock → each retry MUST rebuild the body with a fresh timestamp and
re-sign. Arcadia returns `200 {"success":true}` on accept; `403` on bad sig/stale/IP.

Envelope:
```jsonc
{
  "action": "recharge",      // create|recharge|redeem|reset_password|freeplay|read
  "status": "success",       // success | failed | error
  "message": "…",            // user-facing on failure
  "timestamp": 1739999999,
  "user_id": 123,
  "backend_id": 5,           // resolved Game.id (preferred by Arcadia)
  "backend_name": "milkyway",
  // action-specific (below)
}
```
Action-specific fields:
- `create` success: `account_created: [{username, password, id_from_backend}]`
- `recharge`: `transaction_id`, `amount` (echo original int $)
- `redeem`: `redeem_id`, `amount` (echo original int $)
- `reset_password` success: `reset_password_id`, `new_password`; failure: `reset_password_id`
- `freeplay`: `freeplay_id`, `amount` (echo original int $)
- `read` success: `read_id`, `user_data: {balance}` (dollars, `balance_cents/100`); failure: `read_id`

**Status mapping:** backend success → `success`; business failure (preflight, invalid
payload, `BackendError` with reason) → `failed` (message = provider/reason text); unexpected
error / transient-after-budget / `retry_blocked` → `error` (generic message). For money
actions Arcadia refunds on any non-`success`, so the success/failed/error distinction is
cosmetic (toast text) but semantically correct.

## 4. Architecture — translate at the edge

```
Arcadia ──POST /create|/recharge|…──► app/api/automation.py (HMAC verify, validate, normalize)
                                          │ enqueue arq job (idempotency_key, _max_tries policy)
                                          ▼
                                   app/worker/tasks.py ──► app/operations/executor.py
                                          │  (UNCHANGED money-safety flow)
                                          │  preflight (by name/username) → resolve_backend → dispatch
                                          ▼
                                   app/webhook/payload.py (build Arcadia envelope)
                                          │
                                          ▼
                                   app/webhook/client.py ──POST /api/automation/webhook──► Arcadia
```

The internal **Operation** carries both execution inputs and webhook metadata so the
executor stays generic and the Arcadia-specific shape is isolated to the schemas + the
webhook payload builder.

## 5. Component-by-component changes

### 5.1 `app/api/` — new `automation.py` (replaces `operations.py`)
- Six routes, each `Depends(verify_request_signature)`.
- A shared `enqueue_operation(request, op_dict)` helper: peeks the driver for the
  `NON_IDEMPOTENT_DRIVERS` `_max_tries=1` policy (unchanged), enqueues
  `execute_operation_task` with `_job_id=idempotency_key`, returns `202`.
- Per route: parse + validate body → resolve idempotency key → for `/create` generate the
  username → normalize to the internal op dict → enqueue.
- Driver peek currently uses `game_id`; switch to resolve by `backend_name`
  (`GamesRepository.get_driver_by_name`).

### 5.2 `app/security/hmac.py`
- `verify_request(secret, ts, signature, body, window)`: signed string `"{ts}.{body}"`,
  plain-hex compare (no prefix). Used inbound with `API_SECRET`.
- `sign_webhook(secret, raw_body) -> {"X-Webhook-Signature": hex, "Content-Type": ...}`:
  signed string = raw body only. Used outbound with `WEBHOOK_SECRET`.
- Remove the `sha256=` prefix and the single-secret assumption.

### 5.3 `app/api/deps.py`
- `verify_request_signature`: read `X-Request-Signature` / `X-Request-Timestamp`, verify
  with `settings.api_secret` + `verify_request`, `401` on failure. Returns raw bytes.

### 5.4 `app/schemas/`
- `requests.py` (replaces `operations.py`): one model per Arcadia action with that action's
  exact fields. No discriminated union over a single body — each endpoint owns its model.
- Internal op: a normalized dict/model carrying `action`, internal `type`, `idempotency_key`,
  `user_id`, `backend_name`, `username` (None for create), `account_username` (create only),
  `amount` (int $, money ops), and the correlation id field/value.
- `results.py`: rename to dollar-native — `ReadBalanceResult.balance` (float $),
  `RechargeResult.balance`/`RedeemResult.balance` (optional float $),
  `AgentBalanceResult.agent_balance` (float $). `CreateAccountResult`/`ResetPasswordResult`
  unchanged. Amounts in are `int` dollars; balances out are `float` dollars (2dp display
  values; authoritative money lives in Arcadia's wallet).

### 5.5 `app/operations/dispatch.py`
- Map internal type → backend method. `FREEPLAY` → `backend.recharge(...)` (freeplay is an
  additive credit). **No conversion** — pass the dollar `amount` straight through:
  `backend.recharge(ctx, amount=op.amount)`, `backend.redeem(ctx, amount=op.amount)`.
- `AGENT_BALANCE` no longer reachable (no endpoint); leave the backend method in place.

### 5.6 `app/operations/executor.py`
- Flow unchanged (validate → cache → preflight → resolve → call → cache → deliver).
- `_deliver` now calls the new `build_webhook_payload(op, outcome)` and posts via the
  updated client. The executor passes the full internal op (for action + correlation + echo
  amount) instead of just the idempotency_key.
- `retry_blocked` and transient/terminal handling unchanged; map to `error`/`failed` status.

### 5.7 `app/webhook/payload.py` (new)
- `build_webhook_payload(op, outcome) -> dict`: assembles the Arcadia envelope incl.
  `backend_id` (resolved Game.id from preflight/result), `backend_name`, fresh `timestamp`
  placeholder (the client refreshes it per attempt), and action-specific fields. Maps the
  internal result models to Arcadia fields: `balance` (dollars) → `user_data.balance`,
  `password` → `new_password`, create result → `account_created[0]`. Money ops echo the
  original int `amount` from the request.

### 5.8 `app/webhook/client.py`
- Sign the **raw body** with `WEBHOOK_SECRET` (header `X-Webhook-Signature`, plain hex).
- **Refresh `payload["timestamp"]` and re-serialize + re-sign on every attempt** (60s
  freshness window). Accept `200` as delivered. Treat `403` (sig/stale/IP) as
  non-retryable. Keep budget < 15 min (the reaper window).

### 5.9 `app/preflight/checks.py`
- Resolve game by name: `GamesRepository.get_by_name(backend_name)` → `game_not_found`.
- Account-scoped ops (recharge, redeem, reset_password, read, freeplay) resolve the account
  by `(game_id, username)`: `GameAccountsRepository.get_by_username(game_id, username)` →
  `game_account_not_found`. Create needs no account.
- Credential presence checks per driver, reading the Arcadia column names (below). GameVault
  family checks `api_*`; session families check `backend_url`/`username`/`password`.

### 5.10 `app/db/models.py` + `repositories.py`
- `Game`: `id, name, login_url, backend_url, game_url, username, password, active,
  backend_driver, api_base_url, api_agent_id, api_secret_key, binding_key`. **No
  `deleted_at`** (games table has no soft deletes in Arcadia).
- `GameAccount`: `id, user_id, game_id, username, password, id_from_backend, deleted_at`
  (has soft deletes). Map `id_from_backend` → `external_user_id` in the context.
- `GameCredentials`/preflight mapping: `login_page_url ← login_url`,
  `backend_username ← username`, `backend_password ← password`; keep `api_*`, `binding_key`,
  `backend_driver`, `game_url`.
- Add `GamesRepository.get_by_name`, `get_driver_by_name`,
  `GameAccountsRepository.get_by_username(game_id, username)`.
- **Drop** `GameOperation` model + `GameOperationsRepository` (dead code; no Arcadia table).

### 5.11 `app/config.py`
- Replace `python_signing_secret` with `api_secret` (inbound) + `webhook_secret` (outbound).
- `webhook_url` → `{app_url}/api/automation/webhook`. Keep/repoint `ping_url` if used.
- `require_runtime_settings` checks both secrets.
- `.env.example` updated: `AUTOMATION_*`-aligned names, Arcadia DB creds.

### 5.12 Username generation — `app/backends/usernames.py` (new)
- `generate_username(full_name) -> str`: slugify `full_name` to lowercase alphanumerics,
  truncate to a safe length, append random digits for uniqueness; fall back to a random
  handle if the name yields nothing. Called by the `/create` route so the value is stable
  across arq retries (carried in the enqueued payload as `account_username`).

### 5.13 Dollar-native backends (`app/backends/*`)
The providers already speak dollars; the cents layer was only an adapter to the old
contract. Make the protocol and every backend dollar-native:
- `base.py` protocol: `recharge(ctx, *, amount: int)`, `redeem(ctx, *, amount: int)`;
  `read_balance`/`agent_balance` return dollar results.
- Delete `_to_cents`, `_to_dollars`, `_to_cents_opt`, `_cents_to_score` across gamevault,
  gameroom, goldentreasure, orionstars, milkyway, ultrapanda, mock.
- Provider-wire formatting stays byte-identical for whole-dollar amounts:
  - whole-dollar providers (gamevault, gameroom, goldentreasure, ASP.NET cashier): send
    `str(amount)` instead of `str(ceil(cents/100))`.
  - ultrapanda `score`: send `f"{amount:.2f}"` instead of `f"{cents/100:.2f}"`.
- Balance reads return the parsed provider dollar value directly (e.g. `float(curScore)`)
  instead of `round(value*100)`.
- This touches the backend **modules** but **not** their clients/sessions/crypto. It is a
  net deletion (removes conversion + the unused `bonus`/`total_credit` recharge args).

## 6. Idempotency keys
- Money/read/reset/freeplay: `"{action}:{correlation_id}"` (e.g. `recharge:{uuid}`,
  `read:{read_id}`) → at-least-once dedup via arq `_job_id` + result-cache replay safety.
- Create (no correlation id): `"create:{user_id}:{backend_name}:{X-Request-Timestamp}"` —
  effectively unique per click; create is `_max_tries=1` for non-idempotent drivers so a
  crash mid-create surfaces as `error` and the user can safely retry.

## 7. Error & status semantics
- Bad inbound signature → `401`. Unparseable/uncorrelatable body → `400`.
- Backend business failure → webhook `status:"failed"`, `message` = provider/reason text.
- Preflight failure (game/account not found, missing creds) → `failed` with the reason.
- Unexpected error / transient-after-budget / `retry_blocked` → `status:"error"`, generic
  message.
- Result cache: cache terminal outcomes (success + business failure); never cache transient
  errors (so arq re-run can retry; non-idempotent drivers are already capped at 1 try).

## 8. Logging / secrets
- `app/logging.py` `SECRET_KEYS` must keep redacting `password`, `new_password`,
  `account_created`, `user_data`, `amount`, `api_secret_key`, `binding_key`, plus the two
  HMAC secrets. Mirror Arcadia's redaction set.

## 9. Laravel-side changes (to be written into the contract)
1. **Required:** add `backend_driver` (string) to `games`.
2. **Required for full parity:** add `api_base_url`, `api_agent_id`, `api_secret_key`,
   `binding_key` (nullable strings) to `games`.
3. **Recommended:** persist `id_from_backend` from the create webhook's
   `account_created[0].id_from_backend` onto `game_accounts`, so read/recharge skip the
   per-call provider search.
4. **Recommended:** make `handleCreate` idempotent (it currently creates a duplicate
   `GameAccount` on a duplicate create webhook).

## 10. Testing strategy
- Unit: new HMAC verify/sign (vectors matching Arcadia's PHP `hash_hmac`); each request
  schema (valid/invalid); username generator; webhook payload builder per action
  (success/failed/error, amount echo, balance dollars).
- Integration: each endpoint → 202 + enqueued payload shape; signature failure → 401;
  preflight-by-name/username; webhook delivery refreshes timestamp + re-signs across retries;
  `retry_blocked` → `error` webhook without backend call.
- Backends: update existing unit tests for the dollar-native signatures (assert the
  provider-wire value is unchanged for whole-dollar inputs; `read_balance` returns dollars).
- Reuse the existing live-gated backend integration scaffolding unchanged.

## 11. Out of scope
- No changes to backend driver **clients/sessions/crypto** (only the backend modules' money
  arg/result handling changes — see §5.13).
- `AGENT_BALANCE` external endpoint (Arcadia doesn't use it).
- Arcadia-side implementation (tracked in the contract for the Laravel team).
</content>
</invoke>
