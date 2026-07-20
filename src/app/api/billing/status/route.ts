import { NextRequest } from 'next/server';

import { billingConfig, billingIsConfigured } from '@/lib/billing/config';
import {
  authenticatedBillingUser,
  billingDatabase,
  getBillingAccount,
} from '@/lib/billing/supabase';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

export async function GET(request: NextRequest) {
  try {
    const user = await authenticatedBillingUser(request);
    if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });

    const account = await getBillingAccount(user.id);
    const periodStartMs = Date.parse(account?.current_period_start || '');
    const periodEndMs = Date.parse(account?.current_period_end || '');
    const periodTrackingValid = (
      Number.isFinite(periodStartMs) &&
      Number.isFinite(periodEndMs) &&
      periodEndMs - periodStartMs === 7 * 24 * 60 * 60 * 1000
    );

    let ledger: Array<{ fee_microusd: number | string | null; status: string }> = [];
    if (periodTrackingValid) {
      const { data, error } = await billingDatabase()
        .from('billing_ledger')
        .select('fee_microusd,status')
        .eq('user_id', user.id)
        .gte('occurred_at', new Date(periodStartMs).toISOString())
        .lt('occurred_at', new Date(periodEndMs).toISOString());
      if (error) throw error;
      ledger = data || [];
    }

    const feeMicrousd = ledger
      .filter((row) => !['capped', 'expired', 'dead'].includes(row.status))
      .reduce((sum, row) => sum + Number(row.fee_microusd || 0), 0);
    const reportedMicrousd = ledger
      .filter((row) => row.status === 'reported')
      .reduce((sum, row) => sum + Number(row.fee_microusd || 0), 0);
    const config = billingConfig();

    return Response.json({
      configured: billingIsConfigured(),
      billing_period: 'weekly',
      subscription_status: account?.subscription_status || 'not_started',
      current_period_start: account?.current_period_start || null,
      current_period_end: account?.current_period_end || null,
      period_tracking_valid: periodTrackingValid,
      last_invoice_status: account?.last_invoice_status || null,
      estimated_fee_usd: periodTrackingValid ? feeMicrousd / 1_000_000 : null,
      reported_fee_usd: periodTrackingValid ? reportedMicrousd / 1_000_000 : null,
      weekly_safety_cap_usd: config.weeklyCapUsd > 0 ? config.weeklyCapUsd : null,
      has_customer: Boolean(account?.stripe_customer_id),
      needs_review: ledger.filter((row) => row.status === 'review').length,
      capped_entries: ledger.filter((row) => row.status === 'capped').length,
    }, {
      headers: { 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff' },
    });
  } catch (error) {
    console.error('Billing status failed', error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Could not load billing status' }, { status: 500 });
  }
}
