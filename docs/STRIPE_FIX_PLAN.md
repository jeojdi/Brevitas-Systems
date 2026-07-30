# Stripe Billing Fix Plan

Status: authored 2026-07-29 at `71e20ef` (branch `chore/retire-per-row-fee-trigger`).
Convention: every claim is tagged `[verified]` (reproduced by execution or read against the exact
file/line at this commit) or `[unverified]` (could not be established from this checkout), following
BILLING_CORRECTNESS_PLAN.md. All line numbers below were opened and checked; none are quoted from
memory. Companion document: `docs/STRIPE_TEST_PLAN.md` (test work implied by these fixes).

---

## 1. Executive summary

**Not shippable.** Two independent gates are red before any launch conversation can start:

1. **CI is broken twice over.** The migration integration harness fails on *both* of its paths
   (reproduced against a real PostgreSQL 17.10, exit 3 — `[verified]`), and
   `tests/stripe_billing_config.test.mjs` is red on every PR to `main` because `security.yml` has no
   paths filter (`[verified]`, measured 8 pass / 1 fail). Nothing billing-related can merge with
   confidence until FIX-1..FIX-3 land.

2. **The billing model is mid-surgery.** Since migration `202607280006` dropped
   `queue_brevitas_fee_after_usage` — the *only* writer into `billing_ledger` — nothing in the
   system can queue a fee (`[verified]`: supabase/migrations/202607280006_retire_per_row_fee_trigger.sql:29;
   all four `insert into billing_ledger` statements in the tree live inside successive definitions of
   that trigger's function). The replacement `period_settlement_ledger` (202607280007) has no writer
   by design, and the customer-facing status endpoint still reads the dead table
   (src/app/api/billing/status/route.ts:34-49 `[verified]`).

**The single biggest risk** is not any one bug — it is that the 1,884 lines of new billing SQL that
now define what customers are charged (202607280007/0008/0009) have **zero executable coverage at
any tier** `[verified]`, and the only tier that could cover them (the ephemeral-Postgres job) is the
one that is red. Within that surface, the worst confirmed defect is **PSL-LATCH (FIX-4)**: a
`reported` settlement can be voided, its outbound evidence cleared, and the period re-billed at the
full rate — a reproduced 2x charge (45,000,000 µUSD committed against a 22,500,000 µUSD ceiling,
`[verified]` by execution). It is unreachable today only because no writer and no role privilege
exist yet; it must be fixed **before** the Phase 4 settlement RPC that 202607280007:76-77 promises.

Everything else is ordered work, not crisis. `BREVITAS_BILLING_ENABLED` remains un-flipped
`[verified]`, so no customer money is moving and no fix below is a production hotfix.

---

## 2. Ordered fix list

Sorted by (severity, blast radius, cheapness). Only defects that survived adversarial verification
appear; the two PLAUSIBLE items are at the bottom and flagged "needs reproduction first."

---

### FIX-1 | Repair the migration integration harness (both paths) | **blocker** | est: M (1 day)

- **Consolidates:** MIG-UPGRADE-ABORT, TC-1, TC-2, CF-2, WR-4, MIG-FRESH-012-REPLAY — six claims,
  one defect cluster. Full analysis in §3.
- **Evidence `[verified]`:** scripts/ci/run-migration-tests.sh:86 (last hand-bound upgrade index is
  `upgrade_migrations[39]` = 202607280004), :87-117 (order guard stops there — fails open),
  :390 (upgrade path dies in `run_forward_assertions`), :483 (fresh path replays 202607170012 after
  the fresh loop at :477-479 already applied 202607280006);
  scripts/ci/migration-receipt-accounting-assertions.sql:49-54 (raises if trigger exists);
  supabase/migrations/202607170012_receipt_accounting_alignment.sql:5-25 (raises if trigger absent).
- **Failure scenario:** Upgrade path: trigger still attached (280006 never applied) → inverted
  assertion raises `retired per-row billing trigger has been reattached` → job exits 3
  (`[verified]` by running the harness end-to-end). Fresh path: 280006 drops the trigger, then :483
  replays 202607170012 whose guard requires it → `202607170012 requires the canonical usage and
  billing trigger chain` (`[verified]` by hand-replaying :481-483 on a fully-applied fresh schema).
  The two preconditions are mutually exclusive; no ordering of the current script satisfies both.
- **The fix:** (a) replace the hand-written index bindings at :56-86 with a loop that applies the
  entire upgrade array, and rewrite the guard at :87-117 to compare the whole upgrade array against
  the fresh manifest's tail so a future 46th migration **fails closed**; (b) stop replaying
  202607170012 into a post-280006 schema — either move the frozen 010-013 replay block (:480-484)
  to *before* the 280005+ suffix, or re-apply 202607280006 immediately after each
  `apply_migration "${receipt_migration}"` (:466 and :483). Do **not** edit 202607170012 itself: its
  checksum is frozen, and (critically) it has already been applied to wyfz, which has no migration
  ledger — see §4.4.
  > **CORRECTION (integrator, verified by execution).** The second option is **insufficient on its
  > own at :466**: 202607170012's own guard (:5-25) *raises* when the trigger is absent, so it never
  > gets far enough for a subsequent 202607280006 to run — reproduced as `ERROR: 202607170012
  > requires the canonical usage and billing trigger chain`. The replay must first **re-attach**
  > `queue_brevitas_fee_after_usage` (202607280006 deliberately retains `public.queue_brevitas_fee()`
  > at :18-21 precisely so this is one statement), then apply 202607170012, then re-apply
  > 202607280006 to retire it again. As shipped: the fresh path uses the *first* option (replay moved
  > before the 280005+ suffix, no re-attach needed) and the upgrade path uses the re-attach form at
  > run-migration-tests.sh:596-613, where nothing inserts into `usage_log` between the two applies
  > and `assert_authoritative_counts receipt-reapply` re-proves `billing_ledger` did not grow.
- **Verify:** `DATABASE_URL=postgresql://...@127.0.0.1:5432/x bash scripts/ci/run-migration-tests.sh`
  exits 0 on both paths; the log shows 202607280005-202607280009 applied on the upgrade path.
- **Note:** fixing (a)+(b) alone converts one red into seven — six assertion suites then fail on the
  now-genuinely-absent trigger. FIX-2 must land in the same PR.

### FIX-2 | Re-fixture the six trigger-dependent assertion suites | **high** | est: M (0.5–1 day)

- **Defect ID:** MIG-TRIGGER-FIXTURES (CONFIRMED — all six failures reproduced by executing the
  suites against a fully-applied fresh schema `[verified]`).
- **Evidence `[verified]`:** scripts/ci/migration-assertions.sql:259 ("billing trigger did not
  create a ledger row"); scripts/ci/migration-key-audit-assertions.sql:333 (pure cascade — its
  fixture is created at migration-assertions.sql:299-343, never reached);
  scripts/dr/compliance-workflow-assertions.sql:407; scripts/ci/migration-company-billing-assertions.sql:165-176;
  scripts/ci/migration-billing-recovery-scope-assertions.sql:13 (`INTO STRICT` → no_data_found);
  scripts/ci/migration-compliance-billing-isolation-assertions.sql:90.
- **Failure scenario:** each suite uses the retired trigger as its fixture generator; with the
  trigger genuinely gone, no ledger row appears and the suite raises.
- **The fix:** after each usage_log fixture insert, seed `public.billing_ledger` directly with
  `insert into public.billing_ledger(usage_log_id, organization_id, user_id, occurred_at, fee_microusd)`
  mirroring the retired `queue_brevitas_fee()` arithmetic (there is no BEFORE INSERT trigger on the
  table — only delete/identity guards — so the harness role can do this `[verified]`:
  202607170004_billing_recovery.sql:471-509). Update the frozen checksum for
  `scripts/dr/compliance-workflow-assertions.sql` in scripts/ci/verify-migrations.mjs **in the same
  commit** (it is pinned immediately after 202607170007, verify-migrations.mjs:85-90 `[verified]`).
  migration-key-audit-assertions.sql needs no change — it heals when migration-assertions.sql does.
  > **CORRECTIONS (integrator).** (1) The evidence list above records only the **first** raise per
  > file, so it undercounts `scripts/dr/compliance-workflow-assertions.sql`: that file has **four**
  > trigger-dependent ledger fixtures (`retention-financial`, `member-subject-billing`,
  > `customer-subject-billing`, `compliance-billing-usage`), each guarded by its own
  > `before_count = 0` check. Seeding only `retention-financial` leaves it red — reproduced. All four
  > are seeded as shipped. (2) The frozen digest does **not** live in verify-migrations.mjs; that
  > file only *derives the inventory* of pinned paths (:85-90). The digest itself is a line in
  > `scripts/ci/migration-frozen-checksums.txt`, and the parser at verify-migrations.mjs:455 requires
  > exactly two spaces as the separator. Both were updated as shipped
  > (`1a6a38a8…3ebc4`), together with the matching hard-coded pin in tests/release_security.test.mjs.
- **Verify:** run each of the six files individually via psql against the fresh schema (all must
  pass with the trigger absent), then the full harness.

### FIX-3 | Resolve the red fee-clamp assertion (human decision) | low defect / **high process** | est: XS

- **Defect ID:** BILLING-CFG-TEST-RED (CONFIRMED `[verified]`: measured 8/9 pass; the failure is
  tests/stripe_billing_config.test.mjs:69 asserting `/fee = round\(verified \* BREVITAS_FEE_RATE, 10\)/`
  while api/server.py:4007 reads `fee = round(max(0.0, verified) * BREVITAS_FEE_RATE, 10)`).
- **Failure scenario:** not a behavior break — a stale regex over a deliberate clamp introduced by
  f37067b. But `security.yml` runs `npm test` on every PR to `main` with no paths filter
  (`[verified]`: .github/workflows/security.yml:2-8, :59), so this one assertion blocks the PR that
  fixes FIX-1/FIX-2. It sits on the critical path of everything else in this plan.
- **The fix:** this is explicitly a **human decision**, deferred as such by
  BILLING_CORRECTNESS_PLAN.md (editing a billing assertion to green a build is not an agent call).
  The decision: is `max(0.0, verified)` the intended contract? The clamp is consistent with the
  period-level netting model (per-row fee floors at 0; signedness is preserved in
  `verified_savings_usd` itself — tests/test_cloud_usage_api.py:535 covers it behaviorally
  `[verified]`). If confirmed: update the regex to
  `/fee = round\(max\(0\.0, verified\) \* BREVITAS_FEE_RATE, 10\)/` and keep the sibling rate
  assertion (`BREVITAS_FEE_RATE = 0.25` at api/server.py:3741 `[verified]`) unchanged.
- **Verify:** `node --test tests/stripe_billing_config.test.mjs` → 9/9.

### FIX-4 | Latch settlement send-evidence in `prevent_period_settlement_identity_change` | **high** | est: S (new migration)

- **Defect ID:** PSL-LATCH (CONFIRMED — full double-charge reproduced by execution on a real
  cluster `[verified]`).
- **Evidence `[verified]`:** supabase/migrations/202607280007_period_settlement_ledger.sql:320-332
  (frozen-field list omits `outbound_started_at`, `reported_at`, `settled_at`; `status` deliberately
  not frozen), :338-345 (latches cover only supersession), :157-159 (`void` is a legal status),
  :200 and :207 (`void` exempt from the billing-owner requirement and the live-period unique index);
  supabase/migrations/202607280008_billing_halting_conditions.sql:561-568 (cumulative ceiling counts
  `status in ('sending','reported') OR outbound_started_at IS NOT NULL`).
- **Failure scenario (reproduced):** promote a settlement to `reported` with `outbound_started_at`
  set; the cumulative ceiling correctly refuses a second fee. Then
  `update ... set status='void', outbound_started_at=null, reported_at=null, settled_at=null` is
  **accepted**; the ceiling now sees 0 committed; a second revision at the full fee is accepted.
  Result: 45,000,000 µUSD committed against a 22,500,000 µUSD ceiling for one (org, period) — a
  100% overcharge. Each revision derives a distinct Stripe idempotency key from its row id
  (api/billing_recovery.py:111-117 `[verified]`), so Stripe deduplicates nothing.
- **Reachability today:** none — no writer exists and `has_table_privilege` is false for
  anon/authenticated/service_role on the table `[verified]`. **Must land before the Phase 4
  settlement RPC**, which 202607280007:76-77 defines as "a new security-definer RPC over an
  unchanged table."
- **The fix:** ship a follow-on migration `202607280010` (280007's checksum is frozen — do not edit
  in place) adding one-way latches beside :338-345: for each of `outbound_started_at`,
  `reported_at`, `settled_at`: `if old.<col> is not null and new.<col> is distinct from old.<col>
  then raise;`. Additionally reject leaving `sending`/`reported` for any state that does not
  preserve the markers: `if old.status in ('sending','reported') and new.status <> old.status and
  new.outbound_started_at is null then raise;`.
- **Verify:** the four reproduction UPDATEs above must all raise; the cumulative-ceiling refusal
  must survive a `void` attempt. Add these to the new settlement assertion fixture (§5).

### FIX-5 | Repoint / fail-safe the billing status endpoint | **medium** | est: S

- **Defect IDs:** AR-1 + TC-3 (CONFIRMED, same defect `[verified]`).
- **Evidence `[verified]`:** src/app/api/billing/status/route.ts:34-39 (selects from
  `billing_ledger` only), :44-49 (sums), :60-65 (money fields + `needs_review`);
  202607280006:29 (only writer dropped); `period_settlement_ledger` referenced nowhere in src/ or
  dashboard/src `[verified]`; dashboard/src/components/Billing.jsx:162-163 renders the estimate.
- **Failure scenario:** *latent, not live* — today `period_tracking_valid` is false (no
  Stripe-written boundaries), so fields are null and the UI shows "Unavailable." The moment
  `BREVITAS_BILLING_ENABLED` flips and a real subscription supplies boundaries, every customer sees
  `estimated_fee_usd: $0.000000` forever, regardless of real usage — a wrong-money display with no
  error anywhere.
- **The fix:** two stages. Now: return `estimated_fee_usd`/`reported_fee_usd` as `null` with an
  explicit `settlement_pending: true` flag so a disconnected total can never render as $0. At Phase
  3 wiring: repoint the query at `period_settlement_ledger` filtered on `organization_id` and
  `period_start = account.current_period_start`, excluding `draft`/`void` from the estimate and
  counting only `reported` as reported (note the new `void` status the current filter at :45 does
  not know about `[verified]`: 202607280007:157).
  > **CORRECTIONS (integrator) — READ BEFORE "FIXING" THE SHIPPED ROUTE BACK.** Two errors in the
  > stage-two sentence above; both were reproduced, and the shipped route deliberately deviates.
  >
  > 1. **"Exclude `draft`/`void` from the estimate" produces the very bug FIX-5 exists to kill.**
  >    Settlement only happens *after* a period closes, so the **current** period has no non-draft
  >    row and that rule returns `0` for every customer forever. As shipped, `estimated_fee_usd` is
  >    an evidence **projection** — `least(period_settlement_fee_microusd(net_after_warm,
  >    net_verified), fee_ceiling_microusd)` over the same evidence the settlement guard reads, which
  >    converges exactly to the settled fee once the week closes. The plan's ledger rule lives on a
  >    **new** field, `settled_fee_usd` (the live row's fee once its status is neither `draft` nor
  >    `void`), which is where it is actually correct.
  > 2. **The route cannot read `period_settlement_ledger` at all.** 202607280007:232-233 revokes
  >    *every* privilege on it from `service_role`; its own self-check (:467-474) plus
  >    scripts/ci/migration-period-settlement-assertions.sql and
  >    scripts/ci/migration-settlement-writer-assertions.sql all assert 7 privileges × 3 PostgREST
  >    roles are absent. A PostgREST select is permission-denied, and "fixing" that with
  >    `grant select … to service_role` turns three CI files red and dismantles the Phase 4 privilege
  >    model. The read goes through the SECURITY DEFINER RPC
  >    `public.billing_period_settlement_summary` (202607280013).
  >
  > Also note the user-visible half: `dashboard/src/components/Billing.jsx`'s `fmt()` is
  > `Number(n || 0).toFixed(decimals)`, so returning `null` from the route is **necessary but not
  > sufficient** — `fmt(null, 6)` renders the string `"0.000000"`. The card now routes every fee
  > through a guarded `fmtMoney()` that renders `Unavailable`, pinned by
  > tests/billing_dashboard_money_display.test.mjs (dashboard/** is excluded from the root eslint
  > config and has no component runner, so nothing else would catch a regression there).
- **Verify:** handler-level test (the route currently has zero executable coverage — only text
  assertions at tests/stripe_billing_config.test.mjs:54-56 `[verified]`); pin the table name with a
  text assertion so it cannot silently drift again.

### FIX-6 | Require https `BREVITAS_PUBLIC_URL` when deployed | **medium** | est: XS

- **Defect ID:** CF-1 (CONFIRMED `[verified]`).
- **Evidence `[verified]`:** src/lib/billing/config.ts:18 (defaults to `http://localhost:3000`),
  :29 (accepts localhost/127.0.0.1 with no environment test — re-checked at this commit), :33-44
  (`safePublicUrl` is the only URL condition); consumed at
  src/app/api/billing/checkout/route.ts:268-269 (success/cancel URLs) and
  src/app/api/billing/portal/route.ts:49 (return_url). Exactly one reader repo-wide; nothing in
  scripts/ci checks it. docs/ENV_ROLLOUT_CHECKLIST.md:58 claims "billingIsConfigured() requires
  https," which is **false** — the doc must be corrected too.
- **Failure scenario:** the var is missed on Vercel (the checklist itself lists it "VERIFY / likely
  MISSING" at :344 `[unverified]` as to actual Vercel state — Sensitive vars are unreadable), billing
  flips on, and a paying customer's Checkout success_url is `http://localhost:3000/dashboard` — a
  dead-end redirect after entering card details. Fails open where every comparable var
  (`BREVITAS_API_URL`, next.config.ts:31-51) fails the build.
- **The fix:** in `billingIsConfigured()`:
  `const deployed = process.env.NODE_ENV === 'production' || Boolean(process.env.VERCEL_ENV);`
  and accept localhost only when `!deployed`; drop the localhost default in `billingConfig()` when
  deployed. Add `BREVITAS_PUBLIC_URL` to the release-preflight required-var list.
- **Verify:** truth-table unit test over the (refactored, env-injectable) predicate — see
  STRIPE_TEST_PLAN.md; plus the preflight assertion.

### FIX-7 | Tolerate absent org metadata on legacy Checkout sessions | **medium** | est: S

- **Defect ID:** AR-2 (CONFIRMED by read-trace; not executed — no Stripe access under this pass's
  rules, so the end-to-end path is `[verified]` in code, `[unverified]` in execution).
- **Evidence `[verified]`:** src/lib/billing/checkout-reservation.mjs:86 (unconditional throw when
  `session?.metadata?.brevitas_organization_id !== organizationId`) vs :89-92 (a *missing
  generation* is tolerated — tolerance is on the wrong field); the backfill at
  supabase/migrations/202607200014_billing_checkout_session_reservations.sql:46-67 persists
  pre-reservation sessions that were created with `brevitas_user_id` metadata only (confirmed via
  `git show 53715dc`); route path: checkout/route.ts:134 → inspect throws → :145-147 manualReview →
  reservation forced to `manual_review`, and **no code path anywhere writes state back** to
  reserved/persisted `[verified]`. Result: permanent 503 short-circuit at 202607200014:153-156.
- **Failure scenario:** any company whose `billing_accounts.checkout_session_id` predates the
  reservation migration is permanently locked out of Checkout with an operator-opaque 503.
  Reachability today: zero — 202607280006:14 records 0 billing_accounts rows in production
  `[verified]` — so this is a pre-launch correctness fix, not an incident.
- **The fix:** in checkout-reservation.mjs replace :86-88 with a present-but-mismatched check
  (`typeof persistedOrganization === 'string' && persistedOrganization !== organizationId` → throw),
  mirroring the generation tolerance; apply the same form to the org half of :59 for the open-session
  recovery scan (keep the generation half strict there). Fix the inverted test fixture at
  tests/billing_checkout_session_reservation.test.mjs:96-98, which currently pins the *opposite* of
  what shipped `[verified]`.
- **Verify:** unit tests on `inspectPersistedCheckoutSession` with a legacy-shaped session
  (`metadata: { brevitas_user_id }`) must return the open session rather than throw.

### FIX-8 | Make `billing_supervisor` actually supervise | **medium** | est: S

- **Defect ID:** WR-1 (CONFIRMED `[verified]` by read; the restart/escalation block is dead code
  under *every* execution, not just failures).
- **Evidence `[verified]`:** api/worker.py:777-778 (`await inner` guarded only by
  `except asyncio.CancelledError`), :783-811 (**precision (integrator): not unreachable in its entirety on a clean
  return** — the "loop stopped" health report and the `break` DO execute; what was unreachable in
  practice is the restart/backoff/escalation tail from `inner.exception()` onward. The conclusion —
  escalation never runs — is correct as written. And since
  api/billing_recovery.py:947's `while not stop.is_set()` is the only clean exit, a normal return
  always hits the `break` — so `inner.exception()` at :791 is provably always None), :938
  (`await billing_task` in run()'s finally with no try — a stored exception skips shutdown cleanup
  at :939-966).
- **Failure scenario:** an unhandled exception escaping the loop's internal try blocks kills the
  supervisor silently; the advertised restart/backoff/escalation (comment at :752-761) never runs.
  Escape probability is low (the plausible raisers are already caught `[verified]`), hence medium
  not high. Additional wart: for a non-required billing worker, /ready hardcodes
  `billing_ready=True` (api/worker.py:229-238), so a dead loop reports "ready" forever.
- **The fix:** add `except Exception as exc:` after :778 that marks `_BILLING_LOOP_RUNNING = False`
  and falls through to the restart/backoff/escalate block, using `exc` for `error_type` (delete the
  dead `inner.exception()` read); wrap `await billing_task` at :938 in try/except so supervisor
  failure cannot skip cleanup; compute the non-required /ready status from the real snapshot.
- **Verify:** new asyncio unit tests driving a loop factory that raises N times then succeeds
  (restart count, escalation at max, no `.cancel()` ever issued) — currently zero tests reference
  `billing_supervisor` `[verified]`.

### FIX-9 | Check `recurring.interval_count` in all three catalog validators | **low** | est: XS

- **Defect ID:** WR-3 (CONFIRMED `[verified]`: `interval_count` appears nowhere in api/, src/,
  tests/, scripts/, supabase/, dashboard/src).
- **Evidence `[verified]`:** api/billing_recovery.py:448; src/lib/billing/config.ts:68;
  scripts/setup-stripe-billing.mjs:56-62 (reuse guard also omits it). Downstream fail-closed chain:
  202607170004_billing_recovery.sql:78-82 raises on any non-7-day anchor;
  202607200006_company_billing_authorization.sql:386-389 converts that to `review`;
  status/route.ts:26-30 blanks the UI.
- **Failure scenario:** a `week`/`interval_count: 2` price passes every validator; subscriptions get
  14-day anchors; every fee row parks in `review` and the dashboard shows "Unavailable." Everything
  fails closed (no wrong charge), hence low — but it presents as a total, confusing revenue stall.
- **The fix:** `or recurring.get("interval_count") not in (None, 1)` at api/billing_recovery.py:448;
  `|| (price.recurring?.interval_count ?? 1) !== 1` at config.ts:68; same condition in the
  setup script's reuse guard.
- **Verify:** parametrized catalog-mutation tests on both validators (offline, injected
  session/client — see STRIPE_TEST_PLAN.md).

### FIX-10 | Stop burning attempts on Stripe 429 after `begin_send`; wire the dropped metric | **low** | est: S

- **Defect ID:** WR-2 (CONFIRMED `[verified]`, including the release-RPC no-op reproduced on a real
  cluster: with `outbound_started_at` set, `release_billing_ledger_leases` returned 0 rows).
- **Evidence `[verified]`:** api/billing_recovery.py:710-719 (`except StripeUnavailable` after
  begin_send calls `release_owner`, a documented no-op once the marker is set —
  202607170004:399-407); attempts re-increment on every reclaim (202607170004:290); sweep to
  `review` at attempts>=max_attempts (202607200006:349-353); `billing.stripe_unavailable` has no
  branch in brevitas/observability.py:666-687 and is silently dropped, along with
  `billing.pending_count` and `billing.catalog_validation_error`.
- **Failure scenario:** ~4-5 sustained rate-limited cycles strand a provably-unsent fee in `review`
  (recoverable by hand via the 6-arg resolve RPC; alerted on the next health cycle — hence low).
  The only metric distinguishing "Stripe rate-limited us" never reaches the backend.
- **The fix:** add a lease-fenced RPC `release_billing_ledger_unsent(p_entry_id, p_owner)` that
  resets `status='pending', attempts=greatest(0,attempts-1), outbound_started_at=null` — sound
  precisely because 429 is documented non-ingestion (api/billing_recovery.py:372-378) — and call it
  from the post-begin_send `StripeUnavailable` branch. Add the three missing metric branches to
  `record_billing_metric`, with a contract test that every emitted name has a branch.
- **Verify:** processor-level test with an injected 429 after begin_send: row returns to `pending`
  with attempts decremented; metric contract test fails on exactly zero names.

### FIX-11 | `warm_spend_days` counts (provider, day) rows, not days | **low** | est: XS (follow-up migration)

- **Defect ID:** EVIDENCE-WARM-DAYS (CONFIRMED `[verified]` by execution: two providers, one day →
  `warm_spend_days=2`).
- **Evidence `[verified]`:** supabase/migrations/202607280008_billing_halting_conditions.sql:352-358
  (`count(*)` over a table whose PK is (organization_id, provider, day) — 202607280001:80-88);
  surfaces only in operator diagnostics (:474, :477, :614) — no money arithmetic consumes it.
- **The fix:** `count(distinct warm.day)` in a follow-up migration that recreates
  `billing_period_settlement_evidence` (280008 is checksum-pinned).
- **Verify:** assertion in the halting-conditions fixture (§5): 2 providers × 1 day → 1.

---

### PLAUSIBLE — needs reproduction before filing as a fix

**P-1 | TC-4: no pinned Stripe API version on the money path** — facts `[verified]`
(api/billing_recovery.py:353-355 sets no `Stripe-Version`; Node SDK pins `2026-06-24.dahlia` by
default via config.ts:50-52 omitting `apiVersion`; scripts/ci/staging-canary.mjs:303 hard-codes
`2025-06-30.basil` — three versions, nothing asserting agreement). The *consequence* is
`[unverified]`: Stripe fixes an account's default version at first API call and does not silently
move active accounts, so "the account default drifts" is asserted, not established. **Reproduction
required:** read the target account's default API version (test-mode dashboard) and diff the
`GET /v1/prices/{id}` payload with and without an explicit version header against the fields both
validators read. Regardless of the repro outcome, the cheap hardening is worth doing at Phase-4
time: one shared version constant used in all three places, plus one test asserting byte-identity.

**P-2 | CF-4: Stripe self-service gated on Railway API availability** — structure `[verified]`
(Billing.jsx:104 returns early without `apiKey`; the portal route itself needs nothing from Railway
— portal/route.ts:14-50), but the claimed causal chain to line 104 during an outage is wrong: a
mint failure trips App.jsx:743-757's full-page error before any tab renders, so the whole dashboard
(not just billing) is down — different bug, same coupling. **Reproduction required:** run the built
SPA with no FastAPI on :8000 and record exactly which screen a signed-in user gets. The fix (hoist
the billing card above the apiKey/stats gates so it renders on `accessToken` alone) is UI work that
should follow the repro, not precede it.

---

## 3. The CI blocker: 528bae5 (`retire the per-row fee trigger`, Phase 0)

### What actually happens `[verified by execution]`

The received ground truth ("~6 suites still expect the trigger; harness never applies 280005/280006")
is imprecise on both clauses — corrected here from reproduction:

- **Upgrade path** (dies first, at run-migration-tests.sh:390): applies migrations only through
  `upgrade_migrations[39]` = 202607280004 (:86); 280005-280009 are in the manifest but never bound
  and never applied. The trigger from 20260716_stripe_billing.sql:97 is therefore still attached
  when `run_forward_assertions` runs migration-receipt-accounting-assertions.sql, whose assertion
  528bae5 *inverted* (:49-54 now demands absence). Actual output:
  `ERROR: retired per-row billing trigger has been reattached`. Exit 3.
- **Fresh path** (never reached in CI because the upgrade path aborts first, but independently
  broken): the generic loop at :477-479 **does** apply 280005-280009 — then :483 replays frozen
  202607170012, whose precondition (:5-25) requires the trigger 280006 just dropped. Actual output:
  `ERROR: 202607170012 requires the canonical usage and billing trigger chain`.
- **After the ordering is fixed**, six suites fail on the genuinely-absent trigger (the FIX-2 list),
  measured by running the full `run_forward_assertions` sequence on the fresh schema: 18 pass /
  6 fail. Two more files (migration-upgrade-baseline-fixture.sql:37-45,
  migration-upgrade-assertions.sql:28-32) are trigger-dependent but survive because their fixture
  row is created at :223, before the chain reaches the drop — they need **no change** provided the
  upgrade path keeps creating the baseline row before applying 280006.
- The static layer is structurally blind to all of this: `node scripts/ci/verify-migrations.mjs`
  exits 0 and `tests/release_security.test.mjs` is 12/12 at 71e20ef `[verified]` (the 0008 checksum
  failure recorded in BILLING_CORRECTNESS_PLAN.md is fixed on this branch).

### Files that must change

| File | Change |
|---|---|
| scripts/ci/run-migration-tests.sh | Loop the full upgrade array (replace hand bindings :56-86); order guard :87-117 fails closed on unbound entries; fresh-path replay ordering fixed (:480-505) so 202607170012 never replays into a post-280006 schema |
| scripts/ci/migration-assertions.sql | Seed billing_ledger directly (:240-259 block); keep the claim/reclaim exercise at :282-296 running |
| scripts/ci/migration-company-billing-assertions.sql | Direct ledger seeds for both companies (:165-176) |
| scripts/ci/migration-billing-recovery-scope-assertions.sql | Direct ledger seeds for the `INTO STRICT` fixtures (:7-20) |
| scripts/ci/migration-compliance-billing-isolation-assertions.sql | Direct ledger seeds (:71-93, re-checked at :246-251) |
| scripts/dr/compliance-workflow-assertions.sql | Direct ledger seed for `retention-financial` (:327-328, joined at :406-407) |
| scripts/ci/verify-migrations.mjs | Register 202607280010-202607280013 in `expectedFreshMigrationOrder` (the upgrade order and the frozen-checksum inventory are both derived from it). **The digests themselves live in scripts/ci/migration-frozen-checksums.txt, not here** — including the refrozen compliance-workflow-assertions.sql. The manifest-coverage assertion landed in tests/release_security.test.mjs (U5), not here |
| scripts/ci/migration-key-audit-assertions.sql | **No change** (cascade heals) |
| scripts/ci/migration-upgrade-baseline-fixture.sql, migration-upgrade-assertions.sql | **No change**, but document the ordering dependence |

**Must run-migration-tests.sh learn to apply 202607280005/280006?** Yes — and 280007/0008/0009 with
them, on the upgrade path. Anything else leaves the entire generation-3 billing schema with zero
DB-level coverage while its checksums imply otherwise.

### The decision it forces

**Option A — restore the trigger** (revert 280006's drop and un-invert the two receipt assertions).
Cheapest CI fix (~2 file reverts), and 202607280006:31-35 deliberately kept `queue_brevitas_fee()`
so re-attachment is one CREATE TRIGGER. **Rejected**, because it is not just a CI choice: 280008 and
280009 *refuse to install while the trigger is attached* (`[verified]`:
202607280008:220-240, 202607280009:112-129 — "The per-row path bypasses these conditions
entirely"). Restoring the trigger means abandoning the halting-condition/attestation model, i.e.
reversing the entire Phase 0-4 direction, and reviving a per-row writer that bypasses every new
circuit breaker the moment verified savings resume.

**Option B — migrate the assertions to the new model and fix the harness ordering** (FIX-1 + FIX-2
as specified). More files touched, but every change is in test/CI fixtures, none in shipped
migrations, and it makes CI exercise the schema production is actually headed for.

**Recommendation: Option B.** It is the only option consistent with migrations already
checksum-frozen and (possibly — see §4.4) already applied to production. Sequencing note: FIX-3
(the red regex) must be resolved first or in the same PR, since `security.yml` blocks the merge.

---

## 4. Production-truth items (deployment questions, not code bugs)

These need diagnostics, not speculative fixes. All production reads must go through the sanctioned
one-file path (`supabase db query --linked -f <file>.sql`) — never `db push`, never a blind chain
replay (wyfz has no migration ledger).

### 4.1 No verified savings since 2026-07-17

`[verified]` as recorded ground truth; root cause `[unverified]` from this checkout. The only code
path that can mint an authoritative row with non-zero savings is the in-process hosted-proxy bridge
`_hosted_proxy_receipt` (api/server.py:4959-4972, wired at :5040-5041 `[verified]`) — **not** the
worker. So the question is "are requests traversing the hosted proxy, and are they verifying?", not
"is the worker up?". Settle it with:

```sql
select date_trunc('day', ts) as day,
       count(*) filter (where authoritative) as auth_rows,
       count(*) filter (where authoritative and verified_savings_usd > 0) as auth_with_savings,
       count(*) filter (where authoritative and mode='byte_preserving'
                          and quality_status='verified') as verified_rows,
       max(ts) as last_row
from public.usage_log
where ts >= '2026-07-15'
group by 1 order by 1;
```

If `auth_rows` is nonzero but `auth_with_savings` is zero, the proxy is receiving traffic that fails
one of the four gates at api/server.py:3997-4006 (authoritative ∧ byte_preserving ∧
quality_status='verified' ∧ strategy≠cache_warm) — break the count down by `mode`,
`quality_status`, `strategy_name` next. If `auth_rows` is zero, traffic is not traversing the hosted
proxy at all (routing/deployment question, not billing code).

### 4.2 Is the worker emitting authoritative rows at all — and is it even booting?

Two separable checks. (a) Worker-authored rows are authoritative-but-zero-savings **by design**
(api/worker.py call sites pass no savings fields; api/server.py:1590-1601 defaults
authoritative=True `[verified]`) — do not treat zero-savings worker rows as a defect.
(b) The real deployment question `[unverified]`: deploy/railway-worker.json:9 pins
`BREVITAS_WORKER_BILLING_ROLE=authoritative`, and api/worker.py:735-736 raises at boot when
`billing_recovery_is_configured()` is false — which it is, since `BREVITAS_BILLING_ENABLED` is
un-flipped. **If the deployed service uses the tracked start command, the worker is crash-looping
and consuming no durable jobs.** Check: Railway dashboard → worker-production → restart history +
`GET /ready` (the billing_recovery block, api/worker.py:261-271). If it is crash-looping, the
runbook fork at docs/GO_LIVE_RUNBOOK.md:89-96 applies: override the role to `nonbilling` in the
Railway dashboard only (the repo default must stay `authoritative` —
tests/test_production_topology asserts it).

### 4.3 The 1,897 hand-repriced fee rows

`[verified]` as recorded ground truth (hand-repriced to 25% on 07-29); their treatment by the new
model `[verified]` in code: the settlement evidence predicate is
`authoritative AND pricing_status='priced'` (202607280008:361-366), and every usage row since 07-17
is authoritative=false, so the hand-repriced rows' *usage* cannot enter a settlement. What must be
settled by query is whether the 1,897 rows are `billing_ledger` rows (fee side) and what statuses
they hold, since anything in `sending`/`reported`/outbound-marked `review` would count toward a
period's cap arithmetic if the per-row path were ever revived:

```sql
select status, count(*), sum(fee_microusd) as total_microusd,
       min(occurred_at), max(occurred_at),
       count(*) filter (where outbound_started_at is not null) as outbound_marked
from public.billing_ledger
group by status order by status;
```

Expected if the trigger's absence predates them: 0 rows (the 07-29 repricing was on usage rows, not
ledger rows). Any nonzero result changes the FIX-5 repoint and needs a decision on
disposition (manual resolve vs. leave as historical evidence — ledger rows are undeletable by
design).

### 4.4 Does wyfz have the trigger attached? (the 202607170012 replay landmine, in production form)

`[unverified]` and **decision-critical**: on CI this mismatch is a red job; on wyfz it decides
whether production is currently on the per-row path (a live trigger that would start writing fees
the moment verified savings resume, **bypassing every halting condition**) or on the aggregate path
(no writer, nothing bills). One read-only catalog query settles it:

```sql
select tgname, tgenabled from pg_trigger
 where tgrelid='public.usage_log'::regclass and not tgisinternal order by tgname;
select to_regclass('public.period_settlement_ledger') is not null as has_settlement,
       to_regclass('public.billing_halting_conditions')  is not null as has_halting,
       to_regclass('public.organization_billing_arrangement') is not null as has_attestation;
```

If the trigger is attached and the 280007-0009 tables are absent: wyfz is pre-Phase-0, and
202607280006 must be applied (one file, sanctioned path) before any launch step — after which
202607170012 must never be replayed there. Record the result in the migration-ledger-gap memory doc
either way.

### 4.5 A defect in 202607280007's own Phase-4 guidance (integrator finding, for the CLAIM RPC)

`202607280007_period_settlement_ledger.sql:73-76` tells whoever writes the settlement *claim* RPC
that `expected_period_microusd` may come from "the row's own `fee_microusd` (a period ledger has one
row per period, so the per-period sum collapses to the row itself)". **That is false once the
revision chain is used**, and `202607280008_billing_halting_conditions.sql:98-119` explains why: a
revision is a **second** Stripe charge that SUMS onto the period meter. If revision 1 reported
100 µUSD and a later revision reports 50, Stripe's aggregate is 150 while a row-local `expected` is
50, and reconciliation mismatches forever.

The claim RPC MUST compute `expected_period_microusd` as the **sum over committed rows** for the
(organization, period), exactly as `claim_billing_ledger_entries` computes `committed`
(202607170004:269-276). The shipped writer contains the hazard today by refusing to write **any**
revision once `committed_period_microusd > 0`, so nothing can currently reach the bad state — but the
claim RPC must not inherit that sentence. 202607280007 is checksum-frozen and already applied, so the
comment cannot be corrected in place; this note is the correction of record.

---

## 5. Test work implied by the fixes

The full matrix lives in **docs/STRIPE_TEST_PLAN.md**. Minimum regression per fix in this plan:

- **FIX-1/FIX-2:** a static coverage assertion in tests/release_security.test.mjs that every
  manifest entry is applied by the harness (fails today naming 280005-280009); the harness itself
  green on both paths.
- **FIX-3:** behavioral fee tests on the Python side (positive → 0.25×verified; negative → 0),
  regex reduced to the rate constant.
- **FIX-4:** new `scripts/ci/migration-period-settlement-assertions.sql` wired into
  `run_forward_assertions`: 25% CHECK boundary, generated zero-floor, delete/identity guards,
  live-period uniqueness, weekly-window CHECK, id-space start ≥ 1e9, **and** the four PSL-LATCH
  reproduction UPDATEs raising. Companion `migration-halting-conditions-assertions.sql` for the
  three breakers against non-empty evidence (all five `halting_condition=` outcomes) and the
  attestation privilege posture — 1,884 lines of money SQL currently have zero executable coverage.
- **FIX-5:** first executable route test in the repo (status): seeded periods (exact 604800000 ms,
  ±1 ms, DST week), half-open boundary rows, per-status bucket sums, `settlement_pending` flag.
- **FIX-6:** truth-table test over an env-injectable `billingIsConfigured()`; preflight name check.
- **FIX-7:** legacy-session unit tests on `inspectPersistedCheckoutSession` + corrected fixture.
- **FIX-8:** supervisor asyncio tests (restart, escalation, never-cancel).
- **FIX-9:** catalog-mutation tables on both validators, offline.
- **FIX-10:** injected-429 processor test + the emitted-metric/handled-metric contract test.
- **Cross-cutting (from STRIPE_TEST_PLAN.md, not per-fix):** offline webhook signature tests via
  `stripe.webhooks.generateTestHeaderString` (verified working with a dummy key — the trust boundary
  currently has zero executable coverage), and the Stripe test-mode bootstrap
  (`npm run billing:setup` has never been executed by anything `[verified]`; local setup is
  greenfield — no CLI, zero STRIPE_* keys in .env.local).

---

## 6. Explicitly out of scope / deliberately not fixed

Do not refile these.

1. **`authoritative=false` on proxy-reported rows.** Intentional anti-forgery boundary
   (api/server.py:4090-4098 forces it on POST /v1/usage `[verified]`; see
   BILLING_CORRECTNESS_PLAN.md). Not a defect; also the designed reason INV-1's arithmetic is
   currently unexercised in production.
2. **REFUTED: "docs instruct `cd dashboard && npm run build`, shipping a broken bundle."** The
   stale commands are real (ENV_ROLLOUT_CHECKLIST.md:139, :364), but `public/dashboard/` is
   gitignored and vercel.json's buildCommand regenerates the bundle through the guarded
   `scripts/build-dashboard.mjs` on every deploy — no deploy path ships a local mis-build. Residual
   work: correct the two doc lines. Docs-only; not in the fix list.
3. **REFUTED: "webhook 503 + no re-sync path permanently corrupts subscription state."** The same
   config predicate 503s Checkout *creation* first (so no charge exists to lose), and
   `applySubscriptionEvent` re-syncs from the weekly subscription/invoice event stream once config
   is fixed. Residual (accepted, not fixed now): the 503 carries no per-condition diagnostic, and
   `recoverySecretIsStrong`/`safePublicUrl` are Vercel-only conditions not mirrored in the worker's
   predicate — worth a log line at Phase-4 time.
4. **The red fee-clamp test as a "regression."** It is a documented, deliberate red pending a human
   decision (BILLING_CORRECTNESS_PLAN.md) — handled as FIX-3, not as a code defect.
5. **Dead/undocumented env config** (`BREVITAS_BILLING_BATCH_SIZE` read by nothing yet pinned by
   tests/test_observability.py:702; `BREVITAS_BILLING_WORKER_ID` bypassed on the worker path;
   readiness vars absent from .env.example) `[verified]`. Cleanup, not launch-blocking; covered by
   the env-manifest test in STRIPE_TEST_PLAN.md.
6. **docs/STRIPE_BILLING.md staleness** (still credits the dropped trigger at :10; manifest ends at
   202607200017 at :135-146; "generate priced verified usage" step at :289 is impossible today)
   `[verified]`. Doc rewrite belongs with the Phase-3 wiring PR, not this fix list.
7. **Phase 5: provider-invoice reconciliation of `actual_cost_usd`.** Calendar-time human gate (a
   real Anthropic invoice must be reconciled once before any period leaves `draft`); no code fix can
   discharge it. Recorded in BILLING_CORRECTNESS_PLAN.md Q2.
8. **The plausibility-quarantine numbers** (27% of rows / 34% of net savings; owner eb996359's 100%
   quarantine) remain `[unverified]` — production data questions under §4's rules, not code fixes.
9. **Two-replica authoritative worker + `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=false`**
   (deploy/railway-worker.json:8 vs GO_LIVE_RUNBOOK.md:131 "exactly ONE authoritative worker")
   `[verified]` as a tension, but it is a launch-configuration decision for the flip checklist —
   with the shipped default, reconciliation-ACCEPTED is deliberately unreachable and safety rests on
   SKIP LOCKED + lease fencing, which is coherent. Decide at flip time; do not "fix" in code now.
