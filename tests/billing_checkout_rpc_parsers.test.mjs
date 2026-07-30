// U9 (docs/STRIPE_TEST_PLAN.md §2.1) — the four Checkout reservation RPC result
// parsers in src/lib/billing/supabase.ts fail CLOSED.
//
// These parsers sit between PostgREST and the money path. The reservation
// protocol's whole purpose is that at most one Checkout session can exist per
// (organization, generation); if a parser ever treated an unrecognised RPC body
// as `acquired`, the route would proceed to `sessions.create` without holding
// the lease and a customer could end up with two subscriptions. There is no
// safe default here, so every unknown shape must throw (the routes turn that
// into a 500) rather than be coerced.
//
// The seam: supabase.ts imports `server-only` and builds its client with
// `createClient` from '@supabase/supabase-js'. The loader shims `server-only`
// and the `@/*` alias and strips the types; this file stubs '@supabase/supabase-js'
// so `billingDatabase()` returns a recorder. The REAL parser code runs.
//
// Run: node --test tests/billing_checkout_rpc_parsers.test.mjs

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  loadModule,
  loaderUnavailable,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const TOKEN = 'db1f9d0c-6f3b-4d21-9a55-1f3c9a6f4e21'

// serviceSettings() requires both names; neither is used by the stubbed client.
process.env.SUPABASE_URL ||= 'https://local.invalid'
process.env.SUPABASE_SERVICE_ROLE_KEY ||= 'service-role-fixture'

let response = { data: null, error: null }
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

/** Answer the next RPC with `data` and run the parser under test. */
function answering(data, run) {
  response = { data, error: null }
  rpcCalls.length = 0
  return run()
}

const RESERVE_ARGS = {
  organizationId: ORGANIZATION_ID,
  stripeCustomerId: 'cus_localFixture',
  reservationToken: TOKEN,
  leaseSeconds: 300,
}
const PERSIST_ARGS = {
  organizationId: ORGANIZATION_ID,
  generation: 4,
  reservationToken: TOKEN,
  checkoutSessionId: 'cs_localFixture',
}
const ADVANCE_ARGS = { ...PERSIST_ARGS, expectedCheckoutSessionId: 'cs_localFixture', leaseSeconds: 300 }
const RELEASE_ARGS = {
  organizationId: ORGANIZATION_ID,
  generation: 4,
  reservationToken: TOKEN,
  manualReview: false,
}

/** Bodies that are not a JSON object at all. Every parser must reject these. */
const NON_OBJECT_BODIES = [null, undefined, [], [{ ok: true, code: 'acquired' }], 'acquired', 42, 0, false, true]

