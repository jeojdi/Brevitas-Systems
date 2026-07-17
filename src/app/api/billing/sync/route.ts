import { timingSafeEqual } from 'node:crypto';

import { billingConfig, billingIsConfigured, getStripe, validateStripeCatalog } from '@/lib/billing/config';
import { billingDatabase } from '@/lib/billing/supabase';

export const runtime = 'nodejs';
export const maxDuration = 60;

function cronAuthorized(request: Request): boolean {
  const expected = process.env.CRON_SECRET || '';
  const supplied = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  if (!expected || expected.length !== supplied.length) return false;
  return timingSafeEqual(Buffer.from(expected), Buffer.from(supplied));
}

export async function POST(request: Request) {
  if (!cronAuthorized(request)) return Response.json({ error: 'Unauthorized' }, { status: 401 });
  if (!billingIsConfigured()) {
    return Response.json({ error: 'Billing synchronization is not fully configured' }, { status: 503 });
  }

  const db = billingDatabase();
  const config = billingConfig();
  await validateStripeCatalog();
  const capMicrousd = Math.floor(config.monthlyCapUsd * 1_000_000);
  const oldestAllowed = new Date(Date.now() - 34 * 86_400_000).toISOString();
  const { data, error } = await db
    .from('billing_ledger')
    .select('id,user_id,occurred_at,fee_microusd')
    .eq('status', 'pending')
    .order('id', { ascending: true })
    .limit(200);
  if (error) throw error;

  const entries = data || [];
  const userIds = [...new Set(entries.map((entry) => entry.user_id))];
  const { data: accounts, error: accountError } = userIds.length
    ? await db.from('billing_accounts').select('user_id,stripe_customer_id').in('user_id', userIds)
    : { data: [], error: null };
  if (accountError) throw accountError;
  const customers = new Map((accounts || []).map((account) => [account.user_id, account.stripe_customer_id]));

  const result = { scanned: entries.length, reported: 0, capped: 0, expired: 0, review: 0, skipped: 0 };
  for (const entry of entries) {
    if (entry.occurred_at < oldestAllowed) {
      await db.from('billing_ledger').update({ status: 'expired', last_error: 'Stripe 35-day reporting window elapsed' }).eq('id', entry.id).eq('status', 'pending');
      result.expired += 1;
      continue;
    }

    const customerId = customers.get(entry.user_id);
    if (!customerId) {
      result.skipped += 1;
      continue;
    }

    const { data: claim, error: claimError } = await db.rpc('claim_billing_ledger_entry', {
      p_entry_id: entry.id,
      p_cap_microusd: capMicrousd,
    });
    if (claimError) throw claimError;
    if (claim === 'capped') {
      result.capped += 1;
      continue;
    }
    if (claim !== 'sending') {
      result.skipped += 1;
      continue;
    }

    try {
      await getStripe().billing.meterEvents.create({
        event_name: config.meterEventName,
        identifier: `brevitas-fee-${entry.id}`,
        timestamp: Math.floor(new Date(entry.occurred_at).getTime() / 1000),
        payload: {
          stripe_customer_id: customerId,
          value: String(entry.fee_microusd),
        },
      }, { idempotencyKey: `brevitas-meter-${entry.id}` });
      await db.from('billing_ledger').update({
        status: 'reported', reported_at: new Date().toISOString(), last_error: '',
      }).eq('id', entry.id).eq('status', 'sending');
      result.reported += 1;
    } catch (syncError) {
      // A timeout can be ambiguous: Stripe might have accepted the event. Never
      // auto-retry it; leave it for operator reconciliation to prevent duplicates.
      await db.from('billing_ledger').update({
        status: 'review',
        last_error: syncError instanceof Error ? syncError.message.slice(0, 500) : 'unknown Stripe error',
      }).eq('id', entry.id).eq('status', 'sending');
      result.review += 1;
    }
  }

  return Response.json(result, { headers: { 'Cache-Control': 'no-store' } });
}

export const GET = POST;
