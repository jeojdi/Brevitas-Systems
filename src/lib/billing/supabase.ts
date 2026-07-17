import 'server-only';

import { createClient, type User } from '@supabase/supabase-js';

export interface BillingAccount {
  user_id: string;
  stripe_customer_id: string | null;
  stripe_subscription_id: string | null;
  subscription_status: string;
  checkout_session_id: string | null;
  billing_started_at: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
  last_invoice_id: string | null;
  last_invoice_status: string | null;
  stripe_subscription_event_created: number;
  stripe_invoice_event_created: number;
}

export interface PendingLedgerEntry {
  id: number;
  user_id: string;
  occurred_at: string;
  fee_microusd: number;
  billing_accounts: {
    stripe_customer_id: string | null;
    subscription_status: string;
  } | null;
}

function supabaseSettings() {
  const url = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL || '';
  const serviceKey = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
  const authKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || serviceKey;
  if (!url || !serviceKey || !authKey) {
    throw new Error('Supabase billing configuration is missing');
  }
  return { url, serviceKey, authKey };
}

export function billingDatabase() {
  const { url, serviceKey } = supabaseSettings();
  return createClient(url, serviceKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
}

export async function authenticatedBillingUser(request: Request): Promise<User | null> {
  const auth = request.headers.get('authorization') || '';
  if (!auth.toLowerCase().startsWith('bearer ')) return null;
  const token = auth.slice(7).trim();
  if (!token) return null;

  const { url, authKey } = supabaseSettings();
  const client = createClient(url, authKey, {
    auth: { autoRefreshToken: false, persistSession: false },
  });
  const { data, error } = await client.auth.getUser(token);
  return error ? null : data.user;
}

export async function getBillingAccount(userId: string): Promise<BillingAccount | null> {
  const { data, error } = await billingDatabase()
    .from('billing_accounts')
    .select('*')
    .eq('user_id', userId)
    .maybeSingle();
  if (error) throw error;
  return data as BillingAccount | null;
}

export async function saveBillingAccount(
  userId: string,
  values: Partial<Omit<BillingAccount, 'user_id'>>,
): Promise<BillingAccount> {
  const { data, error } = await billingDatabase()
    .from('billing_accounts')
    .upsert({ user_id: userId, ...values, updated_at: new Date().toISOString() }, { onConflict: 'user_id' })
    .select('*')
    .single();
  if (error) throw error;
  return data as BillingAccount;
}

export async function saveSubscriptionState(
  userId: string,
  values: Partial<Omit<BillingAccount, 'user_id'>>,
  eventCreated: number,
): Promise<void> {
  const { error } = await billingDatabase()
    .from('billing_accounts')
    .update({
      ...values,
      stripe_subscription_event_created: eventCreated,
      updated_at: new Date().toISOString(),
    })
    .eq('user_id', userId)
    .lte('stripe_subscription_event_created', eventCreated);
  if (error) throw error;
}

export async function saveInvoiceState(
  userId: string,
  values: Partial<Omit<BillingAccount, 'user_id'>>,
  eventCreated: number,
): Promise<void> {
  const { error } = await billingDatabase()
    .from('billing_accounts')
    .update({
      ...values,
      stripe_invoice_event_created: eventCreated,
      updated_at: new Date().toISOString(),
    })
    .eq('user_id', userId)
    .lte('stripe_invoice_event_created', eventCreated);
  if (error) throw error;
}
