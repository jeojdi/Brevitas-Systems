import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  CheckoutSessionRecoveryError,
  checkoutIdempotencyKey,
  inspectPersistedCheckoutSession,
  selectRecoveredOpenCheckoutSession,
} from '../src/lib/billing/checkout-reservation.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const migrationPath = 'supabase/migrations/202607200014_billing_checkout_session_reservations.sql'

const organizationId = '40000000-0000-4000-8000-000000000001'
const customerId = 'cus_checkout_company'

function openSession(id, generation = 7) {
  return {
    id,
    customer: customerId,
    mode: 'subscription',
    status: 'open',
    url: `https://checkout.stripe.test/${id}`,
    metadata: {
      brevitas_organization_id: organizationId,
      brevitas_checkout_generation: String(generation),
    },
  }
}

test('Checkout idempotency is generation-stable and has no time bucket', () => {
  const first = checkoutIdempotencyKey(organizationId, 7)
  const afterFiveMinuteBoundary = checkoutIdempotencyKey(organizationId, 7)
  assert.equal(first, afterFiveMinuteBoundary)
  assert.equal(first, `brevitas-checkout-${organizationId}-generation-7`)
  assert.notEqual(first, checkoutIdempotencyKey(organizationId, 8))

  const route = read('src/app/api/billing/checkout/route.ts')
  assert.match(route, /checkoutIdempotencyKey\(organizationId, generation\)/)
  assert.doesNotMatch(route, /Date\.now\(\)|300_000|Math\.floor/)
})

