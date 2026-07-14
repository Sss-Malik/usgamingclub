# Diagnostics contract — Arcadia handover diff

**This is a handover note, not a change to Arcadia.** The target file —
`/Applications/development/laravel/arcadia/docs/AUTOMATION_SERVICE_CONTRACT.md` — lives in a
separate repo owned by its own maintainer; this service does not edit it. Source of truth for the
shape below is `docs/superpowers/specs/2026-07-14-webhook-diagnostics-design.md` §4 and §8, verified
against what Tasks 1–17 actually shipped in `app/`.

## Read the live file first — most of this is already done

The Arcadia contract doc was checked (read-only) on 2026-07-14 and **already implements most of
spec §8**: `op_id` on outbound requests (§3.2), the top-level `op_id` echo + optional `diagnostics`
envelope field (§4.2), the "echo `op_id`" reliability guarantee (§4.4), and diagnostics redaction
(§4.5) are all present and accurate. It even has a dedicated `### 4.6 diagnostics (optional,
operator-only)` section already. **Only §4.6's diagnostics *shape* has drifted** from what actually
shipped — it reflects an earlier revision of the design. The edits below are scoped to that drift;
do not re-add what's already there.

Line numbers below are as read on 2026-07-14 and will shift as the file changes — use them as a
locator, not a guarantee.

---

## §3.2 Endpoints — no change required

Already covers it: the prose immediately after the endpoint table (around line 92) states `op_id`
is sent as a ULID on **all six** endpoints, folded into the JSON body before the HMAC is computed,
and that the automation service must echo it back. This matches spec §8's ask exactly.

**Optional cosmetic nit (not required):** the endpoint table's "Body fields" column doesn't list
`op_id` per row — it's only documented in the prose below the table. If the maintainer wants the
table to be self-contained, append `, op_id (ULID, optional)` to each of the six "Body fields"
cells. Skip this if the prose-note style is intentional.

## §4.2 Webhook payload (envelope) — no change required

Already covers it (around lines 162–184): the envelope example includes both `"op_id"` and
`"diagnostics"` as top-level optional fields, and the prose explicitly states neither is used to
reconcile money, that a webhook with neither field processes exactly as before, and that the two
repos are deploy-order-independent. This matches spec §8/§3-decision-7 exactly.

## §4.4 Reliability guarantees — no change required

Already covers it (around lines 215–217): the "**Echo `op_id`**" bullet correctly states it's the
only correlation id `create` has ever had, and that an absent echo degrades correlation but never
processing. Matches spec §8 exactly.

## §4.5 Logging / PII — no change required

Already covers it (around lines 233–247): `diagnostics` is explicitly named as a column the
round-trip ledger redacts credentials from at any depth, and the doc explicitly calls out that
"amounts, usernames, ids, balances and provider error text are kept" intentionally. This already
states diagnostics is redaction-safe and loggable for the monitoring module — matches spec §8.

## §4.6 `diagnostics` (optional, operator-only) — NEEDS UPDATING

This section's example (around lines 255–269) and failure_kind list are stale. Four concrete gaps
versus what's actually shipped:

### 1. Missing `session_reuse`
Not present anywhere in §4.6. Add a field:
```
"session_reuse": "hit",   // "hit" | "fresh" | "relogin" | null — was a cached backend
                          // session/token reused, freshly created, or re-established mid-call?
                          // null = no session concept for this backend (mock, gamevault)
```

### 2. Missing success-side balance/identity fields
Not present anywhere in §4.6. Add, each optional and present only when truthfully known:
```
"external_user_id": "12345",  // resolved backend account id used this call
"balance_before": 40.0,       // gameroom recharge/redeem snapshot ONLY — absent elsewhere
"balance_after": 90.0         // gamevault + gameroom money ops ONLY — yolo/aspnet/ultrapanda/
                               // goldentreasure money-op success responses carry no balance,
                               // so it is absent (not null-guessed) for those drivers
```

