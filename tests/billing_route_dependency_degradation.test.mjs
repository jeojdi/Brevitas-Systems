// DEPLOY-ORDER DEGRADATION for the web tier.
//
// Production Postgres has no migration ledger and its migrations are applied by
// hand, one file at a time, while Railway and Vercel deploy on push. Code-first
// is therefore the DEFAULT ordering, not an accident: for some window after
// every push, a route is live against a database that has not yet learned the
// RPC it wants. A route that answers that window with a 500 takes a page down
// for a schema state that is expected and temporary.
//
// This file is the sweep, kept executable so it cannot rot:
//
//   1. A SCANNER pairs every RPC the web tier calls with the migration that
//      first defines it. Any RPC introduced at or after the 202607280013 cutoff
//      -- the set that is NOT applied to production yet -- must be called from a
//      file that carries a registered degrade guard. A lane that wires a new
//      post-cutoff RPC into a route without one is named by this test.
//   2. The /api/billing/sync degrade is then EXECUTED, because the scanner can
//      only see that a guard exists, not that it works.
//
// The other four billing routes are covered where their contracts live:
// status by tests/billing_status_settlement_repoint.test.mjs (the summary RPC
// degrades to the stage-one nulls behind settlement_pending), webhook by
// tests/billing_webhook_config_gate.test.mjs (the config gate is narrowed to
// the three values the route cannot work without, and an incomplete config
// still verifies the signature and persists the event before any dependent
// work), and checkout/portal by tests/billing_control_rate_limit.test.mjs.
//
// Run: node --test tests/billing_route_dependency_degradation.test.mjs

import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  loadBillingRoute,
  loaderUnavailable,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

/**
 * Migrations from here on are the ones production has NOT been given yet.
 * 202607280013 is the settlement writer; everything above it followed in the
 * same unapplied batch.
 */
const UNAPPLIED_FROM = '202607280013'

/** Every web-tier file that can reach PostgREST. */
const CALLERS = [
  'src/app/api/billing/checkout/route.ts',
  'src/app/api/billing/portal/route.ts',
  'src/app/api/billing/status/route.ts',
  'src/app/api/billing/sync/route.ts',
  'src/app/api/billing/webhook/route.ts',
  'src/lib/billing/supabase.ts',
  'src/lib/billing/canonical-persistence.ts',
  'src/lib/waitlist-server.ts',
]

/**
 * The degrade a file must demonstrate before it is allowed to call an RPC that
 * production does not have yet. Each pattern is the guard's own load-bearing
 * line, so deleting the guard fails this test rather than silently passing.
 */