test('open-session recovery is one bounded exact-generation lookup', () => {
  const match = openSession('cs_generation_7')
  assert.equal(selectRecoveredOpenCheckoutSession({
    page: {
      data: [match],
      has_more: false,
    },
    organizationId,
    customerId,
    generation: 7,
  }), match)

  for (const page of [
    { data: [match], has_more: true },
    { data: [match, openSession('cs_duplicate_generation')], has_more: false },
    { data: [{
      ...openSession('cs_legacy'),
      metadata: { brevitas_organization_id: organizationId },
    }], has_more: false },
    { data: [match, openSession('cs_other_generation', 6)], has_more: false },
    { data: Array.from({ length: 101 }, (_, index) => openSession(`cs_${index}`, index + 10)), has_more: false },
  ]) {
    assert.throws(
      () => selectRecoveredOpenCheckoutSession({
        page, organizationId, customerId, generation: 7,
      }),
      CheckoutSessionRecoveryError,
    )
  }

  const route = read('src/app/api/billing/checkout/route.ts')
  assert.match(route, /checkout\.sessions\.list\(\{[\s\S]+customer: customerId[\s\S]+status: 'open'[\s\S]+limit: 100/)
  assert.ok(
    route.indexOf('selectRecoveredOpenCheckoutSession')
      < route.indexOf('stripe.checkout.sessions.create'),
  )
})

test('persisted exact-ID inspection allows only legacy missing generation metadata', () => {
  const current = openSession('cs_persisted')
  assert.deepEqual(inspectPersistedCheckoutSession({
    session: current,
    expectedSessionId: current.id,
    organizationId,
    customerId,
    generation: 7,
  }), { status: 'open', url: current.url, legacyGeneration: false })

  const legacy = {
    ...current,
    metadata: { brevitas_organization_id: organizationId },
  }
  assert.equal(inspectPersistedCheckoutSession({
    session: legacy,
    expectedSessionId: legacy.id,
    organizationId,
    customerId,
    generation: 7,
  }).legacyGeneration, true)

  for (const session of [
    openSession('cs_persisted', 8),
    { ...current, customer: 'cus_other' },
    { ...current, mode: 'payment' },
    { ...current, status: 'unexpected' },
    { ...current, url: null },
  ]) {
    assert.throws(
      () => inspectPersistedCheckoutSession({
        session,
        expectedSessionId: current.id,
        organizationId,
        customerId,
        generation: 7,
      }),
      CheckoutSessionRecoveryError,
    )
  }
})

test('route recovers a crash before create and advances only an exact terminal persisted session', () => {
  const route = read('src/app/api/billing/checkout/route.ts')
  const inspectStart = route.indexOf("mode === 'inspect_persisted'")
  const openReturn = route.indexOf("inspection.status === 'open'", inspectStart)
  const advance = route.indexOf('advanceBillingCheckoutGeneration({', inspectStart)
  const lookup = route.indexOf('stripe.checkout.sessions.list({', advance)
  const create = route.indexOf('stripe.checkout.sessions.create({', lookup)
  const persist = route.indexOf('persistBillingCheckoutSession({', create)

  assert.ok(inspectStart >= 0 && inspectStart < openReturn && openReturn < advance)
  assert.ok(advance < lookup && lookup < create && create < persist)
  assert.match(route, /if \(mode === 'recover_only'\)[\s\S]+manualReview = true[\s\S]+checkoutManualReviewResponse/)
  assert.match(route, /finally \{[\s\S]+releaseBillingCheckoutGeneration\(\{/)
  assert.match(route, /returningCheckoutUrl && !released[\s\S]+checkoutBusyResponse/)
  assert.match(route, /persistence\.status === 'stale'[\s\S]+checkoutBusyResponse/)
  const persistedOpen = route.slice(openReturn, advance)
  assert.match(persistedOpen, /persistBillingCheckoutSession\(\{/)
  assert.ok(
    persistedOpen.indexOf('captureServerEvent({')
      < persistedOpen.indexOf('persistBillingCheckoutSession({'),
  )
  assert.ok(
    persistedOpen.indexOf('persistBillingCheckoutSession({')
      < persistedOpen.indexOf('Response.json({ url: inspection.url })'),
  )
})

test('database protocol fences generation token lease and account occupancy', () => {
  const migration = read(migrationPath)
  const helper = read('src/lib/billing/supabase.ts')

  assert.match(migration, /create table if not exists public\.billing_checkout_reservations/)
  assert.match(migration, /organization_id uuid primary key/)
  assert.match(migration, /state in \('reserved', 'persisted', 'manual_review'\)/)
  assert.match(migration, /generation_started_at \+ interval '23 hours'/)
  assert.match(migration, /v_mode := 'recover_only'/)
  assert.match(migration, /v_reservation\.generation <> p_generation/g)
  assert.match(migration, /v_reservation\.reservation_token is distinct from p_reservation_token/g)
  assert.match(migration, /v_reservation\.lease_expires_at <= v_now/g)
  assert.match(migration, /checkout_session_id <> p_checkout_session_id/)
  assert.match(migration, /subscription_status in \([\s\S]+past_due[\s\S]+incomplete/)
  assert.match(migration, /update public\.billing_checkout_reservations[\s\S]+update public\.billing_accounts[\s\S]+checkout_session_id = p_checkout_session_id/)
  assert.match(migration, /generation = v_next_generation[\s\S]+checkout_session_id = null/)
  assert.match(migration, /lease_expires_at > v_now/)

  for (const rpc of [
    'reserve_billing_checkout_generation',
    'persist_billing_checkout_session',
    'advance_billing_checkout_generation',
    'release_billing_checkout_generation',
  ]) {
    assert.match(migration, new RegExp(`revoke all on function public\\.${rpc}`))
    assert.match(migration, new RegExp(`grant execute on function public\\.${rpc}`))
    assert.match(helper, new RegExp(`['"]${rpc}['"]`))
  }
  assert.doesNotMatch(helper, /saveBillingCheckoutSessionIdentity/)
})

test('PostgreSQL fixture is wired for stale-token takeover conflict and occupancy cases', () => {
  const assertions = read('scripts/ci/migration-checkout-session-reservation-assertions.sql')
  const runner = read('scripts/ci/run-migration-tests.sh')

  assert.match(assertions, /concurrent Checkout token was not reported busy/)
  assert.match(assertions, /lease takeover changed the Checkout generation/)
  assert.match(assertions, /stale Checkout token persisted a session/)
  assert.match(assertions, /stale Checkout token released a generation/)
  assert.match(assertions, /expired generation was allowed to create again/)
  assert.match(assertions, /occupying subscription did not block Checkout persistence/)
  assert.match(runner, /migration-checkout-session-reservation-assertions\.sql/)
})
