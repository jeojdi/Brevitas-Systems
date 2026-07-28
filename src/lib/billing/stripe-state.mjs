/**
 * Server-only projections of Stripe resources into the billing account shape.
 *
 * This module runs inside the Stripe webhook route, where a subscription must
 * always resolve to concrete billing-period boundaries. Silently returning null
 * boundaries corrupts the persisted billing account (the boundaries drive
 * dunning, proration, and access windows), so a malformed subscription raises a
 * structured error instead of a null projection.
 *
 * Follows the tested-helper pattern (plain .mjs, no `server-only` import) so the
 * projection logic can be exercised directly by `node --test`.
 */

/**
 * Structured failure raised when a Stripe subscription cannot yield concrete
 * billing-period boundaries. Carries the offending subscription id (when known)
 * so the webhook route can log and route it to manual review instead of
 * persisting nulls.
 */
export class StripeSubscriptionPeriodError extends Error {
  /**
   * @param {string} reason
   * @param {string | null} [subscriptionId]
   */
  constructor(reason, subscriptionId = null) {
    super(`Stripe subscription period is unresolvable: ${reason}`);
    this.name = 'StripeSubscriptionPeriodError';
    this.reason = reason;
    this.subscriptionId = subscriptionId;
  }
}

/**
 * Normalize a Stripe expandable reference (id string or expanded object) to its
 * id.
 *
 * @param {string | { id: string } | null | undefined} value
 * @returns {string | null}
 */
export function stripeId(value) {
  if (!value) return null;
  return typeof value === 'string' ? value : value.id;
}

/**
 * Project the current billing-period boundaries from a Stripe subscription.
 *
 * Throws a {@link StripeSubscriptionPeriodError} when the subscription carries
 * no line item, or when the first item is missing either period boundary,
 * because null boundaries must never be persisted onto a billing account.
 *
 * @param {import('stripe').Stripe.Subscription} subscription
 * @returns {{ current_period_start: string, current_period_end: string }}
 */
export function subscriptionPeriod(subscription) {
  const subscriptionId = stripeId(subscription);
  const items = subscription && subscription.items ? subscription.items.data : undefined;
  if (!Array.isArray(items) || items.length === 0) {
    throw new StripeSubscriptionPeriodError('subscription has no items', subscriptionId);
  }
  const item = items[0];
  const start = item ? item.current_period_start : undefined;
  const end = item ? item.current_period_end : undefined;
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    throw new StripeSubscriptionPeriodError(
      'subscription item is missing period boundaries',
      subscriptionId,
    );
  }
  return {
    current_period_start: new Date(start * 1000).toISOString(),
    current_period_end: new Date(end * 1000).toISOString(),
  };
}
