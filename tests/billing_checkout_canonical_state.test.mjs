import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES,
  USAGE_ELIGIBLE_SUBSCRIPTION_STATUSES,
  customerHasAccountOccupyingSubscription,
  isAccountOccupyingSubscriptionStatus,
  isUsageEligibleSubscriptionStatus,
  subscriptionCandidateIsSuperseded,
} from '../src/lib/billing/subscription-policy.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const canonicalFields = [
  'stripe_subscription_id',
  'subscription_status',
  'billing_started_at',
  'current_period_start',
  'current_period_end',
  'last_invoice_id',
  'last_invoice_status',
  'stripe_subscription_event_created',
  'stripe_subscription_event_id',
  'stripe_subscription_event_type',
  'stripe_subscription_reconcile_revision',
  'stripe_invoice_event_created',
  'stripe_invoice_event_id',
  'stripe_invoice_event_type',
  'stripe_invoice_reconcile_revision',
]

test('usage eligibility and single-subscription occupancy are deliberately separate', () => {
  assert.deepEqual(USAGE_ELIGIBLE_SUBSCRIPTION_STATUSES, ['active', 'trialing'])
  assert.deepEqual(ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES, [
    'active',
    'trialing',
    'past_due',
    'unpaid',
    'paused',
    'incomplete',
  ])

  for (const status of ['active', 'trialing']) {
    assert.equal(isUsageEligibleSubscriptionStatus(status), true)
    assert.equal(isAccountOccupyingSubscriptionStatus(status), true)
  }
  for (const status of ['past_due', 'unpaid', 'paused', 'incomplete']) {
    assert.equal(isUsageEligibleSubscriptionStatus(status), false)
    assert.equal(isAccountOccupyingSubscriptionStatus(status), true)
  }
  for (const status of ['canceled', 'incomplete_expired']) {
    assert.equal(isUsageEligibleSubscriptionStatus(status), false)
    assert.equal(isAccountOccupyingSubscriptionStatus(status), false)
  }
})

test('checkout checks each occupying status directly instead of a truncated all-status page', () => {
  const checkout = read('src/app/api/billing/checkout/route.ts')
  const duplicateGuard = checkout.slice(
    checkout.indexOf('const hasExistingSubscription = await'),
    checkout.indexOf('const session = await stripe.checkout.sessions.create'),
  )

  assert.match(duplicateGuard, /customerHasAccountOccupyingSubscription/)
  assert.match(duplicateGuard, /existingBillingResponse\(\)/)
  assert.match(checkout, /action: 'portal'/)
  assert.doesNotMatch(duplicateGuard, /saveBilling|compareAndSet|subscriptionPeriod/)
  assert.doesNotMatch(checkout, /status: 'all'|limit:\s*10\b|BILLABLE_SUBSCRIPTION_STATUSES/)
})

test('more than ten terminal subscriptions cannot hide an older occupying subscription', async () => {
  const terminalHistory = Array.from({ length: 12 }, (_, index) => ({
    id: `sub_canceled_${index}`,
    status: 'canceled',
  }))
  const active = { id: 'sub_active_older', status: 'active' }
  const stripeHistory = [...terminalHistory, active]
  const calls = []

  const found = await customerHasAccountOccupyingSubscription({
    customerId: 'cus_company',
    listSubscriptions: async params => {
      calls.push(params)
      return {
        data: stripeHistory
          .filter(subscription => subscription.status === params.status)
          .slice(0, params.limit),
      }
    },
  })

  assert.equal(stripeHistory.slice(0, 10).includes(active), false)
  assert.equal(found, true)
  assert.deepEqual(calls.map(call => call.status), ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES)
  assert.equal(calls.every(call => call.customer === 'cus_company' && call.limit === 1), true)
})

test('occupying-status lookup fails closed on any unavailable or malformed status query', async () => {
  await assert.rejects(
    customerHasAccountOccupyingSubscription({
      customerId: 'cus_company',
      listSubscriptions: async ({ status }) => {
        if (status === 'paused') throw new Error('Stripe unavailable')
        return { data: [] }
      },
    }),
    /Stripe unavailable/,
  )

  await assert.rejects(
    customerHasAccountOccupyingSubscription({
      customerId: 'cus_company',
      listSubscriptions: async ({ status }) => (
        status === 'paused'
          ? { data: [{ id: 'sub_wrong', status: 'canceled' }] }
          : status === 'active'
            ? { data: [{ id: 'sub_active', status: 'active' }] }
            : { data: [] }
      ),
    }),
    /outside the requested status/,
  )
})

