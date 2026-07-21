import { randomUUID } from 'node:crypto';

import { billingConfig, billingIsConfigured, getStripe, validateStripeCatalog } from '@/lib/billing/config';
import { billingMaintenanceResponse } from '@/lib/billing/maintenance-gate.mjs';
import {
  CheckoutSessionRecoveryError,
  checkoutIdempotencyKey,
  inspectPersistedCheckoutSession,
  selectRecoveredOpenCheckoutSession,
} from '@/lib/billing/checkout-reservation.mjs';
import {
  advanceBillingCheckoutGeneration,
  authorizeActiveBillingCompany,
  authenticatedBillingUser,
  BillingControlAdmissionError,
  consumeBillingControlAttempt,
  getBillingAccount,
  persistBillingCheckoutSession,
  releaseBillingCheckoutGeneration,
  reserveBillingCheckoutGeneration,
  saveBillingCustomerIdentity,
} from '@/lib/billing/supabase';
import {
  customerHasAccountOccupyingSubscription,
  isAccountOccupyingSubscriptionStatus,
} from '@/lib/billing/subscription-policy.mjs';
import { captureServerEvent } from '@/lib/posthog-server';

export const runtime = 'nodejs';

const CHECKOUT_RESERVATION_LEASE_SECONDS = 300;

function checkoutBusyResponse(retryAfterSeconds = 5) {
  return Response.json(
    { error: 'Secure Checkout is temporarily busy; retry shortly' },
    {
      status: 503,
      headers: {
        'Cache-Control': 'no-store',
        'Retry-After': String(retryAfterSeconds),
      },
    },
  );
}

function checkoutManualReviewResponse() {
  return Response.json(
    { error: 'Secure Checkout requires billing review before it can continue' },
    {
      status: 503,
      headers: { 'Cache-Control': 'no-store', 'Retry-After': '300' },
    },
  );
}

