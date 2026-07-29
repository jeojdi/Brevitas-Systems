# Billing correctness rollout

Status as of 2026-07-29, branch `chore/retire-per-row-fee-trigger` @ `b96d19f`.
Replaces the `/tmp` spec, which did not survive.

Provenance matters in this document, so every claim is tagged:

- **[verified]** — re-checked against this repo at commit `c8e0c2d`, and
  re-confirmed against `b96d19f` where the phase notes below say so.
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

**Phase 1 — attribution. Landed (code), unverified against production.**
[verified: code at `b96d19f`]
- #2 `organization_id`: `api/store.py` gained `_fill_organization_id`, which
  resolves a blank `organization_id` from `api_keys.organization_id` (the
  authoritative key→tenant binding) on both the SQLite and Supabase read paths.
- #3 `session_id`: `brevitas/proxy.py` gained `_stable_session_id`, a SHA-256
  of the bounded session key. The old code passed `_new_session` to
  `get_or_create` as a zero-arg factory, so every bucket minted a fresh random
  id and receipts for one logical session were unjoinable across a restart or
  an LRU/TTL eviction. The key is hashed, not used directly, because it embeds
  the tenant credential digest and would otherwise breach the 128-char cap.
- Bug #1 remains withdrawn. The authoritative flag was not touched. [verified]
- **Not verified in production.** Whether these actually fix the observed NULL
  and empty columns needs a re-query against `wyfz`. BLOCKED (no credential).

**Phase 2 — measurement. Structure landed, policy deliberately inert.**
[verified: code at `b96d19f`]
`api/server.py` gained `_anchor_token_legs` / `AnchoredTokens`, which anchor the
LEVEL of both cost legs to the provider receipt while leaving the DELTA
untouched (`optimized_tokens == receipt.input_tokens`,
`baseline_tokens == optimized + reported_delta`).

Two consequences the code documents and this plan now adopts:
- **Anchoring cannot repair a zero delta.** The wire carries only
  `baseline_tokens` and `compressed_tokens` from the same local counter. If a
  caller reports them equal, no arithmetic over the receipt reconstructs a
  saving. Recovering claim #4 requires a **new wire field** carrying an
  independent pre-optimization measurement. Anchoring is not a savings-recovery
  mechanism, so the Phase 2 expectation "expected to make savings *larger*" is
  only true for rows that already report a non-zero delta.
- The plausibility quarantine ships as **`observe` (annotate only, never
  changes money)**. `BREVITAS_RECEIPT_ANCHOR_IMPLAUSIBLE_ACTION` falls back to
  `observe` on any unknown value; `drop_savings` is opt-in and **post-Q1 only**.
  The 3.0 ratio is an explicit placeholder, not a validated threshold.

**Phase 3 — period settlement ledger. Partially landed.**
`supabase/migrations/202607280007_period_settlement_ledger.sql` adds
**STRUCTURE ONLY** — it attaches no settlement trigger, writes no row, and no
RPC reads or writes the table. Its only two triggers are immutability guards
(`prevent_period_settlement_delete`, `prevent_period_settlement_identity_change`).
Settlement stays manual. [verified: file at `b96d19f`]
- Warm deduction: expressed as `warm_spend_usd` summed from
  `warm_budget_ledger` over every UTC day overlapping the period. [verified]
- ❌ **Replay origin-cost storage did NOT land.** `origin_usage_log_id` does not
  exist anywhere in `api/`, `brevitas/`, `supabase/`, or `scripts/`. The
  origin's token split is still not captured at cache-write time, so replay
  rows still cannot be costed against their origin. Phase 3 is **not complete**.

**Phase 4 — halting conditions. Landed as structure (0008), plus a 4a fix (0009).**
`202607280008_billing_halting_conditions.sql` adds three circuit breakers; it
attaches **no trigger** and moves no money. [verified]
1. Per-org relative ceiling, bounded at 0.25 by a CHECK so it can be lowered in
   an incident but never raised without a new migration. Savings are netted
   across the period and floored at zero, so a losing week bills $0.
