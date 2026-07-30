# Stripe Billing Test Plan

**Scope:** prove that Stripe billing FULLY WORKS — checkout → subscription → usage accrual → meter event → weekly invoice → portal — before `BREVITAS_BILLING_ENABLED` is flipped to `true`.
**Repo state this plan was written against:** branch `chore/retire-per-row-fee-trigger` @ `71e20ef`, 2026-07-29.
**Pricing contract under test:** 25% of verified savings, billed every 7 days on a Stripe-anchored rolling week (not calendar). Stripe Checkout + Stripe Billing meters. Rate constant: `api/server.py:3741` (`BREVITAS_FEE_RATE = 0.25`); fee expression `api/server.py:4007`.

---

## 0. Read this first: known-red state and structural facts

These are not test failures to discover — they are the starting conditions. Every tier below is ordered around them.

| # | Fact | Evidence | Consequence for this plan |
|---|------|----------|---------------------------|
| 0.1 | **Nothing writes `billing_ledger`.** Migration `202607280006` dropped `queue_brevitas_fee_after_usage`, the only INSERT path (`supabase/migrations/202607280006_retire_per_row_fee_trigger.sql:29`; the four historical INSERTs are all inside `queue_brevitas_fee()` bodies, e.g. `202607200006_company_billing_authorization.sql:225`). The replacement `period_settlement_ledger` (`202607280007`) has **no writer and no RPC** — settlement is deliberately manual. | verified by execution: an authoritative priced `usage_log` insert produces zero ledger rows | Every test of the worker/meter path must **hand-seed** `billing_ledger` rows. The full pipeline cannot bill automatically today; that is Phase 3/4 work, and the go/no-go list gates on it. |
| 0.2 | **The migration integration harness is red on both paths** (reproduced against real PostgreSQL 17.10). Upgrade path: `scripts/ci/run-migration-tests.sh:86` binds hand-written indices ending at `202607280004`, so `280005–280009` are never applied and `migration-receipt-accounting-assertions.sql:54` raises `retired per-row billing trigger has been reattached` at `run-migration-tests.sh:390`. Fresh path: the loop at `:477-479` applies `280006` (drops trigger), then `:483` replays `202607170012`, whose guard (`202607170012_receipt_accounting_alignment.sql:5-25`) requires the trigger → raises. Six assertion suites additionally still depend on trigger-created fixture rows. | executed, exit 3 both ways | The integration-local DB tier (§2.2) is **blocked until the harness is fixed** (fix sketch in §2.2 row 1). Do not attempt `run-migration-tests.sh` before that. A correct local schema CAN still be provisioned: loop `scripts/ci/migration-fresh-manifest.txt` with `psql --set ON_ERROR_STOP=1` — all 57 entries apply cleanly (verified). |
| 0.3 | **One Node test is deliberately red:** `tests/stripe_billing_config.test.mjs:69` expects `fee = round(verified * BREVITAS_FEE_RATE, 10)` but `api/server.py:4007` now reads `round(max(0.0, verified) * ...)`. Left red on purpose per `BILLING_CORRECTNESS_PLAN.md:204-213` (human decision on the clamp). Because `.github/workflows/security.yml` has no paths filter, this blocks every PR to main. | measured: 8/9 pass | Go/no-go item G3. Resolve the human decision before anything else merges cleanly. |
| 0.4 | **Local Stripe surface is greenfield.** `.env.local` contains ZERO `STRIPE_*` / `BREVITAS_BILLING_*` names; the `stripe` CLI is not installed. But `stripe@22.3.2` is installed as a project dependency and exposes `webhooks.generateTestHeaderString`, `testHelpers.testClocks.{create,advance,retrieve,list,del}`, and `billing.meters.listEventSummaries` (verified present). | `which stripe` → not found | The CLI is **optional**. Everything except `stripe listen`'s tunnel can be driven from Node with the installed SDK. |
| 0.5 | **`BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=false` (the shipped default, `.env.example:64`) makes reconciliation `ACCEPTED` unreachable** (`api/billing_recovery.py:538-543`). | code read, pinned by `tests/test_observability.py:706` | The reconcile-accept branch can only be tested in a **dedicated** test-mode account with the flag set true (§4 row F4). |
| 0.6 | **The webhook re-retrieves every subscription/invoice from Stripe** (`src/app/api/billing/webhook/route.ts:239,300,318-321`). | code read | Hand-signed local webhooks MUST reference real test-mode object IDs. Fabricated `sub_`/`in_` IDs produce 500s, not happy paths. Signature-only tests (valid/invalid/missing) need no real IDs. |
| 0.7 | **No Stripe API version is pinned.** Node SDK defaults to `2026-06-24.dahlia` (`node_modules/stripe/cjs/apiVersion.js:5`); the Python worker sends no `Stripe-Version` header (`api/billing_recovery.py:37`); `scripts/ci/staging-canary.mjs:303` hard-codes `2025-06-30.basil`. The code depends on ITEM-level `items.data[0].current_period_start/end` (`src/lib/billing/stripe-state.mjs:55-74`) and `invoice.parent.subscription_details.subscription` (`stripe-canonical-state.mjs:82-84`). | code read | **Setup step S6 verifies the shape before anything else.** Go/no-go item G9 requires a single pinned version. |
| 0.8 | **Confirmed defect PSL-LATCH:** `prevent_period_settlement_identity_change` does not latch `outbound_started_at`/`reported_at`/`settled_at`, so a reported settlement can be voided and the period re-billed at full rate (reproduced: 45,000,000 µUSD committed against a 22,500,000 ceiling). | executed on real PG | Go/no-go item G4: the latch migration must land before any settlement writer exists. §4 row F13 is the regression test. |
| 0.9 | **Confirmed defect AR-1:** `/api/billing/status` sums `billing_ledger` (`src/app/api/billing/status/route.ts:34-49`), a table with no writer. Once billing flips on, customers would see `$0.000000` estimates. | code read + 0.1 | Go/no-go item G5: repoint status at the money-of-record before flip. |

---

## 1. Preconditions / setup

Do these in order. S1–S6 need only a laptop and a Stripe test-mode account. S7–S9 build the local loop.

### S1 — Stripe test-mode account + keys
1. Use a **dedicated, disposable** Stripe test-mode account (or a fresh sandbox in the existing account). Two reasons: `npm run billing:setup` calls `products.update`/`prices.update`, and the exclusive-writer reconcile test (F4) requires that *nothing else* ever emits `brevitas_fee_microusd` in the account.
2. Obtain `sk_test_...` from the dashboard. **Never** use `sk_live_` (the setup script refuses it without `--live`, `scripts/setup-stripe-billing.mjs:13-16`).

### S2 — (Optional) install the Stripe CLI
```bash
brew install stripe/stripe-cli/stripe
stripe login   # test mode
```
Only needed for `stripe listen` (real Stripe-originated deliveries to localhost) and `stripe trigger`. Every other step in this plan uses the installed `stripe@22.3.2` SDK.

### S3 — Provision the catalog (product + price + meter)
```bash
STRIPE_SECRET_KEY=sk_test_... npm run billing:setup
```
This is `scripts/setup-stripe-billing.mjs`: idempotent; creates/reuses an active meter with `event_name=brevitas_fee_microusd` (sum aggregation, `customer_mapping` by_id on `stripe_customer_id`, value key `value`) and a Price with lookup_key `brevitas_verified_savings_fee_weekly_v2`, `unit_amount_decimal '0.0001'` (one micro-dollar), `recurring {interval: week, usage_type: metered, meter}` (`:20-53`). It prints `STRIPE_METER_EVENT_NAME` and `STRIPE_PRICE_ID` to copy (`:77-79`).

