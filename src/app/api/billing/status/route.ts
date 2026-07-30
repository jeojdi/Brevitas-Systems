import { NextRequest } from 'next/server';

import { billingConfig, billingIsConfigured } from '@/lib/billing/config';
import {
  authorizeActiveBillingCompany,
  authenticatedBillingUser,
  billingDatabase,
  getBillingAccount,
} from '@/lib/billing/supabase';

// Route Handlers are not cached by default in next@16; this pins dynamic
// rendering explicitly because the response is per-user money state.
// (node_modules/next/dist/docs/01-app/02-guides/caching-without-cache-components.md)
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/**
 * FIX-5 stage two (docs/STRIPE_FIX_PLAN.md, go/no-go item G5): this endpoint now
 * reads the money of record.
 *
 * Stage one returned nulls behind `settlement_pending: true` because
 * `billing_ledger` has had no writer since migration 202607280006 dropped
 * `queue_brevitas_fee_after_usage`, and its replacement
 * `period_settlement_ledger` (202607280007) was deliberately writer-less.
 * Migration 202607280013 supplies the writer, so the flag clears here in the
 * same change that repoints the read — but only once that migration is actually
 * present: see DEPLOY-ORDER DEGRADE below.
 *
 * WHY AN RPC AND NOT A POSTGREST SELECT ON THE SETTLEMENT LEDGER. FIX-5's text
 * says to repoint the query at that table, and that is not implementable as a
 * PostgREST select: 202607280007 revokes EVERY privilege on it from
 * service_role (202607280007_period_settlement_ledger.sql:232-233), its own
 * self-check asserts it (:467-474), and
 * scripts/ci/migration-period-settlement-assertions.sql re-asserts all seven
 * privileges against all three PostgREST roles. A select would be
 * permission-denied and a `grant select` would turn two CI files red and
 * dismantle the Phase 4 privilege model. Every read goes through
 * `public.billing_period_settlement_summary`, a SECURITY DEFINER RPC.
 *
 * WHY `estimated_fee_usd` IS NOT A LEDGER SUM. FIX-5 also says to exclude
 * `draft`/`void` from the estimate. Applied literally to the CURRENT period that
 * returns 0 for every customer forever: settlement only happens after a period
 * CLOSES, so the live period has no non-draft row — the identical confident-wrong
 * `$0.000000` FIX-5 exists to prevent. So the estimate is an evidence
 * PROJECTION (`least(period_settlement_fee_microusd(net_after_warm,
 * net_verified), fee_ceiling_microusd)` over the same evidence the settlement
 * guard uses), which converges exactly to the settled fee once the week closes,
 * and FIX-5's rule lives on the new `settled_fee_usd` field where it belongs —
 * the live row's fee when its status is neither `draft` nor `void` (the `void`
 * status the pre-FIX-5 filter never knew about, 202607280007:157-159).
 *
 * FAIL-CLOSED CONTRACT, unchanged in spirit from stage one: money is `null`,
 * never `0`, whenever the period boundaries are not exactly one Stripe week or
 * the RPC reports `ok: false` (`no_billing_account`, `period_anchor_mismatch`,
 * `guard_unavailable`). An unrecognized RPC transport error is a 500, never a
 * zero total.
 *
 * DEPLOY-ORDER DEGRADE. Vercel ships this route on push, while wyfz has no
 * migration ledger and migrations are applied by hand one file at a time — so
 * the default ordering is code-first, and until 202607280013 is applied
 * PostgREST answers "function not found" (PGRST202/42883; 42501 if the grant is
 * missing). That is not a transport fault: it is the stage-one state — the
 * money of record has no readable writer yet — so those errors degrade to the
 * stage-one contract (every money field `null` behind
 * `settlement_pending: true`) instead of failing the whole page with a 500.
 * Mirrors api/billing_recovery.py `_release_unsent`, which likewise refuses to
 * crash the cycle when the store has not yet learned an RPC mid-rollout.
 */
const MICRO_USD_PER_USD = 1_000_000;

/**
 * PostgREST/Postgres codes that mean the settlement summary itself is not
 * reachable in this database yet (or its grant is missing) — schema drift, not
 * a transient fault. Only these degrade; anything else still throws => 500.
 */
const SUMMARY_UNAVAILABLE_CODES = new Set([
  'PGRST202', // function absent from the PostgREST schema cache (migration 202607280013 not applied)
  '42883', // undefined_function
  '42P01', // undefined_table (the summary's backing objects are missing)
  '42501', // insufficient_privilege (execute grant not applied)
]);

function summaryUnavailableError(error: { code?: unknown; message?: unknown }): boolean {
  if (typeof error?.code === 'string' && SUMMARY_UNAVAILABLE_CODES.has(error.code)) return true;
  // Older PostgREST schema-cache misses surface without a stable code.
  return typeof error?.message === 'string' &&
    /could not find the function|does not exist|permission denied/i.test(error.message);
}

type SettlementSummary = Record<string, unknown>;

/** Micro-dollars to dollars. Anything unrecognised is null, never a number. */
function toUsd(microUsd: unknown): number | null {
  return typeof microUsd === 'number' && Number.isFinite(microUsd)
    ? microUsd / MICRO_USD_PER_USD
    : null;
}

function toCount(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 0;
}

