// The seam's own test (docs/STRIPE_TEST_PLAN.md §2.2, "shared blocker A").
//
// tests/helpers/billing-route-loader.mjs is infrastructure that four other
// suites depend on, so its three claims are asserted here rather than assumed:
//
//   1. `server-only` really is absent from node_modules — i.e. the shim is
//      necessary, not defensive. If the package is ever added as a real
//      dependency this test says so, and the shim can be deleted.
//   2. All FIVE billing route handlers load and expose their handler plus their
//      segment config. This is the canary for a sixth route, or for a new import
//      in an existing route, silently becoming untestable again: an import of a
//      name this file does not stub is a link-time SyntaxError, not a skip.
//   3. The loader's guardrails fire — registering a stub after the real module
//      was already imported, or adding an export name later, both throw instead
//      of silently doing nothing (the mistake that makes a route answer 503 for
//      the wrong reason and turns a whole suite vacuous).
//
// Run: node --test tests/billing_route_loader.test.mjs

import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'

import {
  loadBillingRoute,
  loadModule,
  loaderUnavailable,
  patchStubModule,
  repositoryRoot,
  stubModule,
  stubbedSpecifiers,
} from './helpers/billing-route-loader.mjs'

test('the server-only shim is necessary: the package is genuinely not installed', () => {
  // Next aliases `server-only` at build time; it is not a runtime dependency.
  // src/lib/billing/{config,supabase,canonical-persistence}.ts all import it,
  // so without the shim none of them can be imported by node --test at all.
  assert.equal(existsSync(resolve(repositoryRoot, 'node_modules/server-only')), false)
  for (const file of [
    'src/lib/billing/config.ts',
    'src/lib/billing/supabase.ts',
    'src/lib/billing/canonical-persistence.ts',
  ]) {
    assert.ok(existsSync(resolve(repositoryRoot, file)), file)
  }
})

// Every export name the five routes (and their transitive @/ imports) bind. An
// omission here is a link-time SyntaxError, which is the intended canary.
const SUPABASE_EXPORTS = [
  'advanceBillingCheckoutGeneration',
  'authenticatedBillingUser',
  'authorizeActiveBillingCompany',
  'billingDatabase',
  'BillingControlAdmissionError',
  'BillingRecoveryAdmissionError',
  'consumeBillingControlAttempt',
  'consumeBillingRecoveryAttempt',
  'getBillingAccount',
  'manuallyResolveBillingLedgerEntry',
  'openBillingArrangementRequest',
  'persistBillingCheckoutSession',
  'releaseBillingCheckoutGeneration',
  'reserveBillingCheckoutGeneration',
  'saveBillingCustomerIdentity',
]

if (loaderUnavailable === false) {
  stubModule('next/server', { NextRequest: class NextRequest {} })
  stubModule('@/lib/billing/config', {
    billingConfig: () => ({ weeklyCapUsd: 1, publicUrl: 'https://brevitassystems.com' }),
    billingIsConfigured: () => false,
    getStripe: () => { throw new Error('no Stripe call is permitted in this suite') },
    validateStripeCatalog: async () => {},
  })
  stubModule('@/lib/billing/supabase', Object.fromEntries(
    SUPABASE_EXPORTS.map(name => [
      name,
      name.endsWith('Error') ? class extends Error {} : async () => null,
    ]),
  ))
  stubModule('@/lib/posthog-server', { captureServerEvent: async () => {} })
}

const skip = loaderUnavailable

test('all five billing route handlers load through the seam', { skip }, async () => {
  const expected = [
    ['checkout', 'POST'],
    ['portal', 'POST'],
    ['status', 'GET'],
    ['sync', 'POST'],
    ['webhook', 'POST'],
  ]
  for (const [name, method] of expected) {
    const route = await loadBillingRoute(name)
    assert.equal(typeof route[method], 'function', `${name}.${method}`)
    // Every billing route must pin the Node runtime: they use node:crypto and
    // the Stripe SDK, neither of which works on the edge runtime.
    assert.equal(route.runtime, 'nodejs', `${name}.runtime`)
  }
})

test('the alias resolver and the type stripper handle the real lib modules', { skip }, async () => {
  // A .ts module behind the `@/*` alias, imported by path.
  const persistence = await loadModule('src/lib/billing/canonical-persistence.ts')
  assert.equal(typeof persistence.compareAndSetSubscriptionSnapshot, 'function')
  assert.equal(typeof persistence.compareAndSetInvoiceSnapshot, 'function')
  // A plain .mjs module in the same directory, which must NOT be type-stripped.
  const state = await loadModule('src/lib/billing/stripe-state.mjs')
  assert.equal(typeof state.subscriptionPeriod, 'function')
  assert.ok(new state.StripeSubscriptionPeriodError('x') instanceof Error)
})

test('the loader refuses a stub registered after the real module was imported', { skip }, async () => {
  // maintenance-gate.mjs is imported for real by three of the routes above, so
  // stubbing it now is exactly the silent no-op the guard exists to catch.
  assert.throws(
    () => stubModule('@/lib/billing/maintenance-gate.mjs', { billingMaintenanceResponse: () => null }),
    /was called after the real module was already imported/,
  )
})

test('an export name cannot be added to an existing stub', { skip }, () => {
  assert.throws(
    () => patchStubModule('@/lib/posthog-server', { somethingNew: () => {} }),
    /ES module export names are static/,
  )
  // Replacing a declared name is fine, and takes effect for already-imported
  // consumers (ESM live bindings) — that is what lets one file run several
  // scenarios against one route module.
  const restore = patchStubModule('@/lib/posthog-server', { captureServerEvent: async () => 'replaced' })
  restore()
  assert.ok(stubbedSpecifiers().includes('@/lib/posthog-server'))
})
