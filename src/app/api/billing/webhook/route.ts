import type Stripe from 'stripe';
import { randomUUID } from 'node:crypto';

import { billingConfig, billingIsConfigured, getStripe } from '@/lib/billing/config';
import {
  billingDatabase,
  openBillingArrangementRequest,
  getBillingAccount,
  type BillingAccount,
} from '@/lib/billing/supabase';
import {
  compareAndSetInvoiceSnapshot,
  compareAndSetSubscriptionSnapshot,
  type StripeWebhookDatabaseLease,
  type StripeEventDiagnostic,
} from '@/lib/billing/canonical-persistence';
import {
  stripeId,
  subscriptionPeriod,
  StripeSubscriptionPeriodError,
} from '@/lib/billing/stripe-state.mjs';
import {
  StripeDuplicateSubscriptionReviewError,
  subscriptionCandidateIsSupersededByCanonicalIncumbent,
  throwIfSupersededSubscriptionNeedsReview,
} from '@/lib/billing/subscription-policy.mjs';
import {
  canonicalInvoiceStatus,
  canonicalPaymentOutcome,
  invoiceStateFingerprint,
  invoiceSubscriptionId,
  reconcileCanonicalResource,
  retrieveCanonicalIncumbentSubscription,
  retrieveCanonicalInvoice,
  retrieveCanonicalSubscription,
  subscriptionStateFingerprint,
} from '@/lib/billing/stripe-canonical-state.mjs';
import { captureServerEvent } from '@/lib/posthog-server';
import { processWebhookInbox } from '@/lib/billing/webhook-inbox.mjs';
import { stripeEventDiagnostic } from '@/lib/billing/stripe-event-diagnostic.mjs';

export const runtime = 'nodejs';
export const maxDuration = 30;

const WEBHOOK_LEASE_SECONDS = 60;
const WEBHOOK_HEARTBEAT_INTERVAL_MS = Math.floor(WEBHOOK_LEASE_SECONDS * 1000 / 3);

type RuntimeWebhookLease = {
  signal: AbortSignal;
  assertOwned: () => void;
  fence: () => Promise<void>;
};

type WebhookLease = RuntimeWebhookLease & StripeWebhookDatabaseLease;

type AppliedSubscription = {
  organizationId: string;
  subscription: Stripe.Subscription;
};

type AppliedInvoice = {
  organizationId: string;
  invoice: Stripe.Invoice;
};

function accountRevision(
  account: BillingAccount,
  field: 'stripe_subscription_reconcile_revision' | 'stripe_invoice_reconcile_revision',
): number {
  const value = account[field];
  const revision = typeof value === 'number' ? value : Number(value);
  if (!Number.isSafeInteger(revision) || revision < 0) {
    throw new Error('Billing account has an invalid reconciliation revision');
  }
  return revision;
}

async function claimEvent(event: Stripe.Event, leaseOwner: string): Promise<string> {
  const { data, error } = await billingDatabase().rpc('claim_stripe_webhook_event', {
    p_event_id: event.id,
    p_event_type: event.type,
    p_lease_owner: leaseOwner,
    p_lease_seconds: WEBHOOK_LEASE_SECONDS,
  });
  if (error) throw error;
  return String(data);
}

async function completeEvent(eventId: string, leaseOwner: string): Promise<boolean> {
  const { data, error } = await billingDatabase().rpc('mark_stripe_webhook_event_processed', {
    p_event_id: eventId,
    p_lease_owner: leaseOwner,
  });
  if (error) throw error;
  return data === true;
}

async function renewEvent(eventId: string, leaseOwner: string): Promise<boolean> {
  const { data, error } = await billingDatabase().rpc('renew_stripe_webhook_event_lease', {
    p_event_id: eventId,
    p_lease_owner: leaseOwner,
    p_lease_seconds: WEBHOOK_LEASE_SECONDS,
  });
  if (error) throw error;
  return data === true;
}

async function failEvent(
  eventId: string,
  leaseOwner: string,
  processingError: unknown,
): Promise<boolean> {
  let recordedError = 'webhook application failed';
  if (processingError instanceof StripeDuplicateSubscriptionReviewError) {
    recordedError = 'duplicate Stripe subscription requires manual review';
  } else if (processingError instanceof StripeSubscriptionPeriodError) {
    // Record the concrete reason + subscription id instead of the generic
    // string, so an operator can resolve it rather than see an opaque failure.
    recordedError = `Stripe subscription period unresolved (${processingError.reason}) `
      + `requires manual review${processingError.subscriptionId ? `: ${processingError.subscriptionId}` : ''}`;
  }
  const { data, error } = await billingDatabase().rpc('fail_stripe_webhook_event', {
    p_event_id: eventId,
    p_lease_owner: leaseOwner,
    p_error: recordedError.slice(0, 480),
  });
  if (error) throw error;
  return data === true;
}