**Do not hand-create the price in the dashboard** — both validators assert `unit_amount_decimal` exactly `'0.0001'` (`src/lib/billing/config.ts:67`; `api/billing_recovery.py:441-453`). **Caveat:** the script's reuse guard (`:56-63`) does not check `interval_count`, `currency`, or `active` — inspect the returned price object rather than trusting the guard. (No validator checks `interval_count` either — defect WR-3; unit test U6 pins the fix.)

Meters cannot be deleted, only deactivated — pick event names deliberately.

### S4 — Save a test-mode Customer Portal configuration (manual, Stripe Dashboard)
Settings → Billing → Customer portal, **in test mode**: enable payment-method update, invoice history, cancellation; do NOT enable plan switching (`docs/STRIPE_BILLING.md:277`). Without a saved test-mode portal config, `billingPortal.sessions.create` (`src/app/api/billing/portal/route.ts:47-50`) errors and "Manage billing" returns a bare 500.

### S5 — Generate the recovery secret and webhook secret
```bash
openssl rand -base64 32     # BILLING_RECOVERY_SECRET — must pass recoverySecretIsStrong
```
Strength contract: ASCII, 32–256 bytes, non-repeating, ≥3 char classes with ≥12 distinct chars, OR ≥64 hex chars (`src/lib/billing/recovery-auth.mjs:30-43`). **A weak secret does not 401 — it silently makes `billingIsConfigured()` false and 503s the webhook AND checkout** (`src/lib/billing/config.ts:37`; `src/app/api/billing/webhook/route.ts:358`). `'a'.repeat(64)` fails; `openssl rand -base64 32` passes.

`STRIPE_WEBHOOK_SECRET`: if using `stripe listen`, use the `whsec_` it prints. For hand-signed local tests (§S8 option B), any locally invented `whsec_...` works — `generateTestHeaderString` signs with whatever secret you pass.

### S6 — Verify the API shape BEFORE anything else
One throwaway Node script against the test account:
```js
const s = await stripe.subscriptions.retrieve(subId); // any test sub on the S3 price
assert(Number.isFinite(s.items.data[0].current_period_start));
assert(Number.isFinite(s.items.data[0].current_period_end));
```
If item-level periods are absent, **every subscription webhook will throw `StripeSubscriptionPeriodError`** (`src/lib/billing/stripe-state.mjs:55-74`) and `period_tracking_valid` stays false forever. Pin `apiVersion` (G9) before proceeding if this fails.

### S7 — Environment variables (names only; never print values)

| Variable | local `.env.local` | Vercel Preview + Prod | Railway worker | Notes |
|---|---|---|---|---|
| `BREVITAS_BILLING_ENABLED` | `true` (local test only) | `false` until go/no-go passes | required `true` for authoritative worker | Exact string `'true'` — `'TRUE'`, `'1'` all mean disabled (`maintenance-gate.mjs:4`, `api/billing_recovery.py:796`) |
| `STRIPE_SECRET_KEY` | sk_test | yes (roll the leaked live key first — `GO_LIVE_RUNBOOK.md:30-32`) | yes | |
| `STRIPE_WEBHOOK_SECRET` | whsec (S5) | yes | **NO** — webhooks land on Vercel only (`ENV_ROLLOUT_CHECKLIST.md:275`) | |
| `STRIPE_PRICE_ID` | from S3 | yes | yes | |
| `STRIPE_METER_EVENT_NAME` | `brevitas_fee_microusd` | optional (defaults on Next) | **required** on worker, no default (`api/billing_recovery.py:800`) | |
| `BREVITAS_BILLING_WEEKLY_CAP_USD` | `1` for local tests | yes — currently MISSING per `ENV_ROLLOUT_CHECKLIST.md:343` | yes; unset value RAISES on worker (`api/billing_recovery.py:560`) | Next requires `0 < cap ≤ 100000` |
| `BILLING_RECOVERY_SECRET` | from S5 | yes | no | see S5 trap |
| `BREVITAS_PUBLIC_URL` | omit — plain-http loopback is accepted in local development only (`src/lib/billing/config-predicate.mjs`) | `https://brevitassystems.com` — **CF-1 is CLOSED: code now REQUIRES https once deployed (`NODE_ENV=production` or `VERCEL_ENV` set) and there is no longer a silent `http://localhost:3000` default, so an unset value fails `billingIsConfigured()` outright (503 on checkout/portal/webhook). Code still cannot catch a wrong-but-https value.** Set it BEFORE flipping `BREVITAS_BILLING_ENABLED`, or the flip does nothing | no | |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | already present by name | yes | worker needs URL + service key | anon key deliberately not needed by webhook (`supabase.ts:94-99`) |
| `BREVITAS_API_URL` | optional (defaults `http://localhost:8000`) | required https at BUILD time (`next.config.ts:36-54`) | n/a | canonical; `API_URL` is dead — delete it |
| `BREVITAS_WORKER_BILLING_ROLE` | leave unset locally → `optional` (`api/worker.py:96`) | n/a | injected `authoritative` by start command (`deploy/railway-worker.json:9`) | authoritative + incomplete config = boot RuntimeError (`api/worker.py:735-736`) |
| `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER` | `false` default; `true` only in the dedicated account for F4 | — | decision at launch (G8) | |

Dead config (do not set, do not trust docs that mention it): `BREVITAS_BILLING_BATCH_SIZE` has no reader anywhere (batch size is hard-pinned to 1 at `api/billing_recovery.py:281` + SQL guard). `BREVITAS_BILLING_WORKER_ID` is unused on the worker path (`api/worker.py:742` supplies its own prefix).

### S8 — Signed webhooks to localhost (three options)
- **A (CLI):** `stripe listen --forward-to localhost:3000/api/billing/webhook`; copy the printed `whsec_` into `STRIPE_WEBHOOK_SECRET`. Only option giving genuine Stripe-originated events + Stripe's own retry behavior.
- **B (SDK, no CLI — verified working with a dummy key):**
  ```js
  const payload = JSON.stringify(eventJson);                 // sign THESE EXACT BYTES
  const sig = stripe.webhooks.generateTestHeaderString({ payload, secret: process.env.STRIPE_WEBHOOK_SECRET });
  await fetch('http://localhost:3000/api/billing/webhook', { method: 'POST', headers: { 'stripe-signature': sig }, body: payload });
  ```
  Constraint 0.6 applies: embedded `sub_`/`in_`/`cus_` IDs must exist in test mode for any path past signature verification.
- **C (tunnel):** register a test-mode webhook endpoint on a public tunnel to :3000 with exactly the six event types (`checkout.session.completed`, `customer.subscription.{created,updated,deleted}`, `invoice.{paid,payment_failed}` — `docs/STRIPE_BILLING.md:278-286`). Only way to observe Stripe's real retry/duplicate cadence.

### S9 — Local schema + app stack
1. **Schema (loopback Postgres only; NEVER a remote project — wyfz has no migration ledger, never replay the chain blind):**
   ```bash
   psql "$LOCAL_DSN" -f scripts/ci/migration-bootstrap.sql
   grep -v '^\s*#' scripts/ci/migration-fresh-manifest.txt | while read -r m; do
     psql "$LOCAL_DSN" --set ON_ERROR_STOP=1 -f "$m" || break
   done
   ```
   All 57 entries apply cleanly (verified on PG 17.10). Do NOT use `supabase db reset`, do NOT apply in filename order, and do NOT run `scripts/ci/run-migration-tests.sh` until §2.2 row 1 is fixed.
