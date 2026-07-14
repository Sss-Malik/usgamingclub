# Webhook Diagnostics + `op_id` Echo — Design

**Date:** 2026-07-14
**Status:** Approved design. Adds a diagnostics channel to every webhook and echoes Arcadia's
`op_id`. **Additive and deploy-order-independent** — no existing webhook field, the player-facing
`message`, the HMAC scheme, or the idempotency/replay semantics change.

## 1. Goal

Laravel (Arcadia) is building a super-admin monitoring module. Today the webhook reports only
`status` + `message`, and for `status="error"` the message is **always** the generic sentence
*"Something went wrong. Please try again later."* (`app/webhook/payload.py::_message`). A timeout, a
provider 500, an unhandled exception, and retry-exhaustion are indistinguishable to operators.

Yet at the moment of failure this service holds: the provider HTTP status, the raw provider error
`code` + message, which step of a multi-step flow failed, the arq attempt count, whether a cached
session was reused or a re-login fired, its transient-vs-terminal classification, and its internal
`idempotency_key`. All of it is collapsed into one string, or dropped.

This change **emits a `diagnostics` object on every webhook (success and failure)** carrying that
truth, and **echoes `op_id`** (the ULID Arcadia now sends in every request) top-level and inside
`diagnostics` so Laravel can correlate the round trip — the only correlation id `/create` will have.

**Guiding rule:** a confidently-wrong diagnostic is worse than a missing one. **Omit any field a
backend cannot populate truthfully; never invent one.**

## 2. Backend inventory (what each backend can truthfully report)

Derived from a full read of every `app/backends/*` module + `errors.py`. Legend: ✓ = truthfully
available; — = does not exist (would be fabricated); *partial* footnoted.

| Backend (drivers) | provider `code` | provider `message` | `http_status` | steps | `session_reuse` | `balance_before` | `balance_after` | `provider_txn_id` | `external_user_id` |
|---|---|---|---|---|---|---|---|---|---|
| **mock** (mock) | — | — synthetic reason | — no HTTP | 1 build step | — no session | — | *partial*¹ | — | ✓ create only |
| **gamevault** (gamevault, juwa, juwa2) | ✓ numeric 1–23, 400 | ✓ raw `msg`² | ✓ business path² | ✓ | — stateless MD5 | — | ✓ recharge/redeem (opt) | — | ✓ create / getUserID |
| **gameroom** (gameroom) | ✓ envelope 400/401/410/430/500 | ✓ raw `message`³ | transport only³ | ✓ | ✓ JWT⁴ | ✓ **agentMoney snapshot** | ✓ partial | — | ✓ create `data.id` |
| **goldentreasure** (goldentreasure) | ✓ numeric 8/21/52/167/1003/−3/−17/… | ✓ raw `message`³ | 200-body³ | ✓ + throttle | ✓ token⁴ | — | ✓ read only | — | — (`savePlayer` returns none) |
| **aspnet** (orionstars, milkyway, firekirin, pandamaster) | login `errtype` only⁵ | ✓ untruncated sentinel⁵ | scrape status | ✓ 6-step + captcha | ✓ cookie⁴ | — | ✓ read only | — | ✓ create resolves `uid:gid` |
| **ultrapanda** (ultrapanda, vblink) | ✓ numeric 5/8/21/22/52/167/1003/1086 | — no message field exists | 200-body³ | ✓ + throttle | ✓ token⁴ | — | ✓ read only | — | — |
| **yolo** (yolo) | — boolean `status` + free text⁶ | ✓ raw text (today truncated) | ✓ 200/422/5xx | ✓ 3-step login | ✓ cookie+CSRF⁴ | — | ✓ but today **discarded** | — | ✓ resolved |