function toText(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

export async function GET(request: NextRequest) {
  try {
    const user = await authenticatedBillingUser(request);
    if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
    const authorization = await authorizeActiveBillingCompany(user.id);
    if (!authorization.ok || !authorization.organizationId || !authorization.billingOwnerId) {
      return Response.json({ error: 'Billing permission is required for the active company' }, { status: 403 });
    }

    const account = await getBillingAccount(authorization.organizationId);
    const periodStartMs = Date.parse(account?.current_period_start || '');
    const periodEndMs = Date.parse(account?.current_period_end || '');
    const periodTrackingValid = (
      Number.isFinite(periodStartMs) &&
      Number.isFinite(periodEndMs) &&
      periodEndMs - periodStartMs === 7 * 24 * 60 * 60 * 1000
    );

    // The settlement summary re-derives the same exact-604800000 ms anchor test
    // server-side and refuses with `period_anchor_mismatch` if it disagrees, so
    // the two gates cannot drift apart silently.
    let summary: SettlementSummary = {};
    let summaryOk = false;
    let summaryUnavailable = false;
    if (periodTrackingValid) {
      const { data, error } = await billingDatabase().rpc('billing_period_settlement_summary', {
        p_organization_id: authorization.organizationId,
        p_period_start: new Date(periodStartMs).toISOString(),
      });
      if (error) {
        // A missing function or missing grant means migration 202607280013 has
        // not reached this database yet (code deploys ahead of hand-applied
        // migrations). Degrade to the stage-one contract — money is null, never
        // 0, behind settlement_pending — instead of 500ing the Savings page.
        // Any OTHER error is still fatal: falling through would present "no
        // settlement" as if it were a measured state.
        if (!summaryUnavailableError(error)) throw error;
        console.error(
          'Billing settlement summary unavailable; serving stage-one nulls',
          typeof error.code === 'string' ? error.code : 'no-code',
        );
        summaryUnavailable = true;
      }
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        summary = data as SettlementSummary;
      }
      summaryOk = summary.ok === true;
    }

    const config = billingConfig();

    return Response.json({
      configured: billingIsConfigured(),
      billing_period: 'weekly',
      subscription_status: account?.subscription_status || 'not_started',
      current_period_start: account?.current_period_start || null,
      current_period_end: account?.current_period_end || null,
      period_tracking_valid: periodTrackingValid,
      last_invoice_status: account?.last_invoice_status || null,
      // Evidence projection for the week in progress, NOT a ledger sum: see the
      // note above. Null (never 0) when the anchor is invalid or the RPC fails
      // closed.
      estimated_fee_usd: summaryOk ? toUsd(summary.estimated_fee_microusd) : null,
      // The live settlement row's fee once it has left `draft` and is not
      // `void`. This is where FIX-5's exclude-draft-and-void rule belongs.
      settled_fee_usd: summaryOk ? toUsd(summary.settled_fee_microusd) : null,
      // `reported` only. Not `sending`, not merely outbound-marked: `reported`
      // is the only status meaning Stripe accepted the meter event.
      reported_fee_usd: summaryOk ? toUsd(summary.reported_fee_microusd) : null,
      // 202607280008's committed sum (sending/reported OR outbound-marked, which
      // survives a retraction thanks to 202607280010's latches), so the UI can
      // never display less than what Stripe may already hold.
      committed_fee_usd: summaryOk ? toUsd(summary.committed_fee_microusd) : null,
      // True exactly when the summary RPC is not deployed/readable here yet:
      // the stage-one "money of record has no writer" state, which the billing
      // card renders as pending settlement rather than a load error.
      settlement_pending: summaryUnavailable,
      // 'accruing' is the normal state of the week in progress.
      settlement_status: summaryOk ? toText(summary.settlement_status) : null,
      settlement_revision: summaryOk && typeof summary.revision === 'number' ? summary.revision : null,
      // Absence of an attestation is itself a state, and it is the unbillable
      // one (202607280009). A period that is not billable must not be presented
      // as a fee that will be charged.
      billing_arrangement: summaryOk ? toText(summary.billing_arrangement) : null,
      billable: summaryOk && summary.billable === true,
      weekly_safety_cap_usd: config.weeklyCapUsd > 0 ? config.weeklyCapUsd : null,
      has_customer: Boolean(account?.stripe_customer_id),
      // NOT QUERIED IS NOT ZERO. These two are operational-health counts, not
      // money, but they were the last fields still emitted as an authoritative
      // `0` when the summary never ran (invalid anchor) or failed closed — i.e.
      // they claimed "no entry is stuck" in exactly the states where billing is
      // already degraded and an entry stuck in review is most likely. Null means
      // unknown, matching every money field above.
      needs_review: summaryOk ? toCount(summary.needs_review_count) : null,
      // Now covers capped | expired | dead on the settlement ledger.
      capped_entries: summaryOk ? toCount(summary.attention_count) : null,
      // Honest, non-money diagnostics (eligible_rows, warm_spend_days,
      // zero_spend_share, ...) so a $0 week is explainable rather than mysterious.
      evidence: summaryOk && summary.evidence && typeof summary.evidence === 'object'
        ? summary.evidence
        : null,
    }, {
      headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
    });
  } catch (error) {
    console.error('Billing status failed', error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Could not load billing status' }, { status: 500 });
  }
}
