# Billing correctness rollout

Status as of 2026-07-28. Replaces the `/tmp` spec, which did not survive.

Provenance matters in this document, so every claim is tagged:

- **[verified]** — re-checked against this repo at commit `c8e0c2d`.
- **[unverified]** — produced by an earlier analysis pass against the
  **production** Supabase (`wyfz`). The local `api/brevitas.db` has **0 rows in
  `usage_log`**, so none of these numbers can be reproduced from a checkout.
  Re-run against production before relying on any of them.

---

## The one correction that changes the work

An earlier pass listed "`authoritative=True` is set in code at `proxy.py:4824`,
yet `false` persists on all 32,373 rows" as bug #1. **This is not a bug, and
implementing the "fix" would remove a security guarantee.** [verified]

- `brevitas/proxy.py` is **1512 lines**. There is no line 4824.
- `_record_receipt` never sets `authoritative` at all. Its `_emit_usage`
  payload sends `receipt_source: "proxy"` and nothing else relevant.
- `api/store.py:1553` documents the intent directly: *local BVX proxies report
  receipts over `/v1/usage` (`authoritative=0`), so onboarding evidence keys on
  `receipt_source` + the device-key binding, never on the billing-only
  authoritative flag* (migration `202607280004`).
- The test suite guards this deliberately:
  `tests/test_onboarding_api.py:330` (`forged-authoritative-proxy`),
  `tests/test_cloud_usage_api.py:392` — *caller-reported telemetry can be
  analyzed but is never authoritative billing input.*

`authoritative=false` on proxy rows is the anti-forgery boundary. A client-side
proxy is not permitted to mark its own receipts billable. Authoritative rows
come from the worker (`BREVITAS_WORKER_BILLING_ROLE=authoritative`,
`tests/test_observability.py:699`).

**Do not "fix" this.** The real question is different and still open: whether
the worker path is producing authoritative rows *at all* in production. That is
a deployment question, not a code bug.

## Remaining claimed defects — status

| # | Claim | Status |
|---|---|---|
| 1 | `authoritative` never true | **Withdrawn.** By design. See above. [verified] |
| 2 | `organization_id` mostly NULL (5,039 / 32,637) | Plausible; `_usage_row` defaults it to `""` when the caller omits it (`api/store.py:527`). Caller-side omission, not a store bug. [verified mechanism, unverified counts] |
| 3 | `session_id` never populated | **Partly withdrawn.** The proxy *does* send it (`proxy.py:845`) and `_usage_row` persists it (`store.py:588`). If it is empty in production, the drop is downstream of both. [verified] |
| 4 | Savings compute to exactly zero | Mechanism confirmed: `proxy.py:838` sends `compressed_tokens = baseline` whenever `optimized_tokens is None`, so the delta is 0. The inline comment says the API is expected to re-anchor the optimized side to the provider receipt. **Whether the API actually does that re-anchoring is unconfirmed and is the crux of Phase 2.** [verified mechanism] |

## Settled design decisions

- **Netting unit:** `organization_id` over the existing Stripe 7-day period.
  One immutable row per `(org, period)` with a revision chain.
- **Negative fees: rejected.** Each row today is independently provably
  non-increasing; a negative row landing in review beside reporting positives
  would overcharge with no auto-correction. Stripe rejects negatives anyway.
- **Losing week bills $0.** No deficit carry-forward.
- **Warm pings deduct 100%**, no attribution — the schema has no link. $0 impact
  today (zero warm rows). Must be wired before warming goes live.
- **Pricing:** use a price the customer can verify; never guess a private
  discount. A promo window is a published fact with a published end date, so it
  qualifies. Enterprise/Batch/PTU must be attested and are unbillable by
  default — billing a PTU customer for savings with no marginal dollar is the
  worst available failure mode.
- **Migration: nothing to migrate.** 0 ledger rows, 0 accounts. [verified: local
  schema; production counts unverified]

## Phases

**Phase 0 — done.** `supabase/migrations/202607280006_retire_per_row_fee_trigger.sql`
drops `queue_brevitas_fee_after_usage`. The function is retained but detached.

Not "zero code" as originally scoped: two CI scripts asserted the trigger's
*presence* and had to be inverted, or CI would have gone red on the next run —
`scripts/ci/migration-receipt-accounting-assertions.sql:46,82` and
`migration-receipt-accounting-rollback-assertions.sql:27,32`. Both now assert
the trigger stays absent and that an authoritative priced row settles nothing.

⚠️ **Replay warning:** migration `202607170012` guards on the trigger existing
and will raise if replayed after `202607280006`. Combined with the standing fact
that `wyfz` has no migration ledger, that chain must never be replayed blind.

**Phase 1 — attribution.** Fix #2 (`organization_id` at the call sites that omit
it) and investigate #3 downstream. Bug #1 is withdrawn; do not touch the
authoritative flag.

**Phase 2 — measurement.** Compute both cost legs from the provider receipt,
never the local tokenizer. Gated on Q1 below. Expected to make savings *larger*.

**Phase 3 — period settlement ledger** + 100% warm deduction + replay
origin-cost storage (`origin_usage_log_id` + the origin's token split, captured
at cache-write time — not recoverable today, the cache stores only aggregates).

**Phase 4 — halting conditions** (per-org relative ceiling, zero-spend
concentration). Only then does the cap come off safely.

**Phase 5 — shadow run.** 4 weeks, plus reconciling one real Anthropic invoice
against `actual_cost_usd`. This is not code and cannot be agent-completed; it
needs calendar time and a real invoice.

## Open questions that gate the work

**Q1 — the plausibility quarantine is unvalidated.** The spec quarantines rows
where receipt input > 3× local baseline: 27% of rows, 34% of net savings
[unverified]. Whether those are receipt-parsing bugs or tokenizer bias is
unresolved, and the answer moves roughly a third of revenue. Settle by calling
Anthropic's `count_tokens` on ~50 reconstructed bodies.

**Q2 — no `actual_cost_usd` has ever been reconciled against a real provider
invoice.** Nothing should be billed until one has been.

**Q3 — owner `eb996359`** is reportedly 100% quarantined [unverified] — likely a
broken integration, worth investigating before any invoice.
