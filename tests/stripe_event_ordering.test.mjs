import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  canonicalInvoiceStatus,
  canonicalPaymentOutcome,
  invoiceStateFingerprint,
  reconcileCanonicalResource,
  retrieveCanonicalIncumbentSubscription,
  retrieveCanonicalInvoice,
  retrieveCanonicalSubscription,
  subscriptionStateFingerprint,
} from '../src/lib/billing/stripe-canonical-state.mjs'
import { stripeEventDiagnostic } from '../src/lib/billing/stripe-event-diagnostic.mjs'
import {
  subscriptionCandidateIsSupersededByCanonicalIncumbent,
  throwIfSupersededSubscriptionNeedsReview,
} from '../src/lib/billing/subscription-policy.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const clone = value => structuredClone(value)

function subscription(overrides = {}) {
  return {
    id: 'sub_current',
    object: 'subscription',
    customer: 'cus_company',
    status: 'active',
    created: 100,
    metadata: { brevitas_organization_id: '00000000-0000-4000-8000-000000000001' },
    latest_invoice: 'in_current',
    items: { data: [{ current_period_start: 100, current_period_end: 200 }] },
    ...overrides,
  }
}

function invoice(overrides = {}) {
  return {
    id: 'in_current',
    object: 'invoice',
    customer: 'cus_company',
    status: 'paid',
    attempted: true,
    attempt_count: 1,
    amount_remaining: 0,
    parent: {
      type: 'subscription_details',
      subscription_details: { subscription: 'sub_current' },
    },
    ...overrides,
  }
}

test('Stripe event fields are validated diagnostics, never a causal comparator', () => {
  const lower = stripeEventDiagnostic('evt_a', 'customer.subscription.updated', 300)
  const higher = stripeEventDiagnostic('evt_z', 'customer.subscription.updated', 300)

  assert.equal(lower.eventCreated, higher.eventCreated)
  assert.equal(Object.hasOwn(lower, 'eventSequence'), false)
  assert.throws(
    () => stripeEventDiagnostic('bad-id', 'invoice.paid', 1),
    /Invalid Stripe event diagnostic/,
  )
  assert.throws(
    () => stripeEventDiagnostic('evt_a', 'unknown.event', 1),
    /Invalid Stripe event diagnostic/,
  )
})

test('subscription webhooks retrieve current Stripe state and use a strict terminal tombstone', async () => {
  const historical = subscription({ status: 'past_due' })
  const current = subscription({ status: 'active' })
  const retrieved = await retrieveCanonicalSubscription({
    eventType: 'customer.subscription.updated',
    eventObject: historical,
    retrieveSubscription: async id => {
      assert.equal(id, historical.id)
      return current
    },
  })
  assert.equal(retrieved.source, 'stripe_api')
  assert.equal(retrieved.resource.status, 'active')

  const tombstone = subscription({ status: 'canceled' })
  const missing = Object.assign(new Error('not exposed'), {
    type: 'StripeInvalidRequestError',
    code: 'resource_missing',
  })
  const deleted = await retrieveCanonicalSubscription({
    eventType: 'customer.subscription.deleted',
    eventObject: tombstone,
    retrieveSubscription: async () => { throw missing },
  })
  assert.equal(deleted.source, 'terminal_tombstone')
  assert.equal(deleted.resource.status, 'canceled')

  await assert.rejects(
    retrieveCanonicalSubscription({
      eventType: 'customer.subscription.updated',
      eventObject: tombstone,
      retrieveSubscription: async () => { throw missing },
    }),
    error => error === missing,
  )
  await assert.rejects(
    retrieveCanonicalSubscription({
      eventType: 'customer.subscription.deleted',
      eventObject: subscription({ status: 'active' }),
      retrieveSubscription: async () => { throw missing },
    }),
    error => error === missing,
  )
})

test('same-type same-second reversed event IDs and delivery order persist the same canonical state', async () => {
  const current = subscription({ status: 'canceled' })

  for (const ids of [['evt_z', 'evt_a'], ['evt_a', 'evt_z']]) {
    const database = { revision: 0, value: null }
    for (const eventId of ids) {
      const diagnostic = stripeEventDiagnostic(
        eventId,
        'customer.subscription.updated',
        500,
      )
      await reconcileCanonicalResource({
        retrieve: async () => clone(current),
        readRevision: async () => database.revision,
        writeSnapshot: async (snapshot, expectedRevision) => {
          if (database.revision !== expectedRevision) return null
          database.value = clone(snapshot)
          database.revision += 1
          // The diagnostic is stored for investigation, not compared.
          database.eventId = diagnostic.eventId
          return database.revision
        },
        fingerprint: subscriptionStateFingerprint,
      })
    }
    assert.equal(database.value.status, 'canceled')
    assert.equal(database.revision, 2)
  }
})

