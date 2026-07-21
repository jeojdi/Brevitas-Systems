const SUPPORTED_EVENTS = new Set([
  'checkout.session.completed',
  'customer.subscription.created',
  'customer.subscription.updated',
  'customer.subscription.deleted',
  'invoice.payment_failed',
  'invoice.paid',
]);

const STRIPE_EVENT_ID = /^evt_[A-Za-z0-9]+$/;

export function stripeEventDiagnostic(eventId, eventType, eventCreated) {
  if (
    !STRIPE_EVENT_ID.test(eventId) ||
    eventId.length > 255 ||
    !SUPPORTED_EVENTS.has(eventType) ||
    !Number.isSafeInteger(eventCreated) ||
    eventCreated < 0
  ) {
    throw new Error('Invalid Stripe event diagnostic');
  }
  // These fields are retained for incident investigation only. Stripe event
  // IDs and same-second event types are not a causal resource-state version.
  return { eventId, eventType, eventCreated };
}