async function accountForCustomer(customerId: string) {
  const { data, error } = await billingDatabase()
    .from('billing_accounts')
    .select('*')
    .eq('stripe_customer_id', customerId)
    .maybeSingle();
  if (error) throw error;
  return data;
}

function validateSubscriptionAccount(
  subscription: Stripe.Subscription,
  account: BillingAccount,
) {
  const customerId = stripeId(subscription.customer);
  if (!customerId || customerId !== account.stripe_customer_id) {
    throw new Error('Subscription customer does not match its Brevitas billing account');
  }
  const metadataOrganizationId = subscription.metadata?.brevitas_organization_id;
  if (metadataOrganizationId && metadataOrganizationId !== account.organization_id) {
    throw new Error('Subscription organization does not match its Brevitas billing account');
  }
}

async function subscriptionIsSuperseded(
  subscription: Stripe.Subscription,
  account: BillingAccount,
): Promise<boolean> {
  return subscriptionCandidateIsSupersededByCanonicalIncumbent({
    candidateId: subscription.id,
    candidateStatus: subscription.status,
    incumbentId: account.stripe_subscription_id,
    retrieveIncumbent: async (subscriptionId: string) => {
      const incumbent = await retrieveCanonicalIncumbentSubscription({
        subscriptionId,
        retrieveSubscription: (id: string) => getStripe().subscriptions.retrieve(id),
      }) as Stripe.Subscription | null;
      if (incumbent) validateSubscriptionAccount(incumbent, account);
      return incumbent;
    },
  });
}

async function reconcileSubscription(
  retrieve: () => Promise<Stripe.Subscription>,
  account: BillingAccount,
  diagnostic: StripeEventDiagnostic,
  lease: WebhookLease,
): Promise<AppliedSubscription | null> {
  const initial = await retrieve();
  validateSubscriptionAccount(initial, account);
  if (await subscriptionIsSuperseded(initial, account)) {
    throwIfSupersededSubscriptionNeedsReview(initial.status);
    return null;
  }

  const organizationId = account.organization_id;
  const billingStartedAt = account.billing_started_at;
  const result = await reconcileCanonicalResource({
    retrieve: async () => {
      const subscription = await retrieve();
      validateSubscriptionAccount(subscription, account);
      return subscription;
    },
    readRevision: async (subscription: Stripe.Subscription) => {
      const current = await getBillingAccount(organizationId);
      if (!current) throw new Error('Billing account disappeared during subscription reconciliation');
      validateSubscriptionAccount(subscription, current);
      return await subscriptionIsSuperseded(subscription, current)
        ? null
        : accountRevision(current, 'stripe_subscription_reconcile_revision');
    },
    writeSnapshot: async (subscription: Stripe.Subscription, expectedRevision: number) => {
      // Renew immediately before the canonical database mutation. If a
      // suspended invocation was reclaimed, no business-state write starts.
      await lease.fence();
      return compareAndSetSubscriptionSnapshot(
        organizationId,
        expectedRevision,
        diagnostic,
        {
          stripe_subscription_id: subscription.id,
          subscription_status: subscription.status,
          billing_started_at: billingStartedAt || new Date(subscription.created * 1000).toISOString(),
          ...subscriptionPeriod(subscription),
        },
        lease,
      );
    },
    fingerprint: subscriptionStateFingerprint,
  });
  if (result.superseded) {
    throwIfSupersededSubscriptionNeedsReview(result.resource.status);
    return null;
  }
  return { organizationId, subscription: result.resource };
}

async function applySubscriptionEvent(
  eventType: string,
  eventObject: Stripe.Subscription,
  diagnostic: StripeEventDiagnostic,
  lease: WebhookLease,
): Promise<AppliedSubscription | null> {
  const retrieve = async () => (
    await retrieveCanonicalSubscription({
      eventType,
      eventObject,
      retrieveSubscription: (subscriptionId: string) => (
        getStripe().subscriptions.retrieve(subscriptionId)
      ),
    })
  ).resource as Stripe.Subscription;
  const initial = await retrieve();
  const customerId = stripeId(initial.customer);
  if (!customerId) return null;
  const account = await accountForCustomer(customerId);
  if (!account) return null;
  return reconcileSubscription(retrieve, account as BillingAccount, diagnostic, lease);
}

