import { billingConfig, getStripe } from '@/lib/billing/config';
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
