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
    const monthStart = new Date();
    monthStart.setUTCDate(1);
    monthStart.setUTCHours(0, 0, 0, 0);

    const { data, error } = await billingDatabase()
      .from('billing_ledger')
      .select('fee_microusd,status')
      .eq('user_id', user.id)
      .gte('occurred_at', monthStart.toISOString());
    if (error) throw error;

    const ledger = data || [];
    const feeMicrousd = ledger
      .filter((row) => !['capped', 'expired'].includes(row.status))
      .reduce((sum, row) => sum + Number(row.fee_microusd || 0), 0);
    const reportedMicrousd = ledger
      .filter((row) => row.status === 'reported')
      .reduce((sum, row) => sum + Number(row.fee_microusd || 0), 0);
    const config = billingConfig();

    return Response.json({
      configured: billingIsConfigured(),
      subscription_status: account?.subscription_status || 'not_started',
      current_period_end: account?.current_period_end || null,
      last_invoice_status: account?.last_invoice_status || null,
      estimated_fee_usd: feeMicrousd / 1_000_000,
      reported_fee_usd: reportedMicrousd / 1_000_000,
      monthly_safety_cap_usd: config.monthlyCapUsd > 0 ? config.monthlyCapUsd : null,
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