async function applyCheckout(
  session: Stripe.Checkout.Session,
  diagnostic: StripeEventDiagnostic,
  lease: WebhookLease,
): Promise<AppliedSubscription | null> {
  const referenceOrganizationId = session.client_reference_id;
  const metadataOrganizationId = session.metadata?.brevitas_organization_id;
  if (
    referenceOrganizationId && metadataOrganizationId &&
    referenceOrganizationId !== metadataOrganizationId
  ) {
    throw new Error('Checkout session contains conflicting organization identifiers');
  }
  const customerId = stripeId(session.customer);
  const subscriptionId = stripeId(session.subscription);
  if (!customerId || !subscriptionId) {
    throw new Error('Checkout session is missing billing identifiers');
  }

  // Newly created sessions carry organization metadata. For an already-open
  // legacy session deployed across this migration, the unique Stripe customer
  // is the only safe fallback; its old user reference is never used as a
  // company selector.
  let account = metadataOrganizationId
    ? await getBillingAccount(metadataOrganizationId)
    : null;
  if (!account && referenceOrganizationId) {
    account = await getBillingAccount(referenceOrganizationId);
  }
  if (!account && !metadataOrganizationId) {
    account = await accountForCustomer(customerId);
  }
  if (!account || account.stripe_customer_id !== customerId) {
    throw new Error('Checkout customer does not match the Brevitas company billing account');
  }

  return reconcileSubscription(
    () => getStripe().subscriptions.retrieve(subscriptionId),
    account as BillingAccount,
    diagnostic,
    lease,
  );
}

/**
 * Open the billing-arrangement request for a completed Checkout.
 *
 * The actor recorded is the organization's billing owner rather than whoever
 * happened to click: Stripe's session carries no Brevitas user id, and the
 * billing owner is the identity that the arrangement actually binds. The
 * agreement reference names the Stripe objects, so the record points at the
 * commercial act instead of merely asserting it happened.
 */
async function recordArrangementRequest(
  session: Stripe.Checkout.Session,
  organizationId: string,
): Promise<void> {
  const { data, error } = await billingDatabase()
    .from('organizations')
    .select('billing_owner_id')
    .eq('id', organizationId)
    .maybeSingle();
  if (error) throw error;
  const ownerId = (data as { billing_owner_id?: string } | null)?.billing_owner_id;
  if (!ownerId) {
    // No owner means nothing to bind the arrangement to. Say so rather than
    // inventing an actor for a record whose whole purpose is attribution.
    throw new Error('Organization has no billing owner to attribute the arrangement to');
  }
  const customerId = stripeId(session.customer);
  await openBillingArrangementRequest({
    organizationId,
    actorUserId: ownerId,
    arrangement: 'marginal_per_call',
    agreementReference: `stripe:checkout_session=${session.id}` +
      (customerId ? ` customer=${customerId}` : ''),
    // Presented rate, not the configured one: this field records what the
    // customer was shown, and 202607280008's max_fee_share_of_verified_savings
    // is the separate thing that enforces it.
    ratePresented: '25% of verified savings',
    termsVersion: 'v1',
    // Stripe's own idempotency: at-least-once delivery collapses to one row.
    requestId: session.id,
  });
}

