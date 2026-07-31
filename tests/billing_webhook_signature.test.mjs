// I10 (docs/STRIPE_TEST_PLAN.md §2.2) — the Stripe webhook trust boundary,
// executed for the first time.
//
// src/app/api/billing/webhook/route.ts is the ONE unauthenticated write surface
// in the product: everything past `getStripe().webhooks.constructEvent` is
// treated as Stripe-authored fact. Until this file, that boundary had zero
// executable coverage — the only existing "webhook" suites
// (tests/stripe_webhook_durability.test.mjs, tests/stripe_event_ordering.test.mjs)
// grep the source or exercise the .mjs helpers; neither ever invoked POST.
//
// Everything here runs offline. The signature is produced by the INSTALLED
// stripe@22.3.2 SDK's `webhooks.generateTestHeaderString` against a locally
// invented `whsec_`, and verified by the SDK's real `constructEvent` (HMAC over
// the raw bytes, node:crypto only — no account, no key, no network). The
// database and PostHog are stubbed; `getStripe()` is a real Stripe instance for
// `webhooks` and hand-written fakes for everything else, so a leaked network
// call would throw rather than reach Stripe.
//
// Run: node --test tests/billing_webhook_signature.test.mjs

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

// A locally invented secret. `generateTestHeaderString` signs with whatever it
// is given, so this needs no Stripe account and must never match a real one.
const WEBHOOK_SECRET = 'whsec_localFixtureOnly_5Jq3xR8pW2nT7bY4kM6vC9sH'
// Never used for an API call in this file: every fake below intercepts before
// the SDK would build a request. The value is a syntactically plausible test
// key so `getStripe()`'s own emptiness check is not what is under test.
const STRIPE_KEY = 'sk_test_localFixtureOnly0000000000000000'

const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const CUSTOMER_ID = 'cus_localFixture001'
const SUBSCRIPTION_ID = 'sub_localFixture001'

const stripeSdk = new Stripe(STRIPE_KEY)

// The route logs every rejection (by design — an operator needs to see a
// forged or stale delivery). Capture instead of printing: the suite drives ~30
// deliberate rejections and Stripe's verifier message is six lines long, which
// would bury the real CI output. The capture is also asserted below, so the
// logging itself stays a tested property.
const logs = { warn: [], error: [] }
console.warn = (...args) => { logs.warn.push(args.map(String).join(' ')) }
console.error = (...args) => { logs.error.push(args.map(String).join(' ')) }

// ---------------------------------------------------------------------------
// Always-running text pins. These hold even on a runtime without the loader.
// ---------------------------------------------------------------------------

