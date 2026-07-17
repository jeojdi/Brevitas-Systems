import type Stripe from 'stripe';

import { billingConfig, getStripe } from '@/lib/billing/config';
import {
  billingDatabase,
  getBillingAccount,
  saveInvoiceState,
  saveSubscriptionState,
} from '@/lib/billing/supabase';
import {
  BILLABLE_SUBSCRIPTION_STATUSES,
  stripeId,
  subscriptionPeriod,
} from '@/lib/billing/stripe-state';
import { captureServerEvent } from '@/lib/posthog-server';

export const runtime = 'nodejs';

async function claimEvent(event: Stripe.Event): Promise<boolean> {
  const { error } = await billingDatabase()
    .from('stripe_webhook_events')
    .insert({ event_id: event.id, event_type: event.type });
  if (!error) return true;
  if (error.code === '23505') return false;
  throw error;
}

async function releaseEvent(eventId: string) {
  await billingDatabase().from('stripe_webhook_events').delete().eq('event_id', eventId);
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

async function applySubscription(subscription: Stripe.Subscription, eventCreated: number) {
  const customerId = stripeId(subscription.customer);
  if (!customerId) return null;
  const account = await accountForCustomer(customerId);
  if (!account) return null;
  if (
    account.stripe_subscription_id &&
    account.stripe_subscription_id !== subscription.id &&
    BILLABLE_SUBSCRIPTION_STATUSES.has(account.subscription_status)
  ) {
    if (BILLABLE_SUBSCRIPTION_STATUSES.has(subscription.status)) {
      await getStripe().subscriptions.cancel(subscription.id, { prorate: false });
    }
    return null;
  }
  await saveSubscriptionState(account.user_id, {
    stripe_subscription_id: subscription.id,
    subscription_status: subscription.status,
    billing_started_at: account.billing_started_at || new Date(subscription.created * 1000).toISOString(),
    ...subscriptionPeriod(subscription),
  }, eventCreated);
  return account.user_id as string;
}

async function applyCheckout(session: Stripe.Checkout.Session, eventCreated: number) {
  const userId = session.client_reference_id || session.metadata?.brevitas_user_id;
  const customerId = stripeId(session.customer);
  const subscriptionId = stripeId(session.subscription);
  if (!userId || !customerId || !subscriptionId) throw new Error('Checkout session is missing billing identifiers');

  const account = await getBillingAccount(userId);
  if (!account || account.stripe_customer_id !== customerId) {
    throw new Error('Checkout customer does not match the authenticated Brevitas account');
  }

  if (
    account.stripe_subscription_id &&
    account.stripe_subscription_id !== subscriptionId &&
    BILLABLE_SUBSCRIPTION_STATUSES.has(account.subscription_status)
  ) {
    // A second Checkout can complete only during a narrow concurrency race.
    // Cancel it immediately so a user can never retain two billable subscriptions.
    await getStripe().subscriptions.cancel(subscriptionId, { prorate: false });
    return null;
  }

  const subscription = await getStripe().subscriptions.retrieve(subscriptionId);
  return applySubscription(subscription, eventCreated);
}

async function applyInvoice(invoice: Stripe.Invoice, eventType: string, eventCreated: number) {
  const customerId = stripeId(invoice.customer);
  if (!customerId) return null;
  const account = await accountForCustomer(customerId);
  if (!account) return null;
  const invoiceStatus = eventType === 'invoice.payment_failed' ? 'payment_failed' : invoice.status;
  await saveInvoiceState(account.user_id, {
    last_invoice_id: invoice.id,
    last_invoice_status: invoiceStatus,
  }, eventCreated);
  return account.user_id as string;
}

export async function POST(request: Request) {
  const config = billingConfig();
  const signature = request.headers.get('stripe-signature');
  if (!config.webhookSecret || !signature) {
    return Response.json({ error: 'Webhook signature is missing' }, { status: 400 });
  }

  let event: Stripe.Event;
  try {
    // Signature verification requires the exact, unparsed bytes sent by Stripe.
    event = getStripe().webhooks.constructEvent(await request.text(), signature, config.webhookSecret);
  } catch (error) {
    console.warn('Stripe webhook signature rejected', error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Invalid webhook signature' }, { status: 400 });
  }

  try {
    if (!await claimEvent(event)) return Response.json({ received: true, duplicate: true });

    switch (event.type) {
      case 'checkout.session.completed': {
        const userId = await applyCheckout(event.data.object, event.created);
        if (userId) {
          await captureServerEvent({
            distinctId: userId,
            event: 'billing_checkout_completed',
            properties: { source: 'stripe_webhook' },
          });
        }
        break;
      }
      case 'customer.subscription.created':
      case 'customer.subscription.updated':
      case 'customer.subscription.deleted': {
        const userId = await applySubscription(event.data.object, event.created);
        if (userId) {
          await captureServerEvent({
            distinctId: userId,
            event: 'billing_subscription_updated',
            properties: {
              event_type: event.type,
              subscription_status: event.data.object.status,
            },
          });
        }
        break;
      }
      case 'invoice.paid':
      case 'invoice.payment_failed': {
        const userId = await applyInvoice(event.data.object, event.type, event.created);
        if (userId) {
          await captureServerEvent({
            distinctId: userId,
            event: 'billing_invoice_updated',
            properties: {
              event_type: event.type,
              payment_outcome: event.type === 'invoice.paid' ? 'paid' : 'failed',
            },
          });
        }
        break;
      }
      default:
        break;
    }
    return Response.json({ received: true });
  } catch (error) {
    await releaseEvent(event.id);
    console.error('Stripe webhook processing failed', event.type, error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Webhook processing failed' }, { status: 500 });
  }
}