async function applyInvoice(
  eventObject: Stripe.Invoice,
  diagnostic: StripeEventDiagnostic,
  lease: WebhookLease,
): Promise<AppliedInvoice | null> {
  const currentEventInvoice = await getStripe().invoices.retrieve(eventObject.id);
  const customerId = stripeId(currentEventInvoice.customer);
  if (!customerId) return null;
  const account = await accountForCustomer(customerId);
  if (!account) return null;
  const organizationId = account.organization_id as string;

  const retrieve = async () => {
    const current = await getBillingAccount(organizationId);
    if (!current) throw new Error('Billing account disappeared during invoice reconciliation');
    if (current.stripe_customer_id !== customerId) {
      throw new Error('Invoice customer no longer matches its Brevitas billing account');
    }
    return retrieveCanonicalInvoice({
      eventObject,
      billingSubscriptionId: current.stripe_subscription_id,
      expectedCustomerId: customerId,
      expectedOrganizationId: organizationId,
      retrieveInvoice: (invoiceId: string) => getStripe().invoices.retrieve(invoiceId),
      retrieveSubscription: (subscriptionId: string) => (
        getStripe().subscriptions.retrieve(subscriptionId)
      ),
    }) as Promise<Stripe.Invoice>;
  };

  const result = await reconcileCanonicalResource({
    retrieve,
    readRevision: async (invoice: Stripe.Invoice) => {
      const current = await getBillingAccount(organizationId);
      if (!current) throw new Error('Billing account disappeared during invoice reconciliation');
      if (current.stripe_customer_id !== customerId) {
        throw new Error('Invoice customer no longer matches its Brevitas billing account');
      }
      const invoiceSubscription = invoiceSubscriptionId(invoice);
      if (!invoiceSubscription || invoiceSubscription !== current.stripe_subscription_id) {
        return 'retry';
      }
      return accountRevision(current, 'stripe_invoice_reconcile_revision');
    },
    writeSnapshot: async (invoice: Stripe.Invoice, expectedRevision: number) => {
      await lease.fence();
      return compareAndSetInvoiceSnapshot(
        organizationId,
        expectedRevision,
        diagnostic,
        {
          last_invoice_id: invoice.id,
          last_invoice_status: canonicalInvoiceStatus(invoice),
        },
        lease,
      );
    },
    fingerprint: invoiceStateFingerprint,
  });
  return { organizationId, invoice: result.resource };
}

