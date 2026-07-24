import 'server-only';

import { createClient, type User } from '@supabase/supabase-js';
import { parseBillingControlAdmission } from '@/lib/billing/control-admission.mjs';
import { parseBillingRecoveryAdmission } from '@/lib/billing/recovery-admission.mjs';

export interface BillingAccount {
  organization_id: string;
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
  stripe_subscription_event_id: string;
  stripe_subscription_event_type: string;
  stripe_subscription_reconcile_revision: number;
  stripe_invoice_event_created: number;
  stripe_invoice_event_id: string;
  stripe_invoice_event_type: string;
  stripe_invoice_reconcile_revision: number;
}

export interface BillingCompanyAuthorization {
  ok: boolean;
  code: string;
  organizationId: string | null;
  billingOwnerId: string | null;
  role: string | null;
}

export interface ManualBillingResolution {
  ok: boolean;
  code: string;
  auditId: number | null;
  priorStatus: string | null;
  resolution: string | null;
}

export type BillingRecoveryAdmission =
  | { status: 'accepted' }
  | { status: 'rate_limited'; retryAfterSeconds: number };

export type BillingControlOperation = 'checkout' | 'portal';

export type BillingControlAdmission =
  | { status: 'accepted' }
  | { status: 'rate_limited'; retryAfterSeconds: number };

export type BillingCheckoutReservation =
  | {
    status: 'acquired';
    mode: 'create_or_recover' | 'recover_only' | 'inspect_persisted';
    generation: number;
    checkoutSessionId: string | null;
  }
  | { status: 'busy'; retryAfterSeconds: number }
  | { status: 'occupied' | 'manual_review' | 'identity_mismatch' };

export type BillingCheckoutMutation =
  | { status: 'persisted' | 'advanced'; generation: number }
  | { status: 'occupied' | 'stale' | 'session_conflict' | 'manual_review' };

export class BillingControlAdmissionError extends Error {
  constructor() {
    super('Shared billing control admission is unavailable');
    this.name = 'BillingControlAdmissionError';
  }
}

export class BillingRecoveryAdmissionError extends Error {
  constructor() {
    super('Shared billing recovery admission is unavailable');
    this.name = 'BillingRecoveryAdmissionError';
  }
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

export async function authorizeActiveBillingCompany(
  actorUserId: string,
): Promise<BillingCompanyAuthorization> {
  const { data, error } = await billingDatabase().rpc('company_billing_authorize_actor', {
    p_actor_user_id: actorUserId,
  });
  if (error) throw error;
  const result = data && typeof data === 'object' && !Array.isArray(data)
    ? data as Record<string, unknown>
    : {};
  const ok = result.ok === true;
  return {
    ok,
    code: typeof result.code === 'string' ? result.code : (ok ? 'authorized' : 'forbidden'),
    organizationId: typeof result.organization_id === 'string' ? result.organization_id : null,
    billingOwnerId: typeof result.billing_owner_id === 'string' ? result.billing_owner_id : null,
    role: typeof result.role === 'string' ? result.role : null,
  };
}

export async function manuallyResolveBillingLedgerEntry(values: {
  actorUserId: string;
  expectedOrganizationId: string;
  entryId: number;
  resolution: string;
  note: string;
  requestId: string;
}): Promise<ManualBillingResolution> {
  const { data, error } = await billingDatabase().rpc(
    'manually_resolve_billing_ledger_entry',
    {
      p_actor_user_id: values.actorUserId,
      p_expected_organization_id: values.expectedOrganizationId,
      p_entry_id: values.entryId,
      p_resolution: values.resolution,
      p_note: values.note,
      p_request_id: values.requestId,
    },
  );
  if (error) throw error;
  const result = data && typeof data === 'object' && !Array.isArray(data)
    ? data as Record<string, unknown>
    : {};
  return {
    ok: result.ok === true,
    code: typeof result.code === 'string' ? result.code : 'ineligible',
    auditId: typeof result.audit_id === 'number' ? result.audit_id : null,
    priorStatus: typeof result.prior_status === 'string' ? result.prior_status : null,
    resolution: typeof result.resolution === 'string' ? result.resolution : null,
  };
}

export async function getBillingAccount(organizationId: string): Promise<BillingAccount | null> {
  const { data, error } = await billingDatabase()
    .from('billing_accounts')
    .select('*')
    .eq('organization_id', organizationId)
    .maybeSingle();
  if (error) throw error;
  return data as BillingAccount | null;
}

export async function consumeBillingRecoveryAttempt(
  actorUserId: string,
  organizationId: string,
): Promise<BillingRecoveryAdmission> {
  try {
    const { data, error } = await billingDatabase().rpc(
      'consume_billing_recovery_attempt',
      {
        p_actor_user_id: actorUserId,
        p_organization_id: organizationId,
      },
    );
    if (error) throw new BillingRecoveryAdmissionError();
    return parseBillingRecoveryAdmission(data);
  } catch {
    throw new BillingRecoveryAdmissionError();
  }
}

export async function consumeBillingControlAttempt(
  actorUserId: string,
  organizationId: string,
  operation: BillingControlOperation,
): Promise<BillingControlAdmission> {
  try {
    const { data, error } = await billingDatabase().rpc(
      'consume_billing_control_attempt',
      {
        p_actor_user_id: actorUserId,
        p_organization_id: organizationId,
        p_operation: operation,
      },
    );
    if (error) throw new BillingControlAdmissionError();
    return parseBillingControlAdmission(data);
  } catch {
    throw new BillingControlAdmissionError();
  }
}

export async function saveBillingCustomerIdentity(
  organizationId: string,
  stripeCustomerId: string,
): Promise<BillingAccount> {
  const { data, error } = await billingDatabase().rpc(
    'save_billing_customer_identity',
    {
      p_organization_id: organizationId,
      p_stripe_customer_id: stripeCustomerId,
    },
  );
  if (error) throw error;
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid billing customer identity result');
  }
  return data as BillingAccount;
}