test('interleaved older GET/newer GET/older write is repaired before completion', async () => {
  const older = subscription({ status: 'past_due' })
  const newer = subscription({ status: 'active' })
  let stripeState = older
  const database = { revision: 0, value: null, history: [] }
  let releaseOlderWrite
  let olderReadFinished
  const olderRead = new Promise(resolvePromise => { olderReadFinished = resolvePromise })
  const mayWriteOlder = new Promise(resolvePromise => { releaseOlderWrite = resolvePromise })

  const run = ({ eventId, retrieve, beforeFirstWrite }) => {
    const diagnostic = stripeEventDiagnostic(
      eventId,
      'customer.subscription.updated',
      700,
    )
    let writes = 0
    return reconcileCanonicalResource({
      retrieve,
      readRevision: async () => {
        if (beforeFirstWrite && writes === 0) await beforeFirstWrite()
        return database.revision
      },
      writeSnapshot: async (snapshot, expectedRevision) => {
        writes += 1
        if (database.revision !== expectedRevision) return null
        database.value = clone(snapshot)
        database.revision += 1
        database.history.push({ status: snapshot.status, eventId: diagnostic.eventId })
        return database.revision
      },
      fingerprint: subscriptionStateFingerprint,
    })
  }

  let olderGets = 0
  const olderHandler = run({
    eventId: 'evt_zreversed',
    retrieve: async () => {
      const result = clone(stripeState)
      olderGets += 1
      if (olderGets === 1) olderReadFinished()
      return result
    },
    beforeFirstWrite: () => mayWriteOlder,
  })

  await olderRead
  stripeState = newer
  await run({
    eventId: 'evt_areversed',
    retrieve: async () => clone(stripeState),
  })
  releaseOlderWrite()
  await olderHandler

  assert.deepEqual(database.history.map(item => item.status), ['active', 'past_due', 'active'])
  assert.equal(database.value.status, 'active')
  assert.equal(database.history.at(-1).eventId, 'evt_zreversed')
})

test('delayed old cancellation cannot cause the valid replacement subscription to be canceled', async () => {
  const oldCanceled = subscription({ id: 'sub_old', status: 'canceled' })
  const replacement = subscription({ id: 'sub_new', status: 'active' })
  const stripe = new Map([
    [oldCanceled.id, oldCanceled],
    [replacement.id, replacement],
  ])
  const database = {
    subscriptionId: oldCanceled.id,
    // Deliberately stale: the old cancellation webhook has not arrived.
    subscriptionStatus: 'active',
    revision: 0,
  }

  const superseded = candidate => subscriptionCandidateIsSupersededByCanonicalIncumbent({
    candidateId: candidate.id,
    candidateStatus: candidate.status,
    incumbentId: database.subscriptionId,
    retrieveIncumbent: incumbentId => retrieveCanonicalIncumbentSubscription({
      subscriptionId: incumbentId,
      retrieveSubscription: async id => clone(stripe.get(id)),
    }),
  })

  assert.equal(await superseded(replacement), false)
  await reconcileCanonicalResource({
    retrieve: async () => clone(replacement),
    readRevision: async candidate => await superseded(candidate)
      ? null
      : database.revision,
    writeSnapshot: async (candidate, expectedRevision) => {
      if (database.revision !== expectedRevision) return null
      database.subscriptionId = candidate.id
      database.subscriptionStatus = candidate.status
      database.revision += 1
      return database.revision
    },
    fingerprint: subscriptionStateFingerprint,
  })

  assert.equal(database.subscriptionId, replacement.id)
  assert.equal(database.subscriptionStatus, 'active')
  assert.equal(await superseded(oldCanceled), true)
  assert.equal(database.subscriptionId, replacement.id)
})

test('missing incumbents release the slot while lookup failures fail closed', async () => {
  const replacement = subscription({ id: 'sub_new', status: 'active' })
  const missing = Object.assign(new Error('not exposed'), {
    type: 'StripeInvalidRequestError',
    code: 'resource_missing',
  })

  const absent = await subscriptionCandidateIsSupersededByCanonicalIncumbent({
    candidateId: replacement.id,
    candidateStatus: replacement.status,
    incumbentId: 'sub_missing',
    retrieveIncumbent: incumbentId => retrieveCanonicalIncumbentSubscription({
      subscriptionId: incumbentId,
      retrieveSubscription: async () => { throw missing },
    }),
  })
  assert.equal(absent, false)

  let failedLookups = 0
  await assert.rejects(
    subscriptionCandidateIsSupersededByCanonicalIncumbent({
      candidateId: replacement.id,
      candidateStatus: replacement.status,
      incumbentId: 'sub_unknown',
      retrieveIncumbent: async () => {
        failedLookups += 1
        throw new Error('Stripe unavailable')
      },
    }),
    /Stripe unavailable/,
  )
  assert.equal(failedLookups, 1)
})

