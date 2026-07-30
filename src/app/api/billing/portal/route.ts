import { billingConfig, billingIsConfigured, getStripe } from '@/lib/billing/config';
import { billingMaintenanceResponse } from '@/lib/billing/maintenance-gate.mjs';
import {
  authorizeActiveBillingCompany,
  authenticatedBillingUser,
  BillingControlAdmissionError,
  consumeBillingControlAttempt,
  getBillingAccount,
} from '@/lib/billing/supabase';
import { captureServerEvent } from '@/lib/posthog-server';

export const runtime = 'nodejs';

export async function POST(request: Request) {
  const maintenanceResponse = billingMaintenanceResponse();
  if (maintenanceResponse) return maintenanceResponse;

  try {
    const user = await authenticatedBillingUser(request);
    if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
    const authorization = await authorizeActiveBillingCompany(user.id);
    if (!authorization.ok || !authorization.organizationId || !authorization.billingOwnerId) {
      return Response.json({ error: 'Billing permission is required for the active company' }, { status: 403 });
    }
    const admission = await consumeBillingControlAttempt(
      user.id,
      authorization.organizationId,
      'portal',
    );
    if (admission.status === 'rate_limited') {
      // Attribution, because the limiter cannot provide it: the shared 120/minute
      // global bucket in consume_billing_control_attempt is keyed on an all-zero
      // hash and is charged BEFORE the per-actor bucket, so one looping account
      // saturates the window for every other customer's Checkout and Portal — and
      // the shared limiter table is content-free by design (202607200010), so
      // there is no row to trace back. These two ids are the only way an operator
      // can find the source. The bucket itself has to be fixed in the RPC
      // (202607200013) — see the handoff.
      console.warn(
        'Billing portal admission denied',
        JSON.stringify({ actor: user.id, organization: authorization.organizationId }),
      );
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
    // Mirrors checkout/route.ts. Without this gate an unset BREVITAS_PUBLIC_URL
    // on a deployed surface makes `return_url` the relative string '/dashboard',
    // which Stripe rejects with an operator-opaque 500. billingIsConfigured()
    // requires an https public URL once deployed (src/lib/billing/config.ts ->
    // config-predicate.mjs), so the misconfiguration is diagnosable here instead.
    if (!billingIsConfigured()) {
      return Response.json({ error: 'Billing setup is not available yet' }, { status: 503 });
    }

    const account = await getBillingAccount(authorization.organizationId);
    if (!account?.stripe_customer_id) {
      return Response.json({ error: 'Set up billing before opening the portal' }, { status: 409 });
    }

    const session = await getStripe().billingPortal.sessions.create({
      customer: account.stripe_customer_id,
      return_url: `${billingConfig().publicUrl}/dashboard`,
    });
    await captureServerEvent({
      distinctId: user.id,
      event: 'billing_portal_opened',
      properties: { organization_id: authorization.organizationId },
    });
    return Response.json({ url: session.url });
  } catch (error) {
    if (error instanceof BillingControlAdmissionError) {
      return Response.json(
        { error: 'Billing is temporarily unavailable' },
        {
          status: 503,
          headers: { 'Cache-Control': 'no-store', 'Retry-After': '5' },
        },
      );
    }
    console.error('Stripe portal creation failed', error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Could not open the billing portal' }, { status: 500 });
  }
}
