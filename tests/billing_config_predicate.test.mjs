// U7 (docs/STRIPE_TEST_PLAN.md §2.1) — truth table over the billing
// configuration predicate, plus the FIX-6 deployed-https condition and the FIX-9
// interval_count catalog condition.
//
// The predicate itself lives in src/lib/billing/config-predicate.mjs precisely so
// this file can execute it: src/lib/billing/config.ts imports `server-only`
// (a package Next.js aliases at build time and that is genuinely absent from
// node_modules) and reads process.env directly.
//
// The last section additionally executes the real config.ts through an
// import-hook shim (module.registerHooks + module.stripTypeScriptTypes, both
// available on CI's Node 22.17.1 without any command-line flag, so `npm test`
// runs them as-is). It self-skips on a Node without those APIs, and every
// contract it covers is also pinned by a text assertion that always runs.

import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const {
  billingConfigIsComplete,
  billingDeploymentIsPublic,
  billingPublicUrlIsSafe,
  billingWeeklyCapIsInRange,
  resolveBillingPublicUrl,
} = await import(new URL('../src/lib/billing/config-predicate.mjs', import.meta.url))

// Generated with `openssl rand -base64 32`-shaped entropy; passes
// recoverySecretIsStrong (3 character classes, 12+ distinct characters).
const STRONG_SECRET = 'Bv9_Qx2Lm7-Rp4Tz8Nc5Hs3Wk6Yf1Da0'

const DEPLOYED_ENV = Object.freeze({ NODE_ENV: 'production', VERCEL_ENV: 'production' })
const LOCAL_ENV = Object.freeze({ NODE_ENV: 'development' })

function baseConfig(overrides = {}) {
  return {
    enabled: true,
    secretKey: 'sk_test_51NotARealKey',
    webhookSecret: 'whsec_notARealSecret',
    recoverySecret: STRONG_SECRET,
    priceId: 'price_notARealPrice',
    meterEventName: 'brevitas_fee_microusd',
    publicUrl: 'https://brevitassystems.com',
    weeklyCapUsd: 1,
    ...overrides,
  }
}

test('base configuration is complete both deployed and local', () => {
  assert.equal(billingConfigIsComplete(baseConfig(), DEPLOYED_ENV), true)
  assert.equal(billingConfigIsComplete(baseConfig(), LOCAL_ENV), true)
  // Local development's working default is accepted only while not deployed.
  assert.equal(
    billingConfigIsComplete(baseConfig({ publicUrl: 'http://localhost:3000' }), LOCAL_ENV),
    true,
  )
})

test('every single-field mutation of the base configuration fails closed', () => {
  const mutations = [
    ['enabled false', { enabled: false }],
    ['secret key missing', { secretKey: '' }],
    ['webhook secret missing', { webhookSecret: '' }],
    ['recovery secret missing', { recoverySecret: '' }],
    ['recovery secret weak (repeated hex)', { recoverySecret: 'a'.repeat(64) }],
    ['recovery secret too short', { recoverySecret: 'short-recovery-secret_123' }],
    ['price id missing', { priceId: '' }],
    ['meter event name missing', { meterEventName: '' }],
    ['public url missing', { publicUrl: '' }],
    ['public url unparseable', { publicUrl: 'brevitassystems.com' }],
    ['public url plain http', { publicUrl: 'http://brevitassystems.com' }],
    ['weekly cap zero', { weeklyCapUsd: 0 }],
    ['weekly cap negative', { weeklyCapUsd: -1 }],
    ['weekly cap above maximum', { weeklyCapUsd: 100_001 }],
    ['weekly cap NaN', { weeklyCapUsd: Number.NaN }],
    ['weekly cap infinite', { weeklyCapUsd: Number.POSITIVE_INFINITY }],
    ['weekly cap as string', { weeklyCapUsd: '1' }],
  ]
  for (const [label, overrides] of mutations) {
    assert.equal(
      billingConfigIsComplete(baseConfig(overrides), DEPLOYED_ENV),
      false,
      `${label} must make billing configuration incomplete when deployed`,
    )
  }
  assert.equal(billingConfigIsComplete(null, DEPLOYED_ENV), false)
  assert.equal(billingConfigIsComplete(undefined, LOCAL_ENV), false)
})