test('the webhook verifies the raw request bytes, never a re-serialized object', () => {
  const route = readRepositoryFile('src/app/api/billing/webhook/route.ts')
  // request.text() — NOT request.json(). Re-serializing a parsed object changes
  // the bytes (key order, whitespace, number formatting) and every signature
  // would fail; worse, a `json()`-first handler that fell back to unverified
  // data would accept forged events.
  assert.match(route, /webhooks\.constructEvent\(await request\.text\(\)/)
  assert.doesNotMatch(route, /await request\.json\(\)/)
  // Signature verification must precede the inbox claim, so an unsigned body
  // can never allocate a row or a lease.
  assert.ok(
    route.indexOf('constructEvent') < route.indexOf('processWebhookInbox({'),
    'signature verification must happen before the inbox claim',
  )
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function stripeEvent({ id = 'evt_localFixture001', type, object, created } = {}) {
  return {
    id,
    object: 'event',
    api_version: '2026-06-24.dahlia',
    created: created ?? Math.floor(Date.now() / 1000),
    livemode: false,
    pending_webhooks: 1,
    request: { id: null, idempotency_key: null },
    type,
    data: { object },
  }
}

function subscriptionObject(overrides = {}) {
  return {
    id: SUBSCRIPTION_ID,
    object: 'subscription',
    customer: CUSTOMER_ID,
    status: 'active',
    created: 1784000000,
    metadata: {},
    items: {
      object: 'list',
      data: [{
        id: 'si_localFixture001',
        object: 'subscription_item',
        current_period_start: 1784000000,
        current_period_end: 1784000000 + 604800,
      }],
    },
    ...overrides,
  }
}

function billingAccount(overrides = {}) {
  return {
    organization_id: ORGANIZATION_ID,
    user_id: '22222222-2222-4222-8222-222222222222',
    stripe_customer_id: CUSTOMER_ID,
    stripe_subscription_id: null,
    subscription_status: 'incomplete',
    billing_started_at: null,
    current_period_start: null,
    current_period_end: null,
    last_invoice_id: null,
    last_invoice_status: null,
    stripe_subscription_reconcile_revision: 0,
    stripe_invoice_reconcile_revision: 0,
    ...overrides,
  }
}

/** Sign EXACTLY these bytes, the way Stripe does. */
function signed(payload, { secret = WEBHOOK_SECRET, timestamp } = {}) {
  return stripeSdk.webhooks.generateTestHeaderString({
    payload,
    secret,
    ...(timestamp === undefined ? {} : { timestamp }),
  })
}

function webhookRequest(payload, headers = {}) {
  return new Request('https://brevitassystems.com/api/billing/webhook', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: payload,
  })
}

// ---------------------------------------------------------------------------
// Stub wiring
// ---------------------------------------------------------------------------

// The stub modules are registered ONCE, at file scope, before any route import:
// module resolution is cached for the life of the process, so a stub registered
// after the first `loadBillingRoute()` would be ignored (the loader throws on
// that mistake rather than silently exercising the real module). Each test then
// swaps `scenario`, which every stubbed function reads at call time.
let scenario = null

const currentScenario = () => {
  if (!scenario) throw new Error('no webhook scenario is installed')
  return scenario
}

if (loaderUnavailable === false) {
  stubModule('@/lib/billing/config', {
    billingConfig: () => currentScenario().config,
    billingIsConfigured: () => currentScenario().configured,
    getStripe: () => currentScenario().stripe,
    validateStripeCatalog: async () => {},
  })
  stubModule('@/lib/billing/supabase', {
    // The webhook imports this to open the arrangement request after a
    // completed checkout. This suite never reaches it, so it throws rather
    // than returning a plausible value that would hide a route that did.
    openBillingArrangementRequest: async () => {
      throw new Error('no arrangement request is permitted in this suite')
    },
    billingDatabase: () => currentScenario().database.client,
    getBillingAccount: async () => currentScenario().account,
  })
  stubModule('@/lib/posthog-server', {
    captureServerEvent: async payload => { currentScenario().posthogEvents.push(payload) },
  })
}

/**
 * @param {{
 *   claim?: 'claimed'|'busy'|'processed',
 *   account?: object|null,
 *   subscription?: object,
 *   configured?: boolean,
 *   enabled?: boolean,
 *   webhookSecret?: string,
 * }} options
 */
function installStubs(options = {}) {
  const {
    claim = 'claimed',
    account = billingAccount(),
    subscription = subscriptionObject(),
    configured = true,
    enabled = true,
    webhookSecret = WEBHOOK_SECRET,
  } = options

  const database = recordingBillingDatabase({
    rpc: {
      claim_stripe_webhook_event: () => ({ data: claim, error: null }),
      renew_stripe_webhook_event_lease: () => ({ data: true, error: null }),
      mark_stripe_webhook_event_processed: () => ({ data: true, error: null }),
      fail_stripe_webhook_event: () => ({ data: true, error: null }),
      compare_and_set_stripe_subscription_snapshot_for_webhook: () => ({ data: 1, error: null }),
      compare_and_set_stripe_invoice_snapshot_for_webhook: () => ({ data: 1, error: null }),
    },
    from: { billing_accounts: { data: account, error: null } },
  })

  const stripeCalls = []
  const stripe = {
    // The REAL verifier. Everything below it is a fake that records instead of
    // reaching the network.
    webhooks: stripeSdk.webhooks,
    subscriptions: {
      retrieve: async id => {
        stripeCalls.push(['subscriptions.retrieve', id])
        return subscription
      },
      list: async params => {
        stripeCalls.push(['subscriptions.list', params])
        return { data: [] }
      },
    },
    invoices: {
      retrieve: async id => {
        stripeCalls.push(['invoices.retrieve', id])
        throw new Error('no invoice fixture is installed')
      },
    },
  }

  const posthogEvents = []

  scenario = {
    configured,
    config: {
      enabled,
      secretKey: STRIPE_KEY,
      webhookSecret,
      recoverySecret: '',
      priceId: 'price_localFixture',
      meterEventName: 'brevitas_fee_microusd',
      publicUrl: 'https://brevitassystems.com',
      weeklyCapUsd: 1,
      automaticTax: false,
    },
    stripe,
    database,
    account,
    posthogEvents,
  }
  return { database, stripeCalls, posthogEvents }
}

const skip = loaderUnavailable

// ---------------------------------------------------------------------------
// The trust boundary
// ---------------------------------------------------------------------------

test('a request with no stripe-signature header is refused before anything is read', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database, stripeCalls } = installStubs()

  const payload = JSON.stringify(stripeEvent({
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const response = await POST(webhookRequest(payload))

  assert.equal(response.status, 400)
  assert.deepEqual(await response.json(), { error: 'Webhook signature is missing' })
  // No inbox row, no lease, no Stripe call: an unsigned body buys nothing.
  assert.deepEqual(database.calls, [])
  assert.deepEqual(stripeCalls, [])
})

test('a configured deployment with no webhook secret refuses every delivery', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database } = installStubs({ webhookSecret: '' })

  const payload = JSON.stringify(stripeEvent({ type: 'customer.updated', object: {} }))
  // Even a correctly signed body cannot be trusted when the server holds no
  // secret to verify it against.
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 400)
  assert.deepEqual(await response.json(), { error: 'Webhook signature is missing' })
  assert.deepEqual(database.calls, [])
})

