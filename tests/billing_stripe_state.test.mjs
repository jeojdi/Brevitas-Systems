import assert from 'node:assert/strict'
import test from 'node:test'

import {
  StripeSubscriptionPeriodError,
  stripeId,
  subscriptionPeriod,
} from '../src/lib/billing/stripe-state.mjs'

// A concrete UTC instant so boundary conversion is deterministic.
const START = 1_700_000_000 // 2023-11-14T22:13:20.000Z
const END = 1_702_592_000 // 2023-12-14T22:13:20.000Z

const subscriptionWith = items => ({ id: 'sub_123', items: { data: items } })

test('subscriptionPeriod projects concrete ISO boundaries from a normal subscription', () => {
  const period = subscriptionPeriod(
    subscriptionWith([{ current_period_start: START, current_period_end: END }]),
  )
  assert.deepEqual(period, {
    current_period_start: new Date(START * 1000).toISOString(),
    current_period_end: new Date(END * 1000).toISOString(),
  })
  // Boundaries are always present strings, never null.
  assert.equal(typeof period.current_period_start, 'string')
  assert.equal(typeof period.current_period_end, 'string')
})

test('subscriptionPeriod uses the first line item when several are present', () => {
  const period = subscriptionPeriod(
    subscriptionWith([
      { current_period_start: START, current_period_end: END },
      { current_period_start: 1, current_period_end: 2 },
    ]),
  )
  assert.equal(period.current_period_start, new Date(START * 1000).toISOString())
  assert.equal(period.current_period_end, new Date(END * 1000).toISOString())
})

test('subscriptionPeriod throws instead of returning null boundaries on empty items.data', () => {
  assert.throws(
    () => subscriptionPeriod(subscriptionWith([])),
    error => {
      assert.ok(error instanceof StripeSubscriptionPeriodError)
      assert.match(error.message, /subscription has no items/)
      assert.equal(error.subscriptionId, 'sub_123')
      assert.equal(error.name, 'StripeSubscriptionPeriodError')
      return true
    },
  )
})

test('subscriptionPeriod throws when items is absent entirely', () => {
  assert.throws(() => subscriptionPeriod({ id: 'sub_missing' }), StripeSubscriptionPeriodError)
  assert.throws(
    () => subscriptionPeriod({ id: 'sub_null', items: { data: null } }),
    StripeSubscriptionPeriodError,
  )
})

test('subscriptionPeriod throws when either period field is missing on the item', () => {
  for (const item of [
    { current_period_end: END },
    { current_period_start: START },
    { current_period_start: null, current_period_end: END },
    { current_period_start: START, current_period_end: undefined },
    {},
  ]) {
    assert.throws(
      () => subscriptionPeriod(subscriptionWith([item])),
      error => {
        assert.ok(error instanceof StripeSubscriptionPeriodError)
        assert.match(error.message, /missing period boundaries/)
        return true
      },
    )
  }
})

test('stripeId normalizes strings, expanded objects, and empty values', () => {
  assert.equal(stripeId('cus_123'), 'cus_123')
  assert.equal(stripeId({ id: 'cus_456' }), 'cus_456')
  assert.equal(stripeId(null), null)
  assert.equal(stripeId(undefined), null)
  assert.equal(stripeId(''), null)
})