const DEGRADE_GUARDS = {
  'src/app/api/billing/status/route.ts': /if \(!summaryUnavailableError\(error\)\) throw error;/,
  'src/app/api/billing/sync/route.ts': /if \(!dependencyUnavailable\(error\)\) throw error;/,
  'src/lib/waitlist-server.ts': /logNetworkBudgetDegradeOnce\(/,
}

/** The pairs the scanner must still be finding; a guard against a dead scan. */
const KNOWN_POST_CUTOFF_CALLS = [
  ['src/app/api/billing/status/route.ts', 'billing_period_settlement_summary'],
  ['src/lib/waitlist-server.ts', 'consume_waitlist_network_budget'],
]

/** rpc name -> the earliest migration file that defines it. */
function firstDefiningMigration() {
  const directory = resolve(root, 'supabase/migrations')
  const defined = new Map()
  for (const file of readdirSync(directory).filter(name => name.endsWith('.sql')).sort()) {
    const sql = readFileSync(resolve(directory, file), 'utf8')
    for (const [, name] of sql.matchAll(/create or replace function public\.([a-z0-9_]+)\s*\(/g)) {
      if (!defined.has(name)) defined.set(name, file)
    }
  }
  return defined
}

/** [{file, rpc}] for every `.rpc('name'` in the web tier. */
function webTierRpcCalls() {
  const calls = []
  for (const file of CALLERS) {
    for (const [, rpc] of read(file).matchAll(/\.rpc\(\s*'([a-z0-9_]+)'/g)) {
      calls.push({ file, rpc })
    }
  }
  return calls
}

test('the scanner sees the web tier and the migration set it depends on', () => {
  const defined = firstDefiningMigration()
  const calls = webTierRpcCalls()
  // Sanity: a broken regex here would make every assertion below vacuous.
  assert.ok(calls.length >= 12, `expected the web tier to call many RPCs, saw ${calls.length}`)
  assert.ok(defined.size >= 30, `expected many migration-defined functions, saw ${defined.size}`)
  for (const { file, rpc } of calls) {
    assert.ok(defined.has(rpc), `${file} calls public.${rpc}, which no migration defines`)
  }
  for (const [file, rpc] of KNOWN_POST_CUTOFF_CALLS) {
    assert.ok(
      calls.some(call => call.file === file && call.rpc === rpc),
      `${file} no longer calls ${rpc}; update this file's expectations`,
    )
    assert.ok(defined.get(rpc) >= UNAPPLIED_FROM, `${rpc} is no longer a post-cutoff RPC`)
  }
})

test('every RPC production does not have yet is called behind a degrade guard', () => {
  const defined = firstDefiningMigration()
  const unguarded = []
  for (const { file, rpc } of webTierRpcCalls()) {
    const migration = defined.get(rpc)
    if (migration < UNAPPLIED_FROM) continue
    const guard = DEGRADE_GUARDS[file]
    if (!guard || !guard.test(read(file))) {
      unguarded.push(`${file} calls public.${rpc} (${migration}) with no degrade guard`)
    }
  }
  assert.deepEqual(unguarded, [], unguarded.join('\n'))
})

test('the four non-status billing routes depend on no unapplied RPC at all', () => {
  const defined = firstDefiningMigration()
  // The confirmation this file exists to record: checkout, portal, sync and
  // webhook reach only migrations production already has, so their exposure to
  // the unapplied batch is nil rather than merely handled. If that ever stops
  // being true the previous test still requires a guard, but the reviewer wants
  // to know the fact changed.
  for (const route of ['checkout', 'portal', 'sync', 'webhook']) {
    const file = `src/app/api/billing/${route}/route.ts`
    for (const [, rpc] of read(file).matchAll(/\.rpc\(\s*'([a-z0-9_]+)'/g)) {
      assert.ok(
        defined.get(rpc) < UNAPPLIED_FROM,
        `${file} now calls public.${rpc} from ${defined.get(rpc)}`,
      )
    }
  }
})

test('the webhook verifies and persists before any dependent work', () => {
  const webhook = read('src/app/api/billing/webhook/route.ts')
  const at = needle => {
    const index = webhook.indexOf(needle)
    assert.notEqual(index, -1, needle)
    return index
  }
  // A Stripe event may never be lost to a config or RPC dependency. The only
  // pre-verification refusal is a RETRYABLE 5xx on the three values without
  // which the route cannot function at all, and the durable claim is the first
  // thing that happens after the signature is checked.
  assert.ok(at('!config.enabled || !config.secretKey || !config.webhookSecret') < at('constructEvent('))
  assert.ok(at('constructEvent(') < at('processWebhookInbox('))
  assert.match(webhook, /claim: \(\) => claimEvent\(event, leaseOwner\)/)
  assert.ok(at('claim: () => claimEvent') < at('apply: async'))
  // An incomplete-but-workable config is logged and INGESTED, never refused.
  assert.match(webhook, /Stripe webhook ingesting under an incomplete billing configuration/)
})

// ---------------------------------------------------------------------------
// The sync degrade, executed.
// ---------------------------------------------------------------------------

class BillingRecoveryAdmissionError extends Error {
  constructor(message = 'billing recovery admission failed') {
    super(message)
    this.name = 'BillingRecoveryAdmissionError'
  }
}

const USER_ID = '33333333-3333-4333-8333-333333333333'
const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const STRONG_SECRET = 'Bv7q_R3nX9pL2sK8wM4tC6zH5jF1yD0aQ'

let authorizationFailure = null
let resolutionFailure = null

if (loaderUnavailable === false) {
  stubModule('@/lib/billing/config', {
    billingConfig: () => ({ recoverySecret: STRONG_SECRET }),
  })
  stubModule('@/lib/billing/supabase', {
    BillingRecoveryAdmissionError,
    authenticatedBillingUser: async () => ({ id: USER_ID }),
    authorizeActiveBillingCompany: async () => {
      if (authorizationFailure) throw authorizationFailure
      return { ok: true, organizationId: ORGANIZATION_ID }
    },
    consumeBillingRecoveryAttempt: async () => ({ status: 'accepted' }),
    manuallyResolveBillingLedgerEntry: async () => {
      if (resolutionFailure) throw resolutionFailure
      return { ok: true, code: 'resolved', auditId: 907 }
    },
  })
}

const skip = loaderUnavailable

/** POST a well-formed manual-recovery request with billing switched on. */
async function post() {
  const { POST } = await loadBillingRoute('sync')
  const previous = process.env.BREVITAS_BILLING_ENABLED
  process.env.BREVITAS_BILLING_ENABLED = 'true'
  try {
    return await POST(new Request('https://brevitassystems.com/api/billing/sync', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-billing-recovery-secret': STRONG_SECRET,
      },
      body: JSON.stringify({
        entry_id: 41,
        resolution: 'reported',
        note: 'operator resolved by hand',
      }),
    }))
  } finally {
    if (previous === undefined) delete process.env.BREVITAS_BILLING_ENABLED
    else process.env.BREVITAS_BILLING_ENABLED = previous
  }
}

function driftError(code) {
  const error = new Error('schema drift fixture')
  error.code = code
  return error
}

test('a manual recovery whose RPC has not landed is a retryable 503, not a 500', { skip }, async () => {
  for (const code of ['PGRST202', '42883', '42P01', '42501']) {
    // The authorization RPC and the mutation RPC are the two calls that
    // rethrew the raw PostgREST error out of the handler entirely.
    for (const site of ['authorization', 'resolution']) {
      authorizationFailure = site === 'authorization' ? driftError(code) : null
      resolutionFailure = site === 'resolution' ? driftError(code) : null
      const response = await post()
      assert.equal(response.status, 503, `${site} ${code}`)
      assert.equal(response.headers.get('retry-after'), '5')
      assert.equal(response.headers.get('cache-control'), 'no-store')
      assert.deepEqual(await response.json(), { error: 'Billing recovery is temporarily unavailable' })
    }
  }
  // A schema-cache miss with no stable code degrades on its message too.
  authorizationFailure = new Error('Could not find the function public.x in the schema cache')
  resolutionFailure = null
  assert.equal((await post()).status, 503)
})

test('a genuine fault is never masked as retryable schema drift', { skip }, async () => {
  // The counterpart of the degrade: this route deliberately lets an unexpected
  // error propagate so the platform 500s and the failure stays visible
  // (tests/billing_sync_input_ladder.test.mjs pins the same property on the
  // admission path). Widening the catch would hide real breakage behind a
  // "temporarily unavailable" an operator would retry forever.
  authorizationFailure = null
  resolutionFailure = new Error('unexpected ledger fault')
  await assert.rejects(post(), /unexpected ledger fault/)
  // A constraint violation is a fault, not drift: 23505 must not degrade.
  resolutionFailure = driftError('23505')
  await assert.rejects(post(), /schema drift fixture/)
})

test('the happy path is unchanged by the outer catch', { skip }, async () => {
  authorizationFailure = null
  resolutionFailure = null
  const response = await post()
  assert.equal(response.status, 200)
  assert.equal(response.headers.get('cache-control'), 'no-store')
  const body = await response.json()
  assert.equal(body.resolved, true)
  assert.equal(body.audit_id, 907)
})