test('an unconfigured deployment 503s with Retry-After before reading the body', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  // The gate is the WEBHOOK-relevant subset of the billing configuration
  // (enabled + secretKey + webhookSecret), not billingIsConfigured() as a whole:
  // a route that 503s on an unrelated env var loses money events, because the
  // refusal below happens before the event is ever recorded. `enabled: false` is
  // therefore what this scenario now switches off; the properties asserted are
  // unchanged. The complementary case — an incomplete whole-product config whose
  // webhook values are present must still be ingested — is
  // tests/billing_webhook_config_gate.test.mjs.
  const { database } = installStubs({ configured: false, enabled: false })

  const payload = JSON.stringify(stripeEvent({ type: 'customer.updated', object: {} }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 503)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  assert.equal(response.headers.get('retry-after'), '30')
  assert.deepEqual(await response.json(), { error: 'Billing is temporarily unavailable' })
  // F15: the body is never read, so no inbox row exists to be replayed later.
  assert.deepEqual(database.calls, [])
})

test('a one-byte body mutation invalidates a real Stripe signature', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database } = installStubs()

  const event = stripeEvent({ type: 'customer.subscription.updated', object: subscriptionObject() })
  const payload = JSON.stringify(event)
  const signature = signed(payload)

  // Every mutation below is one byte of the ORIGINAL, signed payload. The
  // header is untouched: this is the forgery an attacker with a captured
  // delivery would attempt.
  const mutations = {
    'flipped status digit': payload.replace('"pending_webhooks":1', '"pending_webhooks":2'),
    'swapped subscription id': payload.replace(SUBSCRIPTION_ID, `${SUBSCRIPTION_ID.slice(0, -1)}9`),
    'appended whitespace': `${payload} `,
    'truncated last byte': payload.slice(0, -1),
  }
  for (const [label, mutated] of Object.entries(mutations)) {
    assert.notEqual(mutated, payload, label)
    const response = await POST(webhookRequest(mutated, { 'stripe-signature': signature }))
    assert.equal(response.status, 400, label)
    assert.deepEqual(await response.json(), { error: 'Invalid webhook signature' }, label)
  }

  // A semantically identical body with different bytes must also fail — this is
  // why the handler signs request.text() and not a re-serialized object.
  const reordered = JSON.stringify({ data: event.data, ...event })
  assert.notEqual(reordered, payload)
  assert.deepEqual(JSON.parse(reordered).data, JSON.parse(payload).data)
  const reorderedResponse = await POST(webhookRequest(reordered, { 'stripe-signature': signature }))
  assert.equal(reorderedResponse.status, 400)

  // A signature made with a DIFFERENT secret over the correct bytes.
  const wrongSecret = await POST(webhookRequest(payload, {
    'stripe-signature': signed(payload, { secret: `${WEBHOOK_SECRET}_other` }),
  }))
  assert.equal(wrongSecret.status, 400)
  assert.deepEqual(await wrongSecret.json(), { error: 'Invalid webhook signature' })

  // A malformed header, a header with no v1 scheme at all, and a v1 value that
  // is simply wrong.
  for (const header of ['garbage', 't=1,v0=deadbeef', 't=1,v1=deadbeef']) {
    const response = await POST(webhookRequest(payload, { 'stripe-signature': header }))
    assert.equal(response.status, 400, JSON.stringify(header))
  }
  // An EMPTY header is indistinguishable from an absent one for the route: both
  // take the `!signature` branch, so the message differs from the verifier's.
  const emptyHeader = await POST(webhookRequest(payload, { 'stripe-signature': '' }))
  assert.equal(emptyHeader.status, 400)
  assert.deepEqual(await emptyHeader.json(), { error: 'Webhook signature is missing' })

  // Not one of those attempts reached the inbox.
  assert.deepEqual(database.calls, [])

  // Every rejection is logged (an operator must be able to see a forged or
  // replayed delivery) and the log never contains the secret.
  assert.ok(logs.warn.length >= Object.keys(mutations).length)
  assert.ok(logs.warn.every(line => line.startsWith('Stripe webhook signature rejected')))
  assert.ok(!logs.warn.join('\n').includes(WEBHOOK_SECRET))

  // Documented SDK behaviour, pinned so nobody "hardens" it by accident: a
  // header may carry several schemes and ONE matching v1 is sufficient, so an
  // extra unknown scheme alongside a valid v1 is ACCEPTED (and does reach the
  // inbox — the assertion above must precede this call).
  const extraScheme = await POST(webhookRequest(payload, {
    'stripe-signature': `${signature},v0=deadbeef`,
  }))
  assert.equal(extraScheme.status, 200)
  assert.equal(database.calls[0].name, 'claim_stripe_webhook_event')
})

