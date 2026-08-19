# Credit-Based Pricing + Smooth Onboarding — Implementation Plan

## Context

Brevitas currently bills a **percentage of verified savings** (25%). That model is
fragile (savings are a contested counterfactual, the local proxy can under-report,
and the pipeline invites attribution disputes) **and** it is half-built and currently
**not shippable** — per `docs/STRIPE_FIX_PLAN.md` the migration harness is red, the
settlement→Stripe send-path RPCs don't exist, the status endpoint reads a dead table,
and there's a double-charge vulnerability. So we are switching to **credit-based
pricing** and making **onboarding as smooth as possible**. Because the savings
pipeline was never live, this is a low-regret replacement, not a rip-out of working
revenue.

The moat stays **verified savings measurement** — we keep measuring and *displaying*
it as the trust/differentiation feature; we just stop *billing* on it.

## Locked product decisions

1. **Credit basis: per-request, BYO provider key.** Users keep their own provider key
   (the provider bills them directly, unchanged). Each hosted-gateway request draws
   down a flat number of Brevitas credits. Credits are **not** tied to token volume,
   so pricing never punishes the savings we deliver.
2. **Purchase: both** — prepaid credit packs (one-time Stripe Checkout) + auto-recharge
   for self-serve, and a recurring subscription that tops up credits for larger accounts.
3. **Free trial credits** granted to each new workspace on signup.
4. **Park savings-billing; keep verified-savings as a displayed feature.**

## Design spine (safety-critical)

- **Credits = integer micro-credits (µUSD, 1 credit = $0.000001).** Matches the existing
  µUSD Stripe meter (`scripts/setup-stripe-billing.mjs:5`) and bigint settlement SQL;
  integer math is race-safe and repriceable by config.
- **Atomic deduction** via one `security definer` Postgres RPC: `INSERT ... ON CONFLICT
  DO NOTHING` (idempotency) + a single `UPDATE balance = balance - price` (concurrency
  serializes on the row lock). **Reuses the per-request `request_id`** the proxy already
  mints (`brevitas/proxy.py` `proxy:<uuid|response-id>`), fenced by the existing
  `usage_log_request_authority_unique` index — so double-charging is structurally
  impossible.
- **Insufficient balance → soft overage (negative balance), not a receipt-time refusal.**
  BYO-key means the provider already charged the user before we can debit, so refusing
  post-hoc is meaningless. Optional **hard 402 admission gate** before the upstream call
  once balance < `-grace`.
- **Ship dark.** With `BREVITAS_CREDIT_PRICE_MICRO` unset, no debits occur; every new DB
  object degrades gracefully if a migration isn't applied (existing `PGRST202` /
  `_postgrest_object_missing` pattern, `api/store.py:274`). Nothing bills until flipped on.

---

## Workstream B — Credit engine (ship dark, behind flags)

**B6 — Park the 25% fee. ✅ DONE (shipped in this branch).**
- `api/server.py`: added `_savings_fee_enabled()` (strict `os.getenv("BREVITAS_SAVINGS_FEE_ENABLED","")=="true"`, default off) and gated the fee at the former `fee = ... * BREVITAS_FEE_RATE` line. `verified_savings_usd`/`measured_savings_usd` still flow to the dashboard; only the charge is withheld.
- 4 legacy-fee tests opt into the flag (`test_openrouter_reported_cost.py`, `test_warming_api.py`, `test_cloud_usage_api.py` ×2) to keep covering the fee math behind the flag; the many `fee == 0` assertions now cover the parked default.
- `.env.example` documents `BREVITAS_SAVINGS_FEE_ENABLED=false`.
- Settlement sweep / recovery loops stay **env-disabled, not deleted** (`api/billing_settlement_sweep.py:497`, `api/billing_recovery.py:1182`) — deletion churns CI-asserted privileges.

**B5 — Ledger schema** (new migration, e.g. `20260813xxxx_credit_ledger.sql`):
- Append-only `credit_ledger(entry_type ∈ {purchase,usage,grant,refund,adjustment}, amount_micro bigint signed, organization_id, customer_id, request_id, stripe_event_id, occurred_at, ...)`.
- Materialized `credit_balances(organization_id, balance_micro)` as the O(1) atomic-decrement target; ledger is source of truth.
- Three partial-unique indexes fence: usage-per-`request_id`, grant-per-`stripe_event_id`, trial-per-org — mirroring `usage_log_request_authority_unique`.

