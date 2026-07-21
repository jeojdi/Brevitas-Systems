import 'server-only';

import { billingDatabase } from '@/lib/billing/supabase';

export interface StripeEventDiagnostic {
  eventId: string;
  eventType: string;
  eventCreated: number;
}

export interface CanonicalSubscriptionState {
  stripe_subscription_id: string;
  subscription_status: string;
  billing_started_at: string | null;
  current_period_start: string | null;
  current_period_end: string | null;
}

export interface CanonicalInvoiceState {
  last_invoice_id: string;
  last_invoice_status: string;
}

export interface StripeWebhookDatabaseLease {
  eventId: string;
  leaseOwner: string;
  leaseSeconds: number;
}

function webhookLeaseParameters(
  lease: StripeWebhookDatabaseLease,
  diagnostic: StripeEventDiagnostic,
) {
  if (lease.eventId !== diagnostic.eventId) {
    throw new Error('Stripe webhook lease does not match its reconciliation event');
  }
  return {
    p_event_id: lease.eventId,
    p_lease_owner: lease.leaseOwner,
    p_lease_seconds: lease.leaseSeconds,
  };
}

function parseRevision(value: unknown, context: string): number | null {
  if (value === null) return null;
  const revision = typeof value === 'number' ? value : Number(value);
  if (!Number.isSafeInteger(revision) || revision < 0) {
    throw new Error(`Invalid ${context} reconciliation revision`);
  }
  return revision;
}

export async function compareAndSetSubscriptionSnapshot(
  organizationId: string,
  expectedRevision: number,
  diagnostic: StripeEventDiagnostic,
  values: CanonicalSubscriptionState,
  lease: StripeWebhookDatabaseLease,
): Promise<number | null> {
  const { data, error } = await billingDatabase().rpc(
    'compare_and_set_stripe_subscription_snapshot_for_webhook',
    {
      ...webhookLeaseParameters(lease, diagnostic),
      p_organization_id: organizationId,
      p_expected_revision: expectedRevision,
      p_event_created: diagnostic.eventCreated,
      p_event_id: diagnostic.eventId,
      p_event_type: diagnostic.eventType,
      p_stripe_subscription_id: values.stripe_subscription_id,
      p_subscription_status: values.subscription_status,
      p_billing_started_at: values.billing_started_at,
      p_current_period_start: values.current_period_start,
      p_current_period_end: values.current_period_end,
    },
  );
  if (error) throw error;
  return parseRevision(data, 'subscription');
}

export async function compareAndSetInvoiceSnapshot(
  organizationId: string,
  expectedRevision: number,
  diagnostic: StripeEventDiagnostic,
  values: CanonicalInvoiceState,
  lease: StripeWebhookDatabaseLease,
): Promise<number | null> {
  const { data, error } = await billingDatabase().rpc(
    'compare_and_set_stripe_invoice_snapshot_for_webhook',
    {
      ...webhookLeaseParameters(lease, diagnostic),
      p_organization_id: organizationId,
      p_expected_revision: expectedRevision,
      p_event_created: diagnostic.eventCreated,
      p_event_id: diagnostic.eventId,
      p_event_type: diagnostic.eventType,
      p_last_invoice_id: values.last_invoice_id,
      p_last_invoice_status: values.last_invoice_status,
    },
  );
  if (error) throw error;
  return parseRevision(data, 'invoice');
}
