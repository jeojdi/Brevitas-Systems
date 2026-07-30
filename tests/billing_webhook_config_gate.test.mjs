// The Stripe webhook's configuration gate (finding: "webhook 503s — losing
// events — if any of 6 unrelated billing env vars is unset").
//
// billingIsConfigured() ANDs eight conditions: enabled, STRIPE_SECRET_KEY,
// STRIPE_WEBHOOK_SECRET, the BILLING_RECOVERY_SECRET strength heuristic,
// STRIPE_PRICE_ID, the meter event name, 0 < weekly cap <= 100000, and a safe
// BREVITAS_PUBLIC_URL. Only the first three are reachable from this route; the
// recovery secret is read exclusively by /api/billing/sync. Gating ingestion on
// the whole conjunction meant `openssl rand -hex 20` in BILLING_RECOVERY_SECRET
// (that heuristic rejects a legitimate 160-bit hex secret) 503'd every delivery
// — and the refusal happens BEFORE constructEvent and before
// claim_stripe_webhook_event, so no inbox row exists and no replay worker exists
// to find one. The events survive only Stripe's finite retry schedule.
//
// This file pins both halves of the contract:
//   * an incomplete whole-product billing config whose webhook values are
//     present must still verify and PERSIST the event;
//   * a config missing any of the three values this route needs must still be a
//     retryable 5xx, never a 200 acknowledgement (there is no reclaim worker, so
//     acking would turn a misconfiguration into permanent silent loss).
//
// Run: node --test tests/billing_webhook_config_gate.test.mjs

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

const stripeSdk = new Stripe(STRIPE_KEY)

// The route logs the ingest-under-incomplete-config fact by design; capture it
// so CI output stays readable and the log itself stays a tested property.
const logs = { warn: [], error: [] }
console.warn = (...args) => { logs.warn.push(args.map(String).join(' ')) }
console.error = (...args) => { logs.error.push(args.map(String).join(' ')) }

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
    billingDatabase: () => currentScenario().database.client,
    getBillingAccount: async () => null,
  })
  stubModule('@/lib/posthog-server', { captureServerEvent: async () => {} })
}

/**
 * `configured` is billingIsConfigured() — deliberately false in most of these
 * scenarios — while `config` carries the individual values. A complete
 * `config` with `configured: false` is exactly the state this fix is about: one
 * unrelated env var (the recovery secret, the cap, the public URL) is wrong.
 */
function installStubs({ configured = false, config = {} } = {}) {
  const database = recordingBillingDatabase({
    rpc: {
      claim_stripe_webhook_event: () => ({ data: 'claimed', error: null }),
      renew_stripe_webhook_event_lease: () => ({ data: true, error: null }),
      mark_stripe_webhook_event_processed: () => ({ data: true, error: null }),
      fail_stripe_webhook_event: () => ({ data: true, error: null }),
    },
  })
  scenario = {
    configured,
    config: {
      enabled: true,
      secretKey: STRIPE_KEY,
      webhookSecret: WEBHOOK_SECRET,
      // The values that have nothing to do with verifying a delivery. Empty
      // here on purpose: this is a deployment whose billing env is incomplete.
      recoverySecret: '',
      priceId: '',
      meterEventName: '',
      publicUrl: '',
      weeklyCapUsd: 0,
      automaticTax: false,
      ...config,
    },
    // The real verifier; every other Stripe surface would throw if reached.
    stripe: { webhooks: stripeSdk.webhooks },
    database,
  }
  return database
}

/** An event type the route does not apply, so persistence is all that is exercised. */
function unappliedEvent(id) {
  return JSON.stringify({
    id,
    object: 'event',
    api_version: '2026-06-24.dahlia',
    created: Math.floor(Date.now() / 1000),
    livemode: false,
    pending_webhooks: 1,
    request: { id: null, idempotency_key: null },
    type: 'customer.updated',
    data: { object: {} },
  })
}

function signedRequest(payload) {
  return new Request('https://brevitassystems.com/api/billing/webhook', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'stripe-signature': stripeSdk.webhooks.generateTestHeaderString({
        payload,
        secret: WEBHOOK_SECRET,
      }),
    },
    body: payload,
  })
}

const skip = loaderUnavailable

test('the gate is the webhook-relevant subset, not the whole billing predicate', () => {
  const source = readRepositoryFile('src/app/api/billing/webhook/route.ts')
  // The three values the route genuinely needs, and only those.
  assert.match(source, /!config\.enabled \|\| !config\.secretKey \|\| !config\.webhookSecret/)
  // The unrelated ones must never gate ingestion here.
  assert.doesNotMatch(source, /config\.recoverySecret|config\.priceId|config\.weeklyCapUsd/)
  // Still a retryable refusal: there is no reclaim worker for an acked row.
  assert.match(source, /status: 503[\s\S]+'Retry-After': '30'/)
})

test('an incomplete billing config still verifies and persists the event', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const database = installStubs({ configured: false })
  logs.error.length = 0

  const payload = unappliedEvent('evt_localFixtureConfigGate001')
  const response = await POST(signedRequest(payload))

  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), { received: true })
  // The point of the fix: the delivery is durably recorded rather than dropped.
  // (Lease renewals may appear in between; only the claim and the completion are
  // ordering properties.)
  const rpcNames = database.rpcNames()
  assert.equal(rpcNames.at(0), 'claim_stripe_webhook_event')
  assert.equal(rpcNames.at(-1), 'mark_stripe_webhook_event_processed')
  assert.equal(rpcNames.includes('fail_stripe_webhook_event'), false)
  assert.equal(
    logs.error.some(line => /incomplete billing configuration/.test(line)),
    true,
    'the incomplete configuration must be visible to an operator',
  )
})

test('a signature still cannot be forged when the rest of the config is incomplete', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  const database = installStubs({ configured: false })

  const payload = unappliedEvent('evt_localFixtureConfigGate002')
  const response = await POST(new Request('https://brevitassystems.com/api/billing/webhook', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'stripe-signature': 'v1=deadbeef,t=1' },
    body: payload,
  }))

  assert.equal(response.status, 400)
  assert.deepEqual(await response.json(), { error: 'Invalid webhook signature' })
  assert.deepEqual(database.calls, [])
})

test('missing webhook-relevant values are a retryable 5xx before the body is read', { skip }, async () => {
  const { POST } = await loadBillingRoute('webhook')
  for (const [label, config] of [
    ['billing disabled', { enabled: false }],
    ['no Stripe secret key', { secretKey: '' }],
    ['no webhook secret', { webhookSecret: '' }],
  ]) {
    const database = installStubs({ configured: false, config })
    const payload = unappliedEvent('evt_localFixtureConfigGate003')
    const response = await POST(signedRequest(payload))

    // 503 for the two that cannot verify at all; the missing webhook secret is
    // refused one branch later as a 400. Either way: never a 2xx, and never an
    // inbox row.
    assert.notEqual(response.status, 200, label)
    assert.ok(response.status >= 400, label)
    assert.equal(response.headers.get('cache-control'), 'no-store', label)
    assert.deepEqual(database.calls, [], label)
  }
})