test('weekly cap boundaries are exact', () => {
  assert.equal(billingWeeklyCapIsInRange(0), false)
  assert.equal(billingWeeklyCapIsInRange(0.0000001), true)
  assert.equal(billingWeeklyCapIsInRange(100_000), true)
  assert.equal(billingWeeklyCapIsInRange(100_001), false)
  // The same boundaries through the whole conjunction.
  for (const [cap, expected] of [[0, false], [0.0000001, true], [100_000, true], [100_001, false]]) {
    assert.equal(
      billingConfigIsComplete(baseConfig({ weeklyCapUsd: cap }), DEPLOYED_ENV),
      expected,
      `weekly cap ${cap}`,
    )
  }
})

test('deployment detection covers production builds and every Vercel environment', () => {
  assert.equal(billingDeploymentIsPublic({}), false)
  assert.equal(billingDeploymentIsPublic({ NODE_ENV: 'development' }), false)
  assert.equal(billingDeploymentIsPublic({ NODE_ENV: 'test' }), false)
  assert.equal(billingDeploymentIsPublic({ VERCEL_ENV: '' }), false)
  assert.equal(billingDeploymentIsPublic({ NODE_ENV: 'production' }), true)
  assert.equal(billingDeploymentIsPublic({ VERCEL_ENV: 'production' }), true)
  // Preview URLs are public and Stripe redirects back to them for real.
  assert.equal(billingDeploymentIsPublic({ VERCEL_ENV: 'preview' }), true)
  assert.equal(billingDeploymentIsPublic({ NODE_ENV: 'development', VERCEL_ENV: 'development' }), true)
})

test('public url is https-only once deployed and loopback-only locally', () => {
  // FIX-6 / CF-1: the exact failure this closes is a missed Vercel variable
  // sending a paying customer's Checkout success_url to a developer laptop.
  assert.equal(billingPublicUrlIsSafe('http://localhost:3000', DEPLOYED_ENV), false)
  assert.equal(billingPublicUrlIsSafe('http://127.0.0.1:3000', DEPLOYED_ENV), false)
  assert.equal(billingPublicUrlIsSafe('http://brevitassystems.com', DEPLOYED_ENV), false)
  assert.equal(billingPublicUrlIsSafe('https://brevitassystems.com', DEPLOYED_ENV), true)

  assert.equal(billingPublicUrlIsSafe('http://localhost:3000', LOCAL_ENV), true)
  assert.equal(billingPublicUrlIsSafe('http://127.0.0.1:3000', LOCAL_ENV), true)
  assert.equal(billingPublicUrlIsSafe('https://brevitassystems.com', LOCAL_ENV), true)
  // Loopback tolerance is for loopback only, never any other plain-http host.
  assert.equal(billingPublicUrlIsSafe('http://brevitassystems.com', LOCAL_ENV), false)
  // The pre-fix predicate accepted any protocol on a loopback hostname.
  assert.equal(billingPublicUrlIsSafe('ftp://localhost', LOCAL_ENV), false)

  for (const environment of [DEPLOYED_ENV, LOCAL_ENV]) {
    assert.equal(billingPublicUrlIsSafe('', environment), false)
    assert.equal(billingPublicUrlIsSafe('localhost:3000', environment), false)
    assert.equal(billingPublicUrlIsSafe(undefined, environment), false)
    assert.equal(billingPublicUrlIsSafe(null, environment), false)
    assert.equal(billingPublicUrlIsSafe(3000, environment), false)
  }
})

test('a deployed surface gets no localhost default for BREVITAS_PUBLIC_URL', () => {
  assert.equal(resolveBillingPublicUrl({}), 'http://localhost:3000')
  assert.equal(resolveBillingPublicUrl({ NODE_ENV: 'development' }), 'http://localhost:3000')
  assert.equal(resolveBillingPublicUrl({ NODE_ENV: 'production' }), '')
  assert.equal(resolveBillingPublicUrl({ VERCEL_ENV: 'preview' }), '')
  assert.equal(
    resolveBillingPublicUrl({ BREVITAS_PUBLIC_URL: 'https://brevitassystems.com/' }),
    'https://brevitassystems.com',
  )
  assert.equal(
    resolveBillingPublicUrl({ BREVITAS_PUBLIC_URL: '  https://brevitassystems.com  ' }),
    'https://brevitassystems.com',
  )
  assert.equal(resolveBillingPublicUrl({ BREVITAS_PUBLIC_URL: '   ', VERCEL_ENV: 'production' }), '')
  // An empty resolution can never satisfy the predicate, so a missed Vercel
  // variable 503s Checkout instead of redirecting to a laptop.
  assert.equal(billingPublicUrlIsSafe(resolveBillingPublicUrl(DEPLOYED_ENV), DEPLOYED_ENV), false)
})