test('reserve rejects every non-object body', { skip }, async () => {
  const { reserveBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')
  for (const data of NON_OBJECT_BODIES) {
    await assert.rejects(
      answering(data, () => reserveBillingCheckoutGeneration(RESERVE_ARGS)),
      /Invalid billing Checkout reservation result/,
      JSON.stringify(data ?? String(data)),
    )
  }
  // A PostgREST transport error is never parsed into a status.
  response = { data: { ok: true, code: 'acquired', mode: 'create_or_recover', generation: 1, checkout_session_id: null }, error: new Error('permission denied for function') }
  await assert.rejects(reserveBillingCheckoutGeneration(RESERVE_ARGS), /permission denied/)
})

test('reserve accepts only the three declared modes', { skip }, async () => {
  const { reserveBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')

  for (const mode of ['create_or_recover', 'recover_only']) {
    const result = await answering(
      { ok: true, code: 'acquired', mode, generation: 7, checkout_session_id: null },
      () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
    )
    assert.deepEqual(result, { status: 'acquired', mode, generation: 7, checkoutSessionId: null })
  }
  const persisted = await answering(
    { ok: true, code: 'acquired', mode: 'inspect_persisted', generation: 7, checkout_session_id: 'cs_x' },
    () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
  )
  assert.deepEqual(persisted, {
    status: 'acquired', mode: 'inspect_persisted', generation: 7, checkoutSessionId: 'cs_x',
  })

  for (const mode of [
    undefined, null, '', 'CREATE_OR_RECOVER', 'create_or_recover ', 'create', 'recover',
    'inspect', 42, true, ['create_or_recover'], { mode: 'create_or_recover' },
  ]) {
    await assert.rejects(
      answering(
        { ok: true, code: 'acquired', mode, generation: 7, checkout_session_id: null },
        () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
      ),
      /Invalid billing Checkout reservation mode/,
      JSON.stringify(mode ?? String(mode)),
    )
  }
})

test('reserve rejects a session identity that is neither null nor a string', { skip }, async () => {
  const { reserveBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')
  for (const checkoutSessionId of [42, true, {}, [], ['cs_x']]) {
    await assert.rejects(
      answering(
        {
          ok: true, code: 'acquired', mode: 'create_or_recover',
          generation: 7, checkout_session_id: checkoutSessionId,
        },
        () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
      ),
      /Invalid billing Checkout session identity/,
      JSON.stringify(checkoutSessionId),
    )
  }
  // `undefined` is not `null`, and the guard is a strict !== null: an absent key
  // therefore lands on the identity error, not silently on null.
  await assert.rejects(
    answering(
      { ok: true, code: 'acquired', mode: 'create_or_recover', generation: 7 },
      () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
    ),
    /Invalid billing Checkout session identity/,
  )
  // inspect_persisted with an empty-string session is the specific dead end:
  // the mode asserts a persisted session exists.
  for (const empty of ['', null]) {
    await assert.rejects(
      answering(
        {
          ok: true, code: 'acquired', mode: 'inspect_persisted',
          generation: 7, checkout_session_id: empty,
        },
        () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
      ),
      /Persisted billing Checkout reservation has no session/,
      JSON.stringify(empty),
    )
  }
})

test('every parser rejects an invalid generation token', { skip }, async () => {
  const supabase = await loadModule('src/lib/billing/supabase.ts')
  const invalid = [undefined, null, 0, -1, 1.5, '4', Number.NaN, Infinity, 9007199254740993, true]
  for (const generation of invalid) {
    const label = JSON.stringify(generation ?? String(generation))
    await assert.rejects(
      answering(
        {
          ok: true, code: 'acquired', mode: 'create_or_recover',
          generation, checkout_session_id: null,
        },
        () => supabase.reserveBillingCheckoutGeneration(RESERVE_ARGS),
      ),
      /Invalid billing Checkout generation/,
      `reserve ${label}`,
    )
    await assert.rejects(
      answering(
        { ok: true, code: 'persisted', generation },
        () => supabase.persistBillingCheckoutSession(PERSIST_ARGS),
      ),
      /Invalid billing Checkout generation/,
      `persist ${label}`,
    )
    await assert.rejects(
      answering(
        { ok: true, code: 'advanced', generation },
        () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS),
      ),
      /Invalid billing Checkout generation/,
      `advance ${label}`,
    )
  }
})

test('busy is honoured only with an in-range integer retry, and never becomes acquired', { skip }, async () => {
  const { reserveBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')

  for (const retryAfterSeconds of [1, 5, 299, 300]) {
    const result = await answering(
      { ok: false, code: 'busy', retry_after_seconds: retryAfterSeconds },
      () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
    )
    assert.deepEqual(result, { status: 'busy', retryAfterSeconds })
  }

  // 301 is the documented out-of-range case: a Retry-After the route would
  // happily echo, from a body it does not recognise. It must throw, NOT degrade
  // to a shorter retry and certainly not to `acquired`.
  for (const retryAfterSeconds of [0, -1, 301, 3600, 1.5, '5', null, undefined, true, Number.NaN]) {
    await assert.rejects(
      answering(
        { ok: false, code: 'busy', retry_after_seconds: retryAfterSeconds },
        () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
      ),
      /Invalid billing Checkout reservation result/,
      JSON.stringify(retryAfterSeconds ?? String(retryAfterSeconds)),
    )
  }
})

test('reserve maps exactly three refusal codes and throws on anything else', { skip }, async () => {
  const { reserveBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')

  for (const code of ['occupied', 'manual_review', 'identity_mismatch']) {
    const result = await answering(
      { ok: false, code },
      () => reserveBillingCheckoutGeneration(RESERVE_ARGS),
    )
    assert.deepEqual(result, { status: code })
  }

  // THE central property: an unrecognised code must never fall through to
  // 'acquired'. Includes near-misses (case, whitespace, a code that belongs to a
  // sibling RPC) and an ok/code combination that disagrees with itself.
  const unrecognised = [
    { ok: false, code: 'unknown_code' },
    { ok: false, code: 'OCCUPIED' },
    { ok: false, code: 'occupied ' },
    { ok: false, code: 'persisted' },
    { ok: false, code: 'advanced' },
    { ok: false, code: 'acquired' },
    { ok: false, code: null },
    { ok: false },
    { ok: true, code: 'occupied' },
    { ok: true, code: 'busy', retry_after_seconds: 5 },
    { ok: true, code: 'ACQUIRED', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    { ok: true, code: 'acquired ', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    { ok: true, code: 'persisted', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    { ok: 'true', code: 'acquired', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    { ok: 1, code: 'acquired', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    { code: 'acquired', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    {},
  ]
  for (const data of unrecognised) {
    let outcome = null
    try {
      outcome = await answering(data, () => reserveBillingCheckoutGeneration(RESERVE_ARGS))
    } catch (error) {
      assert.match(error.message, /Invalid billing Checkout reservation/, JSON.stringify(data))
      continue
    }
    assert.fail(`${JSON.stringify(data)} was parsed as ${JSON.stringify(outcome)}`)
  }
})

test('persist and advance each accept only their own success code', { skip }, async () => {
  const supabase = await loadModule('src/lib/billing/supabase.ts')
  const shared = ['occupied', 'stale', 'session_conflict', 'manual_review']

  const persisted = await answering(
    { ok: true, code: 'persisted', generation: 9 },
    () => supabase.persistBillingCheckoutSession(PERSIST_ARGS),
  )
  assert.deepEqual(persisted, { status: 'persisted', generation: 9 })
  const advanced = await answering(
    { ok: true, code: 'advanced', generation: 10 },
    () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS),
  )
  assert.deepEqual(advanced, { status: 'advanced', generation: 10 })

  for (const code of shared) {
    assert.deepEqual(
      await answering({ ok: false, code }, () => supabase.persistBillingCheckoutSession(PERSIST_ARGS)),
      { status: code },
    )
    assert.deepEqual(
      await answering({ ok: false, code }, () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS)),
      { status: code },
    )
  }

  // A cross-wired success code must not be honoured: `advanced` from the persist
  // RPC (or vice versa) means the database and this code disagree about which
  // state machine step just happened.
  await assert.rejects(
    answering({ ok: true, code: 'advanced', generation: 9 },
      () => supabase.persistBillingCheckoutSession(PERSIST_ARGS)),
    /Invalid billing Checkout persistence result/,
  )
  await assert.rejects(
    answering({ ok: true, code: 'persisted', generation: 9 },
      () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS)),
    /Invalid billing Checkout advance result/,
  )
  // `busy` has no meaning for these two RPCs and must not be silently absorbed.
  await assert.rejects(
    answering({ ok: false, code: 'busy', retry_after_seconds: 5 },
      () => supabase.persistBillingCheckoutSession(PERSIST_ARGS)),
    /Invalid billing Checkout persistence result/,
  )
  await assert.rejects(
    answering({ ok: false, code: 'identity_mismatch' },
      () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS)),
    /Invalid billing Checkout advance result/,
  )
  for (const data of NON_OBJECT_BODIES) {
    await assert.rejects(
      answering(data, () => supabase.persistBillingCheckoutSession(PERSIST_ARGS)),
      /Invalid billing Checkout reservation result/,
      `persist ${JSON.stringify(data ?? String(data))}`,
    )
    await assert.rejects(
      answering(data, () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS)),
      /Invalid billing Checkout reservation result/,
      `advance ${JSON.stringify(data ?? String(data))}`,
    )
  }
})

test('release returns only a real boolean', { skip }, async () => {
  const { releaseBillingCheckoutGeneration } = await loadModule('src/lib/billing/supabase.ts')

  for (const data of [true, false]) {
    assert.equal(
      await answering(data, () => releaseBillingCheckoutGeneration(RELEASE_ARGS)),
      data,
    )
  }
  // A truthy non-boolean would make a FAILED release look successful, which is
  // exactly the case the checkout route's finally-block override exists for.
  for (const data of [null, undefined, 'true', 'false', 1, 0, {}, [], { ok: true }, 'TRUE']) {
    await assert.rejects(
      answering(data, () => releaseBillingCheckoutGeneration(RELEASE_ARGS)),
      /Invalid billing Checkout release result/,
      JSON.stringify(data ?? String(data)),
    )
  }
})

test('each parser calls its own RPC with the whole fenced parameter set', { skip }, async () => {
  const supabase = await loadModule('src/lib/billing/supabase.ts')

  await answering(
    { ok: true, code: 'acquired', mode: 'create_or_recover', generation: 1, checkout_session_id: null },
    () => supabase.reserveBillingCheckoutGeneration(RESERVE_ARGS),
  )
  assert.deepEqual(rpcCalls, [{
    name: 'reserve_billing_checkout_generation',
    args: {
      p_organization_id: ORGANIZATION_ID,
      p_stripe_customer_id: 'cus_localFixture',
      p_reservation_token: TOKEN,
      p_lease_seconds: 300,
    },
  }])

  await answering({ ok: true, code: 'persisted', generation: 5 },
    () => supabase.persistBillingCheckoutSession(PERSIST_ARGS))
  assert.deepEqual(rpcCalls, [{
    name: 'persist_billing_checkout_session',
    args: {
      p_organization_id: ORGANIZATION_ID,
      p_generation: 4,
      p_reservation_token: TOKEN,
      p_checkout_session_id: 'cs_localFixture',
    },
  }])

  await answering({ ok: true, code: 'advanced', generation: 5 },
    () => supabase.advanceBillingCheckoutGeneration(ADVANCE_ARGS))
  assert.deepEqual(rpcCalls, [{
    name: 'advance_billing_checkout_generation',
    args: {
      p_organization_id: ORGANIZATION_ID,
      p_generation: 4,
      p_reservation_token: TOKEN,
      p_expected_checkout_session_id: 'cs_localFixture',
      p_lease_seconds: 300,
    },
  }])

  await answering(true, () => supabase.releaseBillingCheckoutGeneration(RELEASE_ARGS))
  assert.deepEqual(rpcCalls, [{
    name: 'release_billing_checkout_generation',
    args: {
      p_organization_id: ORGANIZATION_ID,
      p_generation: 4,
      p_reservation_token: TOKEN,
      p_manual_review: false,
    },
  }])
})
