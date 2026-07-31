// A completed Checkout opens the billing-arrangement REQUEST, and never more
// than that.
//
// WHY THIS FILE EXISTS. 202607280030 made attestation a two-step: the customer
// requests, a human holding the out-of-band `brevitas_attestor` role attests.
// `attest_billing_arrangement` refuses without the request it answers. Nothing
// in the app ever called the request half, so the first real customer needed one
// hand-written in SQL and every later one would have too.
//
// The webhook now opens it. Two properties matter and both are asserted here:
//
//   1. It IS opened, with the Checkout Session id as the idempotency key, so
//      Stripe's at-least-once delivery collapses onto one row instead of a queue
//      of duplicates for an operator to sift.
//
//   2. It is ONLY a request. attest_billing_arrangement is executable by no
//      PostgREST role -- not even service_role -- and that is the strongest
//      guarantee in the billing system: no bug and no compromised web process can
//      mark a customer billable. Automating enrolment must never quietly become
//      automating consent to charge, so the negative assertion below is the more
//      important of the two.
import assert from 'node:assert/strict'
import test from 'node:test'
import Stripe from 'stripe'

import {
  loadBillingRoute,
  loaderUnavailable,
  readRepositoryFile,
  recordingBillingDatabase,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const WEBHOOK_SECRET = 'whsec_localFixtureOnly_5Jq3xR8pW2nT7bY4kM6vC9sH'
const STRIPE_KEY = 'sk_test_localFixtureOnly0000000000000000'
const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const OWNER_ID = '22222222-2222-4222-8222-222222222222'
const CUSTOMER_ID = 'cus_localFixture001'
const SUBSCRIPTION_ID = 'sub_localFixture001'
const SESSION_ID = 'cs_localFixture001'

const stripeSdk = new Stripe(STRIPE_KEY)
const logs = []
console.error = (...args) => { logs.push(args.map(String).join(' ')) }

// ---------------------------------------------------------------------------
// Text pins. These hold even where the module loader is unavailable, and they
// are what stop the safety property from being edited away silently.
// ---------------------------------------------------------------------------

test('the webhook never attests, only requests', () => {
  const route = readRepositoryFile('src/app/api/billing/webhook/route.ts')
  // A CALL, not a mention: the route documents in prose why it must never
  // attest, and that comment is the point rather than a violation.
  assert.ok(
    !/\.rpc\(\s*['"]attest_billing_arrangement/.test(route),
    'the webhook CALLS attest_billing_arrangement. Enrolment must never ' +
    'become attestation: that function is deliberately executable by no ' +
    'PostgREST role, which is what stops a web process from marking a ' +
    'customer billable.',
  )
  assert.match(route, /openBillingArrangementRequest|recordArrangementRequest/)
})

test('no web-tier file anywhere calls the attestation writer', () => {
  for (const file of [
    'src/app/api/billing/webhook/route.ts',
    'src/app/api/billing/checkout/route.ts',
    'src/app/api/billing/portal/route.ts',
    'src/app/api/billing/sync/route.ts',
    'src/lib/billing/supabase.ts',
  ]) {
    assert.ok(
      !/\.rpc\(\s*['"](attest|revoke)_billing_arrangement/.test(readRepositoryFile(file)),
      `${file} reaches the attestation writer; it must stay operator-only`,
    )
  }
})

test('the request is keyed by the Stripe session, not a fresh id', () => {
  const route = readRepositoryFile('src/app/api/billing/webhook/route.ts')
  assert.match(
    route, /requestId: session\.id/,
    'the arrangement request must be idempotent on the Checkout Session id, or ' +
    "Stripe's at-least-once delivery produces duplicate rows",
  )
})

// ---------------------------------------------------------------------------
// Behavioural: drive a genuinely signed event through the real route.
// ---------------------------------------------------------------------------

let scenario

function installStubs() {
  const requests = []
  const database = recordingBillingDatabase({
    rpc: {
      claim_stripe_webhook_event: () => ({ data: 'claimed', error: null }),
      renew_stripe_webhook_event_lease: () => ({ data: true, error: null }),
      mark_stripe_webhook_event_processed: () => ({ data: true, error: null }),
      fail_stripe_webhook_event: () => ({ data: true, error: null }),
      compare_and_set_stripe_subscription_snapshot_for_webhook: () => ({ data: 1, error: null }),
    },
    // recordArrangementRequest reads the billing owner off the organization.
    from: { organizations: { data: { billing_owner_id: OWNER_ID }, error: null } },
  })

  scenario = { requests, database }

  stubModule('next/server', { NextRequest: class NextRequest {} })
  stubModule('@/lib/billing/config', {
    billingConfig: () => ({
      enabled: true,
      secretKey: STRIPE_KEY,
      webhookSecret: WEBHOOK_SECRET,
      weeklyCapUsd: 2000,
      publicUrl: 'https://brevitassystems.com',
    }),
    billingIsConfigured: () => true,
    getStripe: () => ({
      webhooks: stripeSdk.webhooks,
      subscriptions: {
        // Shaped like the real object: the reconciler derives the billing
        // window from the subscription ITEM's period, and an empty items list
        // makes that a RangeError rather than a readable failure.
        retrieve: async () => ({
          id: SUBSCRIPTION_ID,
          object: 'subscription',
          customer: CUSTOMER_ID,
          status: 'active',
          created: 1784000000,
          metadata: { brevitas_organization_id: ORGANIZATION_ID },
          items: {
            object: 'list',
            data: [{
              id: 'si_localFixture001',
              object: 'subscription_item',
              current_period_start: 1784000000,
              current_period_end: 1784000000 + 604800,
            }],
          },
        }),
      },
    }),
    validateStripeCatalog: async () => {},
  })
  stubModule('@/lib/billing/supabase', {
    billingDatabase: () => database.client,
    getBillingAccount: async () => ({
      organization_id: ORGANIZATION_ID,
      stripe_customer_id: CUSTOMER_ID,
      stripe_subscription_id: null,
      subscription_status: 'incomplete',
      stripe_subscription_reconcile_revision: 0,
    }),
    openBillingArrangementRequest: async input => {
      requests.push(input)
      return { ok: true, requestId: 'req_fixture', created: true }
    },
  })
  stubModule('@/lib/posthog-server', { captureServerEvent: async () => {} })
}

if (loaderUnavailable === false) installStubs()

const skip = loaderUnavailable

function checkoutEvent() {
  return JSON.stringify({
    id: 'evt_localFixture001',
    type: 'checkout.session.completed',
    created: Math.floor(Date.now() / 1000),
    data: {
      object: {
        id: SESSION_ID,
        customer: CUSTOMER_ID,
        subscription: SUBSCRIPTION_ID,
        client_reference_id: ORGANIZATION_ID,
        metadata: { brevitas_organization_id: ORGANIZATION_ID },
      },
    },
  })
}

test('a completed checkout opens exactly one arrangement request', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const payload = checkoutEvent()
  const response = await POST(new Request('https://brevitassystems.com/api/billing/webhook', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'stripe-signature': stripeSdk.webhooks.generateTestHeaderString({
        payload, secret: WEBHOOK_SECRET,
      }),
    },
    body: payload,
  }))

  // Surface what the route logged: a bare 500 !== 200 is not diagnosable.
  assert.equal(response.status, 200, `route logged:\n${logs.join('\n')}`)
  assert.equal(scenario.requests.length, 1, 'exactly one arrangement request per checkout')

  const [opened] = scenario.requests
  assert.equal(opened.organizationId, ORGANIZATION_ID)
  // The billing OWNER, not whoever clicked: Stripe's session carries no
  // Brevitas user id, and the owner is the identity the arrangement binds.
  assert.equal(opened.actorUserId, OWNER_ID)
  assert.equal(opened.arrangement, 'marginal_per_call')
  assert.equal(opened.requestId, SESSION_ID)
  // The record must point at the commercial act, not merely assert one.
  assert.match(opened.agreementReference, new RegExp(SESSION_ID))
  assert.match(opened.agreementReference, new RegExp(CUSTOMER_ID))
})

test('a failed arrangement request never fails the delivery', { skip }, async () => {
  // Losing a checkout.session.completed is worse than losing the enrolment
  // record: Stripe retries for ~3 days and then drops it forever, while a
  // missing request merely leaves a customer an operator cannot yet attest.
  const { POST } = await loadBillingRoute('webhook')
  const originalLength = scenario.requests.length
  scenario.requests.push = () => { throw new Error('arrangement request exploded') }

  const payload = checkoutEvent()
  const response = await POST(new Request('https://brevitassystems.com/api/billing/webhook', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'stripe-signature': stripeSdk.webhooks.generateTestHeaderString({
        payload, secret: WEBHOOK_SECRET,
      }),
    },
    body: payload,
  }))

  delete scenario.requests.push
  assert.equal(response.status, 200, 'the delivery must still be acknowledged')
  assert.ok(
    logs.some(line => /arrangement request could not be opened/i.test(line)),
    'the failure must be logged, never silently swallowed',
  )
  assert.equal(scenario.requests.length, originalLength)
})