test('the predicate module stays loadable outside a Next.js build', () => {
  const predicate = read('src/lib/billing/config-predicate.mjs')
  assert.doesNotMatch(predicate, /import ['"]server-only['"]/)
  assert.doesNotMatch(predicate, /from ['"]stripe['"]/)
  assert.match(predicate, /environment\.NODE_ENV === 'production' \|\| Boolean\(environment\.VERCEL_ENV\)/)
})

test('config.ts delegates to the injectable predicate and keeps no localhost default', () => {
  const config = read('src/lib/billing/config.ts')
  assert.match(config, /publicUrl: resolveBillingPublicUrl\(process\.env\)/)
  assert.match(config, /billingConfigIsComplete\(config, process\.env\)/)
  assert.match(config, /recoverySecretIsStrong\(config\.recoverySecret\)/)
  // The unconditional localhost fallback (and the inline URL predicate that
  // accepted it in production) must not come back.
  assert.doesNotMatch(config, /'http:\/\/localhost:3000'/)
  assert.doesNotMatch(config, /\['localhost', '127\.0\.0\.1'\]/)
})

test('the catalog validator rejects a multi-week recurring interval', () => {
  // FIX-9 / WR-3 text pin: kept alongside the executable coverage below because
  // that section skips on Nodes without TypeScript stripping.
  const config = read('src/lib/billing/config.ts')
  assert.match(config, /\(price\.recurring\?\.interval_count \?\? 1\) !== 1/)
})

// ---------------------------------------------------------------------------
// Executable coverage of the real src/lib/billing/config.ts.
// ---------------------------------------------------------------------------

const STUB_SOURCES = {
  'server-only': 'export {}',
  stripe: `
    export default class Stripe {
      constructor(key, options) {
        globalThis.__brevitasStripeStub.constructions.push({ key, options })
      }
      prices = {
        retrieve: async id => globalThis.__brevitasStripeStub.price(id),
      }
      billing = {
        meters: { retrieve: async id => globalThis.__brevitasStripeStub.meter(id) },
      }
    }
  `,
}

const skipExecutable = typeof registerHooks === 'function' &&
  typeof stripTypeScriptTypes === 'function'
  ? false
  : 'requires module.registerHooks and module.stripTypeScriptTypes (Node >= 22.15)'

if (skipExecutable === false) {
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (Object.hasOwn(STUB_SOURCES, specifier)) {
        return { url: `brevitas-stub:${specifier}`, format: 'module', shortCircuit: true }
      }
      if (specifier.startsWith('@/')) {
        // Mirror the tsconfig "@/*": ["./src/*"] alias, including Next.js's
        // extensionless module specifiers.
        const base = resolve(root, 'src', specifier.slice(2))
        const candidate = ['', '.ts', '.mts', '.mjs', '.js']
          .map(extension => `${base}${extension}`)
          .find(path => existsSync(path))
        if (!candidate) throw new Error(`unresolved alias specifier: ${specifier}`)
        return { url: pathToFileURL(candidate).href, format: 'module', shortCircuit: true }
      }
      return nextResolve(specifier, context)
    },
    load(url, context, nextLoad) {
      if (url.startsWith('brevitas-stub:')) {
        return {
          format: 'module',
          shortCircuit: true,
          source: STUB_SOURCES[url.slice('brevitas-stub:'.length)],
        }
      }
      if (url.endsWith('.ts')) {
        // Next.js compiles this file; here it is type-stripped in-process so the
        // suite needs no build step and no flag.
        return {
          format: 'module',
          shortCircuit: true,
          source: stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8'), { mode: 'strip' }),
        }
      }
      return nextLoad(url, context)
    },
  })
}

const ENV_NAMES = [
  'BREVITAS_BILLING_ENABLED',
  'STRIPE_SECRET_KEY',
  'STRIPE_WEBHOOK_SECRET',
  'BILLING_RECOVERY_SECRET',
  'STRIPE_PRICE_ID',
  'STRIPE_METER_EVENT_NAME',
  'BREVITAS_PUBLIC_URL',
  'BREVITAS_BILLING_WEEKLY_CAP_USD',
  'NODE_ENV',
  'VERCEL_ENV',
]

function applyEnvironment(values) {
  const previous = {}
  for (const name of ENV_NAMES) {
    previous[name] = process.env[name]
    if (values[name] === undefined) delete process.env[name]
    else process.env[name] = values[name]
  }
  return () => {
    for (const name of ENV_NAMES) {
      if (previous[name] === undefined) delete process.env[name]
      else process.env[name] = previous[name]
    }
  }
}

const COMPLETE_ENV = {
  BREVITAS_BILLING_ENABLED: 'true',
  STRIPE_SECRET_KEY: 'sk_test_51NotARealKey',
  STRIPE_WEBHOOK_SECRET: 'whsec_notARealSecret',
  BILLING_RECOVERY_SECRET: STRONG_SECRET,
  STRIPE_PRICE_ID: 'price_notARealPrice',
  BREVITAS_BILLING_WEEKLY_CAP_USD: '1',
  BREVITAS_PUBLIC_URL: 'https://brevitassystems.com',
  VERCEL_ENV: 'production',
  NODE_ENV: 'production',
}

test('billingIsConfigured() rejects a deployed surface with no BREVITAS_PUBLIC_URL', { skip: skipExecutable }, async () => {
  const { billingConfig, billingIsConfigured } = await import('@/lib/billing/config')

  let restore = applyEnvironment(COMPLETE_ENV)
  try {
    assert.equal(billingConfig().publicUrl, 'https://brevitassystems.com')
    assert.equal(billingIsConfigured(), true)
  } finally {
    restore()
  }

  // The exact production accident: the Vercel variable was never set.
  restore = applyEnvironment({ ...COMPLETE_ENV, BREVITAS_PUBLIC_URL: undefined })
  try {
    assert.equal(billingConfig().publicUrl, '')
    assert.equal(billingIsConfigured(), false)
  } finally {
    restore()
  }

  // Set, but left at the local development value.
  restore = applyEnvironment({ ...COMPLETE_ENV, BREVITAS_PUBLIC_URL: 'http://localhost:3000' })
  try {
    assert.equal(billingIsConfigured(), false)
  } finally {
    restore()
  }

  // Same variable state on a developer laptop: still configured.
  restore = applyEnvironment({
    ...COMPLETE_ENV,
    BREVITAS_PUBLIC_URL: undefined,
    NODE_ENV: 'development',
    VERCEL_ENV: undefined,
  })
  try {
    assert.equal(billingConfig().publicUrl, 'http://localhost:3000')
    assert.equal(billingIsConfigured(), true)
  } finally {
    restore()
  }
})

test('validateStripeCatalog() rejects interval_count other than 1', { skip: skipExecutable }, async () => {
  const { validateStripeCatalog } = await import('@/lib/billing/config')
  const restore = applyEnvironment(COMPLETE_ENV)

  function price(overrides = {}) {
    return {
      active: true,
      type: 'recurring',
      currency: 'usd',
      billing_scheme: 'per_unit',
      unit_amount_decimal: '0.0001',
      recurring: { interval: 'week', usage_type: 'metered', meter: 'mtr_x', ...overrides },
    }
  }

  globalThis.__brevitasStripeStub = {
    constructions: [],
    price: () => price({ interval_count: 2 }),
    meter: () => ({ status: 'active', event_name: 'brevitas_fee_microusd' }),
  }

  try {
    // A fortnightly price is what actually slips past every other condition.
    await assert.rejects(
      validateStripeCatalog(),
      /does not match the Brevitas micro-dollar metered billing contract/,
    )
    // A failed validation is not memoized, so the next call re-reads the price.
    globalThis.__brevitasStripeStub.price = () => price({ interval_count: 3 })
    await assert.rejects(
      validateStripeCatalog(),
      /does not match the Brevitas micro-dollar metered billing contract/,
    )
    // Stripe omits interval_count on a plain 1-week price: `?? 1` must accept it.
    globalThis.__brevitasStripeStub.price = () => price()
    await validateStripeCatalog()
    assert.equal(globalThis.__brevitasStripeStub.constructions.length > 0, true)
  } finally {
    restore()
    delete globalThis.__brevitasStripeStub
  }
})