test('recoverable incumbents keep the slot and terminal incumbents release it', () => {
  for (const incumbentStatus of ['past_due', 'unpaid', 'paused', 'incomplete']) {
    for (const candidateStatus of ACCOUNT_OCCUPYING_SUBSCRIPTION_STATUSES) {
      assert.equal(subscriptionCandidateIsSuperseded({
        candidateId: 'sub_new',
        candidateStatus,
        incumbentId: 'sub_recoverable',
        incumbentStatus,
      }), true, `${incumbentStatus} incumbent was displaced by ${candidateStatus}`)
    }
  }

  for (const incumbentStatus of ['canceled', 'incomplete_expired']) {
    assert.equal(subscriptionCandidateIsSuperseded({
      candidateId: 'sub_new',
      candidateStatus: 'active',
      incumbentId: 'sub_terminal',
      incumbentStatus,
    }), false, `${incumbentStatus} incumbent did not release its slot`)
  }

  assert.equal(subscriptionCandidateIsSuperseded({
    candidateId: 'sub_same',
    candidateStatus: 'canceled',
    incumbentId: 'sub_same',
    incumbentStatus: 'active',
  }), false, 'same-ID transition to terminal state must reconcile')

  assert.equal(subscriptionCandidateIsSuperseded({
    candidateId: 'sub_old_terminal',
    candidateStatus: 'canceled',
    incumbentId: 'sub_current',
    incumbentStatus: 'active',
  }), true, 'terminal event for a different ID must not displace the incumbent')

  const billingUi = read('dashboard/src/components/Billing.jsx')
  assert.match(
    billingUi,
    /billingManageable = \['active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'\]/,
    'every recoverable state must open Stripe management instead of offering a new checkout',
  )
})

test('webhook durably defers an occupying duplicate for manual review without cancellation', () => {
  const webhook = read('src/app/api/billing/webhook/route.ts')

  assert.match(webhook, /subscriptionCandidateIsSupersededByCanonicalIncumbent/)
  assert.match(webhook, /retrieveCanonicalIncumbentSubscription/)
  assert.match(webhook, /throwIfSupersededSubscriptionNeedsReview\(initial\.status\)/)
  assert.match(webhook, /duplicate Stripe subscription requires manual review/)
  assert.match(webhook, /Webhook requires manual billing review/)
  assert.doesNotMatch(webhook, /subscriptions\.cancel\(|cancelDuplicateSubscription|brevitas-webhook-cancel/)
  assert.doesNotMatch(webhook, /BILLABLE_SUBSCRIPTION_STATUSES/)
})

test('checkout cannot directly write canonical subscription, invoice, event, or revision state', () => {
  const checkout = read('src/app/api/billing/checkout/route.ts')

  assert.doesNotMatch(checkout, /saveBillingAccount/)
  assert.doesNotMatch(checkout, /canonical-persistence|compareAndSet(?:Subscription|Invoice)Snapshot/)
  for (const field of canonicalFields) {
    assert.doesNotMatch(checkout, new RegExp(`${field}\\s*:`), `${field} must be reconciliation-owned`)
  }
})

test('checkout persistence accepts only customer identity and fenced session identity', () => {
  const helper = read('src/lib/billing/supabase.ts')
  const checkoutPersistence = helper.slice(helper.indexOf('export async function saveBillingCustomerIdentity'))

  assert.match(checkoutPersistence, /saveBillingCustomerIdentity\([\s\S]+stripeCustomerId: string/)
  assert.match(checkoutPersistence, /save_billing_customer_identity/)
  assert.match(checkoutPersistence, /p_stripe_customer_id: stripeCustomerId/)
  assert.match(checkoutPersistence, /persistBillingCheckoutSession\([\s\S]+checkoutSessionId: string/)
  assert.match(checkoutPersistence, /persist_billing_checkout_session/)
  assert.match(checkoutPersistence, /p_generation: values\.generation/)
  assert.match(checkoutPersistence, /p_reservation_token: values\.reservationToken/)
  assert.doesNotMatch(checkoutPersistence, /saveBillingCheckoutSessionIdentity/)
  assert.doesNotMatch(checkoutPersistence, /Partial<|Omit<BillingAccount|\.\.\.values|saveBillingAccount/)
  for (const field of canonicalFields) {
    assert.doesNotMatch(checkoutPersistence, new RegExp(`${field}\\s*:`), `${field} must not be transport-writable`)
  }
})

test('canonical business-state persistence remains revision-checked RPC-only', () => {
  const canonical = read('src/lib/billing/canonical-persistence.ts')
  const webhook = read('src/app/api/billing/webhook/route.ts')

  assert.match(canonical, /compare_and_set_stripe_subscription_snapshot/)
  assert.match(canonical, /p_expected_revision: expectedRevision/)
  assert.match(canonical, /compare_and_set_stripe_invoice_snapshot/)
  assert.match(webhook, /compareAndSetSubscriptionSnapshot\(/)
  assert.match(webhook, /compareAndSetInvoiceSnapshot\(/)
})