test('a signature outside the timestamp tolerance is refused', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database } = installStubs()

  const payload = JSON.stringify(stripeEvent({
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const nowSeconds = Math.floor(Date.now() / 1000)

  // Stripe's default tolerance is 300 s; the route passes no override, so a
  // captured delivery replayed hours later is refused even though its signature
  // is arithmetically correct for those bytes.
  for (const offset of [-86400, -3600, -301]) {
    const response = await POST(webhookRequest(payload, {
      'stripe-signature': signed(payload, { timestamp: nowSeconds + offset }),
    }))
    assert.equal(response.status, 400, `offset ${offset}`)
    assert.deepEqual(await response.json(), { error: 'Invalid webhook signature' }, `offset ${offset}`)
  }
  assert.deepEqual(database.calls, [])

  // Inside the window the same bytes are accepted, so the rejections above are
  // the timestamp and not something else about the fixture.
  const fresh = await POST(webhookRequest(payload, {
    'stripe-signature': signed(payload, { timestamp: nowSeconds - 299 }),
  }))
  assert.equal(fresh.status, 200)

  // MEASURED SDK PROPERTY, recorded because it is easy to assume otherwise: the
  // tolerance is ONE-SIDED. A future-dated timestamp is accepted. That is not a
  // hole (forging one still requires the signing secret), but a test that
  // expects symmetric rejection would be wrong, and an operator debugging clock
  // skew needs to know only the PAST direction is enforced.
  const future = await POST(webhookRequest(payload, {
    'stripe-signature': signed(payload, { timestamp: nowSeconds + 86400 }),
  }))
  assert.equal(future.status, 200)
})

// ---------------------------------------------------------------------------
// Past the boundary: inbox outcomes
// ---------------------------------------------------------------------------

test('a valid signature over an unhandled event type acknowledges with zero writes', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database, stripeCalls, posthogEvents } = installStubs()

  // `customer.updated` is signed, genuine, and NOT one of the six handled
  // types. It must be acknowledged (so Stripe stops retrying) and must not
  // touch business state.
  const payload = JSON.stringify(stripeEvent({
    id: 'evt_localFixtureUnhandled',
    type: 'customer.updated',
    object: { id: CUSTOMER_ID, object: 'customer' },
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 200)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  assert.deepEqual(await response.json(), { received: true })

  // Exactly the inbox lifecycle: claim, the closing renewal, completion.
  assert.deepEqual(database.rpcNames(), [
    'claim_stripe_webhook_event',
    'renew_stripe_webhook_event_lease',
    'mark_stripe_webhook_event_processed',
  ])
  const claim = database.calls[0]
  assert.deepEqual(claim.args, {
    p_event_id: 'evt_localFixtureUnhandled',
    p_event_type: 'customer.updated',
    p_lease_owner: claim.args.p_lease_owner,
    p_lease_seconds: 60,
  })
  assert.match(claim.args.p_lease_owner, /^[0-9a-f-]{36}$/)
  // ZERO business writes: no snapshot CAS, no table select, no Stripe call, no
  // analytics event.
  assert.equal(database.calls.filter(call => call.kind === 'from').length, 0)
  assert.equal(
    database.rpcNames().filter(name => name.startsWith('compare_and_set')).length,
    0,
  )
  assert.deepEqual(stripeCalls, [])
  assert.deepEqual(posthogEvents, [])
})

test('a claim answered busy is a 503 with Retry-After 5, never a false acknowledgement', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database } = installStubs({ claim: 'busy' })

  const payload = JSON.stringify(stripeEvent({
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 503)
  assert.equal(response.headers.get('retry-after'), '5')
  assert.equal(response.headers.get('cache-control'), 'no-store')
  assert.deepEqual(await response.json(), { error: 'Webhook is already processing' })
  // A concurrent delivery must not start a lease heartbeat or apply anything.
  assert.deepEqual(database.rpcNames(), ['claim_stripe_webhook_event'])
})

test('a claim answered processed is an idempotent duplicate acknowledgement', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database, stripeCalls } = installStubs({ claim: 'processed' })

  const payload = JSON.stringify(stripeEvent({
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), { received: true, duplicate: true })
  // F2: replay does no work at all beyond the claim.
  assert.deepEqual(database.rpcNames(), ['claim_stripe_webhook_event'])
  assert.deepEqual(stripeCalls, [])
})

