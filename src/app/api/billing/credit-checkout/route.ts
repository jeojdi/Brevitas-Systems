import { randomUUID } from 'node:crypto';

import { billingConfig, billingIsConfigured, getStripe } from '@/lib/billing/config';
import { billingMaintenanceResponse } from '@/lib/billing/maintenance-gate.mjs';
import {
  authorizeActiveBillingCompany,
  authenticatedBillingUser,
  consumeBillingControlAttempt,
  getBillingAccount,
  getOrganizationAccountType,
} from '@/lib/billing/supabase';
import { captureServerEvent } from '@/lib/posthog-server';

export const runtime = 'nodejs';

// One-time credit-pack purchase (B9). Distinct from /checkout (the metered subscription):
// this creates a `mode: 'payment'` Checkout session for an allowlisted credit-pack price.
// The webhook grants credits on checkout.session.completed. This route never touches the
// subscription flow.
export async function POST(request: Request) {
  const maintenanceResponse = billingMaintenanceResponse();
  if (maintenanceResponse) return maintenanceResponse;

  try {
    const user = await authenticatedBillingUser(request);
    if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });

    const authorization = await authorizeActiveBillingCompany(user.id);
    if (!authorization.ok || !authorization.organizationId || !authorization.billingOwnerId) {
      return Response.json(
        { error: 'Billing permission is required for the active company' },
        { status: 403 },
      );
    }
    const organizationId = authorization.organizationId;

    // Credits are the personal-account pricing model. Enterprise ('company') accounts
    // pay the 25%-of-verified-savings fee instead, so a credit purchase is not offered
    // to them — the dashboard hides the Credits card and this rejects a direct call.
    const accountType = await getOrganizationAccountType(organizationId);
    if (accountType !== 'individual') {
      return Response.json(
        { error: 'Credit purchases are available to personal accounts only' },
        { status: 403 },
      );
    }

    const admission = await consumeBillingControlAttempt(user.id, organizationId, 'checkout');
    if (admission.status === 'rate_limited') {
      return Response.json(
        { error: 'Too many billing requests' },
        {
          status: 429,
          headers: {
            'Cache-Control': 'no-store',
            'Retry-After': String(admission.retryAfterSeconds),
          },
        },
      );
    }

    if (!billingIsConfigured()) {
      return Response.json({ error: 'Billing setup is not available yet' }, { status: 503 });
    }

    const config = billingConfig();
    const body = await request.json().catch(() => ({}));
    const priceId = typeof body?.priceId === 'string' ? body.priceId : '';
    if (!priceId || !config.creditPackPriceIds.includes(priceId)) {
      return Response.json({ error: 'Unknown credit pack' }, { status: 400 });
    }

    const stripe = getStripe();
    // The credits a pack grants are authoritative on the Stripe price; read them here and
    // cache them on the session so the webhook grants exactly this many with no re-lookup.
    const price = await stripe.prices.retrieve(priceId);
    const creditMicro = Number(price.metadata?.brevitas_credit_micro || 0);
    if (!Number.isFinite(creditMicro) || creditMicro <= 0) {
      return Response.json({ error: 'Credit pack is misconfigured' }, { status: 503 });
    }

    const account = await getBillingAccount(organizationId);
    const session = await stripe.checkout.sessions.create(
      {
        mode: 'payment',
        client_reference_id: organizationId,
        line_items: [{ price: priceId, quantity: 1 }],
        ...(account?.stripe_customer_id ? { customer: account.stripe_customer_id } : {}),
        success_url: `${config.publicUrl}/dashboard?credits=purchased`,
        cancel_url: `${config.publicUrl}/dashboard?credits=cancelled`,
        metadata: {
          brevitas_organization_id: organizationId,
          brevitas_credit_micro: String(creditMicro),
        },
        payment_intent_data: {
          metadata: { brevitas_organization_id: organizationId },
        },
      },
      { idempotencyKey: `brevitas-credit-checkout-${randomUUID()}` },
    );

    await captureServerEvent({
      distinctId: `organization:${organizationId}`,
      event: 'billing_credit_checkout_started',
      properties: { organization_id: organizationId, price_id: priceId },
    });

    return Response.json({ url: session.url });
  } catch (error) {
    console.error(
      'Credit checkout failed',
      error instanceof Error ? error.message : 'unknown error',
    );
    return Response.json({ error: 'Credit checkout is temporarily unavailable' }, { status: 503 });
  }
}

// List the credit packs on offer so the dashboard can render one Buy button per pack.
export async function GET(request: Request) {
  try {
    const user = await authenticatedBillingUser(request);
    if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
    const authorization = await authorizeActiveBillingCompany(user.id);
    if (!authorization.ok || !authorization.organizationId) {
      return Response.json({ error: 'Billing permission is required for the active company' }, { status: 403 });
    }
    // Only personal ('individual') accounts buy credits; enterprise accounts are on the
    // 25% fee. No packs are offered to a company account — return an empty list (not an
    // error) so the dashboard simply renders no Buy row.
    const accountType = await getOrganizationAccountType(authorization.organizationId);
    if (accountType !== 'individual') {
      return Response.json({ packs: [] });
    }
    const config = billingConfig();
    if (!billingIsConfigured() || config.creditPackPriceIds.length === 0) {
      return Response.json({ packs: [] });
    }
    const stripe = getStripe();
    const packs = [];
    for (const priceId of config.creditPackPriceIds) {
      try {
        const price = await stripe.prices.retrieve(priceId);
        const creditMicro = Number(price.metadata?.brevitas_credit_micro || 0);
        if (!price.active || !Number.isFinite(creditMicro) || creditMicro <= 0) continue;
        packs.push({
          priceId: price.id,
          amountUsd: typeof price.unit_amount === 'number' ? price.unit_amount / 100 : null,
          creditMicro,
          label: price.nickname || '',
        });
      } catch {
        // A single misconfigured pack must not hide the rest.
      }
    }
    return Response.json({ packs });
  } catch (error) {
    console.error(
      'Credit pack listing failed',
      error instanceof Error ? error.message : 'unknown error',
    );
    return Response.json({ packs: [] });
  }
}
