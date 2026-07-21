/** @type {readonly ['active', 'trialing']} */
export const USAGE_ELIGIBLE_SUBSCRIPTION_STATUSES = Object.freeze([
  'active',
  'trialing',
])

/**
 * A company may have only one nonterminal Stripe subscription. Recovery
 * states keep their slot so Checkout cannot create a second subscription
 * while the incumbent can still be paid, resumed, or completed in Stripe.
 * `canceled` and `incomplete_expired` are terminal and intentionally absent.
 *
 * @type {readonly ['active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete']}
 */
export const ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES = Object.freeze([
  'active',
  'trialing',
  'past_due',
  'unpaid',
  'paused',
  'incomplete',
])

const usageEligibleStatuses = new Set(USAGE_ELIGIBLE_SUBSCRIPTION_STATUSES)
const accountOccupyingStatuses = new Set(ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES)
const supportedStripeSubscriptionStatuses = new Set([
  ...ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES,
  'canceled',
  'incomplete_expired',
])

export class StripeDuplicateSubscriptionReviewError extends Error {
  constructor() {
    super('Duplicate Stripe subscription requires manual review')
    this.name = 'StripeDuplicateSubscriptionReviewError'
  }
}

/** @param {string | null | undefined} status */
export function assertSupportedStripeSubscriptionStatus(status) {
  if (!supportedStripeSubscriptionStatuses.has(status)) {
    throw new Error('Stripe returned an unsupported subscription status')
  }
  return status
}

/** @param {string | null | undefined} status */
export function isUsageEligibleSubscriptionStatus(status) {
  return usageEligibleStatuses.has(status)
}

/** @param {string | null | undefined} status */
export function isAccountOccupyingSubscriptionStatus(status) {
  return accountOccupyingStatuses.has(status)
}

/**
 * Select which of two different subscription IDs owns the company's slot.
 * Terminal candidates never replace another ID. An occupying candidate may
 * replace a terminal incumbent, but never an occupying/recoverable incumbent.
 * The same subscription ID always reconciles so its lifecycle can advance.
 *
 * @param {{
 *   candidateId: string,
 *   candidateStatus: string,
 *   incumbentId: string | null | undefined,
 *   incumbentStatus: string | null | undefined,
 * }} input
 */
export function subscriptionCandidateIsSuperseded(input) {
  assertSupportedStripeSubscriptionStatus(input.candidateStatus)
  if (!input.incumbentId || input.incumbentId === input.candidateId) return false
  assertSupportedStripeSubscriptionStatus(input.incumbentStatus)
  return !isAccountOccupyingSubscriptionStatus(input.candidateStatus) ||
    isAccountOccupyingSubscriptionStatus(input.incumbentStatus)
}

/**
 * A terminal candidate is already closed and can be ignored. An occupying
 * candidate must never be canceled automatically: Stripe cannot atomically
 * honor a database fencing token, so an operator must close one subscription.
 *
 * @param {string} candidateStatus
 */
export function throwIfSupersededSubscriptionNeedsReview(candidateStatus) {
  assertSupportedStripeSubscriptionStatus(candidateStatus)
  if (isAccountOccupyingSubscriptionStatus(candidateStatus)) {
    throw new StripeDuplicateSubscriptionReviewError()
  }
}

/**
 * Decide ownership using the incumbent's current Stripe resource, never its
 * potentially delayed database snapshot. A missing incumbent releases the
 * slot. Retrieval or validation failures reject so the caller fails closed.
 *
 * @param {{
 *   candidateId: string,
 *   candidateStatus: string,
 *   incumbentId: string | null | undefined,
 *   retrieveIncumbent: (subscriptionId: string) => Promise<null | {id: string, status: string}>,
 * }} input
 */
export async function subscriptionCandidateIsSupersededByCanonicalIncumbent(input) {
  assertSupportedStripeSubscriptionStatus(input.candidateStatus)
  if (!input.incumbentId || input.incumbentId === input.candidateId) return false
  if (!isAccountOccupyingSubscriptionStatus(input.candidateStatus)) return true

  const incumbent = await input.retrieveIncumbent(input.incumbentId)
  if (incumbent === null) return false
  if (!incumbent || incumbent.id !== input.incumbentId) {
    throw new Error('Stripe returned the wrong incumbent subscription')
  }
  assertSupportedStripeSubscriptionStatus(incumbent.status)
  return subscriptionCandidateIsSuperseded({
    candidateId: input.candidateId,
    candidateStatus: input.candidateStatus,
    incumbentId: incumbent.id,
    incumbentStatus: incumbent.status,
  })
}

/**
 * Check every occupying status directly. This is bounded and cannot hide an
 * old active subscription behind an arbitrary page of terminal history.
 * Any failed or malformed Stripe response rejects, so Checkout fails closed.
 *
 * @param {{
 *   customerId: string,
 *   listSubscriptions: (params: {
 *     customer: string,
 *     status: 'active' | 'trialing' | 'past_due' | 'unpaid' | 'paused' | 'incomplete',
 *     limit: 1,
 *   }) => Promise<{data: Array<{id: string, status: string}>}>,
 * }} input
 */
export async function customerHasAccountOccupyingSubscription(input) {
  const results = await Promise.all(
    ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES.map(async status => ({
      status,
      page: await input.listSubscriptions({
        customer: input.customerId,
        status,
        limit: 1,
      }),
    })),
  )

  let found = false
  for (const { status, page } of results) {
    if (!page || !Array.isArray(page.data) || page.data.length > 1) {
      throw new Error('Stripe returned an invalid subscription status page')
    }
    if (page.data.length === 0) continue
    const subscription = page.data[0]
    if (
      !subscription ||
      typeof subscription.id !== 'string' ||
      subscription.status !== status
    ) {
      throw new Error('Stripe returned a subscription outside the requested status')
    }
    found = true
  }
  return found
}