### 3. Stale `failure_kind` list — missing `invalid_result`
Line 261 currently reads:
```
"failure_kind": "backend",   // preflight | backend | transient | unexpected | retry_blocked
```
This is missing a sixth value the executor actually emits when a backend returns a structurally
invalid success payload that fails our result-schema validation. Replace with:
```
"failure_kind": "backend",   // preflight | backend | transient | invalid_result | unexpected | retry_blocked
```
(`invalid_result` = backend responded but the payload didn't validate — a provider response-shape
drift, not a business failure.)

### 4. `steps[]` entries are missing fields
Lines 265–267 currently show only `name`/`ok`/`ms`:
```
"steps": [ { "name": "login.page",    "ok": true,  "ms": 210 },
           { "name": "login.captcha", "ok": true,  "ms": 830 },
           { "name": "recharge.post", "ok": false, "ms": 800 } ]
```
Real entries also carry `phase` and `http`, and sometimes `skipped`/`external`:
```
"steps": [
  { "name": "login.page",    "phase": "auth",    "http": true,  "ok": true,  "ms": 210 },
  { "name": "login.captcha_solve", "phase": "auth", "http": false, "external": true, "ok": true, "ms": 830 },
  { "name": "login.submit",  "phase": "auth",    "http": true,  "ok": true,  "ms": 0, "skipped": true },
  { "name": "recharge.post", "phase": "primary", "http": true,  "ok": false, "ms": 800 }
]
```
- `phase` ∈ `preflight | auth | resolve | snapshot | dialog | primary | recovery | finalize`.
- `http` (bool) — whether the step made an HTTP call (throttle waits and the captcha solver do not).
- `skipped` (bool, optional) — a conditional step this call's code path did not take (e.g.
  `login.submit` on a cached-session hit), recorded with `ms: 0`.
- `external` (bool, optional) — a third-party call that is not the game provider itself (currently
  only the aspnet captcha solver).

### Full corrected example for §4.6
Drop-in replacement for the current code block:
```jsonc
"diagnostics": {
  "op_id": "01J8Z...",                  // echo of ours (also accepted at the envelope's top level)
  "idempotency_key": "recharge:123",    // the service's internal key — Arcadia has never seen it
  "attempt": 1,                         // retry/attempt counter
  "cache_hit": false,                   // was this a replay from the result cache?
  "session_reuse": "hit",               // "hit" | "fresh" | "relogin" | null
  "duration_ms": 1840,
  "steps": [
    { "name": "login.page",    "phase": "auth",    "http": true, "ok": true,  "ms": 210 },
    { "name": "recharge.post", "phase": "primary", "http": true, "ok": false, "ms": 800 }
  ],

  // --- failure-only (all omitted on success) ---
  "failure_kind": "backend",            // preflight | backend | transient | invalid_result | unexpected | retry_blocked
  "reason": "gamevault:7:insufficient_user_balance",  // the REAL reason string
  "provider": { "http_status": 200, "code": "7", "message": "<raw provider text>" },

  // --- success-side, each present only when truthfully known ---
  "external_user_id": "12345",
  "balance_before": 40.0,               // gameroom recharge/redeem snapshot only
  "balance_after": 90.0                 // gamevault + gameroom money ops only
}
```

### Rules 1–3 (below the current example) — no change required
The three numbered rules already present after the example — every field optional and the whole
block optional; player-facing `message` unchanged; never put a credential in `diagnostics` (Arcadia
redacts it the same as everything else, but don't rely on that) — remain accurate as written and
need no edits.

---

## Summary for the maintainer
- §3.2 / §4.2 / §4.4 / §4.5: already correct, no action needed (optional cosmetic nit noted in §3.2
  above).
- §4.6: update the example JSON and the `failure_kind` comment as shown above — add `session_reuse`,
  `external_user_id`, `balance_before`, `balance_after`, `invalid_result`, and the fuller `steps[]`
  shape (`phase`, `http`, `skipped`, `external`).
- No other section of `AUTOMATION_SERVICE_CONTRACT.md` references diagnostics.