2. Zero-spend concentration.
3. The only breaker that reads the settlement ledger.

`202607280009_billing_arrangement_attestation.sql` (Phase 4a) exists because
breaker 2's predicate was wrong: it classified "no marginal dollar" from
`coalesce(usage.actual_cost_usd, 0) <= 0`, but `usage_log.actual_cost_usd` is
our computed number, not the customer's cost — so it could not actually protect
a PTU / committed-throughput / Enterprise customer. 0009 supplies the missing
attested input. [verified]

⚠️ The cap does **not** come off yet: the breakers are unattached structure, and
Q2 is still unanswered.

**Phase 5 — shadow run. Not started.** 4 weeks, plus reconciling one real
Anthropic invoice against `actual_cost_usd`. Not code; cannot be
agent-completed. Needs calendar time and a real invoice.

## Open questions that gate the work

**Q1 — the plausibility quarantine is unvalidated. STILL OPEN; harness built,
never run.** The spec quarantines rows where receipt input > 3× local baseline:
27% of rows, 34% of net savings [unverified]. Whether those are receipt-parsing
bugs or tokenizer bias is unresolved, and the answer moves roughly a third of
revenue.

`scripts/analysis/token_basis_check.py` now implements the resolution: it
compares local baseline, receipt input, and Anthropic's
`POST /v1/messages/count_tokens` as an independent third witness, classifying
each row `PARSING_BUG` / `TOKENIZER_BIAS` / `UNKNOWN`. It refuses to emit a
verdict from zero samples, never invents a number, and never rewrites a model
ID. Covered by `tests/test_token_basis_check.py`. [verified: code at `b96d19f`]

**BLOCKED — the harness has never been executed against real data.** It needs
(a) an Anthropic API key and (b) sampled production rows *including the request
body as sent*, which is not recoverable from the database and must be captured.
Until it runs, the quarantine stays on `observe` and no number here is settled.

**Q2 — no `actual_cost_usd` has ever been reconciled against a real provider
invoice. STILL OPEN.** Nothing should be billed until one has been. BLOCKED on
a real Anthropic invoice — calendar time, not code.

**Q3 — owner `eb996359`** is reportedly 100% quarantined [unverified] — likely a
broken integration, worth investigating before any invoice. **STILL OPEN,
BLOCKED** on production (`wyfz`) query access.

---

## CI state at `b96d19f` (measured 2026-07-29)

- `./.venv/bin/python -m pytest tests/ -q` — **839 passed**.
- `node --test tests/release_security.test.mjs` — **10/12 pass, 2 fail**
  (baseline at `c8e0c2d` was 12/12).
- `node --test tests/stripe_billing_config.test.mjs` — **8/9 pass, 1 fail**.

**Failure 1 & 2 (release_security) — one root cause, bookkeeping.** Commit
`5be7a62` edited both `202607280008` and `202607280009` but updated only one
line of `scripts/ci/migration-frozen-checksums.txt`, despite the commit message
claiming "with the CI checksum updated to match". 0009 matches; 0008 does not
(recorded `7ec91b8a…`, actual `9e1e80c0…`). `verifyFrozenChecksums` fails →
`run-migration-tests.sh` exits 1 early → the DSN test, which expects exit 2,
fails too. Correcting that one line restores **12/12** (confirmed locally).
0006, 0007, and 0009 all match; migration registration itself is complete in
all four required places.

**Failure 3 (stripe) — a stale release contract, not a code bug.**
`tests/stripe_billing_config.test.mjs:69` asserts
`/fee = round\(verified \* BREVITAS_FEE_RATE, 10\)/`, but `api/server.py:4007`
now reads `fee = round(max(0.0, verified) * BREVITAS_FEE_RATE, 10)`. The clamp
is deliberate and matches the settled decisions above (negative fees rejected,
losing week bills $0, Stripe rejects negative meter values). **Left failing on
purpose:** this assertion is the contract that defines what we charge, and
editing a billing assertion to green a build is a human decision, not an agent
one. Resolve by updating the regex to accept the clamp — or by deciding the
clamp is wrong.