test('manual duplicate review resolves safely by terminalizing either subscription', async () => {
  const incumbent = subscription({ id: 'sub_incumbent', status: 'active' })
  const candidate = subscription({ id: 'sub_candidate', status: 'active' })
  let incumbentState = incumbent

  const isSuperseded = currentCandidate => (
    subscriptionCandidateIsSupersededByCanonicalIncumbent({
      candidateId: currentCandidate.id,
      candidateStatus: currentCandidate.status,
      incumbentId: incumbent.id,
      retrieveIncumbent: async () => clone(incumbentState),
    })
  )

  assert.equal(await isSuperseded(candidate), true)
  assert.throws(
    () => throwIfSupersededSubscriptionNeedsReview(candidate.status),
    /requires manual review/,
  )

  const canceledCandidate = subscription({ id: candidate.id, status: 'canceled' })
  assert.equal(await isSuperseded(canceledCandidate), true)
  assert.doesNotThrow(
    () => throwIfSupersededSubscriptionNeedsReview(canceledCandidate.status),
    'operator-canceled candidate must become a safe terminal no-op',
  )

  incumbentState = subscription({ id: incumbent.id, status: 'canceled' })
  assert.equal(
    await isSuperseded(candidate),
    false,
    'operator-canceled incumbent must release the candidate for canonical CAS',
  )
})

test('canonical incumbent checks remain bounded under repeated CAS contention', async () => {
  const oldCanceled = subscription({ id: 'sub_old', status: 'canceled' })
  const replacement = subscription({ id: 'sub_new', status: 'active' })
  let incumbentLookups = 0

  await assert.rejects(
    reconcileCanonicalResource({
      retrieve: async () => clone(replacement),
      readRevision: async candidate => {
        const isSuperseded = await subscriptionCandidateIsSupersededByCanonicalIncumbent({
          candidateId: candidate.id,
          candidateStatus: candidate.status,
          incumbentId: oldCanceled.id,
          retrieveIncumbent: async () => {
            incumbentLookups += 1
            return clone(oldCanceled)
          },
        })
        return isSuperseded ? null : 0
      },
      writeSnapshot: async () => null,
      fingerprint: subscriptionStateFingerprint,
      maxAttempts: 3,
    }),
    /did not stabilize/,
  )
  assert.equal(incumbentLookups, 3)
})

test('invoice reconciliation follows the billing subscription latest_invoice pointer', async () => {
  const eventInvoice = invoice({ id: 'in_old', status: 'open', amount_remaining: 25 })
  const latestInvoice = invoice({ id: 'in_latest', status: 'paid', amount_remaining: 0 })
  const calls = []

  const result = await retrieveCanonicalInvoice({
    eventObject: eventInvoice,
    billingSubscriptionId: 'sub_current',
    expectedCustomerId: 'cus_company',
    expectedOrganizationId: '00000000-0000-4000-8000-000000000001',
    retrieveInvoice: async id => {
      calls.push(`invoice:${id}`)
      return id === eventInvoice.id ? eventInvoice : latestInvoice
    },
    retrieveSubscription: async id => {
      calls.push(`subscription:${id}`)
      return subscription({ latest_invoice: latestInvoice.id })
    },
  })

  assert.equal(result.id, latestInvoice.id)
  assert.deepEqual(calls, [
    'invoice:in_old',
    'subscription:sub_current',
    'invoice:in_latest',
  ])
})

test('invoice payment state and analytics outcome come from the canonical resource', () => {
  const failed = invoice({ status: 'open', amount_remaining: 25 })
  const paid = invoice({ status: 'paid', amount_remaining: 0 })

  assert.equal(canonicalInvoiceStatus(failed), 'payment_failed')
  assert.equal(canonicalPaymentOutcome(failed), 'failed')
  assert.equal(canonicalInvoiceStatus(paid), 'paid')
  assert.equal(canonicalPaymentOutcome(paid), 'paid')
  assert.notEqual(invoiceStateFingerprint(failed), invoiceStateFingerprint(paid))
})

test('route and persistence use canonical monotonic CAS instead of event ordering', () => {
  const route = read('src/app/api/billing/webhook/route.ts')
  const persistence = read('src/lib/billing/canonical-persistence.ts')

  assert.match(route, /retrieveCanonicalSubscription/)
  assert.match(route, /retrieveCanonicalInvoice/)
  assert.match(route, /reconcileCanonicalResource/)
  assert.match(route, /canonicalPaymentOutcome\(applied\.invoice\)/)
  assert.doesNotMatch(route, /stripeEventOrder|eventSequence/)
  assert.match(persistence, /compare_and_set_stripe_subscription_snapshot/)
  assert.match(persistence, /compare_and_set_stripe_invoice_snapshot/)
  assert.doesNotMatch(persistence, /apply_stripe_(?:subscription|invoice)_event/)
})
