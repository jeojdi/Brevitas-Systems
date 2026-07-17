import { NextRequest } from 'next/server';

import { billingConfig, getStripe } from '@/lib/billing/config';
import { authenticatedBillingUser, getBillingAccount } from '@/lib/billing/supabase';
import { captureServerEvent } from '@/lib/posthog-server';
import { RATE_LIMITS, withRateLimit } from '@/lib/rate-limiter';

export const runtime = 'nodejs';

export async function POST(request: NextRequest) {
  return withRateLimit(request, async (req) => {
    try {
      const user = await authenticatedBillingUser(req);
      if (!user) return Response.json({ error: 'Authentication required' }, { status: 401 });
      const account = await getBillingAccount(user.id);
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
      });
      return Response.json({ url: session.url });
    } catch (error) {
      console.error('Stripe portal creation failed', error instanceof Error ? error.message : 'unknown error');
      return Response.json({ error: 'Could not open the billing portal' }, { status: 500 });
    }
  }, RATE_LIMITS.api);
}