2. **Seed rows** (because of 0.1): an `organizations` row + membership with `billing:manage`, a `billing_accounts` row (`subscription_status='active'`, `stripe_customer_id` = a real test-mode `cus_`, `current_period_end - current_period_start` = **exactly** `interval '7 days'`), and hand-INSERTed `billing_ledger` rows:
   ```sql
   insert into public.billing_ledger (usage_log_id, organization_id, user_id, occurred_at, fee_microusd)
   values (:usage_id, :org, :owner, now(), 250000);  -- $0.25
   ```
   Legal: there is no BEFORE INSERT trigger on `billing_ledger`, only delete/identity guards (verified).
3. **App stack:** `npm run build:dashboard && npm run dev` (NEVER `cd dashboard && npm run build` — silently drops Supabase config). The Vite dev server on :5174 proxies only `/v1`, so **all billing routes 404 there**; use :3000. Start the FastAPI API on :8000 too, or the dashboard replaces the Savings tab with a stats error before the billing card renders (gate at `dashboard/src/components/Billing.jsx:104-121`).
4. **Worker (local):** `python -m api.worker` with the full billing env and `BREVITAS_WORKER_BILLING_ROLE` unset ('optional' outside production). Health app on :8001 — poll `/ready` for the `billing_recovery` block (`api/worker.py:261-271`).

---

## 2. Tiered test suites

Order: run each tier to green before spending money/time on the next. Tier legend for existing coverage honesty: T1 = source-text regex (vacuous as behavior), T2 = behavior vs. hand-rolled fakes, T3 = real PostgreSQL.

### 2.0 Tier 0 — existing suites, run now (minutes, zero setup)

Baseline before touching anything. Report actual output.