**B7 — Debit hook** (`api/server.py`, `_record_usage_report`): after the `usage_log`
insert (~line 5027, i.e. after the duplicate short-circuit), `authoritative`-gated, call
the atomic-debit RPC. Add a degrade-safe `_store` method for both Postgres and the SQLite
local path (`api/store.py:2902` insert pattern; degrade helpers `:247-274`, `:4485-4521`).

**B8 — Free trial credits**: grant inside the org-creation transaction
(`ensure_workspace_organization`, `api/store.py:3750`), fenced by the one-per-org index;
amount from `BREVITAS_TRIAL_CREDIT_MICRO` (0 ⇒ no grant).

**B9 — Stripe purchase flows**: extend `scripts/setup-stripe-billing.mjs` with one-time
credit-pack prices + a top-up subscription (credit amount in
`metadata.brevitas_credit_micro`), keeping the pinned API version. New credit-checkout
route modeled on `src/app/api/billing/checkout/route.ts`; webhook switch
(`src/app/api/billing/webhook/route.ts:471-555`) gains `checkout.session.completed`
(payment mode), `invoice.paid`, `charge.succeeded` cases granting credits via a
`grant_credits_from_stripe` RPC, fenced on `stripe_event_id` atop the existing
at-most-once lease. Auto-recharge as a follow-up.

**B10 — Balance / burn-down read model**: `credit_balance_summary` RPC modeled on
`billing_period_settlement_summary` (`supabase/migrations/202607280013`), returning
`{balance_micro, burn_rate, days_to_exhaustion, low_balance}`, fail-closed. Python
`GET /v1/organization/credits` + a Next route mirroring `status/route.ts` (force-dynamic,
deploy-order degrade).

**Config/env** (all dark until set): `BREVITAS_CREDIT_PRICE_MICRO`,
`BREVITAS_TRIAL_CREDIT_MICRO`, `BREVITAS_LOW_BALANCE_THRESHOLD_MICRO`,
`BREVITAS_CREDIT_GRACE_MICRO`. Add a **separate** `creditsAreConfigured()` /
`validateCreditCatalog()` in `src/lib/billing/config.ts` — do **not** touch
`validateStripeCatalog()` (asserted by `tests/stripe_billing_config.test.mjs`).

---

## Workstream A — Onboarding smoothness (lower risk, high leverage)