Footnotes:
1. mock returns one balance figure per money op (recharge echoes `amount`, redeem always `0.0`); never a paired before value.
2. gamevault: the numeric code + raw `msg` exist only on the `code != 0` business path (HTTP was ~200). `map_code` **discards** `msg` for known codes and truncates to 80 for unknown. HTTP status is captured only on the explicit `gamevault_http_{status}` paths.
3. HTTP-200-envelope backends (gameroom, goldentreasure, ultrapanda): the business error rides a 200 transport response. The real signal is the envelope/body `code`, not the HTTP status.
4. Session reuse is *observable but not currently surfaced*: `get_token`/`get_or_login`/`get_session` return a bare token/cookie with no hit/miss flag. New instrumentation required (§5.3).
5. aspnet: OP business failures are English sentinel substrings we classify — **no numeric provider code**; only the login `errtype` token is a genuine provider code. The untruncated sentinel (`parse_sentinel` arg) is available at raise but collapsed to a slug.
6. yolo: the provider returns only boolean `status` + free-text message; our five slugs are **our** derived classification, not a provider code.

**Highest-value facts discarded today** (ranked): (1) raw provider message + numeric code on business
failures — collapsed to a slug everywhere; (2) the auth-dead **origin** code, flattened to a generic
`auth_failed` (goldentreasure `{−3,−17,52}`, ultrapanda `1086`, gameroom 2nd 410); (3) all per-step
timing; (4) session reuse vs fresh-login vs re-login; (5) balances already held (gameroom snapshot;
yolo/aspnet parse-then-discard the success `data`); (6) resolved `external_user_id` on non-create ops.

## 3. Locked decisions

1. **`session_reuse` is its own field** (`hit|fresh|relogin|null`). `cache_hit` stays strictly for
   op-level idempotency replay. mock/gamevault emit `session_reuse: null` (no session concept), not
   `false`.
