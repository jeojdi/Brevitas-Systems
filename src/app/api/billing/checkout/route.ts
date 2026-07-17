import { NextRequest } from 'next/server';

import { billingConfig, billingIsConfigured, getStripe, validateStripeCatalog } from '@/lib/billing/config';
import {
  authenticatedBillingUser,
  getBillingAccount,
  saveBillingAccount,
} from '@/lib/billing/supabase';
import { BILLABLE_SUBSCRIPTION_STATUSES, subscriptionPeriod } from '@/lib/billing/stripe-state';
import { RATE_LIMITS, withRateLimit } from '@/lib/rate-limiter';

export const runtime = 'nodejs';

export async function POST(request: NextRequest) {
  return withRateLimit(request, async (req) => {
    try {
      const user = await authenticatedBillingUser(req);
      if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
      if (!billingIsConfigured()) {
        return Response.json({ error: 'Billing setup is not available yet' }, { status: 503 });
      }

      const stripe = getStripe();
      const config = billingConfig();
      await validateStripeCatalog();
      let account = await getBillingAccount(user.id);

      if (BILLABLE_SUBSCRIPTION_STATUSES.has(account?.subscription_status || '')) {
        return Response.json({ error: 'Billing is already active', action: 'portal' }, { status: 409 });
      }

      let customerId = account?.stripe_customer_id || null;
      if (!customerId) {
        const customer = await stripe.customers.create({
          email: user.email,
          description: 'Brevitas usage billing',
          metadata: { brevitas_user_id: user.id },
        }, { idempotencyKey: `brevitas-customer-${user.id}` });
        customerId = customer.id;
        account = await saveBillingAccount(user.id, { stripe_customer_id: customerId });
      }

      if (account?.checkout_session_id) {
        const prior = await stripe.checkout.sessions.retrieve(account.checkout_session_id);
        if (prior.status === 'open' && prior.url) {
          return Response.json({ url: prior.url });
        }
      }

      // A completed session can race its webhook. Check Stripe before allowing
      // another subscription to be created for this customer.
      const subscriptions = await stripe.subscriptions.list({ customer: customerId, status: 'all', limit: 10 });
      const existing = subscriptions.data.find((item) => BILLABLE_SUBSCRIPTION_STATUSES.has(item.status));
      if (existing) {
        await saveBillingAccount(user.id, {
          stripe_subscription_id: existing.id,
          subscription_status: existing.status,
          billing_started_at: account?.billing_started_at || new Date(existing.created * 1000).toISOString(),
          stripe_subscription_event_created: Math.floor(Date.now() / 1000),
          ...subscriptionPeriod(existing),
        });
        return Response.json({ error: 'Billing is already active', action: 'portal' }, { status: 409 });
      }

      const session = await stripe.checkout.sessions.create({
        mode: 'subscription',
        customer: customerId,
        client_reference_id: user.id,
        line_items: [{ price: config.priceId }],
        payment_method_collection: 'always',
        billing_address_collection: 'auto',
        automatic_tax: { enabled: config.automaticTax },
        customer_update: config.automaticTax ? { address: 'auto', name: 'auto' } : undefined,
        success_url: `${config.publicUrl}/dashboard?billing=success`,
        cancel_url: `${config.publicUrl}/dashboard?billing=cancelled`,
        metadata: { brevitas_user_id: user.id },
        subscription_data: { metadata: { brevitas_user_id: user.id } },
      }, { idempotencyKey: `brevitas-checkout-${user.id}-${Math.floor(Date.now() / 300_000)}` });

      if (!session.url) throw new Error('Stripe did not return a Checkout URL');
      await saveBillingAccount(user.id, { checkout_session_id: session.id });
      return Response.json({ url: session.url });
    } catch (error) {
      console.error('Stripe checkout creation failed', error instanceof Error ? error.message : 'unknown error');
      return Response.json({ error: 'Could not start secure Checkout' }, { status: 500 });
    }
  }, RATE_LIMITS.formSubmission);
}