**A1 — Default-customer pin (kills the #1 first-call 400). SECURITY-SENSITIVE.**
The gate at `api/server.py:1921-1924` hard-400s org-service keys without
`X-Brevitas-Customer-ID`. A per-key `default_customer_external_id` pin is
designed-but-not-shipped (`docs/ONBOARD_HOSTED_CUSTOMER.md:130-135`; the doc's cited line
`1755-1757` has drifted — gate now at `1921-1924`).
- Migration adds nullable `service_accounts.default_customer_external_id` (CHECK mirrors
  `_CUSTOMER_EXTERNAL_ID` regex, `api/server.py:1370`); mirror in the SQLite DDL.
- `create_or_replace` the `company_admin_create_service_account` RPC (last at
  `supabase/migrations/202607280022:306-425`) with a new `p_default_customer_external_id`
  arg; re-issue the signature-specific grants.
- `POST /v1/company/service-accounts` (`api/company_admin.py:1434,1559`) accepts the
  optional default; both store paths (`:715` SQLite, `:1202` Supabase) persist it.
- Auth resolve: `service_account_key_context` SELECT (`api/company_admin.py:1299-1312`)
  returns the pin; `_auth_context_for_key` (`api/server.py:1534-1606`) resolves
  **`customer_id or default`** — an explicitly sent header always wins; only fall back to
  the key-bound pin when the header is absent.
- `brevitas connect` pins by default (single-tenant); `--multi-tenant` leaves it null.
- **Multi-tenant stays intact**: pin read only from the key-bound row (never client
  input); multi-tenant keys keep NULL → still hard-400. Add a test asserting a null-pin
  org key still 400s header-less.

**A2 — `brevitas.hosted(client)` wrapper** (`brevitas/__init__.py`, next to `wrap` at
`:71`): sets `base_url` + `X-Brevitas-Key` (+ `X-Brevitas-Customer-ID` only if resolved,
now optional thanks to A1), reading env fallbacks; `/v1`-once guard from
`brevitas/config.py:34`. Hoist the header-name constants (`brevitas/cli.py:497`) into
`brevitas/identity.py` so CLI and wrapper share one definition. Export in `__all__`.

**A3 — Rewrite `integration.md`** — it is actively misleading: wrong domain
(`api.brevitas.systems` vs `api.brevitassystems.com`), wrong header (`X-API-Key` vs
`X-Brevitas-Key`), and documents an unshipped `/v1/compress` + `PUT /v1/provider`
surface. Replace with the shipped hosted quickstart (base-URL swap + `hosted()`
one-liner), a credits section (BYO key, per-request burn, free trial, packs +
subscription), and an honest "local proxy = analytics, not billing" note.

**A4 — Dashboard**:
- Real key/customer-id baked into copy-ready snippets (`InstallCommand.jsx:54-78`,
  currently hardcoded `"acme"`); surface the minted key in-dashboard via `KeySetup.jsx`
  (create route + `api.js:87 createKey`), shown-once with a copy button.
- Credit balance card + "Buy credits" CTA in `Billing.jsx` (~line 200), reusing
  `dashboard/src/lib/api.js` (`billingJson`, checkout/portal at `:100-118`) and money
  helpers in `spend.js` (so an unavailable balance renders `—`, never a false `$0`).
- **Cache self-serve toggle — UI only** (correction: the `/v1/cache-policy` API already
  exists, `api/server.py:3315/3358`, with purge-on-disable). Add `fetchCachePolicy`/
  `setCachePolicy` to `api.js` + a settings toggle noting the global flag can override.
- `SetupBanner.jsx` reframed to "key minted/connected" + "first hosted request
  succeeded" + free-credit burn-down.

**A6 — Minor honesty fixes**: ship a minimal `GET /v1/billing/readiness` (referenced by
`brevitas billing-check`, `brevitas/cli.py:983`, but not shipped) or degrade the CLI;
surface 90-day key expiry as a dashboard warning.

---

## Recommended sequence

1. **B6 park the fee** ✅ (done) — 2. A1 default-customer pin → A2 `hosted()` → A3 docs
(first-call success) → 3. B5 ledger + B7 debit + B8 trial (dark) → 4. B9 Stripe purchase
+ B10 balance read model → 5. A4 dashboard credit UI → 6. auto-recharge + optional hard
admission gate.

## Before go-live (not needed to build — it ships dark)

- **Per-request credit price** (`BREVITAS_CREDIT_PRICE_MICRO`).
- **Trial grant amount** + **pack sizes / subscription tiers.**
- Confirm **soft-overage** vs. adding the hard pre-call 402.

## Risks & kill-criteria

- **Deploy risk is dominant**: red migration harness, code-first deploys — every new DB
  object must degrade on `PGRST202`; ship dark with price unset.
- **Atomicity/idempotency**: single-statement `UPDATE`, no app-level read-modify-write;
  reuse the proxy `request_id` + Stripe event-id fences.
- **Backward-compat**: `usage_log` untouched; savings columns keep flowing;
  `brevitas_fee_usd` stays 0.0 by default; analytics (non-authoritative) rows never debit.
- **Kill-criterion**: if per-request credits don't cover CAC, add a flat platform tier and
  keep verified-savings as the feature (see `PIVOT_STRATEGY.md`).

## Verification

- Unit: the fee-parking suites pass parked-by-default and flag-on
  (`pytest tests/test_cloud_usage_api.py tests/test_warming_api.py
  tests/test_openrouter_reported_cost.py` — 177 green in this branch).
- Credit engine: add tests for atomic debit under concurrent `request_id`s, idempotent
  re-debit (duplicate request), Stripe-event-fenced grants, one-per-org trial grant,
  and soft-overage. Run the local SQLite path and (when available) the Supabase RPCs.
- Onboarding: assert a single-tenant pinned key succeeds **without** the customer-id
  header, and a multi-tenant (null-pin) key still 400s without it.
- E2E: `?preview=billing` in the dashboard for the credit balance/Buy UI; a real
  gateway call against a dark-priced deploy debits nothing.
