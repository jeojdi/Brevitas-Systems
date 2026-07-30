// U10 (docs/STRIPE_TEST_PLAN.md §2.1) — the webhook lease is bound to ONE event,
// and a reconciliation revision is never coerced into nonsense.
//
// src/lib/billing/canonical-persistence.ts:34-36 is a four-line guard with an
// outsized job. Both compare-and-set RPCs are fenced in SQL on (event_id,
// lease_owner, lease_expires_at): the row a webhook invocation is allowed to
// write is the row it claimed. `webhookLeaseParameters` is what guarantees the
// two halves of the call agree — the lease's event id and the snapshot's
// diagnostic event id. If they could diverge, delivery A's lease would be used
// to write delivery B's canonical Stripe state, and the SQL fence would happily
// permit it because the fence checks the LEASE, not the payload.
//
// The guard is module-private, so it is exercised through the two exported
// functions with a recording database. That is the stronger test anyway: it also
// proves the guard runs BEFORE the RPC is issued.
//
// Run: node --test tests/billing_webhook_lease_binding.test.mjs

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  loadModule,
  loaderUnavailable,
  readRepositoryFile,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const EVENT_ID = 'evt_localFixtureOne'
const OTHER_EVENT_ID = 'evt_localFixtureTwo'
const LEASE_OWNER = '9f1d6d54-1c2e-4d0b-9f5b-53d8a2b3c4d5'

process.env.SUPABASE_URL ||= 'https://local.invalid'
process.env.SUPABASE_SERVICE_ROLE_KEY ||= 'service-role-fixture'

let response = { data: 1, error: null }
const rpcCalls = []

if (loaderUnavailable === false) {
  stubModule('@supabase/supabase-js', {
    createClient: () => ({
      rpc(name, args) {
        rpcCalls.push({ name, args })
        return Promise.resolve(response)
      },
    }),
  })
}

const skip = loaderUnavailable

function lease(eventId = EVENT_ID) {
  return { eventId, leaseOwner: LEASE_OWNER, leaseSeconds: 60 }
}

function diagnostic(eventId = EVENT_ID) {
  return { eventId, eventType: 'customer.subscription.updated', eventCreated: 1784000000 }
}

const SUBSCRIPTION_STATE = {
  stripe_subscription_id: 'sub_localFixture001',
  subscription_status: 'active',
  billing_started_at: '2026-07-20T00:00:00.000Z',
  current_period_start: '2026-07-20T00:00:00.000Z',
  current_period_end: '2026-07-27T00:00:00.000Z',
}

const INVOICE_STATE = {
  last_invoice_id: 'in_localFixture001',
  last_invoice_status: 'paid',
}

function answering(data, run) {
  response = { data, error: null }
  rpcCalls.length = 0
  return run()
}

test('the binding guard is stated in source ahead of the parameter object', () => {
  const source = readRepositoryFile('src/lib/billing/canonical-persistence.ts')
  assert.match(source, /if \(lease\.eventId !== diagnostic\.eventId\)/)
  assert.match(source, /Stripe webhook lease does not match its reconciliation event/)
  // Both CAS calls must go through the guard rather than assembling lease
  // parameters inline; that is what makes the property total.
  assert.equal((source.match(/\.\.\.webhookLeaseParameters\(lease, diagnostic\)/g) ?? []).length, 2)
})

test('a lease can never write another event\'s subscription snapshot', { skip }, async () => {
  const { compareAndSetSubscriptionSnapshot } = await loadModule('src/lib/billing/canonical-persistence.ts')

  for (const [leaseEventId, diagnosticEventId] of [
    [EVENT_ID, OTHER_EVENT_ID],
    [OTHER_EVENT_ID, EVENT_ID],
    [EVENT_ID, `${EVENT_ID} `],
    [EVENT_ID, EVENT_ID.toUpperCase()],
    [EVENT_ID, ''],
    ['', EVENT_ID],
  ]) {
    rpcCalls.length = 0
    await assert.rejects(
      compareAndSetSubscriptionSnapshot(
        ORGANIZATION_ID,
        3,
        diagnostic(diagnosticEventId),
        SUBSCRIPTION_STATE,
        lease(leaseEventId),
      ),
      /Stripe webhook lease does not match its reconciliation event/,
      `${leaseEventId} vs ${diagnosticEventId}`,
    )
    // The point of the guard: it runs before the RPC, so no fenced SQL call is
    // ever issued with a mismatched pair.
    assert.deepEqual(rpcCalls, [], `${leaseEventId} vs ${diagnosticEventId}`)
  }
})

test('a lease can never write another event\'s invoice snapshot', { skip }, async () => {
  const { compareAndSetInvoiceSnapshot } = await loadModule('src/lib/billing/canonical-persistence.ts')
  rpcCalls.length = 0
  await assert.rejects(
    compareAndSetInvoiceSnapshot(
      ORGANIZATION_ID,
      3,
      diagnostic(OTHER_EVENT_ID),
      INVOICE_STATE,
      lease(EVENT_ID),
    ),
    /Stripe webhook lease does not match its reconciliation event/,
  )
  assert.deepEqual(rpcCalls, [])
})

