const SUPPORTED_SUBSCRIPTION_EVENTS = new Set([
  'customer.subscription.created',
  'customer.subscription.updated',
  'customer.subscription.deleted',
]);

const STRIPE_RESOURCE_MISSING = 'resource_missing';

function stripeObjectId(value) {
  if (!value) return null;
  return typeof value === 'string' ? value : value.id;
}

function assertStripeObject(resource, expectedObject, expectedId) {
  if (
    !resource ||
    typeof resource !== 'object' ||
    resource.object !== expectedObject ||
    resource.id !== expectedId
  ) {
    throw new Error(`Stripe returned the wrong ${expectedObject} resource`);
  }
  return resource;
}

export function isStripeResourceMissing(error) {
  return Boolean(
    error &&
    typeof error === 'object' &&
    error.type === 'StripeInvalidRequestError' &&
    error.code === STRIPE_RESOURCE_MISSING,
  );
}

export async function retrieveCanonicalSubscription({
  eventType,
  eventObject,
  retrieveSubscription,
}) {
  if (!SUPPORTED_SUBSCRIPTION_EVENTS.has(eventType)) {
    throw new Error('Unsupported Stripe subscription event');
  }
  assertStripeObject(eventObject, 'subscription', eventObject?.id);

  try {
    const subscription = await retrieveSubscription(eventObject.id);
    return {
      resource: assertStripeObject(subscription, 'subscription', eventObject.id),
      source: 'stripe_api',
    };
  } catch (error) {
    // Stripe normally keeps canceled subscriptions retrievable. A signed
    // customer.subscription.deleted object is a terminal tombstone only when
    // Stripe explicitly says that exact resource no longer exists.
    if (
      eventType !== 'customer.subscription.deleted' ||
      !isStripeResourceMissing(error) ||
      eventObject.status !== 'canceled'
    ) {
      throw error;
    }
    return { resource: eventObject, source: 'terminal_tombstone' };
  }
}

export async function retrieveCanonicalIncumbentSubscription({
  subscriptionId,
  retrieveSubscription,
}) {
  try {
    return assertStripeObject(
      await retrieveSubscription(subscriptionId),
      'subscription',
      subscriptionId,
    );
  } catch (error) {
    if (isStripeResourceMissing(error)) return null;
    throw error;
  }
}

export function invoiceSubscriptionId(invoice) {
  return stripeObjectId(invoice?.parent?.subscription_details?.subscription);
}

export async function retrieveCanonicalInvoice({
  eventObject,
  billingSubscriptionId,
  expectedCustomerId,
  expectedOrganizationId,
  retrieveInvoice,
  retrieveSubscription,
}) {
  assertStripeObject(eventObject, 'invoice', eventObject?.id);

  // Retrieve the event resource first instead of trusting its historical
  // payload for customer/account selection.
  const eventInvoice = assertStripeObject(
    await retrieveInvoice(eventObject.id),
    'invoice',
    eventObject.id,
  );
  if (stripeObjectId(eventInvoice.customer) !== expectedCustomerId) {
    throw new Error('Stripe invoice customer does not match its billing account');
  }

  if (!billingSubscriptionId) {
    throw new Error('Billing subscription is not established for invoice reconciliation');
  }

  // `latest_invoice` is Stripe's authoritative pointer for account-level
  // "last invoice" state. The delivered invoice ID is not an ordering key.
  const subscription = assertStripeObject(
    await retrieveSubscription(billingSubscriptionId),
    'subscription',
    billingSubscriptionId,
  );
  if (stripeObjectId(subscription.customer) !== expectedCustomerId) {
    throw new Error('Stripe subscription customer does not match its billing account');
  }
  const metadataOrganizationId = subscription.metadata?.brevitas_organization_id;
  if (metadataOrganizationId && metadataOrganizationId !== expectedOrganizationId) {
    throw new Error('Stripe subscription organization does not match its billing account');
  }

  const latestInvoiceId = stripeObjectId(subscription.latest_invoice);
  if (!latestInvoiceId) {
    throw new Error('Stripe subscription has no authoritative latest invoice');
  }
  const latestInvoice = latestInvoiceId === eventInvoice.id
    ? eventInvoice
    : assertStripeObject(await retrieveInvoice(latestInvoiceId), 'invoice', latestInvoiceId);
  if (stripeObjectId(latestInvoice.customer) !== expectedCustomerId) {
    throw new Error('Stripe latest invoice customer does not match its billing account');
  }
  const latestInvoiceSubscriptionId = invoiceSubscriptionId(latestInvoice);
  if (latestInvoiceSubscriptionId && latestInvoiceSubscriptionId !== billingSubscriptionId) {
    throw new Error('Stripe latest invoice does not belong to the billing subscription');
  }
  return latestInvoice;
}

export function canonicalInvoiceStatus(invoice) {
  if (invoice.status === 'paid') return 'paid';
  if (
    invoice.status === 'open' &&
    invoice.attempted === true &&
    Number.isSafeInteger(invoice.attempt_count) &&
    invoice.attempt_count > 0 &&
    typeof invoice.amount_remaining === 'number' &&
    invoice.amount_remaining > 0
  ) {
    return 'payment_failed';
  }
  return invoice.status || 'unknown';
}

export function canonicalPaymentOutcome(invoice) {
  const status = canonicalInvoiceStatus(invoice);
  if (status === 'paid') return 'paid';
  if (status === 'payment_failed') return 'failed';
  return 'pending';
}

export function subscriptionStateFingerprint(subscription) {
  const item = subscription.items?.data?.[0];
  return JSON.stringify([
    subscription.id,
    stripeObjectId(subscription.customer),
    subscription.status,
    subscription.created,
    item?.current_period_start ?? null,
    item?.current_period_end ?? null,
    subscription.metadata?.brevitas_organization_id ?? null,
  ]);
}

export function invoiceStateFingerprint(invoice) {
  return JSON.stringify([
    invoice.id,
    stripeObjectId(invoice.customer),
    canonicalInvoiceStatus(invoice),
    invoiceSubscriptionId(invoice),
  ]);
}

export async function reconcileCanonicalResource({
  retrieve,
  readRevision,
  writeSnapshot,
  fingerprint,
  maxAttempts = 4,
}) {
  if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 8) {
    throw new Error('Invalid Stripe reconciliation attempt limit');
  }

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const snapshot = await retrieve();
    const expectedRevision = await readRevision(snapshot);
    if (expectedRevision === 'retry') continue;
    if (expectedRevision === null) {
      return { resource: snapshot, revision: null, attempts: attempt, superseded: true };
    }
    if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 0) {
      throw new Error('Invalid billing reconciliation revision');
    }
    const nextRevision = await writeSnapshot(snapshot, expectedRevision);
    if (nextRevision === null) continue;
    if (!Number.isSafeInteger(nextRevision) || nextRevision <= expectedRevision) {
      throw new Error('Invalid billing reconciliation write result');
    }

    // Close the GET/write race: only finish after a fresh Stripe GET agrees
    // with the state just persisted. If Stripe moved, write the new canonical
    // snapshot under another monotonic compare-and-set revision.
    const confirmed = await retrieve();
    if (fingerprint(confirmed) === fingerprint(snapshot)) {
      return {
        resource: confirmed,
        revision: nextRevision,
        attempts: attempt,
        superseded: false,
      };
    }
  }

  throw new Error('Stripe resource did not stabilize during reconciliation');
}