| # | what it proves | how to run | pass criteria | blockers |
|---|---|---|---|---|
| 0.1 | Node billing suite baseline | `npm test` (node --test tests/*.test.mjs) | Everything green **except** `stripe_billing_config.test.mjs` 8/9 (known red at `:69`, see 0.3) | none |
| 0.2 | Worker recovery control flow (T2, fakes) | `./.venv/bin/python -m pytest tests/test_billing_recovery.py -q` | 29 passed | none |
| 0.3 | Fee/savings arithmetic incl. signed rows, authoritative boundary (T2, real SQLite) | `./.venv/bin/python -m pytest tests/test_cloud_usage_api.py -q` | 27 passed; negative-savings row bills $0 (`:535,587`) | none |
| 0.4 | Release contracts: order, drift, checksums | `node scripts/ci/verify-migrations.mjs && node --test tests/release_security.test.mjs` | "verified." + 12/12 (measured green at 71e20ef) | none |
| 0.5 | Dashboard lib checks | `cd dashboard && node --test src/lib/*.check.mjs` | 108/108 (measured) | none — note this suite has ZERO billing coverage (see U11) |
| 0.6 | Migration harness — **expected RED, do not "fix" by editing assertions** | `DATABASE_URL=postgresql://...@127.0.0.1:5432/x bash scripts/ci/run-migration-tests.sh` | Known exit 3: upgrade path dies at `migration-receipt-accounting-assertions.sql:54`. Green only after I1. | local loopback PG |

### 2.1 Tier 1 — unit (no DB, no Stripe, no network; fail-fastest)

New tests. Rows U1–U5 need only existing seams; U6–U12 include small production refactors noted in blockers.

| # | what it proves | how to run (exact) | pass criteria | blockers |
|---|---|---|---|---|
| U1 | HTTP status → outcome mapping in the worker: 429→`StripeUnavailable`, 5xx/408/409→`StripeAmbiguous`, `idempotency_error`→Ambiguous, other 4xx→`StripeRejected` (`api/billing_recovery.py:372-388`). This branch decides release-and-retry vs. leave-in-'sending' — the single most consequential branch in the module, currently 0% covered. | Extend `tests/test_billing_recovery.py` using the existing `FakeResponse/FakeSession` pattern (`:691-719`); `pytest tests/test_billing_recovery.py -x -q` | Each of {429, 503, 408, 409, 400+idempotency_error, 402, 400-non-JSON} maps to the documented exception; processor-level: **CORRECTED (integrator)** — this row described the pre-FIX-10 snapshot and directly contradicted FIX-10's own Verify line. Post-FIX-10 the row returns to `pending` with `attempts` DECREMENTED and `outbound_started_at` cleared, via the new `public.release_billing_ledger_unsent(bigint, text)` (202607280011 — note **bigint**, not uuid: `billing_ledger.id` is `bigint generated always as identity`). Do not "restore" the leave-in-`sending` behaviour. | none |
| U2 | The outbound meter-event POST body is byte-exact: `event_name`, `identifier=brevitas-fee-{id}`, `timestamp=int(occurred_at)`, `payload[stripe_customer_id]`, `payload[value]=str(fee_microusd)`, header `Idempotency-Key: brevitas-meter-{id}` (`api/billing_recovery.py:396-407`) | Two-line extension of `test_valid_catalog_is_cached_before_normal_meter_event_post` (`tests/test_billing_recovery.py:861`): assert `session.requests[-1]` kwargs verbatim | Exact dict equality on `data` and `headers`, timeout tuple `(3.0, 10.0)` | none |
| U3 | Worker env cross-validation: `lease_seconds ≥ 3×timeout+5` boundary (`api/billing_recovery.py:812-816`); per-variable required-set of `billing_recovery_is_configured()` (`:795-803`) | pytest with `monkeypatch.delenv`, one param per var | `'34'` raises / `'35'` builds; deleting any of the 5 required names → configured False; `NEXT_PUBLIC_SUPABASE_URL` fallback works | none |
| U4 | Launch-gate parity JS↔Python: both sides treat the SAME value set as disabled, including `' true '` / `'true '` (the realistic paste error) | Shared fixture `tests/fixtures/billing-gate-values.json`, consumed as shipped by `tests/billing_launch_gate_parity.test.mjs` and `tests/test_billing_launch_gate_parity.py` (both new; the two files named here already carry their own divergent value lists and were left untouched — de-duplicating onto the fixture is a follow-up) | Both accept only exact `'true'`; all 10 falsy values disabled on both sides | none |
| U5 | Manifest coverage fails CLOSED: every entry of `migration-upgrade-manifest.txt` is actually applied by `run-migration-tests.sh` | New assertion in `tests/release_security.test.mjs`: parse `${upgrade_migrations[N]}` bindings + literal `apply_migration` paths; assert covered index set = {0..len-1}. **As shipped** it also credits the manifest-resolved handles (`foo="$(manifest_entry '…')"` + `apply_migration "${foo}"`) and the whole-array driver loop — recognised only in its exact shape, with entries diverted by a non-default `case` arm NOT credited to the loop and required instead to be the guarded billing-identity set. | **Green as shipped** (I1 landed: the harness loops the whole array). Mutation-verified non-vacuous: bounding the loop to 40 fails with 'must drive the entire upgrade manifest', and diverting one extra migration out of the default arm fails with 'only the guarded billing-identity migrations may bypass the driver loop'. | none (static analysis) |
| U6 | Catalog validators reject `interval_count ≠ 1` (defect WR-3: a 2-week price passes all three validators today, then every claim raises invalid-anchor) | Add `interval_count` param to `catalog_responses()` (`tests/test_billing_recovery.py:720`); mirror table for `src/lib/billing/config.ts` via injected retriever | `interval_count: 2` → `CatalogContractError` / rejected promise; `1` and absent → pass | requires adding the check to `api/billing_recovery.py:448`, `config.ts:68`, `setup-stripe-billing.mjs:56-62` first |
| U7 | `billingIsConfigured()` truth table: 9-condition conjunction (`src/lib/billing/config.ts:33-44`), cap boundaries (0/0.0000001/100000/100001), weak-secret false, `https`-only public URL when deployed (CF-1 fix) | Extract predicate to a plain `.mjs` taking an env object (pattern of `maintenance-gate.mjs:3`); `node --test tests/billing_config_predicate.test.mjs` | Base env true; each single-field mutation false; boundary rows exact | production refactor: config.ts imports `server-only` and reads `process.env` directly |
| U8 | `validateStripeCatalog()` negative cases ×8 (inactive, one_time, eur, tiered, 0.001, month, licensed, no meter, inactive meter, wrong event_name) + memoization contract (success memoized for process life, failure retried — `config.ts:57,79-84`) | One scenario per process (memo is process-wide); loader shim + stubbed Stripe client | Each mutation rejects with the contract message; post-failure retry works | `server-only` shim + `@/*` resolver (see §2.2 note) |
| U9 | Checkout RPC result parsers fail closed on unknown shapes (`src/lib/billing/supabase.ts:335,366,399,419`) — an unrecognized code must throw (→500), never fall through to 'acquired' | Table-driven stubs of `billingDatabase().rpc` | null/[]/bogus-mode/`retry_after_seconds:301`/unknown code all throw; `release` returning non-boolean throws | loader shim |
| U10 | `webhookLeaseParameters` event-id binding: a lease can never write another event's snapshot (`src/lib/billing/canonical-persistence.ts:34-36`); `parseRevision` rejects negative/non-safe ints | Stubbed rpc recorder | evt mismatch rejects before any RPC; params carry the lease's `p_event_id` | loader shim |
| U11 | Dashboard `billingJson` contract: relative path, `Authorization: Bearer` (never `X-Brevitas-Key`), `error.status` copied — the ONLY reason the 409→portal fallback works (`dashboard/src/lib/api.js:100-118`, `Billing.jsx:88-95`) | Add to `dashboard/src/lib/api.check.mjs` using the injectable `request` seam; `cd dashboard && node --test src/lib/api.check.mjs` | 409 propagates `.status===409`; POST verbs; header set exact | none |
| U12 | **DONE.** Every metric name the worker emits has a branch in `brevitas/observability.py` — the three formerly-dropped names (`billing.pending_count`, `billing.stripe_unavailable`, `billing.catalog_validation_error`) are mapped onto EXISTING instruments (`brevitas.queue.depth{queue="billing"}` and `brevitas.billing.recovery{outcome=…}`), so no dashboard or alert file needed editing | Contract test in `tests/test_observability.py`: regex-harvest metric literals from `api/billing_recovery.py`, drive `record_billing_metric` | Fails today on exactly 3 names; green after branches added | none |

### 2.2 Tier 2 — integration-local (loopback PostgreSQL + invocable route handlers; no Stripe account)

**Shared blocker A (routes):** the five route handlers have never been executed by any test. To invoke them under `node --test`: (a) a resolve-hook shim mapping `server-only` → empty module (package genuinely absent from node_modules; Next aliases it at build time), (b) a resolver for the `@/*` tsconfig alias, (c) type stripping. **Integrator correction: (c) needs NO command-line flag** — the shipped loader calls `module.stripTypeScriptTypes` inside a load hook, so `npm test` is unchanged; verified by running every suite under `--no-experimental-strip-types`. Do not edit the `test` script for this. Build `tests/helpers/billing-route-loader.mjs` once; every row below reuses it. **Blocker A is CLOSED.**
**Shared blocker B (schema):** row I1 first.

| # | what it proves | how to run (exact) | pass criteria | blockers |
|---|---|---|---|---|
| I1 | **The harness itself.** Fix + prove both migration paths green. Fix set: (1) loop the full upgrade array instead of hand bindings at `run-migration-tests.sh:56-86`; extend the order guard `:87-117` to compare the whole array so migration 46 fails CLOSED; (2) stop replaying `202607170012` into a post-280006 schema (drop/reorder the replay at `:483`, re-assert trigger absence after); (3) re-fixture the 6 trigger-dependent suites (`migration-assertions.sql:240-259`, `migration-key-audit-…:333`, `dr/compliance-workflow-…:407`, `migration-company-billing-…:165-176`, `migration-billing-recovery-scope-…:13`, `migration-compliance-billing-isolation-…:90`) with direct `billing_ledger` INSERTs; update frozen checksums in the same commit (`verify-migrations.mjs:85-90`) | `DATABASE_URL=postgresql://...@127.0.0.1:5432/ci bash scripts/ci/run-migration-tests.sh` | exit 0, both paths, trigger absent at the end of each | local PG (Docker fails the loopback guard at `run-migration-tests.sh:16-26`; use native postgres with pgvector dylib symlinked) |
| I2 | Live org-scoped `claim_billing_ledger_entries` (`202607200006:302-434`) behaves: fresh claim, reclaim with UNCHANGED `expected_period_microusd`, `p_limit<>1` rejection. Currently pinned only by a grep of the SUPERSEDED 170004 definition | New `scripts/ci/migration-billing-claim-assertions.sql` wired into `run_forward_assertions`; fixture rows via direct INSERT | reclaimed=false → expected = own fee; reclaimed=true → expected unchanged; `p_limit=2` raises | I1 |
| I3 | Weekly cap → `capped` transition and the reclaim exemption (`202607200006:412-419`) — the ONLY ceiling on what the worker can meter; zero coverage today | Same fixture file: two 600000-µUSD rows, cap 1000000; claim twice; then reclaim test | row 2 → `capped`, `last_error='weekly safety cap reached'`; reclaimed in-flight row is NOT cap-blocked | I1 |
| I4 | The three DB sweeps: 34-day pending→`expired`, 23h outbound→`review`, attempts-exhausted→`review` (**cite `202607200006:344-360`**, not `202607170004:181-207` — integrator correction: 202607200006 does a `create or replace` of the same signature, so 170004's copy is the SUPERSEDED body. The sweep code is byte-identical between the two, so the intent is unaffected, but editing 170004 to "fix a sweep" would patch a dead function) | Three seeded rows, one claim call, assert final statuses + exact `last_error` strings | as specified | I1 |
| I5 | Invalid anchor → `review` (`202607200006:386-391`); `billing_period_for_occurrence` rejects non-7-day span with 22023, half-open boundaries, DST invariance | SQL assertions incl. the boundary cases the migration self-checks — **integrator correction: there are THREE, at `202607170004:91-133`** (prior seven-day period; end boundary enters next period; UTC/DST crossing), not four. The most valuable boundary the migration does NOT self-check is the INCLUSIVE lower bound (an occurrence exactly on `anchor_start`); as shipped, all three are replayed and six more added, including that one. Rerunnable file, run under a non-UTC session timezone on purpose | 30-day anchor → row `review`, `last_error='invalid Stripe weekly billing-period anchor'` | I1 |
| I6 | `service_role` cannot write `billing_ledger` at all (`202607220001:60-92`) — foundation of the lease-fencing model | `set local role service_role;` + INSERT/UPDATE/DELETE each asserting SQLSTATE 42501 | all three denied | I1 |
| I7 | `period_settlement_ledger` structure: 25% CHECK cap (`202607280007:192-197`), generated zero-floor net (`:137`), 6-day window rejection, live-period uniqueness (`:205-207`), undeletability, zero PostgREST privileges, id sequence ≥ 1000000000 | New `scripts/ci/migration-period-settlement-assertions.sql` in `run_forward_assertions` | fee 22500001 on verified=100/warm=10 → 23514; verified=5/warm=10 → net 0; delete → P0001; duplicate live period → 23505 | I1 (table doesn't exist on upgrade path until then) |
| I8 | **PSL-LATCH regression** (0.8): after the fix migration, `outbound_started_at`/`reported_at`/`settled_at` cannot be cleared, and voiding a reported row does not reset the cumulative ceiling | Same file: promote row to `reported`, then attempt the void+clear UPDATE; re-run the guard | UPDATE raises P0001; guard still raises `cumulative_ceiling` for a second fee | I1 + the latch migration (G4) |
| I9 | All three halting conditions with NON-EMPTY evidence (`202607280008`): relative ceiling exact boundary (verified=100, warm=10 → ceiling 22500000), zero_spend, zero_spend_concentration, cumulative_ceiling, negative fee 22023, unattested 55000, attestation privilege posture (`has_function_privilege` false for service_role on the inner guard) | New `scripts/ci/migration-halting-conditions-assertions.sql`, `begin;...rollback;` wrapped | 22500000 passes / 22500001 raises `relative_ceiling`; each condition's exact `halting_condition=` tag | I1 |
| I10 | Webhook signature verification over raw bytes, end to end — currently ZERO executable coverage of the trust boundary (INV-7) | Loader shim; local `whsec_`; SDK `generateTestHeaderString`; POST real `Request` objects to the route | no header → 400 'missing'; 1-byte body mutation → 400 'Invalid webhook signature'; stale timestamp → 400 (**note, measured:** Stripe's tolerance is ONE-SIDED — a future-dated timestamp is ACCEPTED by the SDK. Not a hole, since forging one still needs the secret, but a test expecting symmetric rejection is wrong); valid+unknown type → 200 `{received:true}` with zero writes; claim 'busy' → 503 Retry-After 5; 'processed' → 200 `{duplicate:true}`; `StripeSubscriptionPeriodError` → 500 'manual billing review' + `fail_…` called with reason+subId | blockers A; stub `billingDatabase()` or point at the I1 schema |
| I11 | `/api/billing/sync` full input ladder + ordering: 415, both 413s independently, 4 distinct 400s, weak-secret 503 (Retry-After 300) vs mismatch 401, and the recovery header read only AFTER auth+authz+admission (`sync/route.ts:63-84`) | Loader shim, **genuinely strong** fixture secret (a weak one silently asserts the wrong branch), stubbed RPCs | every row of the ladder; header untouched when maintenance gate/auth fails; X-Request-ID echo rules | blockers A |
| I12 | `/api/billing/status` money semantics against seeded data: exact-604800000 gate (±1ms flips to null fields), half-open window (`gte`/`lt` at `:38-39`, boundary row at `period_end` excluded), **integrator correction:** that filter no longer exists. The route reads `public.billing_period_settlement_summary` (202607280013), so `void` (202607280007:157-159) is excluded in SQL, `review` is no longer counted into the estimate, and the estimate is an evidence projection rather than a ledger sum — see the FIX-5 corrections in STRIPE_FIX_PLAN.md. Assert against the RPC's buckets: `reported_fee_usd` counts only `reported`; `settled_fee_usd` excludes `draft` and `void` | Seed 7 rows (one per status) + boundary rows; GET via loader shim | estimated = 4× fee, reported = 1×, needs_review = 1, capped = 1; boundary exact | blockers A + I1 |
| I13 | Header hygiene sweep: every non-2xx from all five routes carries `Cache-Control: no-store` (status: `private, no-store`) and every 503/429 an integer Retry-After — would have caught the missing headers on checkout's configured-503 (`checkout/route.ts:93`) and the bare 409 (`:56-60`) | Table-driven over every producible status per route | uniform headers. **Measured and fixed as shipped:** the webhook's two 400s ('Webhook signature is missing', 'Invalid webhook signature') were the only bare non-2xx responses and now carry `Cache-Control: no-store`. Separately, `portal/route.ts` had NO configuration gate at all and now answers a diagnosable 503 instead of a Stripe-rejected relative `return_url` | blockers A |
| I14 | Owner-transfer lock serialization — the REAL PostgreSQL proof, currently executed nowhere (the Node test that claims it is 100% grep) | `bash scripts/ci/run-billing-owner-transfer-race-test.sh` (invoked by harness at `run-migration-tests.sh:399`) | race assertions pass: `pg_advisory_lock(170017)` serialization + lock-timeout cancel observed | I1 |
| I15 | Checkout crash-recovery: crash between `sessions.create` and persist CAS → same generation, same idempotency key, SAME session recovered, no second create; release-failure override → 503 even after URL produced (`checkout/route.ts:302-318`) | Loader shim + stubbed Stripe + stubbed reservation store; synthetic abort after create | second attempt returns original session URL; `checkoutIdempotencyKey` identical; deleting the finally-`return` fails the test | blockers A |

### 2.3 Tier 3 — stripe-test-mode (real Stripe test account; no production)

All rows need S1–S6 done. Gate each test file on `STRIPE_SECRET_KEY` starting `sk_test_` and skip otherwise so CI stays hermetic.

| # | what it proves | how to run (exact) | pass criteria | blockers |
|---|---|---|---|---|
| M1 | `npm run billing:setup` actually produces a catalog BOTH validators accept — the script has never been executed by anything | Run S3, then: `validateStripeCatalog()` resolves (Node) AND `StripeRestBillingGateway(key, price, event).validate_contract()` returns the meter id (Python — it checks 4 extra meter fields the JS side doesn't: `api/billing_recovery.py:465-472`) | both accept; any disagreement between the two validators and the script is the bug this finds | S1–S3 |
| M2 | Real meter-event idempotency: same `identifier` posted twice counts ONCE; the ~24h dedup window and error taxonomy match what the fake models (`tests/test_billing_recovery.py:175`) | POST `/v1/billing/meter_events` twice with identical identifier + value 250000; `billing.meters.listEventSummaries` over the window (summaries LAG ingestion — wait/poll; same-second UNKNOWN is expected, not a bug) | summary total = 250000, not 500000; timestamp now−40d → 400 (pins the 34-day expiry assumption, `202607170004:181-187`) | S1–S3 |
| M3 | Checkout session: server-price-only, idempotency per (org, generation), generation advance after terminal session | POST `/api/billing/checkout` twice same generation → same session id; `checkout.sessions.expire` then re-POST → NEW idempotency key, new session; pay with `4242 4242 4242 4242` | as stated; `client_reference_id` = org id round-trips; amount is server-derived | S1–S9 + local schema |
| M4 | Occupying-subscription fencing against live Stripe: existing sub → checkout 409 `{action:'portal'}`; dashboard silently converts to portal | `subscriptions.create` directly (bypassing checkout fence), then POST checkout | 409; UI redirects to portal (manual check M-UI row 5) | M3 |
| M5 | Portal session opens with the saved test config; 409 without a customer; unconfigured portal currently = bare 500 (decide if acceptable) | POST `/api/billing/portal` in the three states | 200+url on billing.stripe.com / 409 exact message / documented 500 | S4 |
| M6 | Worker end-to-end meter write: seeded `billing_ledger` row → claim → send → `reported`, meter summary equals fee exactly | S9 seed fee_microusd=1234567; run local worker (role unset); poll `:8001/ready`; then `listEventSummaries` | row `status='reported'`; summary sums to exactly 1234567 | S1–S3, S9, I1 schema |
| M7 | Worker catalog failure containment: point worker at a mismatched price → row RELEASED (not dead, attempt not burned pre-outbound), `billing_catalog_contract_invalid` alert fires | swap `STRIPE_PRICE_ID` to a monthly sibling price; run one cycle | row back to pending; page-severity alert in logs; never `dead` | M6 |
| M8 | Real subscription object shape feeds `subscriptionPeriod()`: item-level boundaries differ by exactly 604800000 ms across ≥3 periods incl. a DST crossing | test clock (see §3B); feed the retrieved object to `subscriptionPeriod()` | exact 604800000 every period | S6, test clock |
| M9 | Six-status occupancy sweep against real statuses: `active`/`trialing` via checkout; `past_due`/`unpaid` via test clock + `4000 0000 0000 0341`; `incomplete` via `4000 0025 0000 3155` unconfirmed; `paused` via `pause_collection` | drive each, then `customerHasAccountOccupyingSubscription` | every status detected as occupying; a rejecting list call fails CLOSED (unit half in U-tier) | test clock |
| M10 | Reconcile-ACCEPT path (only reachable with `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER=true` in an account where nothing else emits the event name) | post a known event set to a dedicated meter; wait for summaries; run reconcile with the flag true | exact aggregate equality → `ACCEPTED`; with flag false the same state stays `UNKNOWN` (pins the shipped default) | dedicated account; 0.5 |

### 2.4 Tier 4 — staging

| # | what it proves | how to run | pass criteria | blockers |
|---|---|---|---|---|
| ST1 | Staging Vercel routes are wired: status returns `configured:false` while disabled; checkout/portal/sync 503 from the maintenance gate BEFORE auth | curl the staging URLs unauthenticated | 503 with Retry-After 30 on the three POSTs; status 401 (auth) not 503 | staging deploy topology (promote from preview; `BREVITAS_API_URL` canonical) |
| ST2 | Deployed bundle really contains the intended dashboard billing code | grep the deployed JS per the bundle-grep procedure (asset hashes differ local vs Vercel; minifier uses backticks) | expected billing strings present | none |
| ST3 | Worker `/ready` billing block on staging: with role `optional`/`nonbilling` shows honest status; with `authoritative` + incomplete config the service crash-loops (expected, documented `GO_LIVE_RUNBOOK.md:89-96`) | poll Railway worker `/ready` | billing_recovery block matches configured role; **FIXED (was WR-1 adjunct)**: `/ready` now derives the block from the real `BillingLoopHealth` snapshot for every role — `disabled` when the role is `nonbilling` or billing is unconfigured, `unavailable` when a configured loop is dead. Assert THAT: an optional-role worker with billing configured but a dead loop must report `unavailable`, not `ready`. Billing remains non-authoritative for job acceptance, so the HTTP status is unchanged | Railway access |
| ST4 | Real Stripe → staging webhook delivery, retry, and duplicate handling with a test-mode endpoint pointed at staging | register staging URL as test-mode endpoint; `stripe trigger customer.subscription.updated` or SDK-driven events | 2xx only after inbox row `processed`; concurrent delivery 503-busy → Stripe retries; replay → `duplicate:true` | staging env vars set (S7 Vercel column, with `BREVITAS_BILLING_ENABLED=true` on staging ONLY) |
| ST5 | staging smoke + canary still green with billing env present | `node scripts/ci/staging-smoke.mjs` / staging-canary | pass; note canary pins API version `2025-06-30.basil` (`staging-canary.mjs:303`) — align under G9 | staging creds |

### 2.5 Tier 5 — manual-UI (human click-path)

Preconditions: full local loop (S7–S9) or staging with billing enabled.

| # | step | pass criteria |
|---|---|---|
| MU1 | Sign in at `http://localhost:3000/login` → `/dashboard`; wait for API key mint | dashboard loads; key minted (`App.jsx:534-552`) |
| MU2 | Click **Savings** tab | billing card renders; with billing disabled it reads "Billing enrollment is not enabled in this environment", button greyed (`Billing.jsx:180-182`) |
| MU3 | With billing enabled: button reads **Set up billing**, enabled | `/api/billing/status` returned `configured:true` |
| MU4 | Click → Stripe-hosted Checkout → pay `4242 4242 4242 4242` | full-page navigation (no Stripe.js — CSP has no Stripe host and doesn't need one); returns to `/dashboard?billing=success` |
| MU5 | **Known UX gap:** the SPA ignores `?billing=success` — no confirmation renders. Re-click Savings | badge `active`; button now **Manage billing**; Current estimate / Reported to Stripe / Billing week populated (requires 0.9/AR-1 fixed, else "$0.000000" or "Unavailable") |
| MU6 | Click **Manage billing** → Customer Portal → update card → return | portal opens (needs S4); returns to `/dashboard` |
| MU7 | In portal: cancel subscription → return → re-click Savings | badge reflects cancellation after the webhook lands; no error |
| MU8 | Period-invalid rendering: corrupt `current_period_end` by +1ms in DB, reload | money fields show "Unavailable" + red fail-closed banner (`Billing.jsx:162-168`); no $0 |
| MU9 | Visual-only preview (no Stripe): `http://localhost:3000/dashboard?preview=billing` | card renders with hard-coded active payload, button disabled (localhost only) |

---

## 3. End-to-end happy path

Two variants, because a Stripe Customer **cannot be attached to a test clock after creation**, and Checkout normally mints the customer (`checkout/route.ts:105-108`).

### 3A — Real checkout flow (no clock): proves checkout → webhook → state → portal

| step | action | expected in **Stripe** | expected in **Supabase (local)** |
|---|---|---|---|
| 1 | Complete S1–S9; `BREVITAS_BILLING_ENABLED=true` locally; `stripe listen --forward-to localhost:3000/api/billing/webhook` running (or option B replays) | — | — |
| 2 | `GET /api/billing/status` with a Supabase bearer for the seeded org | — | 200, `configured:true`, `subscription_status:null`, `period_tracking_valid:false` |
| 3 | `POST /api/billing/checkout` | Customer created with idempotency key `brevitas-customer-<org>`, metadata `brevitas_organization_id`; Checkout Session `mode=subscription`, `line_items=[{price: STRIPE_PRICE_ID}]`, `client_reference_id=<org>` | `billing_accounts.stripe_customer_id` set via `save_billing_customer_identity`; `billing_checkout_reservations` row `state='persisted'` with the session id |
| 4 | Pay in the hosted page with `4242 4242 4242 4242` | Session `complete`; Subscription `active` on the weekly metered price; `checkout.session.completed`, `customer.subscription.created`, `invoice.paid` emitted | — |
| 5 | Webhooks arrive | endpoint answers 200 each, only after processing | `stripe_webhook_events`: one row per event id, `status='processed'`; `billing_accounts`: `stripe_subscription_id` set, `subscription_status='active'`, `current_period_start/end` set with `end−start = exactly 7 days`, reconcile revisions incremented |
| 6 | `GET /api/billing/status` again | — | `subscription_status:'active'`, `period_tracking_valid:true`, `current_period_*` ISO strings 604800000 ms apart, `estimated_fee_usd: 0` (no ledger rows yet), `weekly_safety_cap_usd` echoed |
| 7 | Seed usage/fee: hand-INSERT a `billing_ledger` row (fee_microusd=250000) with `occurred_at` inside the window (0.1 — nothing does this automatically) | — | row `status='pending'` |
| 8 | Start the worker (`python -m api.worker`, role unset) | after ≤1 poll cycle: `POST /v1/billing/meter_events` with `identifier=brevitas-fee-<id>`, `payload[value]='250000'`, `Idempotency-Key brevitas-meter-<id>` | row → `sending` → `reported`, `reported_at` stamped, lease cleared; worker `:8001/ready` billing block healthy |
| 9 | `stripe.billing.meters.listEventSummaries({customer, start_time, end_time})` (summaries lag — poll) | aggregate for the customer/window = 250000 | `GET /api/billing/status`: `reported_fee_usd: 0.25` |
| 10 | `POST /api/billing/checkout` again | NO new session; six status-scoped subscription list calls find the active sub | 409 `{action:'portal'}`; dashboard converts to portal redirect |
| 11 | `POST /api/billing/portal`; in the portal, cancel at period end, then immediately | `billingPortal.Session` created, return_url `/dashboard`; `customer.subscription.updated` then `customer.subscription.deleted` (terminal `canceled`) | CAS-applied status updates; on the deleted tombstone the account records the terminal state; `stripe_webhook_events` rows processed |

### 3B — Test-clock week roll: proves the 7-day boundary, invoice, and meter aggregation over time

| step | action | expected in **Stripe** | expected in **Supabase** |
|---|---|---|---|
| 1 | `const clock = await stripe.testHelpers.testClocks.create({frozen_time: T0})` | clock `ready` | — |
| 2 | `customers.create({test_clock: clock.id})`; seed `billing_accounts.stripe_customer_id` for the org **via the `save_billing_customer_identity` RPC** so checkout's `if (!customerId)` short-circuits — the customer must be clock-attached BEFORE any checkout runs | clocked customer | `billing_accounts.stripe_customer_id = cus_...` |
| 3 | Create the subscription: try Checkout first; if Stripe rejects a clocked customer in Checkout, `subscriptions.create({customer, items:[{price}], default_payment_method: pm_card_visa})` directly | Subscription `active`, item-level `current_period_*` present (S6) | — |
| 4 | Replay/deliver `customer.subscription.created` | — | `billing_accounts` period boundaries set, exactly 7 days apart; `period_tracking_valid:true` |
| 5 | Seed a `billing_ledger` row inside week 1; run the worker | meter event ingested | row `reported` |
| 6 | `testClocks.advance({frozen_time: T0 + 604800})`; wait for clock `ready` | Stripe finalizes the week-1 invoice for the metered usage; `invoice.paid` (card auto-pays) + `customer.subscription.updated` emitted; **invoice line total = 250000 units × $0.000001 = $0.25** | — |
| 7 | Deliver both events | — | `current_period_*` rolled forward by exactly 604800000 ms (`period_tracking_valid` STILL true — the critical assertion); `last_invoice_status='paid'` surfaced in `/api/billing/status` |
| 8 | Repeat 5–7 for week 2, choosing a window that crosses a DST transition | second invoice exact | boundaries still exactly 7 days; `billing_period_for_occurrence` reconstruction agrees (I5) |
| 9 | Advance a third week with NO seeded usage | $0 invoice (metered, no events) | estimate 0; nothing enters `review`/`capped` |

---

## 4. Failure-injection matrix

Each row: how to induce **in test mode / locally**, and the single correct observable outcome. Wrong outcome = stop-ship.

| # | scenario | how to induce | correct observable outcome |
|---|---|---|---|
| F1 | **Duplicate subscription** (2nd occupying sub for one customer) | `subscriptions.create` directly for a customer that already has one, then deliver its `customer.subscription.updated` | Checkout: 409 `{action:'portal'}` (three independent checks: DB snapshot, reservation RPC, live Stripe lists). Webhook: `StripeDuplicateSubscriptionReviewError` → 500 "Webhook requires manual billing review"; event row stays retryable (`fail_…` sets `lease_expires_at=now`), NEVER auto-cancelled (`subscription-policy.mjs:85-90`) |
| F2 | **Replayed webhook** (identical delivery) | re-POST the same signed body+header (option B makes this trivial) | 200 `{received:true, duplicate:true}`; `attempts` unchanged; zero business writes. Same event_id with a DIFFERENT event_type → hard error (`202607200001:96-100`) → 500, never a silent overwrite |
| F3 | **Lease loss mid-write** | expire the webhook lease in DB (`update stripe_webhook_events set lease_expires_at=now()...`) between claim and CAS; or stub `renew` false | in-transaction fence raises SQLSTATE 55000 (`202607200012:69-87`); no snapshot write; delivery → 500 → Stripe retries; a stale owner can neither complete nor fail the reclaimed event |
| F4 | **Ambiguous meter response** | kill the worker between `begin_send` and response (or inject a timeout via FakeSession at the gateway) | row stays `sending` with `outbound_started_at` set; NEVER released to pending (`release` requires marker NULL, `202607170004:399-407`); one reconcile attempt → `UNKNOWN` (exclusive_writer=false); after 23h the DB sweep → `review`; replay inside the window reuses the byte-identical identifier so Stripe dedups |
| F5 | **Weekly cap hit** | seed two rows whose fees sum past `BREVITAS_BILLING_WEEKLY_CAP_USD` (set cap to $1 locally) | second claim → `status='capped'`, `last_error='weekly safety cap reached'`; excluded from `estimated_fee_usd`; a RECLAIMED in-flight row is exempt from the cap |
| F6 | **Negative / zero savings** | per-row: usage row with `verified_savings_usd=-0.0075` (already covered `test_cloud_usage_api.py:587`); period: settlement row verified=5, warm=10; guard: fee>0 with zero actual spend | per-row fee = 0 (`api/server.py:4007` clamp); `net_savings_usd` generated column = 0, not −5; fee CHECK forbids any positive fee; `assert_billing_period_halting_conditions` raises 55000 `halting_condition=zero_spend`. A losing week bills $0, no deficit carry-forward |
| F7 | **Out-of-order events** | deliver `customer.subscription.updated` events in reverse creation order (option B, two real states) | canonical state converges to the LIVE Stripe resource, not the last event: handler re-retrieves and CASes on `reconcile_revision`; a lost CAS returns NULL → re-read → retry; event id/created are diagnostic only (`stripe-event-diagnostic.mjs:22-24`) |
| F8 | **Owner transfer race** | `bash scripts/ci/run-billing-owner-transfer-race-test.sh` (two real PG sessions) | transfer serialized under `pg_advisory_lock(170017)` + `FOR UPDATE OF organization, member`; loser gets lock-timeout cancel; customer identity never swapped (`202607200017:49-71` — differing customer raises unique_violation) |
| F9 | **Stripe 429 vs 5xx on send** | inject via FakeSession (do NOT try to force real 429s) | 429 → `StripeUnavailable` → release-and-retry path; 5xx/408/409 → `StripeAmbiguous` → F4 behavior. **BOTH WARTS FIXED (integrator note).** WR-2: the post-begin_send 429 path now calls `public.release_billing_ledger_unsent(bigint, text)` (202607280011), which returns the row to `pending`, refunds the attempt, and clears `outbound_started_at` — fenced on `id + status='sending' + lease_owner`, so a worker that lost its lease to a reclaim cannot write. Sustained rate limiting no longer walks `attempts` toward `review` (verified over 8 cycles). Soundness rests on HTTP 429 being the ONLY status that raises `StripeUnavailable` (`api/billing_recovery.py:372-378`, pinned by a 13-case table) — widening that exception is a money-path change. U12: `billing.stripe_unavailable` is now emitted |
| F10 | **Catalog mismatch at checkout** | point `STRIPE_PRICE_ID` at a monthly sibling price; restart `next dev` (validator memoizes per process) | `POST /api/billing/checkout` → 500 BEFORE any Customer or Session is created (zero new `cus_`, zero open sessions — assert via list calls) |
| F11 | **Concurrent webhook delivery** | fire two identical signed deliveries in parallel | one processes; the other gets 503 `Retry-After: 5` (busy), never a false 2xx ack |
| F12 | **Checkout crash between create and persist** | abort after `sessions.create`, before the persist CAS (stub or kill) | reservation lease expires (≤300 s); next POST re-acquires the SAME generation, `sessions.list` recovers the open session, returns its ORIGINAL url; idempotency key identical; no second session |
| F13 | **Settlement void-and-rebill (PSL-LATCH)** | after G4's latch migration: promote a settlement row to `reported`, attempt `set status='void', outbound_started_at=null` then insert a second full-rate revision | UPDATE raises P0001; guard still counts the committed 22500000 µUSD → `cumulative_ceiling` blocks the second charge. (Pre-fix this test FAILS — that is the reproduction) |
| F14 | **Duplicate meter send across worker replicas** | run two local worker processes against one seeded row | `FOR UPDATE SKIP LOCKED` + unique owner identity (hostname+pid+uuid, `api/billing_recovery.py:854-862`) → exactly one claims; the other's fenced mutations return false and are treated as lost lease; meter summary counts once |
| F15 | **Webhook while misconfigured** | unset `BREVITAS_BILLING_WEEKLY_CAP_USD` (or weaken the recovery secret) and deliver a signed event | 503 Retry-After 30 with the body never read (no inbox row). Note the recovery property: the identical predicate also 503s checkout, so no charge can occur while events drop; the next weekly `customer.subscription.updated` re-syncs state after the config is fixed |

---

## 5. Go/no-go checklist for `BREVITAS_BILLING_ENABLED=true`

All items must be YES, in writing, before the flip. Items marked ⛔ cannot be tested/completed without production access or calendar time.

| # | gate | how verified |
|---|---|---|
| G1 | Migration harness green on BOTH paths (I1) and merged; U5 manifest guard failing-closed in CI | **MET as shipped**: `bash scripts/ci/run-migration-tests.sh` exits 0 on a loopback DB with zero `ERROR` lines, 202607280005-202607280013 each applied twice on the upgrade path, the per-row fee trigger absent at the end of both paths, and the U5 guard green + mutation-verified |
| G2 | Tiers 1–3 green; failure matrix F1–F15 all showing the correct outcome | this document's tables, with actual output attached |
| G3 | The red assertion `tests/stripe_billing_config.test.mjs:69` resolved by **human decision** (accept the `max(0.0, verified)` clamp and update the regex, or change the code) — per `BILLING_CORRECTNESS_PLAN.md:204-213` this is not an agent edit | `npm test` fully green |
| G4 | PSL-LATCH fix migration (latch `outbound_started_at`/`reported_at`/`settled_at`; forbid marker-clearing status exits) landed + F13 green | I8/F13 |
| G5 | AR-1 resolved: `/api/billing/status` no longer sums a writer-less `billing_ledger` — either repointed at the settlement ledger or returns `settlement_pending` nulls; a test pins the table name | I12 rewritten against the money-of-record |
| G6 | **A settlement/fee writer exists** (Phase 3/4) or leadership explicitly accepts launch with manual settlement only. Today NOTHING writes either ledger — flipping the gate alone bills $0 forever | signed decision + the writer's own tests |
| G7 | Exactly ONE authoritative meter-event writer at flip time. Resolve the standing conflict: `deploy/railway-worker.json:8` runs numReplicas 2 authoritative vs `GO_LIVE_RUNBOOK.md:131` "exactly ONE" | Railway dashboard check + F14 evidence for whichever topology is chosen |
| G8 | `BREVITAS_STRIPE_METER_EXCLUSIVE_WRITER` decision recorded: stays `false` (ambiguities resolve via review) or flips `true` with proven sole writership of the event name | M10 evidence |
| G9 | Stripe API version pinned to ONE constant across Node client, Python worker header, and canary; S6 shape check passes on the live account | grep + M8 |
| G10 | Env complete per §S7 on Vercel Preview+Prod AND the worker: notably `BREVITAS_BILLING_WEEKLY_CAP_USD` (currently MISSING both places), strong `BILLING_RECOVERY_SECRET`, https `BREVITAS_PUBLIC_URL` (CF-1 closed: code now catches a MISSING or non-https value once deployed and fails closed; it still cannot catch a wrong-but-https value), dead `API_URL` deleted | `docs/ENV_ROLLOUT_CHECKLIST.md` walked; Vercel Sensitive vars are unreadable — verify by behavior, not by reading |
| G11 | ⛔ Leaked `sk_live` key ROLLED in Stripe (not just removed from Vercel) — `GO_LIVE_RUNBOOK.md:30-32` | Stripe dashboard |
| G12 | ⛔ Production (wyfz) trigger state verified read-only before/after applying `202607280006` there: `select tgname from pg_trigger where tgrelid='public.usage_log'::regclass and not tgisinternal;` via `supabase db query --linked -f` (one file, never `db push`, never replay the chain — wyfz has no migration ledger) | recorded catalog query output |
| G13 | ⛔ At least one real Anthropic provider invoice reconciled against `sum(actual_cost_usd)` for a closed 7-day window, and `organization_billing_arrangement` rows attested (`marginal_per_call`) for every org that will be billed. Calendar-time gate; no schema change or test-mode object can discharge it (`BILLING_CORRECTNESS_PLAN.md` Q2; `202607280009:13-26`) | attestation rows with `attested_evidence` naming the invoice |
| G14 | LIVE-mode Customer Portal configuration saved; webhook endpoint registered in live mode with exactly the six event types and the live `whsec_` set on Vercel | Stripe dashboard + one live `customer.subscription.updated` test event answered 200 |
| G15 | Observability: the three dropped metrics (U12) **fixed** — reused `brevitas.queue.depth{queue="billing"}` and `brevitas.billing.recovery{outcome=…}`, so no dashboard/alert edit was needed; nothing alerts on sustained rate limiting yet (optional follow-up in `observability/prometheus/alerts.yml`); alert routing for `billing_entries_require_review` / `billing_catalog_contract_invalid` verified end to end | staged alert test |
| G16 | Staging soak: ST1–ST5 green with billing enabled on staging for ≥1 full billing week including one week-roll webhook cycle | staging evidence |

Flip procedure itself: `GO_LIVE_RUNBOOK.md:127-133` (cap first, then gate; deploy order compressor → API → worker with `/ready` waits).

---

## 6. What this plan explicitly does NOT cover

- **Real money.** No live-mode charge, no real card networks, no dispute/refund/chargeback flows, no Stripe Tax (keep `STRIPE_AUTOMATIC_TAX=false` per `docs/STRIPE_BILLING.md:288`).
- **Provider-side cost truth.** Whether `actual_cost_usd` (published list price from `MODEL_PRICES`) matches real Anthropic spend is G13, a manual invoice reconciliation — not reproducible in Stripe test mode at all. Both money inputs are estimates until then.
- **Production database state.** Everything against wyfz is read-only catalog queries (G12) and out of scope for automated tests. This plan never runs migrations, `db push`, or the harness against any remote project.
- **The missing settlement writer.** This plan tests every component that exists; it cannot test Phase 3/4 code that has not been written (`period_settlement_ledger` writer, the RPC that calls `assert_billing_period_settlement_allowed`). G6 gates on it; when it lands, I7–I9 and F13 are its acceptance tests.
- **Stripe's own availability/ordering guarantees** beyond what F2/F7/F11 exercise: we do not simulate multi-day Stripe outages, event-delivery loss beyond the retry horizon, or account-level API version migration by Stripe.
- **Load/performance.** No throughput testing of the worker loop, webhook concurrency beyond F11, or PostgREST row limits under production volume (the status-route truncation concern is noted in I12's design follow-up but not load-tested here).
- **Dashboard visual regression** beyond the MU click-path; dashboard code also has zero lint coverage (`eslint.config.mjs:16` ignores `dashboard/**`) — "lint clean" claims are vacuous there and fixing that is tracked as a hygiene item, not a billing test.
- **Analytics correctness.** PostHog billing events drop `organization_id` (known, low); not a billing-correctness gate.
- **GoTrue/auth edge cases** (e.g. repeat-signup password drop) — separate track.