test('a matching pair carries the lease identity and the state into one fenced call', { skip }, async () => {
  const persistence = await loadModule('src/lib/billing/canonical-persistence.ts')

  const revision = await answering(4, () => persistence.compareAndSetSubscriptionSnapshot(
    ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
  ))
  assert.equal(revision, 4)
  assert.deepEqual(rpcCalls, [{
    name: 'compare_and_set_stripe_subscription_snapshot_for_webhook',
    args: {
      p_event_id: EVENT_ID,
      p_lease_owner: LEASE_OWNER,
      p_lease_seconds: 60,
      p_organization_id: ORGANIZATION_ID,
      p_expected_revision: 3,
      p_event_created: 1784000000,
      p_event_type: 'customer.subscription.updated',
      p_stripe_subscription_id: SUBSCRIPTION_STATE.stripe_subscription_id,
      p_subscription_status: 'active',
      p_billing_started_at: SUBSCRIPTION_STATE.billing_started_at,
      p_current_period_start: SUBSCRIPTION_STATE.current_period_start,
      p_current_period_end: SUBSCRIPTION_STATE.current_period_end,
    },
  }])
  // There is exactly ONE p_event_id in the payload. The lease's copy and the
  // diagnostic's copy are the same key, so the guard is the only thing making
  // the value unambiguous — which is precisely why it cannot be removed.
  assert.equal(Object.keys(rpcCalls[0].args).filter(key => key === 'p_event_id').length, 1)

  const invoiceRevision = await answering(9, () => persistence.compareAndSetInvoiceSnapshot(
    ORGANIZATION_ID, 8, diagnostic(), INVOICE_STATE, lease(),
  ))
  assert.equal(invoiceRevision, 9)
  assert.deepEqual(rpcCalls, [{
    name: 'compare_and_set_stripe_invoice_snapshot_for_webhook',
    args: {
      p_event_id: EVENT_ID,
      p_lease_owner: LEASE_OWNER,
      p_lease_seconds: 60,
      p_organization_id: ORGANIZATION_ID,
      p_expected_revision: 8,
      p_event_created: 1784000000,
      p_event_type: 'customer.subscription.updated',
      p_last_invoice_id: INVOICE_STATE.last_invoice_id,
      p_last_invoice_status: 'paid',
    },
  }])
})

test('a lost compare-and-set is null, not an error', { skip }, async () => {
  const persistence = await loadModule('src/lib/billing/canonical-persistence.ts')
  // NULL is the CAS-lost signal reconcileCanonicalResource retries on. It must
  // stay distinguishable from a malformed revision.
  assert.equal(
    await answering(null, () => persistence.compareAndSetSubscriptionSnapshot(
      ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
    )),
    null,
  )
  assert.equal(
    await answering(null, () => persistence.compareAndSetInvoiceSnapshot(
      ORGANIZATION_ID, 3, diagnostic(), INVOICE_STATE, lease(),
    )),
    null,
  )
})

test('parseRevision rejects negative and non-safe integers on both paths', { skip }, async () => {
  const persistence = await loadModule('src/lib/billing/canonical-persistence.ts')

  const rejected = [
    -1, -0.5, -9007199254740991,
    1.5, 0.1,
    Number.NaN, Infinity, -Infinity,
    9007199254740993, 1e308,
    'not-a-number', {}, { revision: 4 },
  ]
  for (const data of rejected) {
    await assert.rejects(
      answering(data, () => persistence.compareAndSetSubscriptionSnapshot(
        ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
      )),
      /Invalid subscription reconciliation revision/,
      `subscription ${String(data)}`,
    )
    await assert.rejects(
      answering(data, () => persistence.compareAndSetInvoiceSnapshot(
        ORGANIZATION_ID, 3, diagnostic(), INVOICE_STATE, lease(),
      )),
      // The context word is part of the contract: the two revisions are separate
      // columns and an operator must be able to tell which one failed.
      /Invalid invoice reconciliation revision/,
      `invoice ${String(data)}`,
    )
  }

  // Accepted: 0 and a numeric STRING, because PostgREST may render a bigint as
  // either a JSON number or a JSON string.
  for (const [data, expected] of [[0, 0], [7, 7], ['7', 7], [9007199254740991, 9007199254740991]]) {
    assert.equal(
      await answering(data, () => persistence.compareAndSetSubscriptionSnapshot(
        ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
      )),
      expected,
      String(data),
    )
  }

  // MEASURED LAXNESS, pinned so it is a known quantity rather than a surprise:
  // the parser coerces with Number(), so '', [], and booleans become 0/1 instead
  // of throwing. Harmless in situ — reconcileCanonicalResource requires the
  // returned revision to be strictly greater than the expected one, so a
  // coerced 0/1 fails there — but it is NOT the fail-closed behaviour the
  // function's shape suggests. Recorded as a cross-lane note, not fixed here.
  for (const [data, coerced] of [['', 0], [[], 0], [true, 1], [false, 0], [[5], 5]]) {
    assert.equal(
      await answering(data, () => persistence.compareAndSetSubscriptionSnapshot(
        ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
      )),
      coerced,
      JSON.stringify(data),
    )
  }
})

test('a transport error is propagated, never parsed as a revision', { skip }, async () => {
  const persistence = await loadModule('src/lib/billing/canonical-persistence.ts')
  response = { data: 4, error: new Error('55000 webhook lease is no longer owned') }
  await assert.rejects(
    persistence.compareAndSetSubscriptionSnapshot(
      ORGANIZATION_ID, 3, diagnostic(), SUBSCRIPTION_STATE, lease(),
    ),
    /webhook lease is no longer owned/,
  )
})