function checkoutRpcRecord(data: unknown): Record<string, unknown> {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('Invalid billing Checkout reservation result');
  }
  return data as Record<string, unknown>;
}

function checkoutGeneration(result: Record<string, unknown>): number {
  const generation = result.generation;
  if (!Number.isSafeInteger(generation) || (generation as number) <= 0) {
    throw new Error('Invalid billing Checkout generation');
  }
  return generation as number;
}

export async function reserveBillingCheckoutGeneration(values: {
  organizationId: string,
  stripeCustomerId: string,
  reservationToken: string,
  leaseSeconds: number,
}): Promise<BillingCheckoutReservation> {
  const { data, error } = await billingDatabase().rpc(
    'reserve_billing_checkout_generation',
    {
      p_organization_id: values.organizationId,
      p_stripe_customer_id: values.stripeCustomerId,
      p_reservation_token: values.reservationToken,
      p_lease_seconds: values.leaseSeconds,
    },
  );
  if (error) throw error;
  const result = checkoutRpcRecord(data);
  if (result.ok === true && result.code === 'acquired') {
    const mode = result.mode;
    if (mode !== 'create_or_recover'
        && mode !== 'recover_only'
        && mode !== 'inspect_persisted') {
      throw new Error('Invalid billing Checkout reservation mode');
    }
    const checkoutSessionId = result.checkout_session_id;
    if (checkoutSessionId !== null && typeof checkoutSessionId !== 'string') {
      throw new Error('Invalid billing Checkout session identity');
    }
    if (result.mode === 'inspect_persisted' && !checkoutSessionId) {
      throw new Error('Persisted billing Checkout reservation has no session');
    }
    return {
      status: 'acquired',
      mode,
      generation: checkoutGeneration(result),
      checkoutSessionId,
    };
  }
  if (result.ok === false && result.code === 'busy'
      && Number.isInteger(result.retry_after_seconds)
      && (result.retry_after_seconds as number) >= 1
      && (result.retry_after_seconds as number) <= 300) {
    return {
      status: 'busy',
      retryAfterSeconds: result.retry_after_seconds as number,
    };
  }
  if (result.ok === false
      && ['occupied', 'manual_review', 'identity_mismatch'].includes(result.code as string)) {
    return { status: result.code as 'occupied' | 'manual_review' | 'identity_mismatch' };
  }
  throw new Error('Invalid billing Checkout reservation result');
}

export async function persistBillingCheckoutSession(values: {
  organizationId: string,
  generation: number,
  reservationToken: string,
  checkoutSessionId: string,
}): Promise<BillingCheckoutMutation> {
  const { data, error } = await billingDatabase().rpc(
    'persist_billing_checkout_session',
    {
      p_organization_id: values.organizationId,
      p_generation: values.generation,
      p_reservation_token: values.reservationToken,
      p_checkout_session_id: values.checkoutSessionId,
    },
  );
  if (error) throw error;
  const result = checkoutRpcRecord(data);
  if (result.ok === true && result.code === 'persisted') {
    return { status: 'persisted', generation: checkoutGeneration(result) };
  }
  if (result.ok === false
      && ['occupied', 'stale', 'session_conflict', 'manual_review'].includes(
        result.code as string,
      )) {
    return {
      status: result.code as 'occupied' | 'stale' | 'session_conflict' | 'manual_review',
    };
  }
  throw new Error('Invalid billing Checkout persistence result');
}

export async function advanceBillingCheckoutGeneration(values: {
  organizationId: string,
  generation: number,
  reservationToken: string,
  expectedCheckoutSessionId: string,
  leaseSeconds: number,
}): Promise<BillingCheckoutMutation> {
  const { data, error } = await billingDatabase().rpc(
    'advance_billing_checkout_generation',
    {
      p_organization_id: values.organizationId,
      p_generation: values.generation,
      p_reservation_token: values.reservationToken,
      p_expected_checkout_session_id: values.expectedCheckoutSessionId,
      p_lease_seconds: values.leaseSeconds,
    },
  );
  if (error) throw error;
  const result = checkoutRpcRecord(data);
  if (result.ok === true && result.code === 'advanced') {
    return { status: 'advanced', generation: checkoutGeneration(result) };
  }
  if (result.ok === false
      && ['occupied', 'stale', 'session_conflict', 'manual_review'].includes(
        result.code as string,
      )) {
    return {
      status: result.code as 'occupied' | 'stale' | 'session_conflict' | 'manual_review',
    };
  }
  throw new Error('Invalid billing Checkout advance result');
}

export async function releaseBillingCheckoutGeneration(values: {
  organizationId: string,
  generation: number,
  reservationToken: string,
  manualReview: boolean,
}): Promise<boolean> {
  const { data, error } = await billingDatabase().rpc(
    'release_billing_checkout_generation',
    {
      p_organization_id: values.organizationId,
      p_generation: values.generation,
      p_reservation_token: values.reservationToken,
      p_manual_review: values.manualReview,
    },
  );
  if (error) throw error;
  if (typeof data !== 'boolean') {
    throw new Error('Invalid billing Checkout release result');
  }
  return data;
}