export async function POST(request: Request) {
  const config = billingConfig();
  if (!billingIsConfigured()) {
    // The whole-product predicate is a SIGNAL here, not the gate. It also
    // requires the price, the meter, the weekly cap, BREVITAS_PUBLIC_URL and the
    // BILLING_RECOVERY_SECRET strength heuristic — none of which this route
    // touches (the recovery secret is read only by /api/billing/sync). Refusing
    // every delivery on those dropped real money events: the 503 below precedes
    // constructEvent and the inbox claim, so nothing lands in
    // stripe_webhook_events, and no replay worker exists — durability was
    // Stripe's ~3-day retry schedule and then permanent loss. One rotated
    // BILLING_RECOVERY_SECRET could therefore lose an
    // `invoice.payment_failed`/`customer.subscription.deleted` for a paying
    // tenant.
    //
    // So the gate is narrowed to the three values this route cannot work
    // without: `enabled`, `secretKey` (constructEvent goes through getStripe(),
    // which throws 'Stripe billing is not configured' without it, turning the
    // 503 into an unhandled 500) and `webhookSecret`. Everything else is logged
    // and the event is still verified and persisted. The 5xx stays retryable
    // rather than acknowledging: with no reclaim worker, a 200 here would
    // convert a transient misconfiguration into silent permanent loss.
    if (!config.enabled || !config.secretKey || !config.webhookSecret) {
      return Response.json(
        { error: 'Billing is temporarily unavailable' },
        {
          status: 503,
          headers: { 'Cache-Control': 'no-store', 'Retry-After': '30' },
        },
      );
    }
    // No secret material, no event body: just the fact, so an operator can see
    // that deliveries are being ingested under an incomplete billing config.
    console.error('Stripe webhook ingesting under an incomplete billing configuration');
  }
  const signature = request.headers.get('stripe-signature');
  if (!config.webhookSecret || !signature) {
    // I13: every non-2xx from this route carries no-store. These two 400s were
    // the only bare ones.
    return Response.json(
      { error: 'Webhook signature is missing' },
      { status: 400, headers: { 'Cache-Control': 'no-store' } },
    );
  }

  let event: Stripe.Event;
  try {
    // Signature verification requires the exact, unparsed bytes sent by Stripe.
    event = getStripe().webhooks.constructEvent(await request.text(), signature, config.webhookSecret);
  } catch (error) {
    console.warn('Stripe webhook signature rejected', error instanceof Error ? error.message : 'unknown error');
    return Response.json(
      { error: 'Invalid webhook signature' },
      { status: 400, headers: { 'Cache-Control': 'no-store' } },
    );
  }

  const leaseOwner = randomUUID();
  try {
    const result = await processWebhookInbox({
      claim: () => claimEvent(event, leaseOwner),
      renew: () => renewEvent(event.id, leaseOwner),
      heartbeatIntervalMs: WEBHOOK_HEARTBEAT_INTERVAL_MS,
      apply: async (runtimeLease: RuntimeWebhookLease) => {
        const lease: WebhookLease = {
          ...runtimeLease,
          eventId: event.id,
          leaseOwner,
          leaseSeconds: WEBHOOK_LEASE_SECONDS,
        };
        lease.assertOwned();
        switch (event.type) {
          case 'checkout.session.completed': {
            const diagnostic = stripeEventDiagnostic(event.id, event.type, event.created);
            const applied = await applyCheckout(event.data.object, diagnostic, lease);
            if (applied) {
              lease.assertOwned();
              // The customer has now accepted commercial terms in the strongest
              // form available: they completed live Checkout for this price.
              // Record the arrangement REQUEST so an operator can attest it.
              //
              // Deliberately NOT an attestation. attest_billing_arrangement is
              // executable by no PostgREST role, and that is what guarantees a
              // web process can never mark a customer billable. This only opens
              // the request that a human then answers out of band.
              //
              // Non-fatal on purpose. The subscription state above is already
              // persisted; throwing here would 5xx a delivery Stripe has only a
              // ~3-day retry window for, and losing a subscription event to
              // protect an enrolment record is the wrong trade. A missing
              // request surfaces as an un-attestable customer, which is visible
              // and fixable; a lost checkout.session.completed is neither.
              try {
                await recordArrangementRequest(event.data.object, applied.organizationId);
              } catch (requestError) {
                console.error(
                  'Billing arrangement request could not be opened after checkout',
                  requestError instanceof Error ? requestError.message : 'unknown error',
                );
              }
              await captureServerEvent({
                distinctId: `organization:${applied.organizationId}`,
                event: 'billing_checkout_completed',
                properties: {
                  organization_id: applied.organizationId,
                  source: 'stripe_webhook',
                },
              });
            }
            break;
          }
          case 'customer.subscription.created':
          case 'customer.subscription.updated':
          case 'customer.subscription.deleted': {
            const diagnostic = stripeEventDiagnostic(event.id, event.type, event.created);
            const applied = await applySubscriptionEvent(
              event.type,
              event.data.object,
              diagnostic,
              lease,
            );
            if (applied) {
              lease.assertOwned();
              await captureServerEvent({
                distinctId: `organization:${applied.organizationId}`,
                event: 'billing_subscription_updated',
                properties: {
                  organization_id: applied.organizationId,
                  event_type: event.type,
                  subscription_status: applied.subscription.status,
                },
              });
            }
            break;
          }
          case 'invoice.paid':
          case 'invoice.payment_failed': {
            const diagnostic = stripeEventDiagnostic(event.id, event.type, event.created);
            const applied = await applyInvoice(event.data.object, diagnostic, lease);
            if (applied) {
              lease.assertOwned();
              await captureServerEvent({
                distinctId: `organization:${applied.organizationId}`,
                event: 'billing_invoice_updated',
                properties: {
                  organization_id: applied.organizationId,
                  event_type: event.type,
                  payment_outcome: canonicalPaymentOutcome(applied.invoice),
                },
              });
            }
            break;
          }
          default:
            break;
        }
      },
      complete: () => completeEvent(event.id, leaseOwner),
      fail: (processingError: unknown) => failEvent(event.id, leaseOwner, processingError),
      reportCleanupError: (cleanupError: unknown) => {
        console.error(
          'Stripe webhook failure cleanup failed',
          event.type,
          cleanupError instanceof Error ? cleanupError.name : 'unknown error',
        );
      },
    });
    if (result.kind === 'busy') {
      return Response.json(
        { error: 'Webhook is already processing' },
        { status: 503, headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' } },
      );
    }
    return Response.json(
      { received: true, ...(result.kind === 'duplicate' ? { duplicate: true } : {}) },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (error) {
    console.error(
      'Stripe webhook processing failed',
      event.type,
      error instanceof Error ? error.name : 'unknown error',
      // Include the malformed-period reason/subscription so the failure is
      // diagnosable in logs, not just recorded in the event row.
      error instanceof StripeSubscriptionPeriodError
        ? { reason: error.reason, subscriptionId: error.subscriptionId }
        : undefined,
    );
    const requiresManualReview = error instanceof StripeDuplicateSubscriptionReviewError
      || error instanceof StripeSubscriptionPeriodError;
    return Response.json(
      {
        error: requiresManualReview
          ? 'Webhook requires manual billing review'
          : 'Webhook processing failed',
      },
      { status: 500, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