function existingBillingResponse() {
  return Response.json(
    { error: 'Billing already exists; manage or recover it in Stripe', action: 'portal' },
    { status: 409 },
  );
}

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
    const organizationId = authorization.organizationId;
    const admission = await consumeBillingControlAttempt(
      user.id,
      organizationId,
      'checkout',
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
    if (!billingIsConfigured()) {
      return Response.json({ error: 'Billing setup is not available yet' }, { status: 503 });
    }

    const stripe = getStripe();
    const config = billingConfig();
    await validateStripeCatalog();
    const account = await getBillingAccount(organizationId);

    if (isAccountOccupyingSubscriptionStatus(account?.subscription_status)) return existingBillingResponse();

    let customerId = account?.stripe_customer_id || null;
    if (!customerId) {
      const customer = await stripe.customers.create({
        description: 'Brevitas company usage billing',
        metadata: { brevitas_organization_id: organizationId },
      }, { idempotencyKey: `brevitas-customer-${organizationId}` });
      customerId = customer.id;
      await saveBillingCustomerIdentity(organizationId, customerId);
    }

    const reservationToken = randomUUID();
    const reservation = await reserveBillingCheckoutGeneration({
      organizationId,
      stripeCustomerId: customerId,
      reservationToken,
      leaseSeconds: CHECKOUT_RESERVATION_LEASE_SECONDS,
    });
    if (reservation.status === 'busy') {
      return checkoutBusyResponse(reservation.retryAfterSeconds);
    }
    if (reservation.status === 'occupied') return existingBillingResponse();
    if (reservation.status !== 'acquired') return checkoutManualReviewResponse();

    let generation = reservation.generation;
    let mode = reservation.mode;
    let manualReview = false;
    let returningCheckoutUrl = false;
    try {
      if (mode === 'inspect_persisted') {
        let inspection;
        try {
          const prior = await stripe.checkout.sessions.retrieve(
            reservation.checkoutSessionId as string,
          );
          inspection = inspectPersistedCheckoutSession({
            session: prior,
            expectedSessionId: reservation.checkoutSessionId,
            organizationId,
            customerId,
            generation,
          });
        } catch (error) {
          if (error instanceof CheckoutSessionRecoveryError) {
            manualReview = true;
            return checkoutManualReviewResponse();
          }
          throw error;
        }

        if (inspection.status === 'open') {
          await captureServerEvent({
            distinctId: user.id,
            event: 'billing_checkout_started',
            properties: { organization_id: organizationId, session_reused: true },
          });
          // Retrieval is external work and may outlive the lease. Re-persisting
          // the same immutable ID is the final live-token CAS before any URL is
          // returned to the caller.
          const persistence = await persistBillingCheckoutSession({
            organizationId,
            generation,
            reservationToken,
            checkoutSessionId: reservation.checkoutSessionId as string,
          });
          if (persistence.status === 'occupied') return existingBillingResponse();
          if (persistence.status === 'stale') return checkoutBusyResponse();
          if (persistence.status !== 'persisted') {
            manualReview = true;
            return checkoutManualReviewResponse();
          }
          returningCheckoutUrl = true;
          return Response.json({ url: inspection.url });
        }

        // A persisted generation is immutable. Only its exact, company-bound
        // terminal session may release the company to a new generation.
        const hasExistingSubscription = await customerHasAccountOccupyingSubscription({
          customerId,
          listSubscriptions: (params) => stripe.subscriptions.list(params),
        });
        if (hasExistingSubscription) return existingBillingResponse();

        const advance = await advanceBillingCheckoutGeneration({
          organizationId,
          generation,
          reservationToken,
          expectedCheckoutSessionId: reservation.checkoutSessionId as string,
          leaseSeconds: CHECKOUT_RESERVATION_LEASE_SECONDS,
        });
        if (advance.status === 'occupied') return existingBillingResponse();
        if (advance.status === 'stale') return checkoutBusyResponse();
        if (advance.status !== 'advanced') {
          manualReview = true;
          return checkoutManualReviewResponse();
        }
        generation = advance.generation;
        mode = 'create_or_recover';
      }

      // A completed session can race its webhook. Check Stripe before allowing
      // another subscription to be created for this customer.
      const hasExistingSubscription = await customerHasAccountOccupyingSubscription({
        customerId,
        listSubscriptions: (params) => stripe.subscriptions.list(params),
      });
      if (hasExistingSubscription) return existingBillingResponse();

      let recovered;
      try {
        const openSessions = await stripe.checkout.sessions.list({
          customer: customerId,
          status: 'open',
          limit: 100,
        });
        recovered = selectRecoveredOpenCheckoutSession({
          page: openSessions,
          organizationId,
          customerId,
          generation,
        });
      } catch (error) {
        if (error instanceof CheckoutSessionRecoveryError) {
          manualReview = true;
          return checkoutManualReviewResponse();
        }
        throw error;
      }

      if (recovered) {
        await captureServerEvent({
          distinctId: user.id,
          event: 'billing_checkout_started',
          properties: { organization_id: organizationId, session_reused: true },
        });
        const persistence = await persistBillingCheckoutSession({
          organizationId,
          generation,
          reservationToken,
          checkoutSessionId: recovered.id,
        });
        if (persistence.status === 'occupied') return existingBillingResponse();
        if (persistence.status === 'stale') return checkoutBusyResponse();
        if (persistence.status !== 'persisted') {
          manualReview = true;
          return checkoutManualReviewResponse();
        }
        returningCheckoutUrl = true;
        return Response.json({ url: recovered.url });
      }

      if (mode === 'recover_only') {
        manualReview = true;
        return checkoutManualReviewResponse();
      }

      const generationMetadata = String(generation);
      const session = await stripe.checkout.sessions.create({
        mode: 'subscription',
        customer: customerId,
        client_reference_id: organizationId,
        line_items: [{ price: config.priceId }],
        payment_method_collection: 'always',
        billing_address_collection: 'auto',
        automatic_tax: { enabled: config.automaticTax },
        customer_update: config.automaticTax ? { address: 'auto', name: 'auto' } : undefined,
        success_url: `${config.publicUrl}/dashboard?billing=success`,
        cancel_url: `${config.publicUrl}/dashboard?billing=cancelled`,
        metadata: {
          brevitas_organization_id: organizationId,
          brevitas_checkout_generation: generationMetadata,
        },
        subscription_data: {
          metadata: {
            brevitas_organization_id: organizationId,
            brevitas_checkout_generation: generationMetadata,
          },
        },
      }, { idempotencyKey: checkoutIdempotencyKey(organizationId, generation) });

      if (!session.url) throw new Error('Stripe did not return a Checkout URL');
      await captureServerEvent({
        distinctId: user.id,
        event: 'billing_checkout_started',
        properties: { organization_id: organizationId, session_reused: false },
      });
      const persistence = await persistBillingCheckoutSession({
        organizationId,
        generation,
        reservationToken,
        checkoutSessionId: session.id,
      });
      if (persistence.status === 'occupied') return existingBillingResponse();
      if (persistence.status === 'stale') return checkoutBusyResponse();
      if (persistence.status !== 'persisted') {
        manualReview = true;
        return checkoutManualReviewResponse();
      }
      returningCheckoutUrl = true;
      return Response.json({ url: session.url });
    } finally {
      try {
        const released = await releaseBillingCheckoutGeneration({
          organizationId,
          generation,
          reservationToken,
          manualReview,
        });
        if (returningCheckoutUrl && !released) return checkoutBusyResponse();
      } catch (releaseError) {
        console.error(
          'Stripe checkout reservation release failed',
          releaseError instanceof Error ? releaseError.message : 'unknown error',
        );
        if (returningCheckoutUrl) return checkoutBusyResponse();
      }
    }
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
    console.error('Stripe checkout creation failed', error instanceof Error ? error.message : 'unknown error');
    return Response.json({ error: 'Could not start secure Checkout' }, { status: 500 });
  }
}
