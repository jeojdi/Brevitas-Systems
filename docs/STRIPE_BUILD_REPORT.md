# Stripe Billing Build Report

Written 2026-07-29 against branch `chore/retire-per-row-fee-trigger` @ `71e20ef` (all work is
uncommitted, in the working tree). Companion specs: `docs/STRIPE_FIX_PLAN.md`,
`docs/STRIPE_TEST_PLAN.md`. Convention as in BILLING_CORRECTNESS_PLAN.md: every claim is tagged
`[verified]` (I ran it or read the exact lines today, or the executing lane pasted real output that
I could reconcile) or `[unverified]` (inherited on trust, or the tree has moved since it was
measured).

---

## SHIP-PREP — 2026-07-30

Appended after the G9 lane, the wyfz apply-runbook lane, the savings-drought lane and the commit
plan all completed. This section supersedes the two stale G9 mentions below (the §5 G9 row
"still-open — nothing shipped touches this" and the Tier-3 box's "still unpinned, see G9") — both
now describe a closed gate; they are kept as historical record. `docs/STRIPE_TEST_PLAN.md:21,206`
are stale the same way.

### 1. Production probe (wyfz, read-only, 2026-07-29/30)

`[verified — probe output supplied by the supervisor, reconciled against docs/WYFZ_APPLY_PLAN.md §1]`

- **Schema stops at `202607280004`.** `period_settlement_ledger`, `billing_halting_conditions`,
  `organization_billing_arrangement`, `release_billing_ledger_unsent` are all ABSENT.
- **The per-row fee trigger is NOT attached** — `usage_log` has zero non-internal triggers — and
  **`billing_ledger` has ZERO rows.** Consequence: the `280005`–`280013` apply cannot double-write
  anything, and `202607280006` is a checked no-op there.
- **Traffic is real but 100% unbillable.** Per-day totals: 07-15 `2715`, 07-16 `3662`, 07-17 `551`,
  07-18…07-26 **no rows at all**, 07-27 `5583`, 07-28 `11406`, 07-29 `12418`, 07-30 `1491` — and
  on EVERY day `authoritative=0`, `authoritative-with-savings=0`, `priced=0`.
- **Open contradiction, gating Window B of the apply** `[unverified]`: session memory says 1,897
  fee rows were hand-repriced to 25% on 07-29, but the probe found `billing_ledger` empty. Both
  cannot describe the same table (likely `billing_events` or another project). `202607280007`
  aborts on a non-empty ledger with high ids; resolve before applying it.

### 2. G9 (one pinned Stripe API version) — CLOSED, review verdict "ship-with-fixes"

`[verified — lane pasted real runs I reconciled; suites re-run at final state]`

All three Stripe callers now pin `2026-06-24.dahlia`: `src/lib/billing/config.ts`
(`STRIPE_API_VERSION` passed to the SDK), `api/billing_recovery.py` (`Stripe-Version` merged
last in `_request`, so callers can neither drop nor downgrade it; all four endpoints funnel
through `_request`), `scripts/ci/staging-canary.mjs` (both basil literals replaced). Guarded by
two new suites (`tests/stripe_api_version_pin.test.mjs` 6/6, `tests/test_stripe_api_version_pin.py`
6/6) with a demonstrated mutation kill; one byte-exact assertion in `tests/test_billing_recovery.py`
was legitimately red and re-pinned. Final state: npm 292/292, pytest 1020, tsc/eslint clean.
Live TEST-mode check (sk_test only, read-only GETs): `validate_contract()` PASS with the header
on the wire, plus a negative control (bogus version → HTTP 400) proving the header is read.

Review verdict: **ship-with-fixes**, 1 high / 3 medium / 4 low. The high: the one money-moving
call, `POST /v1/billing/meter_events`, was never exercised under the pin, and a version-class 400
there maps to `StripeRejected` → status `dead` — silently discarded fees. Prescribed fixes: re-run
the Tier-3 sandbox worker E2E (send+reconcile) with the pin in place, and make version-rejection
400s non-terminal (`StripeUnavailable`, park-and-retry). Mediums: webhook payload version is
governed by the endpoint's dashboard config, not the SDK pin (canary should assert
`webhook_endpoints[].api_version === STRIPE_API_VERSION`); two regex holes in the stray-literal
guard; the bypass guard doesn't bar `self.session.post(` inside the gateway. Lows include pinning
`"stripe"` exactly in package.json (caret range + typed `LatestApiVersion` means an SDK bump goes
red at build) and `scripts/setup-stripe-billing.mjs` agreeing with the pin only by coincidence.
None of the fixes has been applied yet `[verified — none of the prescribed edits are in the tree]`.

### 3. New companion docs (both read in full today)

- **`docs/WYFZ_APPLY_PLAN.md`** (2,245 lines) `[verified — read]`: hand-apply runbook for wyfz
  `202607280004 → tip`. Verdict: apply **all 20 files** (`280005`–`280024` — `280024` is NOT
  optional once `280015` lands, or browser roles keep default GRANT ALL incl. TRUNCATE) in three
  windows, **DB-first** relative to code deploy, one file per `supabase db query --linked -f`,
  never `db push` (wyfz has no migration ledger). Its headline finding reordered the plan:
  `202607280017` goes FIRST because the deployed worker purges `warm_budget_ledger` on a 7-day
  horizon every 300s, and that ledger is the only settlement evidence for warm spend — a live,
  ongoing destruction of billing evidence; the no-DDL stopgap is setting
  `BREVITAS_WARM_RETENTION_DAYS=365` in the service env. All 20 files verified single-transaction
  and checksum-frozen; probe fixtures self-unwind; 12 of 20 are PITR-only to reverse (i.e. no real
  rollback). Sharpest risks: no `lock_timeout` injectable through the sanctioned applier (280015/
  280022/280024 take heavy locks — low-traffic window required), `280021` swaps the `auth.users`
  signup trigger (mandatory live throwaway signup + pre-staged emergency detach script), and the
  §1 billing_ledger contradiction above.
