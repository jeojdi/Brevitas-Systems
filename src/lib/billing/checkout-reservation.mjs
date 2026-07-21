const CHECKOUT_SESSION_STATUSES = new Set(['open', 'complete', 'expired'])

export class CheckoutSessionRecoveryError extends Error {
  constructor(message) {
    super(message)
    this.name = 'CheckoutSessionRecoveryError'
  }
}

function stripeCustomerId(session) {
  if (typeof session?.customer === 'string') return session.customer
  if (session?.customer && typeof session.customer.id === 'string') {
    return session.customer.id
  }
  return null
}

function generationMetadata(session) {
  const value = session?.metadata?.brevitas_checkout_generation
  return typeof value === 'string' ? value : null
}

export function checkoutIdempotencyKey(organizationId, generation) {
  if (typeof organizationId !== 'string' || !organizationId) {
    throw new TypeError('Checkout organization ID is required')
  }
  if (!Number.isSafeInteger(generation) || generation <= 0) {
    throw new TypeError('Checkout generation must be a positive safe integer')
  }
  return `brevitas-checkout-${organizationId}-generation-${generation}`
}

export function selectRecoveredOpenCheckoutSession({
  page,
  organizationId,
  customerId,
  generation,
}) {
  if (!page || !Array.isArray(page.data) || typeof page.has_more !== 'boolean') {
    throw new CheckoutSessionRecoveryError('Stripe returned a malformed Checkout session page')
  }
  if (page.data.length > 100 || page.has_more) {
    throw new CheckoutSessionRecoveryError('Stripe Checkout session recovery exceeded its page bound')
  }

  const openSubscriptionSessions = page.data.filter(session => (
    session?.status === 'open'
    && session?.mode === 'subscription'
  ))
  if (openSubscriptionSessions.some(session => stripeCustomerId(session) !== customerId)) {
    throw new CheckoutSessionRecoveryError('Stripe returned an open Checkout session for another customer')
  }
  if (openSubscriptionSessions.length > 1) {
    throw new CheckoutSessionRecoveryError('Multiple open subscription Checkout sessions exist for one customer')
  }
  if (openSubscriptionSessions.length === 0) return null

  const [matching] = openSubscriptionSessions
  if (matching?.metadata?.brevitas_organization_id !== organizationId
      || generationMetadata(matching) !== String(generation)) {
    throw new CheckoutSessionRecoveryError('Open Checkout session does not match the reserved generation')
  }
  if (typeof matching.id !== 'string' || !matching.id
      || typeof matching.url !== 'string' || !matching.url) {
    throw new CheckoutSessionRecoveryError('Recovered Checkout session has no usable identity or URL')
  }
  return matching
}

export function inspectPersistedCheckoutSession({
  session,
  expectedSessionId,
  organizationId,
  customerId,
  generation,
}) {
  if (!session || session.id !== expectedSessionId) {
    throw new CheckoutSessionRecoveryError('Stripe returned the wrong persisted Checkout session')
  }
  if (stripeCustomerId(session) !== customerId) {
    throw new CheckoutSessionRecoveryError('Persisted Checkout session belongs to another customer')
  }
  if (session.mode !== 'subscription') {
    throw new CheckoutSessionRecoveryError('Persisted Checkout session is not a subscription session')
  }
  if (session?.metadata?.brevitas_organization_id !== organizationId) {
    throw new CheckoutSessionRecoveryError('Persisted Checkout session belongs to another company')
  }
  const persistedGeneration = generationMetadata(session)
  if (persistedGeneration !== null && persistedGeneration !== String(generation)) {
    throw new CheckoutSessionRecoveryError('Persisted Checkout session has the wrong generation')
  }
  if (!CHECKOUT_SESSION_STATUSES.has(session.status)) {
    throw new CheckoutSessionRecoveryError('Persisted Checkout session has an unknown status')
  }
  if (session.status === 'open'
      && (typeof session.url !== 'string' || !session.url)) {
    throw new CheckoutSessionRecoveryError('Persisted open Checkout session has no usable URL')
  }
  return {
    status: session.status,
    url: session.status === 'open' ? session.url : null,
    legacyGeneration: persistedGeneration === null,
  }
}