2. **Balances: honest-only, no extra provider calls.** Report `balance_before` only where a snapshot
   already exists (gameroom recharge/redeem); report `balance_after` wherever the response already
   carries it — including by **retaining the yolo/aspnet money-op success `data`** we currently
   discard. `null`/omit elsewhere. No read-after round trips (they would add load, risk the
   goldentreasure/ultrapanda `code:167` rate limit, and change each money op's provider footprint).
3. **Full per-HTTP-call steps, every backend.** Instrument all client families with named steps.
4. **`external_user_id` whenever truthfully known** — from the create result, a cached
   `ctx.account.external_user_id`, or an id resolved mid-op. Always a real value we actually used;
   `null` where genuinely unknown (goldentreasure, ultrapanda).
5. **`provider_txn_id` is dropped entirely.** No backend returns one. Never synthesize it from the
   `idempotency_key` (gamevault `order_id`), the aspnet dialog `param_token` (a CSRF token), or a
   resolved pid.
6. **`provider.http_status` = the true transport status** (honestly `200` when the error rode a 200
   body); **`provider.code` = the business/envelope code** (string). Matches the example in the task.
7. **`op_id`** is optional on input (one deploy cycle of older Laravel may omit it); **always echoed
   when present**, top-level and in `diagnostics`.

## 4. The `diagnostics` object (contract)

```jsonc
{
  // ...existing envelope fields unchanged (action, status, message, timestamp, user_id,
  //    backend_id, backend_name, correlation ids, amount, account_created, etc.)...
  "op_id": "01J...",                     // top-level echo; present only when Laravel sent it
  "diagnostics": {
    "op_id": "01J...",                   // echoed when present
    "idempotency_key": "recharge:123",
    "attempt": 1,                        // arq job_try; in-call re-login is session_reuse:"relogin"
    "cache_hit": false,                  // op-level idempotency replay ONLY
    "session_reuse": "hit",              // hit|fresh|relogin|null (null = no session concept)
    "duration_ms": 1840,
    "steps": [
      {"name": "login.submit",  "phase": "auth",    "http": true, "ok": true,  "ms": 210, "skipped": false},
      {"name": "recharge.post", "phase": "primary", "http": true, "ok": false, "ms": 800}
    ],

    // --- failure-only (all omitted on success) ---
    "failure_kind": "backend",           // preflight|backend|transient|invalid_result|unexpected|retry_blocked
    "reason": "gamevault:7:insufficient_user_balance",  // the REAL reason, never the generic sentence
    "provider": {                        // whole block omitted when nothing truthful is available
      "http_status": 200,
      "code": "7",                       // omitted when the backend has no provider code
      "message": "..."                   // raw provider text, untruncated; omitted when none exists
    },

    // --- success-side, each omitted when not truthfully known ---
    "external_user_id": "12345",
    "balance_before": 40.0,              // gameroom snapshot only
    "balance_after": 90.0
  }
}
```

**`steps[]` entry:** `name` (dotted), `phase` ∈ {`preflight`,`auth`,`resolve`,`snapshot`,`dialog`,
`primary`,`recovery`,`finalize`}, `http` (bool), `ok` (bool), `ms` (int). Optional `skipped` (bool —
a conditional step the code path didn't take, e.g. `login.submit` on a session cache hit, recorded
`ms:0`) and `external` (bool — the aspnet captcha solver, which is a third party, not the game
provider). The presence of a non-skipped `login.submit` is itself corroboration of
`session_reuse:"fresh"`.

**`failure_kind` taxonomy** (set by *which* executor branch fired):

| Executor branch | `failure_kind` | webhook `status` |
|---|---|---|
| `retry_blocked` short-circuit (before any backend call) | `retry_blocked` | `error` |
| `PreflightError`, or `resolve_backend` config `BackendError` (no provider call made) | `preflight` | `failed` |
| `TransientBackendError` (timeout / 5xx / transient code) | `transient` | `error` |
| `BackendError` from the backend call | `backend` | `failed` |
| result `ValidationError` (backend returned a malformed success) | `invalid_result` | `failed` |
| bare `Exception` | `unexpected` | `error` |

`diagnostics.reason` carries the real internal reason with the redundant executor stage-prefix
stripped (e.g. `backend_error: ` / `preflight_failed: `), so it reads `gamevault:7:...`,
`gamevault_http_503`, `retry_blocked: manual reconcile may be required`, `unexpected` — **never** the
generic sentence. This is the crux: `message` stays generic for the player; `reason` tells operators
the truth.

## 5. Component-by-component plumbing

### 5.1 `app/backends/base.py` — enriched exceptions + recorder
- Add optional structured fields to `BackendError` **and** `TransientBackendError`:
  `provider_http_status: int | None`, `provider_code: str | int | None`,
  `provider_message: str | None` (untruncated). `reason` (the slug) is unchanged, so the
  player-facing `_message()` path is untouched.
- Add `DiagnosticsRecorder`:
  - `step(name, *, phase, http=True, external=False)` — an (async-safe) context manager that times
    the block, captures `ok` from whether it raised, and appends a step. On exception it records the
    step as `ok:false` then re-raises, so a failing op still yields the steps that ran.
  - `skip(name, *, phase)` — record a `skipped:true, ms:0` step for a not-taken conditional path.
  - `session_event(kind)` — `hit|fresh|relogin`; last write wins (`relogin` supersedes `fresh`).
  - `mark_external_user_id(v)`, `mark_balance_before(v)`, `mark_balance_after(v)`.
  - Accumulates into a plain dict the executor reads after the op.

### 5.2 The 6 `map_*` / raise sites — attach provider fields (failure-only)
- **gamevault** `map_code` / client: carry numeric `code` + untruncated `msg`; capture
  `resp.status_code` at the `code != 0` site and on the `gamevault_http_*` paths.
- **gameroom** `map_response`: carry the **envelope** `status_code` as `provider_code` + untruncated
  `message`; keep the transport HTTP status separate (only meaningful on the `>=300` paths).
- **goldentreasure** `map_response`: carry `code` + untruncated `message`; on the auth-dead retry
  path, **preserve the origin code** (`−3/−17/52`) instead of only reporting `auth_failed`.
- **ultrapanda** `map_code`: carry `body['code']` (known at the raise point, currently discarded). No
  `provider_message` — none exists.
- **yolo** `map_envelope`: carry `http_status` + untruncated `msg`. **No `provider_code`** — the
  derived slug lives in `reason`, not `provider.code`.
- **aspnet** `business_failure_to_error` / login errtype: carry the untruncated sentinel as
  `provider_message`; `provider_code` = the login `errtype` only, else absent.

### 5.3 Each client — emit steps + session events
Wrap every round trip in `ctx.diag.step(...)`; call `ctx.diag.session_event(...)` from the session
getters. Concrete step names:

- **mock:** one `{op}.build` (`phase:finalize`, `http:false`); `session_reuse:null`.
- **gamevault:** `resolve.user_id` (only when not cached) → `primary` (`addUser.post` /
  `balance.read` / `reset.post` / `recharge.post` / `withdraw.post`); `session_reuse:null`.
- **gameroom:** `session.check` → `login.submit` (auth, conditional) → `resolve.user_id` (userList,
  conditional) → `recharge.snapshot` / `redeem.snapshot` (agentMoney → `balance_before`) →
  `primary` → `recovery.relogin` (on envelope 410).
- **goldentreasure:** `throttle.acquire` (mutating ops) → `login.submit` (conditional) → `primary` →
  `recovery.relogin` (on `−3/−17/52`).
- **aspnet:** `login.page` + `login.captcha_solve` (`external:true`) + `login.submit` +
  `login.confirm` (all conditional) → `resolve.accounts_list_get` + `resolve.search_post` →
  `dialog.tourl_post` + `dialog.get` + `dialog.post` → `recovery` (on dead-session). Read balance:
  `resolve.accounts_list_get` + `resolve.search_post` (or `balance.getscore_post` for OrionStars).
- **ultrapanda:** `throttle.acquire` (mutating) → `login.submit` (conditional) → `primary` →
  `recovery.relogin` (on `1086`).
- **yolo:** `login.page` + `login.submit` + `login.confirm` (conditional) → `resolve.search`
  (player_list) → `primary` → `recovery` (on auth-failure). **Retain the money-op success `data`**
  to populate `balance_after` when present (do **not** retain any password field).

### 5.4 `app/backends/context.py`
`BackendContext` gains `diagnostics: DiagnosticsRecorder | None = None`, `op_id: str | None = None`,
`attempt: int = 1`. Backwards-compatible defaults keep existing constructions valid.

### 5.5 `app/preflight/checks.py`
`build_context(...)` accepts and attaches the recorder so preflight can record a coarse
`preflight.db` step; preflight failures still deliver via the executor (recorder lives in executor
scope).

### 5.6 `app/worker/tasks.py`
Pass `attempt=ctx.get("job_try", 1)` into `execute_operation`.

### 5.7 `app/operations/executor.py` — assembly seam
- Create a `DiagnosticsRecorder` and start a wall-clock timer at the top of `execute_operation`.
- Thread `op_id`, `attempt`, and the recorder into `build_context`/`BackendContext`.
- In each `except` branch, set `failure_kind` from the branch and read `provider_*` off the caught
  exception into the recorder/detail.
- `cache_hit` = the replay short-circuit fired (`result_cache.get` returned non-None).
- Assemble a `Diagnostics` value (recorder dict + `op_id` + `attempt` + `cache_hit` + `duration_ms` +
  `failure_kind` + stripped `reason` + provider block) and pass it to `build_webhook_payload`.
- `apply_post_effects` stays the success-only no-op; assembly lives in the executor so success and
  failure share it.

### 5.8 Replay safety — `app/operations/result_cache.py`
`CachedOutcome` gains an optional **replay-stable** `detail: dict | None` holding
`{failure_kind, provider, external_user_id, balance_before, balance_after}` (reason is already
stored). Only `succeeded`/`failed` outcomes are cached (unchanged); transient `error` outcomes are
never cached, so nothing stale is replayed. On a cache hit the webhook reports `cache_hit:true`,
`steps:[]`, `session_reuse:null`, `duration_ms` of the replay handling, and the cached `detail` —
honest, because no HTTP happened on the replay.

### 5.9 `app/webhook/payload.py`
- Add `op_id` (top-level, when present) and the assembled `diagnostics` object.
- **`_message()` unchanged** — including the generic sentence for `error`.
- `build_webhook_payload(op, outcome, *, backend_id, diagnostics=None)` — `diagnostics=None` yields
  the exact legacy body (keeps old call sites and tests valid).

### 5.10 `app/schemas/requests.py`
- `op_id: str | None = None` on `_In` (so all six request models parse it; `extra="ignore"` already
  tolerates its absence) and on `Operation`.
- Thread `op_id=req.op_id` through all six endpoint `Operation(...)` constructions. It rides the
  enqueued payload to the worker.

### 5.11 `app/logging.py`
Keep diagnostics out of trouble: ensure the redaction discipline covers any newly-retained
structures; never retain a `data` dict containing a generated/account password (yolo/aspnet
balance-only retention). `provider.message` is untruncated by design (§3) but provider error strings
carry no credentials.

## 6. Non-goals / unchanged
- Player-facing `message`, all existing webhook fields, the HMAC scheme (inbound + outbound),
  idempotency + 300s replay window, `_max_tries=1` policy for `NON_IDEMPOTENT_DRIVERS`, and the
  transient-vs-terminal classification are **untouched**.
- No read-after round trips; no new provider calls anywhere.
- No `provider_txn_id`; no fabricated fields.
- Every `diagnostics` field is optional on the Laravel side → both repos deploy in any order.

## 7. Testing strategy
- **Unit:** enriched `BackendError`/`TransientBackendError` fields; `DiagnosticsRecorder`
  (timing, ok/skip, session_event precedence, survives-exception); each `map_*` populates the right
  provider fields; `payload.py` diagnostics assembly for success / failed / error / retry_blocked /
  cache-hit and `op_id` present/absent; `reason` never equals the generic sentence on failure while
  `message` still does for `error`.
- **Backend/client:** each client emits the expected step names/phases and skip markers; session
  getters emit the right `session_event`; gameroom `balance_before` from snapshot; yolo/aspnet
  `balance_after` retention.
- **Executor:** `failure_kind` per branch; `duration_ms` measured; cache-replay produces
  `cache_hit:true, steps:[]` + cached detail; `attempt` from `job_try`.
- **Integration:** endpoints accept + echo `op_id`; a full loop asserts the `diagnostics` block on a
  delivered webhook; HMAC still verifies over the larger body.

## 8. Documentation deliverables
- **This repo:** this spec is the source of truth; add a short "Diagnostics" section to
  `docs/architecture.md` / `docs/runbook.md` pointing operators at the field meanings.
- **Arcadia** (`/Applications/development/laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md`, the
  maintainer owns this — exact diff to hand over):
  - **§3.2 Endpoints:** add `op_id` (ULID, optional) to the body-fields of **all six** endpoints; a
    one-line note that the automation service echoes it back.
  - **§4.2 Webhook envelope:** add top-level `op_id` (echoed when sent) and an optional
    `diagnostics` object (document the shape from §4 here; stress **every field optional**, present
    on success and failure, purely observational — Arcadia must not gate any wallet/credential
    effect on it).
  - **§4.4:** note `op_id` is the create correlation id (no other id exists for `/create`).
  - **§4.5 Logging/PII:** note `diagnostics` is redaction-safe (no credentials; `provider.message`
    is provider error text) and can be logged for the monitoring module.
```