- **`docs/SAVINGS_DROUGHT_DIAGNOSIS.md`** (487 lines) `[verified — read]`: the zero-billable
  drought is **three independent faults**; fixing any one alone changes nothing. (A) all traffic
  arrives via local `bvx` proxies over `POST /v1/usage`, which hardcodes `authoritative=false` by
  design — the hosted in-process bridge is the only `authoritative=True` writer in non-test code.
  (B) `pricing_status` is set once, synchronously, at insert — no worker/job/trigger ever
  re-prices — and it is failing its own precondition (`receipt_available` false and/or
  `model_price()` miss); this is independent of A and separately fatal (unpriced ⇒
  `measured_savings_usd` NULL ⇒ verified $0), so **fix order is B before A**. (C) the 07-18…07-26
  gap was the wyfz `usage_log` missing 11 columns (every POST 400'd); already remediated 07-27.
  The 07-17 cliff is datable to the minute: commit `3cd4cca` changed the verification rule
  mid-flight and no client was ever updated to satisfy it. Six ranked hypotheses (H1/H2
  near-certain) each carry one read-only SQL check; a single composite GROUP BY query
  discriminates all of them and must be run before any fix is built. Warns explicitly that H4
  (widening `cache_attributable` to caller-owned cache) is a product decision that would bill
  customers for their own caching — recommended against. The 07-27+ volume is plausibly
  self-generated dev traffic, not customers `[unverified until the orgs/client query runs]`.

### 4. Commit plan

`[verified — plan document reconciled against git status by its lane; not yet executed]`

**16 commits**, ordered: 1–10 independent hardening lanes (credential hygiene, release gate
fail-closed, SRI/CSP, replay, subprocessors, SDK, scanner, RLM gate, breaker scoping, admission
bounds), 11 = billing workstream A (280010–280013 + Stripe writer + worker supervisor), 12 =
observability (must sit between 11 and 14), 13 = workstream B schema (280014–280024 + harness +
verifier), 14 = control-plane code (api/server.py, unsplittable, ~90 hunks), 15 = dashboard,
16 = lint. **The shared-registrar-file hazard is the central one:** `verify-migrations.mjs`
requires `readdirSync(supabase/migrations)` to exactly equal its array and both manifests, so any
commit adding a migration must update all four registrar files in the SAME commit. The plan
line-splits the four registrars at the `280013`/`280014` boundary (pure appends, trivial);
declining that split leaves 5 red intermediate commits. Three files need hunk/test-block splits
(`.env.example` 3-way, `tests/release_security.test.mjs` per-test, registrars); `.env.local`,
`api/.secret_key`, `*.db` verified gitignored and never to be committed.

### 5. Remaining human gates — nothing below can be done by an agent

1. **Prod DDL approval + execution**: human with wyfz access walks `docs/WYFZ_APPLY_PLAN.md`
   (20 files, 3 windows, §1.1 contradiction resolved first, low-traffic window for Window C).
2. **Live key rotation (G11)**: the leaked `sk_live` key must be rolled in the Stripe dashboard
   before any live-mode step; the burned repo key material is documented in
   `docs/CREDENTIAL_SECURITY.md`.
3. **Live webhook endpoint + `whsec_` (G14)**: create the live-mode webhook endpoint (and portal
   config), capture its `STRIPE_WEBHOOK_SECRET`, and — per the G9 review — set/verify the
   endpoint's own `api_version` matches `2026-06-24.dahlia`.
4. **Vercel/Railway env (G10, G7)**: set `BREVITAS_PUBLIC_URL` BEFORE the flip,
   `BREVITAS_BILLING_WEEKLY_CAP_USD` (recorded MISSING both places), a strong
   `BILLING_RECOVERY_SECRET`, delete dead `API_URL`, set `BREVITAS_WARM_RETENTION_DAYS=365` as the
   pre-280017 stopgap, and resolve the G7 replica topology (2 authoritative replicas vs "exactly
   ONE" in the runbook).
5. **Drought fix decision**: run the diagnosis doc's composite query read-only, then decide the
   product questions it isolates — hosted-proxy-as-default (H1), receipt/pricing repairs (H2/H3)
   **before** any authoritative-path work, and whether caller-owned cache is ever billable (H4 —
   the doc says no). Until this, every settlement the new writer computes is $0 by construction.

Also still open from the G9 review, code-side but gated on a human "go": the meter_events E2E
re-run under the pin and the `StripeRejected`→`StripeUnavailable` reclassification for
version-class 400s — the one prescribed fix that protects revenue.

---

> ## UPDATE 2026-07-29, after this report was written — BOTH BLOCKERS ARE CLOSED
>
> This report's §1 and §4 were written against a transient tree state. Both blockers have since
> been resolved and re-verified end to end. Read this box in preference to §1.2 and §4 below,
> which are preserved as the historical record.
>
> **Blocker (a) — the promote-door double charge: FIXED and pinned.**
> `202607280013` inherited `202607280008`'s committed-money predicate verbatim, which counts only
> `'sending'`/`'reported'` OR `outbound_started_at is not null` and is therefore blind to
> `'pending'`. Fixed in three places: `'pending'` added to `settle_billing_period`'s step-9
> predicate; a `'pending'`-scoped sibling check added to `promote_billing_period_settlement`; and a
> `55000` raise added to the revision path so superseding a committed predecessor fails loudly
> instead of leaving it billable. Proven by replaying the exact reported sequence
> (`settle → promote → late receipt → settle(p_allow_revision) → promote`) on PostgreSQL 17.10:
> pre-fix it returned `outcome='revised'`, `fee_microusd=47500000`, `committed_period_microusd=0`
> and left **2** queued rows totalling **70,000,000 µUSD**; post-fix it returns
> `outcome='blocked'`, `code='period_already_committed'` and leaves **1** row at
> **22,500,000 µUSD** `[verified by execution]`.
>
> The promote-side check is deliberately scoped to `'pending'` **only**. A broader predicate
> short-circuited `promote_billing_period_settlement`'s designed second line of defense — the
> `cumulative_ceiling` re-run — and silently deleted the existing assertion coverage for it. The
> ceiling genuinely catches every other committed state; `'pending'` is the sole gap.
>
> Pinned permanently by a new promote-door section in
> `scripts/ci/migration-settlement-writer-assertions.sql` (with its own `v_promote2_org` fixture).
> It **fails against the pre-fix function** and passes against the fixed one, so it is a real
> discriminator, not a tautology `[verified]`.
>
> **Blocker (b) — registrar staleness: RESOLVED, and misdiagnosed here.** The migrations
> `202607280014`–`202607280023` were **not** produced by this build's lanes (whose assignments
> stopped at `280013`). They are unrelated workstreams — legacy view removal, browser role
> privileges, compliance/warm-state erasure, device-key revocation, job reclaim fencing, legal
> acceptance, retention/waitlist — written by a **concurrent session editing the same working
> tree**. All 71 migrations on disk are now registered in both manifests and the frozen checksums.
>
> **Full gate, all re-run after the fix `[verified by execution]`:**
> `node scripts/ci/verify-migrations.mjs` → exit 0 · `npm test` → **265/265** ·
> `pytest tests/` → **944 passed** ·
> `DATABASE_URL=postgresql://jamesyang:unused@127.0.0.1:5432/brevitas_harness bash scripts/ci/run-migration-tests.sh`
> → **exit 0**, "Ephemeral fresh-install and production-upgrade migration checks passed",
> with `202607280005`–`202607280013` all applied on the upgrade path · all six money-path
> assertion suites pass and are wired into `run_forward_assertions`.
>
> Two verification notes worth keeping. The harness needs an **authenticated** DSN
> (`user:pass@`) or the billing maintenance gate rejects it with "DATABASE_URL must be an
> authenticated PostgreSQL URI without a fragment" — a passwordless URI exits 2 before any DDL.
> And `run_forward_assertions` invoking `psql --file` **without** `--set ON_ERROR_STOP=1` looks
> like a fail-open but is not: every assertion file sets `\set ON_ERROR_STOP on` as its own first
> line, and psql exits 3 on a raising suite — confirmed empirically rather than assumed.
>
> **Finding 3 (MEDIUM) — CLOSED.** The review was right that the old regression case
> hand-advanced the predecessor to `sending`/`reported` and so never exercised the state the
> promoter actually produces (`pending`, no marker) — which is precisely why all six suites
> passed with finding 1 live. The new promote-door section uses the real sequence with no
> hand-written status updates, and it fails against the pre-fix function.
>
> **Finding 4 (LOW) — CLOSED.** `release_billing_ledger_unsent` (202607280011) now uses
> `set search_path = pg_catalog, public, pg_temp`, matching 280008/280010/280013 instead of the
> bare `= public` that left `pg_temp` implicitly searched first. Re-applied the whole chain and
> re-exercised the function under the stricter path (invalid-args guard still raises; unknown row
> still returns false). Digest refrozen. This also required updating
> `tests/test_billing_recovery.py:1329`, which pinned the *weaker* string — it now pins the
> hardened one so it cannot silently regress.
>
> **Revised verdict: offline-green, all four review findings closed.** Everything provable
> without Stripe credentials is built, executed, and passing.
>
> **Final gate, re-run after the finding-3/4 fixes `[verified by execution]`:**
> `verify-migrations` → exit 0 · `npm test` → **284/284** · `pytest tests/` → **999 passed** ·
> full harness → **exit 0 both paths** · all six money-path suites pass.
> (Suite counts keep rising — 265→284 JS, 944→999 Python — because the concurrent session is
> still adding tests. Re-run before relying on any number here.)
>
> **Caveat:** a concurrent session is actively writing this tree. Numbers above were true when
> measured; re-run the gate before you rely on them.

---

> ## TIER-3 RESULTS — run 2026-07-29 against a real Stripe sandbox
>
> Sandbox `New business sandbox` / `acct_1TvyF5C7NZKjd1s3`, test mode, API version
> `2026-06-24.dahlia` (the Node SDK default — still unpinned, see G9).
> Catalog created by `npm run billing:setup`: price `price_1TylXFC7NZKjd1s34UeYaLyu`,
> meter `mtr_test_61V89UbCnX6YYVGg541C7NZKjd1s3DYm`, event `brevitas_fee_microusd`.
>
> | # | test | result |
> |---|---|---|
> | S3 | `npm run billing:setup` creates a conforming catalog | **PASS** — first execution ever; idempotent on re-run |
> | M1 | both validators accept the catalog | **PASS** — JS predicate *and* Python `validate_contract()` (which checks 4 extra meter fields) |
> | **S6** | **item-level period boundaries exist** | **PASS — and decisive.** `current_period_start` is **absent at the top level** in `2026-06-24.dahlia`; it exists only on the subscription **item**. The code reads item-level (`stripe-state.mjs:55-74`), so `subscriptionPeriod()` works. Had it read top-level — as most older integrations do — every subscription webhook would 500 and `period_tracking_valid` would stay false forever |
> | M2 | meter-event identifier idempotency | **PASS** — same identifier posted twice summed to **250000**, not 500000 |
> | M3 | Checkout session shape (Stripe half) | **PASS** — `client_reference_id` round-trips; `amount_total` server-derived; repeat call with same idempotency key returned the **identical** session id, not a duplicate |
> | M5 | portal session opens | **PASS** — config `bpc_1TylmBC7NZKjd1s3uDBRtlqV` saved; session on `billing.stripe.com` |
> | M8 | period span and roll-forward | **PASS** — span exactly `604800000` ms; after the clock advance it rolled forward by exactly `604800000` ms |
> | M9 | occupancy across every real status | **PASS** — all 8 Stripe statuses accepted by `assertSupportedStripeSubscriptionStatus`; only `active`/`trialing` usage-eligible; `past_due`/`unpaid`/`incomplete`/`paused` occupying-but-not-billable; `canceled`/`incomplete_expired` free the slot |
> | M10 | reconcile ACCEPTED branch | **PASS**, all three ways — `exclusive_meter_writer=true` + exact match → `accepted`; `false` → `unknown` (pins the shipped default, so ACCEPTED is deliberately unreachable in prod config); **mismatched expected amount → `unknown`, never a false accept** |
> | **3B** | **test-clock week roll → invoice** | **PASS** — 250000 micro-USD metered into week 1 produced a **25-cent invoice** (`total === 25`). The pricing chain is correct end to end: 0.0001 cents/unit × 250,000 = $0.25 = 25% of $1.00 verified savings |
>
> Also confirmed: `recurring.interval_count === 1` on the real price, so **FIX-9** is right;
> and the generated `BILLING_RECOVERY_SECRET` passes `recoverySecretIsStrong` (a weak one does
> not 401 — it silently makes `billingIsConfigured()` false and 503s checkout *and* the webhook).
>
> **Operational finding not in the test plan:** for a **clock-attached** customer, Stripe
> validates the meter-event `timestamp` against the **test clock's** frozen time, not wall
> time. A timestamp after the clock is rejected with "The event timestamp cannot be in future."
> Meter events in a clocked scenario must be stamped at or before the clock.
>
> ### UPDATE 2026-07-30 — the Supabase half is now DONE on a throwaway project
>
> `caestus-labs` (`evpoxdrluvihryvqhraz`, same login as the sandbox, a **different account**
> from prod `wyfz`) was adopted as the throwaway. All **71 migrations applied first try in
> 67s** — the first proof the chain installs cleanly on a real hosted Supabase project.
> The app/worker were pointed at it via shell-env overrides only; `.env.local`'s canonical
> `SUPABASE_URL` still points at prod for everything else.
>
> | # | test | result |
> |---|---|---|
> | M3 (route) | authed `POST /api/billing/checkout` via real JWT + membership | **PASS** — 200 + `checkout.stripe.com` URL; unauth 401; authed status shows `settlement_pending` (FIX-5 live) |
> | M4 | real occupying subscription → second checkout | **PASS** — 409 `{action:'portal'}` from the live-Stripe-list check |
> | M6 | worker end-to-end: seeded 1,234,567 µUSD → `process_once` | **PASS** — claim via PostgREST RPC → catalog validate → meter send → `reported`; Stripe aggregate **exactly 1234567** (summaries lagged ~54s) |
> | M7 | wrong-interval price in `STRIPE_PRICE_ID` | **PASS** — row released to `pending`, attempts **0**, no outbound marker, `billing_catalog_contract_invalid` page alert; probe price deactivated after |
>
> Incidental finds, both worth keeping: (a) reusing an org while swapping its Stripe customer
> strands the reservation in `manual_review` → checkout 503 "requires billing review" — the
> route defending itself correctly against a customer-identity mismatch; (b) the `280021`
> legal-acceptance trigger makes bare `auth.users` seeds fail unless `created_at` is set
> (its NOT NULL `accepted_at` copies it) — real GoTrue always sets it, hand-seeds must too.
> - **Live `stripe listen` webhook forwarding.** The CLI is installed (`stripe 1.44.1`), but
>   `--print-secret` was blocked by the sandbox policy both ways it was attempted. Run it
>   yourself: `stripe login`, then
>   `stripe listen --forward-to localhost:3000/api/billing/webhook`, and paste the printed
>   `whsec_` over the placeholder in `.env.local`. Offline signature coverage does not need it.
>
> ### Sandbox objects created (disposable, but permanent within the sandbox)
>
> Meters cannot be deleted, only deactivated, so `brevitas_fee_microusd` is permanent there.
> Also created: customers `cus_Uyj1b9n4H0Z4nt` (M2 probe), `cus_UyjBm51zkDt5wf` (clocked),
> `cus_*` for the M3 probe; test clock `clock_1TyljJC7NZKjd1s3AZQPGk1O`; subscription
> `sub_1TyljKC7NZKjd1s37sULpE6l`; one open Checkout session.

---

## 1. Bottom line — does it work?

*(§1.2 below is superseded by the update box above — both blockers are now closed.)*

**Not yet end to end, and the adversarial review verdict is do-not-ship as the tree stands.
But the honest answer has three layers:**

1. **Everything that can be built and proven offline has been built and proven offline.**
   All eleven fixes (FIX-1..FIX-11) shipped and were verified by execution — the migration harness
   went from red-on-both-paths to exit 0, the six previously-failing assertion suites pass, the
   1,884+ lines of money SQL that had *zero* executable coverage now have six real PostgreSQL
   assertion suites, the settlement writer exists (something the test plan said "cannot bill
   automatically today"), the status endpoint no longer reads a dead table, and the dashboard can
   no longer render unstated money as `$0.000000`. Node suite: **263/265** `[verified today]`.
   Python suite: **944/944** `[verified today]`.

2. **Two blockers stand between here and "offline-green," both found by the adversarial review
   and both still open** (§4): (a) a reproduced double-charge path through the settlement writer's
   promote door — `status='pending'` is invisible to the committed-money check
   (202607280013:615-622, confirmed unfixed by me today `[verified]`); (b) the tree kept moving
   under the registrar — **nine** migrations (202607280014–202607280022) from *other workstreams*
   now sit on disk unregistered, so `node scripts/ci/verify-migrations.mjs` exits 1 and the
   migration harness aborts at line 11 before any DDL `[verified today]`. (b) is fail-closed and
   mechanical to fix (re-run the registrar step last); (a) is a real money-path edit plus one
   assertion-suite case.

3. **Nothing has ever touched real Stripe.** No keys exist; every Stripe interaction in every test
   is an injected fake. `npm run billing:setup` has never been executed by anything. Checkout,
   webhooks, meter ingestion, invoices, the portal — all of Tier 3 (§6) starts the moment your
   `sk_test_` key arrives. And even after Tier 3 is green, **nothing bills a customer until two
   deliberate human acts**: a reviewed migration granting EXECUTE on
   `promote_billing_period_settlement` (the writer can only produce `draft` rows), and the
   `BREVITAS_BILLING_ENABLED` flip — plus the production-access and calendar gates in §5
   (wyfz trigger state, the leaked live key roll, one real Anthropic invoice reconciled).

So: the plumbing is real, tested, and safe-by-construction in the right places; it is not yet
proven against Stripe, it has one known overcharge defect to fix first, and it cannot move money
without your signature. That is the accurate answer to "does it work."

---

## 2. What shipped, per fix

Legend: **[exec]** = verified by execution with real output in the lane evidence (and re-checked
where noted); **[read]** = verified by code read only.

| ID | What | Files | Status |
|---|---|---|---|
| **FIX-1** harness order | Upgrade path now drives the ENTIRE upgrade manifest in a loop (was hand-bound through index 39 = 202607280004), fails closed on any unapplied entry, double-applies everything past the 202607170011 idempotence boundary; fresh path replays the frozen 010-013 block *before* the 280005+ suffix so 202607170012 never replays into a post-280006 schema; the second 170012 replay re-attaches the trigger for exactly two applies then re-retires it (spec correction — see §4.5 of the fix plan); four new pre-COMMIT rollback probes for 280006-280009 and four more for 280010-280013 | `scripts/ci/run-migration-tests.sh` | **[exec]** — harness exit 0 on both paths, 5,423-line log, 0 ERROR lines, 202607280005-0013 each applied twice on the upgrade path, trigger absent at the end of both. ~~Currently unreproducible~~ **Reproduced 2026-07-30 after the registrar re-run: exit 0, 6,058-line log, 0 ERROR lines, 202607280014-0024 covered on both paths** (§3 update). |
| **FIX-2** trigger fixtures | All six trigger-dependent assertion suites re-seeded with direct `billing_ledger` INSERTs reproducing the retired `queue_brevitas_fee()` predicate and arithmetic verbatim; the DR file needed FOUR seeds, not the plan's one (spec error, corrected in the plan); fee arithmetic (250000/1000000 µUSD) now asserted explicitly | `scripts/ci/migration-assertions.sql`, `migration-company-billing-assertions.sql`, `migration-billing-recovery-scope-assertions.sql`, `migration-compliance-billing-isolation-assertions.sql`, `scripts/dr/compliance-workflow-assertions.sql` | **[exec]** — all 24 forward suites pass with the trigger absent AND with it artificially re-attached (`on conflict do nothing` absorbs both worlds). |
| **FIX-3** red regex | The deliberately-red fee-clamp assertion updated per the recorded human decision: `max(0.0, verified)` clamp accepted, regex made whitespace-tolerant, rate pin hardened (`0.25\b`). `api/server.py` untouched | `tests/stripe_billing_config.test.mjs` | **[exec]** — 9/9, and the whole suite green in `npm test` today. |
| **FIX-4** PSL-LATCH | New migration adding one-way latches on `outbound_started_at`/`reported_at`/`settled_at` plus the marker-preserving status-exit rule; all seven pre-existing guards preserved verbatim; behavioural self-check drives the full reproduction inside an unwound subtransaction | `supabase/migrations/202607280010_period_settlement_send_latches.sql`, `scripts/ci/migration-period-settlement-assertions.sql` (592 lines, first executable coverage of 202607280007) | **[exec]** — the pre-fix 45,000,000-vs-22,500,000 µUSD double charge was reproduced first, then all four reproduction UPDATEs raise P0001 post-fix and the committed sum survives a marker-preserving void. **Caveat resolved 2026-07-30: the promote-door sibling is closed too (§4 finding 1 — writer predicate, promoter sibling refusal, summary bucket, and the pinned regression).** |
| **FIX-5** status endpoint | Stage 1: `settlement_pending: true` + unconditionally-null money (no fee column read at all). Stage 2 (settlement-writer lane): repointed at the `billing_period_settlement_summary` RPC — NOT a table select, which is impossible (zero PostgREST privileges, asserted in three CI files); `estimated_fee_usd` is an evidence projection (the plan's literal rule returns $0 forever for the live period — spec error, corrected); new `settled/reported/committed_fee_usd`, `settlement_status`, `billable`. Dashboard half: guarded `fmtMoney()` renders `Unavailable` instead of `fmt(null)`'s `"0.000000"` | `src/app/api/billing/status/route.ts`, `src/lib/billing/supabase.ts` untouched (inline RPC), `dashboard/src/components/Billing.jsx`, `dashboard/src/App.jsx`, `tests/billing_status_settlement_repoint.test.mjs` (11 tests, real handler executed), `tests/billing_dashboard_money_display.test.mjs` (5 tests, evaluates the real formatter) | **[exec]** — both suites green, mutation-verified (reverting the route or the formatter fails 4-5 tests each). |
| **FIX-6** https public URL | No more silent `http://localhost:3000` default when deployed; https required whenever `NODE_ENV=production` or `VERCEL_ENV` is set; loopback http local-only (and now http-only — the old predicate accepted `ftp://localhost`); predicate extracted to an env-injectable module | `src/lib/billing/config.ts`, `src/lib/billing/config-predicate.mjs` (new), `tests/billing_config_predicate.test.mjs` | **[exec]** — truth-table suite green incl. the exact production accident (deployed + var unset → `configured:false`); mutation-verified. |
| **FIX-7** legacy checkout metadata | Absent `brevitas_organization_id` on a pre-reservation session is tolerated (the 202607200014 backfill shape is `brevitas_user_id` only); present-but-mismatched still throws; generation check stays strict; the inverted test fixture fixed | `src/lib/billing/checkout-reservation.mjs`, `tests/billing_checkout_session_reservation.test.mjs` | **[exec]** — 6/6; the verify-migrations contract pin on the generation expression preserved. End-to-end against real Stripe remains `[unverified]` until Tier 3. |
| **FIX-8** worker supervisor | Supervisor catches escaping exceptions and actually restarts/backs off/escalates (the tail was dead code); dead `inner.exception()` read removed; drain-time failures logged, never restarted; supervisor failure can no longer skip shutdown cleanup; `/ready` reports the honest billing block for every role (`disabled`/`unavailable`, never fabricated `ready`) | `api/worker.py`, `tests/test_billing_supervisor.py` (10 tests) | **[exec]** — 10/10; mutation check restoring the pre-fix behaviour fails 6. Operator note: external alerts keyed on the literal `ready` for an optional worker will now see the honest value. |
| **FIX-9** interval_count | All three catalog validators reject `interval_count ≠ 1` (Node config, Python gateway, setup-script reuse guard — the last also gained active/recurring/usd/per_unit checks) | `src/lib/billing/config.ts`, `api/billing_recovery.py`, `scripts/setup-stripe-billing.mjs` | **[exec]** — parametrized tables on both validators (0/2/3/4/52 reject; absent/1 pass); mutation-verified. |
| **FIX-10** 429 release + metrics | New lease-fenced RPC `release_billing_ledger_unsent(bigint, text)` — **bigint, not the uuid the lane brief specified** (spec error: `billing_ledger.id` is bigint) — returns a provably-unsent row to `pending`, refunds the attempt, clears the marker; called only from the post-`begin_send` 429 branch; the three dropped metrics (`billing.pending_count`, `billing.stripe_unavailable`, `billing.catalog_validation_error`) mapped onto existing instruments | `supabase/migrations/202607280011_billing_ledger_unsent_release.sql`, `api/billing_recovery.py`, `brevitas/observability.py`, `tests/test_billing_recovery.py` (+~15 tests incl. a 13-case status→outcome table), `tests/test_observability.py` | **[exec]** — RPC fencing proven on real PG (wrong owner, wrong id, idempotence, attempt floor, blank owner); 8-cycle sustained-429 test proves attempts never walk to `review`; metric contract test failed on exactly 3 names pre-fix. Review found a low-severity search_path inconsistency here (§4 finding 4 — resolved 2026-07-30, 202607280011:89 pins the hardened path). |
| **FIX-11** warm_spend_days | `count(distinct warm.day)` instead of counting (provider, day) rows; only token changed from 202607280008's body; money arithmetic untouched | `supabase/migrations/202607280012_settlement_evidence_warm_days.sql`, `scripts/ci/migration-halting-conditions-assertions.sql` (645 lines, first executable coverage of 202607280008/0009) | **[exec]** — pre-fix reproduced (4 rows/2 days → 4), post-fix 2; fee ceiling proven unmoved. |
| **Settlement writer** (the G6 gap) | `202607280013`: `settle_billing_period` (service_role; derives the fee from the guard's own evidence — no fee argument exists; writes `draft` only; idempotent per (org, period); refuses open/pre-enrollment/grid-shifted/already-committed periods; halts write nothing), `promote_billing_period_settlement` (granted to NOBODY — psql only), `billing_period_settlement_summary` (the status route's read path), `billing_periods_awaiting_settlement` (sweep enumeration, no caller yet by design), evidence function widened with `usage_log_watermark_id` | `supabase/migrations/202607280013_period_settlement_writer.sql` (1,534 lines), `scripts/ci/migration-settlement-writer-assertions.sql` (1,027 lines, runs under a non-UTC session timezone on purpose) | **[exec]** — full decision table, 3-step revision chain, void-and-rebill defense (void door), idempotence under 4 calls, promoter privilege posture, $225 end-to-end demo fixture; mutation tests all bite (incl. the 2026-07-30 committed-bucket mutation). **The §4 finding 1 blocker is resolved (2026-07-30).** |
| **Integrator / registrar** | 280010-0013 registered in both manifests + frozen checksums; U5 fail-closed manifest-coverage guard (mutation-verified twice); five stale release-security assertion blocks repaired (two were passing *vacuously*); superseded status test deleted after property-for-property supersession check; portal route got its missing `billingIsConfigured()` 503 gate; webhook 400s got `Cache-Control: no-store`; doc corrections inlined into both plan documents at each error site; `.venv/**` eslint ignore | `scripts/ci/verify-migrations.mjs`, both manifests, `migration-frozen-checksums.txt`, `tests/release_security.test.mjs`, `src/app/api/billing/portal/route.ts`, `src/app/api/billing/webhook/route.ts`, docs | **[exec] — re-established 2026-07-30**: registration now covers 280010-280024 and the gates were re-run green (§3 update; §4 blocker 2 resolved). The portal 503 gate and webhook headers are typechecked/linted/built but have no handler-level test of their own `[read]`. |

Spec errors found and handled loudly (all now annotated inline in the plan docs): FIX-1(b)'s
re-apply prescription insufficient at the receipt replay; FIX-2's undercount of the DR file's
fixtures (4, not 1) and the frozen-digest location; FIX-5's two stage-two errors (draft/void
exclusion returns $0 forever for the live period; the table is unreadable by PostgREST — the RPC is
mandatory); FIX-10's uuid-vs-bigint signature; U1's stale pre-FIX-10 expectation; the release-preflight
"required-var list" that does not exist; and a defect in 202607280007's own frozen Phase-4 comment
(`expected_period_microusd` must be the SUM over committed rows, not the row's own fee — recorded as
fix-plan §4.5, the correction of record).

---

## 3. Test results as actually observed

**Lead with the red. Measured by me today, on the current tree:**

```
$ node scripts/ci/verify-migrations.mjs
Fresh manifest must cover every forward Supabase migration exactly once:
  ... (lists the chain; 70 files on disk, 61 registered)
EXIT=1

$ npm test
ℹ tests 265   ℹ pass 263   ℹ fail 2
✖ migration order, generated drift, idempotence, and rollback contracts pass
✖ migration DSN validation rejects endpoint bait outside the hostname   (1 !== 2)

$ bash scripts/ci/run-migration-tests.sh   (any DSN)
→ aborts at line 11 on the verify-migrations failure, before any DDL
```

> **UPDATE 2026-07-30 (dbci repair pass): the red above is stale — re-registration is done and
> the gates were re-run for real.** 202607280014-202607280023 (plus the new 202607280024
> browser-privilege completion) are registered in `expectedFreshMigrationOrder`, both manifests,
> and `migration-frozen-checksums.txt`. Measured on the current tree against a loopback
> PostgreSQL 17.10: `node scripts/ci/verify-migrations.mjs` → exit 0;
> `bash scripts/ci/run-migration-tests.sh` → **exit 0, 6,058 log lines, 0 ERROR lines**, both
> paths, "Ephemeral fresh-install and production-upgrade migration checks passed."
> 202607280014-202607280024 each applied twice on the upgrade path (idempotence) and once on the
> fresh path; all 36 forward assertion suites executed on both paths. Mutation evidence for the
> new material: narrowing the summary's committed bucket back to `('sending','reported')` fails
> `migration-settlement-writer-assertions.sql` (27,500,000 ≠ 32,500,000 µUSD), and re-granting
> `TRUNCATE on public.audit_events to anon` fails both the browser-privilege inventory and
> `assert_browser_role_table_privileges()`. Every migration from 202607280013 onward now carries
> a machine-checked `-- REVERSE:` posture header: `verifyReversePosture()` gained a backfill
> floor at 202607280013 (the cutoff itself stays at the next unused number, as
> `tests/release_security.test.mjs` pins), the governed set is asserted non-empty, and removing
> a header fails the gate — verified by mutation.

Both Node failures and the harness abort share one root cause: **nine migrations
(202607280014_drop_billing_monthly_view … 202607280022_audit_read_and_transition_evidence) were
written into this tree by other workstreams after the registrar pass ran** — one of them
(280022) appeared even after the adversarial review, which saw eight. None of them is part of this
build. The failure mode is fail-closed (exit 1, never a silent pass), which is exactly what the
registration machinery is for — but it means **the integration lane's headline numbers below
describe a tree state that no longer exists** and must be re-established after a final registrar
pass.

**Green, measured by me today:**

```
$ ./.venv/bin/python -m pytest tests -q
944 passed, 1 warning in 28.70s
```

**Green as measured by the integration lane at its tree state `[verified then, unreproducible now
until re-registration]`:**

- `node scripts/ci/verify-migrations.mjs` → "Migration order, drift, rollback, and lock contracts
  verified.", exit 0.
- `npm test` → 265/265.
- `node --test tests/release_security.test.mjs` → 13/13 including the new U5 guard.
- **The full migration harness**: `DATABASE_URL=postgresql://jamesyang:unused@127.0.0.1:5432/… bash
  scripts/ci/run-migration-tests.sh` → **exit 0, 5,423 log lines, 0 ERROR lines**, "Ephemeral
  fresh-install and production-upgrade migration checks passed." Upgrade path applied
  202607280005-202607280013 each twice (280006 three times — the deliberate re-retire after the
  receipt replay); fresh path applied all nine once; per-row fee trigger provably absent at the end
  of both paths (`pg_trigger` count 0); the four timezone-sensitive money suites ran at both call
  sites.
- `cd dashboard && node --test src/lib/*.check.mjs` → 112/112.
- `npm run build:dashboard` → built; bundle grep confirms the new strings ("Unavailable",
  "Accruing this week") present and the pre-fix "Current estimate" gone.
- `npx next build` (with the three CI env vars) → compiled, TypeScript clean, all five
  `/api/billing/*` routes emitted. `npx tsc --noEmit` and `npm run lint` → clean.

**Mutation evidence (the tests bite; not vacuous):** each lane temporarily reverted its fix and
re-ran — FIX-3/5/6/8/9/10 and the dashboard formatter all went red on revert and green on restore,
with shasum-verified restores. The integrator mutation-tested the U5 guard (re-bounding the loop to
40 fails; diverting an entry fails), the rollback-invariant inventory, and the dashboard money
display. The settlement-writer assertion suite has 4 mutation checks, all of which fail when
flipped. The review independently re-verified a sample (280012-after-280013 replay refusal, FIX-6
predicate, FIX-7 mismatch rejection, fmtMoney null handling) and found them honest. What the review
could NOT reproduce were the integrator's three headline greens — because of the foreign
migrations, not fabrication (file mtimes prove the tree moved mid-review).

**Never run, anywhere: anything against real Stripe.** Zero API calls, zero keys, `npm run
billing:setup` never executed, no webhook has ever carried a genuine Stripe signature. That is §6.

---

## 4. Adversarial review findings (verdict: do-not-ship as-is)

**UPDATE 2026-07-30: all four findings are now RESOLVED in this tree** (fix lanes ran after the
review; per-row status below). The original text is kept for the record; do not hold the release
on this table.

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | **BLOCKER** | **Double-charge through the promote door.** `settle_billing_period`'s committed check (202607280013:615-622 `[verified today: 'pending' absent from the predicate]`) counts only `('sending','reported')` or marker-bearing rows — same predicate as 202607280008:567's cumulative ceiling. A settlement an operator has promoted to `pending` is invisible to BOTH. Reproduced by the reviewer end to end: settle → promote (`pending`) → late usage lands → `settle(..., p_allow_revision => true)` returns `revised` (supersedes the pending row WITHOUT un-queueing it) → promote rev 2 also succeeds → **two `pending` rows for one (org, period), 70,000,000 µUSD queued where 47,500,000 is correct**, distinct row ids ⇒ distinct Stripe idempotency keys ⇒ Stripe sums both. Latent today (no settlement claim/send RPC exists), but that is exactly PSL-LATCH's reachability status and the plan's own standard says this class lands before the Phase-4 claim RPC. | **RESOLVED 2026-07-30.** The writer's committed predicate includes `'pending'` (202607280013:637-638); the promoter refuses when any sibling (org, period) row is `'pending'` (:884-897); the customer-facing summary's committed bucket was also widened to match the writer (202607280013:1113-1115 — a promoted-but-unsent period now reports its fee as committed instead of $0), with the digest refrozen in the same change. Regression pinned: the exact settle → promote → late-receipt → `settle(p_allow_revision)` sequence returns `period_already_committed` with exactly one queued row (`migration-settlement-writer-assertions.sql`, promote-door section), and a `'pending'`-only row is asserted to reach `committed_fee_microusd` (32,500,000 µUSD bucket case; mutation-verified). |
| 2 | **BLOCKER** | **Registrar state stale — the whole DB tier is unreachable.** Nine foreign migrations (280014-280022) unregistered → verify-migrations exit 1, npm test 263/265, harness aborts at line 11, so every new money-path assertion suite currently has zero CI execution. Fail-closed, timing-caused (lanes from other workstreams still writing during and after the registrar pass and the review). | **RESOLVED 2026-07-30.** Registrar re-run complete: 280014-280024 are in `expectedFreshMigrationOrder`, both manifests, and `migration-frozen-checksums.txt`; `verify-migrations.mjs` exits 0 and the full harness ran green on a clean loopback PG 17.10 (exit 0, 6,058 log lines, 0 ERROR lines, both paths; the new files each double-applied on the upgrade path past the 202607170011 idempotence boundary). 280013 remains ordered after 280012. See the dated update in §3. |
| 3 | **MEDIUM** | **The double-bill regression test tests the wrong state.** `migration-settlement-writer-assertions.sql:803-812` hand-advances the predecessor to `sending`/`reported` before asserting refusal, so the state the promoter actually produces (`pending`, no marker) is never exercised — which is why all six suites pass with finding 1 live. | **RESOLVED 2026-07-30.** The real sequence exists with no hand-written status updates: settle → promote → late eligible receipt → `settle(..., p_allow_revision => true)` returns `period_already_committed`, and exactly one row in `('pending','sending','reported')` is asserted (`migration-settlement-writer-assertions.sql`, "THE PROMOTE DOOR" section). Green in the 2026-07-30 harness run on both paths. |
| 4 | **LOW** | `release_billing_ledger_unsent` (202607280011:83) uses `set search_path = public` rather than the hardened `pg_catalog, public, pg_temp` its sibling money migrations use — pg_temp is implicitly searched first, a theoretical temp-relation shadowing vector. Only service_role holds EXECUTE, and 22 pre-existing migrations share the bare form; inherited convention, not a regression. | **RESOLVED 2026-07-30.** `release_billing_ledger_unsent` now pins `set search_path = pg_catalog, public, pg_temp` (202607280011:89). |

Review's positive confirmations worth keeping: the void-and-rebill door is genuinely closed; the
fee CHECK is satisfied by construction including negative-verified/positive-warm weeks; halts write
nothing; the writer can only produce `draft`; the promoter is granted to nobody; no new RPC widens
table access (service_role: SELECT-only on `billing_ledger`, zero privileges on
`period_settlement_ledger`); all 50 frozen checksums matched their files; no frozen migration was
edited in place.

---

## 5. The go/no-go delta (the 16 gates, STRIPE_TEST_PLAN.md §5)

| Gate | State | Notes |
|---|---|---|
| **G1** harness green both paths + U5 guard | **now-satisfied** `[exec 2026-07-30]` | Re-registration of 280014-280024 done; harness exit 0 on both paths on loopback PG 17.10 (§3 update). |
| **G2** Tiers 1–3 green; F1–F15 correct | **part now-satisfied / part needs-your-keys** | Tier 0/1/2 built and green offline (route handlers executed for the first time; webhook signature tests exist via `generateTestHeaderString`; F4/F5/F6/F9/F13/F14-class behaviour pinned at the DB/worker tier). Tier 3 (M1-M10) and the Stripe-touching failure rows need `sk_test_`. F13's promote-door sibling is closed (finding 1, resolved 2026-07-30). |
| **G3** red fee-clamp assertion resolved by human decision | **now-satisfied** `[verified today]` | Clamp accepted per the recorded decision; 9/9; whole suite green. |
| **G4** PSL-LATCH landed + F13 green | **now-satisfied** `[exec 2026-07-30]` | The latch migration (280010) landed and the original reproduction is dead `[exec]`; the equivalent overcharge via `pending` is closed and pinned by regression (§4 finding 1). |
| **G5** AR-1: status no longer sums writer-less `billing_ledger` | **now-satisfied** `[exec]` | Repointed at the summary RPC; table name pinned in both directions; dashboard renders `Unavailable`, never a fabricated $0. |
| **G6** a settlement writer exists, or leadership accepts manual-only | **code now-satisfied; decision still-open** | The writer exists with real coverage. Billing still requires a reviewed GRANT migration on the promoter + your signature — deliberately. The automated shadow sweep (stage 2) is a specified follow-on PR, not yet written. |
| **G7** exactly ONE authoritative meter writer at flip | **needs-production-access + decision** | `deploy/railway-worker.json` (2 replicas authoritative) vs GO_LIVE_RUNBOOK "exactly ONE" — unresolved; decide at the Railway dashboard, with F14 evidence for the chosen topology. |
| **G8** `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER` decision | **needs-your-keys + decision** | M10 is the evidence vehicle; requires the dedicated test account. |
| **G9** ONE pinned Stripe API version everywhere | **still-open — nothing shipped touches this** | Node SDK defaults `2026-06-24.dahlia`, Python worker sends no header, canary hard-codes `2025-06-30.basil`. Small code change + the S6 shape check (needs your keys). Do this during M-setup, before M1. |
| **G10** env complete on Vercel Preview+Prod and worker | **needs-production-access** | Code half done (CF-1 closed: missing/non-https `BREVITAS_PUBLIC_URL` now fails closed `[exec]`). The actual Vercel/Railway values — notably `BREVITAS_BILLING_WEEKLY_CAP_USD` (recorded MISSING both places), strong `BILLING_RECOVERY_SECRET`, dead `API_URL` deletion — must be set/verified by behaviour (Sensitive vars unreadable). **Set `BREVITAS_PUBLIC_URL` BEFORE the flip or the flip does nothing.** |
| **G11** leaked `sk_live` key ROLLED in Stripe | **needs-production-access** ⛔ | Stripe dashboard act. Do it before any live-mode step. |
| **G12** wyfz trigger state verified read-only | **needs-production-access** ⛔ | One catalog query via the sanctioned `supabase db query --linked -f` path (fix-plan §4.4). Decides whether 202607280006 must be applied there. Note from the migration self-checks: 280010 refuses to run without 280007's trigger; 280012 refuses if the per-row trigger is still attached — both fail closed with named hints. |
| **G13** one real Anthropic invoice reconciled + attestation rows | **needs-production-access + calendar time** ⛔ | No schema change or test-mode object can discharge it. Zero `organization_billing_arrangement` rows exist; nothing is billable (`unattested_billing_arrangement` halts) until attested — by design. |
| **G14** live-mode portal config + webhook endpoint | **needs-your-keys (live mode)** | Test-mode twin is setup step S4 below. |
| **G15** observability: dropped metrics + alert routing | **code now-satisfied; routing still-open** | The three metrics now emit onto existing instruments `[exec]`; nothing yet alerts on sustained `stripe_unavailable` (optional follow-up in `observability/prometheus/alerts.yml`); the end-to-end staged alert test has not been run. |
| **G16** staging soak ≥1 full billing week | **still-open, calendar time** | ST1-ST5; needs staging env with billing enabled and one real week-roll webhook cycle. |

**Also unresolved, called out by multiple lanes as business decisions, not code:** the partial
first week is not billed (`period_precedes_enrollment` — under-bills at most one week; the clamp
alternative is a revenue decision); warm-spend boundary days deduct from BOTH adjacent periods and
100% of all providers' spend ($0 impact today, real money once warming ships); the dashboard's
period-netted estimate and the Savings tab's per-row clamped fees will visibly disagree for any org
with negative rows (copy fix — it will be the first support question).

---

## 6. The moment the Stripe test keys arrive

Ordered. Do §6.0 first; it costs nothing and everything downstream depends on it.

### 6.0 Before touching Stripe (same day, no keys needed)

1. ~~Fix review finding 1 (+3, +4)~~ **DONE 2026-07-30** (§4): `'pending'` is in the writer's
   committed predicate AND the customer-facing summary bucket, the promoter has the sibling
   check, 280011 pins the hardened search_path, and the real promote→revise→promote regression
   case is in `scripts/ci/migration-settlement-writer-assertions.sql`.
2. ~~Re-run the registrar as the LAST write~~ **DONE 2026-07-30** for 202607280014-202607280024;
   repeat only if the tree gains migrations after 202607280024.
3. Prove it (last run green 2026-07-30 — verify-migrations exit 0, `npm test` 286/286, harness
   exit 0 both paths; re-run after any further tree change):
   ```bash
   node scripts/ci/verify-migrations.mjs            # must exit 0
   npm test                                         # must be fully green
   createdb -h 127.0.0.1 -p 5432 brevitas_gate
   DATABASE_URL="postgresql://$(whoami):unused@127.0.0.1:5432/brevitas_gate" \
     bash scripts/ci/run-migration-tests.sh         # must exit 0, both paths
   ```
   (The `:unused` dummy password is required by the maintenance gate's DSN parser; trust auth
   ignores it.)

### 6.1 Setup (S1–S9, minutes)

```bash
# S1 — dedicated, DISPOSABLE test-mode account (billing:setup mutates products/prices,
#      and M10 requires that nothing else ever emits the meter event name). sk_test_ ONLY.

# S3 — provision the catalog (idempotent; never hand-create the price — validators pin
#      unit_amount_decimal exactly '0.0001'):
STRIPE_SECRET_KEY=sk_test_... npm run billing:setup
# copy the printed STRIPE_METER_EVENT_NAME and STRIPE_PRICE_ID

# S4 — MANUAL, Stripe Dashboard, test mode: Settings → Billing → Customer portal —
#      enable payment-method update, invoice history, cancellation; NO plan switching.
#      Without a saved config, "Manage billing" is a bare 500.

# S5 — secrets:
openssl rand -base64 32        # BILLING_RECOVERY_SECRET ('a'.repeat(64) FAILS the strength check
                               # and silently 503s checkout+webhook — use real entropy)
# STRIPE_WEBHOOK_SECRET: whsec_ from `stripe listen`, or any invented whsec_ for hand-signed tests

# S6 — verify the API shape BEFORE anything else (and pin the version, G9):
node -e '
const Stripe = require("stripe");
const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);
(async () => {
  const subs = await stripe.subscriptions.list({ limit: 1 });   // create one on the S3 price first
  const s = subs.data[0];
  const item = s.items.data[0];
  console.log("item period start/end:", item.current_period_start, item.current_period_end);
  if (!Number.isFinite(item.current_period_start) || !Number.isFinite(item.current_period_end))
    throw new Error("ITEM-LEVEL PERIODS ABSENT — pin apiVersion (G9) before proceeding");
})();'

# S7 — .env.local (names only; local values): BREVITAS_BILLING_ENABLED=true (exact string),
#      STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_ID,
#      STRIPE_METER_EVENT_NAME=brevitas_fee_microusd, BREVITAS_BILLING_WEEKLY_CAP_USD=1,
#      BILLING_RECOVERY_SECRET, omit BREVITAS_PUBLIC_URL locally.

# S8 — webhooks: `stripe listen --forward-to localhost:3000/api/billing/webhook` (CLI), or
#      SDK-signed replays (generateTestHeaderString) — embedded sub_/in_/cus_ IDs must be REAL
#      test-mode objects for anything past signature verification.

# S9 — local schema (loopback ONLY — never wyfz):
psql "$LOCAL_DSN" -f scripts/ci/migration-bootstrap.sql
grep -v '^\s*#' scripts/ci/migration-fresh-manifest.txt | while read -r m; do
  psql "$LOCAL_DSN" --set ON_ERROR_STOP=1 -f "$m" || break
done
# seed: organizations row + billing:manage membership + billing_accounts row
#   (subscription_status='active', stripe_customer_id = a REAL test cus_,
#    current_period_end - current_period_start = exactly interval '7 days')
# app: npm run build:dashboard && npm run dev      (NEVER `cd dashboard && npm run build`)
#      use :3000, not :5174; start the FastAPI API on :8000 too
# worker: python -m api.worker   (BREVITAS_WORKER_BILLING_ROLE unset locally; health on :8001)
```

### 6.2 Tier 3, in order (M1–M10)

| # | Do | Pass |
|---|---|---|
| **M1** | After S3: run `validateStripeCatalog()` (Node) AND `StripeRestBillingGateway(key, price, event).validate_contract()` (Python) against the real catalog — the script has never been executed by anything, and the Python side checks 4 meter fields the JS side doesn't | Both accept. Any disagreement between the two validators and the script IS the bug this step exists to find |
| **M2** | POST `/v1/billing/meter_events` twice with an identical `identifier` + value 250000; then `stripe.billing.meters.listEventSummaries(...)` (summaries LAG — poll; same-second UNKNOWN is expected) | Summary total = 250000, not 500000; a `timestamp` of now−40d → 400 (pins the 34-day expiry assumption) |
| **M3** | `POST /api/billing/checkout` twice same generation → same session id; `checkout.sessions.expire` then re-POST → NEW key, new session; pay with `4242 4242 4242 4242` | Server-derived amount; `client_reference_id` = org id round-trips |
| **M4** | `subscriptions.create` directly (bypassing the fence), then POST checkout | 409 `{action:'portal'}`; dashboard silently converts to portal |
| **M5** | POST `/api/billing/portal` in three states (configured customer / no customer / portal config unsaved) | 200+url / 409 exact message / documented 500 (decide if acceptable) |
| **M6** | Seed a `billing_ledger` row `fee_microusd=1234567`; run the local worker; poll `:8001/ready`; then `listEventSummaries` | Row `status='reported'`; meter summary EXACTLY 1234567. **This is the first time the money pipe touches real Stripe — treat any discrepancy as stop-ship** |
| **M7** | Swap `STRIPE_PRICE_ID` to a monthly sibling price; run one worker cycle | Row RELEASED back to pending (attempt not burned pre-outbound); `billing_catalog_contract_invalid` alert in logs; never `dead` |
| **M8** | Test clock (§6.3); feed the retrieved subscription to `subscriptionPeriod()` across ≥3 periods incl. a DST crossing | Item-level boundaries differ by exactly 604800000 ms every period |
| **M9** | Drive all six occupying statuses: `active`/`trialing` via checkout; `past_due`/`unpaid` via test clock + card `4000 0000 0000 0341`; `incomplete` via `4000 0025 0000 3155` unconfirmed; `paused` via `pause_collection` | `customerHasAccountOccupyingSubscription` detects every one |
| **M10** | Dedicated meter + `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=true` in the disposable account: post a known event set, wait for summaries, run reconcile; repeat with the flag false | Exact aggregate equality → `ACCEPTED`; flag false → same state stays `UNKNOWN` (pins the shipped default). Records the G8 decision |

### 6.3 The test-clock week roll (§3B — proves the 7-day boundary, invoice, and meter aggregation)

A Customer cannot be attached to a test clock after creation, so the customer is minted clocked
FIRST and seeded via the RPC so checkout's `if (!customerId)` short-circuits:

```js
const clock = await stripe.testHelpers.testClocks.create({ frozen_time: T0 });
const customer = await stripe.customers.create({ test_clock: clock.id });
// seed billing_accounts.stripe_customer_id via the save_billing_customer_identity RPC
// subscription: try Checkout; if Stripe refuses a clocked customer there:
await stripe.subscriptions.create({ customer: customer.id,
  items: [{ price: process.env.STRIPE_PRICE_ID }], default_payment_method: 'pm_card_visa' });
// deliver customer.subscription.created → billing_accounts boundaries exactly 7 days apart,
//                                          period_tracking_valid: true
// week 1: seed a billing_ledger row (fee_microusd=250000), run the worker → 'reported'
await stripe.testHelpers.testClocks.advance({ test_clock: clock.id, frozen_time: T0 + 604800 });
// wait for clock 'ready' → Stripe finalizes the week-1 invoice:
//   LINE TOTAL MUST BE 250000 × $0.000001 = $0.25 — this is the single number that proves
//   the whole pricing pipeline end to end
// deliver invoice.paid + customer.subscription.updated →
//   current_period_* rolled forward by EXACTLY 604800000 ms and period_tracking_valid STILL true
//   (the critical assertion), last_invoice_status='paid' in /api/billing/status
// week 2: repeat across a DST transition window → second invoice exact, boundaries still exact
// week 3: advance with NO usage → $0 invoice; nothing enters review/capped
```

While the clocked weeks close, this is also the moment to shadow-run the settlement writer by
hand against your local schema (the operator recipe in 202607280013's header):

```sql
select public.settle_billing_period('<org>'::uuid,
         '<any instant inside the closed week>'::timestamptz, 'operator:<you>');
```

Drafts only; it cannot bill. Compare its `fee_microusd` against the Stripe invoice line from the
clock roll — they are computed by two independent paths and must agree.

### 6.4 After Tier 3 is green

Failure-injection rows that need Stripe objects (F1, F2, F7, F10, F12 from §4 of the test plan),
then staging (ST1-ST5, G16 soak), then the production-access gates (G7, G10-G14), then — and only
then — the flip procedure in GO_LIVE_RUNBOOK.md:127-133 (cap first, then gate; compressor → API →
worker with `/ready` waits).

---

## 7. Still out of scope, plainly

- **Production (wyfz) state.** Nothing in this build read or wrote production. G12's trigger-state
  catalog query is the one decision-critical read, and it must go through the sanctioned one-file
  path — wyfz has no migration ledger; never replay the chain, never `db push`.
- **The real Anthropic invoice reconciliation (G13).** Calendar time and a human. Until one closed
  7-day window's `sum(actual_cost_usd)` is reconciled against a real provider invoice AND
  `organization_billing_arrangement` rows are attested, every org halts at
  `unattested_billing_arrangement` and nothing is billable. That is deliberate.
- **The promotion grant.** `promote_billing_period_settlement` is EXECUTE-granted to nobody. The
  writer computes; only a psql session can queue money, and automating that is a separate,
  signed, one-line migration that should not be smuggled in with anything else.
- **The settlement shadow sweep (stage 2).** Specified in full (enumeration RPC shipped, cadence,
  env gate `BREVITAS_SETTLEMENT_SWEEP_ENABLED`, its own supervised task — NOT inside
  `run_billing_recovery_loop`), but not implemented; a follow-on PR in `api/`.
- **Live mode entirely.** No live key, charge, refund, dispute, tax. G11 (roll the leaked
  `sk_live`) is a Stripe-dashboard act that predates any live step.
- **Staging soak (G16), load/perf, dashboard visual regression, PostHog analytics gaps, GoTrue
  edge cases** — per the test plan's §6.
- **A settlement audit table.** Halts are returned, not persisted; "why didn't org X settle" lives
  only in logs/metrics. Known operability gap, deferred until the shadow run shows whether it hurts.
- **Nine foreign migrations (280014-280022).** Not reviewed by this build's adversarial pass beyond
  their registrar impact. Whoever registers them owns confirming they are double-apply idempotent
  (the harness now enforces that at run time) and that none touches the money path.

---

## Appendix: one-line status of every file this build touched

Modified (tracked): `scripts/ci/run-migration-tests.sh`, `scripts/ci/verify-migrations.mjs`, both
manifests, `migration-frozen-checksums.txt`, 5 assertion SQL files + `scripts/dr/compliance-workflow-assertions.sql`,
`api/worker.py`, `api/billing_recovery.py`, `brevitas/observability.py`, `scripts/setup-stripe-billing.mjs`,
`src/lib/billing/config.ts`, `src/lib/billing/checkout-reservation.mjs`,
`src/app/api/billing/{status,portal,webhook}/route.ts`, `dashboard/src/{App.jsx,components/Billing.jsx}`,
`eslint.config.mjs`, `tests/{stripe_billing_config,company_billing_authorization,release_security,billing_checkout_session_reservation}.test.mjs`,
`tests/{test_billing_recovery,test_observability}.py`, `docs/{ENV_ROLLOUT_CHECKLIST,STRIPE_FIX_PLAN,STRIPE_TEST_PLAN}.md`.
New (untracked): migrations `202607280010`-`202607280013`, 6 new `scripts/ci/migration-*-assertions.sql`
suites, `src/lib/billing/config-predicate.mjs`, `tests/{billing_config_predicate,billing_status_settlement_repoint,billing_dashboard_money_display,billing_checkout_rpc_parsers}.test.mjs`,
`tests/test_billing_supervisor.py`. Deleted: `tests/billing_status_settlement_pending.test.mjs`
(superseded property-for-property). Nothing committed; HEAD is still `71e20ef` `[verified today]`.
Also present in the tree but NOT this build's work: migrations `202607280014`-`202607280022` and
modifications to `api/server.py`, `api/store.py`, `brevitas/proxy.py`, `dashboard/src/lib/api.check.mjs`,
`tests/test_backend_contract_repairs.py`, `tests/test_cloud_usage_api.py` — other workstreams,
sharing this working tree.