test('an invalid claim outcome fails closed rather than acknowledging', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  installStubs({ claim: 'something_else' })

  const payload = JSON.stringify(stripeEvent({
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 500)
  assert.deepEqual(await response.json(), { error: 'Webhook processing failed' })
})

test('an unresolvable subscription period becomes a reviewable failure, not a persisted null', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  // The S6 failure mode: Stripe returns a subscription whose item-level
  // period boundaries are absent (or no items at all). Persisting nulls would
  // corrupt the billing account, so stripe-state.mjs raises
  // StripeSubscriptionPeriodError and the route routes it to manual review.
  const { database, posthogEvents } = installStubs({
    subscription: subscriptionObject({ items: { object: 'list', data: [] } }),
  })

  const payload = JSON.stringify(stripeEvent({
    id: 'evt_localFixturePeriod',
    type: 'customer.subscription.updated',
    object: subscriptionObject({ items: { object: 'list', data: [] } }),
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 500)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  // The distinct body is what tells an operator this is a review item and not a
  // transient failure.
  assert.deepEqual(await response.json(), { error: 'Webhook requires manual billing review' })

  // No snapshot was written...
  assert.equal(
    database.rpcNames().filter(name => name.startsWith('compare_and_set')).length,
    0,
  )
  assert.deepEqual(posthogEvents, [])
  // ...and the row carries the concrete reason plus the subscription id, so the
  // failure is resolvable instead of opaque.
  const failure = database.calls.find(call => call.name === 'fail_stripe_webhook_event')
  assert.ok(failure, 'fail_stripe_webhook_event must be called')
  assert.equal(failure.args.p_event_id, 'evt_localFixturePeriod')
  assert.equal(
    failure.args.p_error,
    'Stripe subscription period unresolved (subscription has no items) '
    + `requires manual review: ${SUBSCRIPTION_ID}`,
  )
  assert.ok(failure.args.p_error.length <= 480)
  assert.equal(failure.args.p_lease_owner, database.calls[0].args.p_lease_owner)
  // The same reason reaches the logs, with the subscription id, so the failure
  // is diagnosable without a database query.
  const logged = logs.error.filter(line => line.startsWith('Stripe webhook processing failed'))
  assert.ok(logged.some(line => line.includes('StripeSubscriptionPeriodError')), logged.join('|'))

  // The other malformed shape: an item present but without boundaries.
  database.reset()
  const boundaryless = subscriptionObject({
    items: { object: 'list', data: [{ id: 'si_x', object: 'subscription_item' }] },
  })
  installStubs({ subscription: boundaryless })
  const secondPayload = JSON.stringify(stripeEvent({
    id: 'evt_localFixturePeriodTwo',
    type: 'customer.subscription.updated',
    object: boundaryless,
  }))
  const secondResponse = await POST(
    webhookRequest(secondPayload, { 'stripe-signature': signed(secondPayload) }),
  )
  assert.equal(secondResponse.status, 500)
  assert.deepEqual(await secondResponse.json(), { error: 'Webhook requires manual billing review' })
})

test('a well-formed subscription event writes exactly one canonical snapshot', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const { database, posthogEvents } = installStubs()

  const payload = JSON.stringify(stripeEvent({
    id: 'evt_localFixtureHappy',
    type: 'customer.subscription.updated',
    object: subscriptionObject(),
  }))
  const response = await POST(webhookRequest(payload, { 'stripe-signature': signed(payload) }))

  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), { received: true })

  const snapshot = database.calls.find(
    call => call.name === 'compare_and_set_stripe_subscription_snapshot_for_webhook',
  )
  assert.ok(snapshot, 'the snapshot CAS must run for a handled event')
  // The lease that writes carries THIS event's id (U10's property, proven here
  // through the route rather than the unit seam).
  assert.equal(snapshot.args.p_event_id, 'evt_localFixtureHappy')
  assert.equal(snapshot.args.p_lease_owner, database.calls[0].args.p_lease_owner)
  assert.equal(snapshot.args.p_expected_revision, 0)
  assert.equal(snapshot.args.p_organization_id, ORGANIZATION_ID)
  assert.equal(snapshot.args.p_stripe_subscription_id, SUBSCRIPTION_ID)
  // 604800000 ms apart, which is what keeps period_tracking_valid true.
  assert.equal(
    Date.parse(snapshot.args.p_current_period_end)
    - Date.parse(snapshot.args.p_current_period_start),
    604800000,
  )
  assert.equal(
    database.rpcNames().filter(n => n === 'compare_and_set_stripe_subscription_snapshot_for_webhook').length,
    1,
  )
  assert.equal(database.rpcNames().at(-1), 'mark_stripe_webhook_event_processed')
  assert.deepEqual(posthogEvents.map(event => event.event), ['billing_subscription_updated'])
})
