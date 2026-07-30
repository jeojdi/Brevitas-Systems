// FIX-5 stage two (docs/STRIPE_FIX_PLAN.md; docs/STRIPE_TEST_PLAN.md go/no-go
// item G5): /api/billing/status now reads the money of record through
// public.billing_period_settlement_summary (migration 202607280013).
//
// This file REPLACES the contract in tests/billing_status_settlement_pending.test.mjs,
// every assertion of which inverts with the repoint (it pins
// `const SETTLEMENT_PENDING = true`, `estimated_fee_usd: null`,
// `.from('billing_ledger')`, and the absence of `fee_microusd` / micro-dollar
// arithmetic anywhere in the handler). That file must be deleted in the same
// commit; see the settlement-writer lane's crossLaneRequests.
//
// The handler is executed here, not grepped: src/app/api/billing/status/route.ts
// is loaded through an import-hook shim that stubs `next/server` and the two
// `@/lib/billing/*` modules it imports (module.registerHooks +
// module.stripTypeScriptTypes, both available on CI's Node 22.17.1 without any
// command-line flag). That section self-skips on a Node without those APIs, so
// the money-shape contract is ALSO pinned by text assertions that always run.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks, stripTypeScriptTypes } from 'node:module'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const WEEK_MS = 7 * 24 * 60 * 60 * 1000

test('the status route reads the settlement summary RPC and nothing else', () => {
  const status = read('src/app/api/billing/status/route.ts')
  // Pin the RPC name so a repoint cannot happen silently in either direction.
  assert.match(status, /\.rpc\('billing_period_settlement_summary'/)
  assert.match(status, /p_organization_id: authorization\.organizationId/)
  assert.match(status, /p_period_start: new Date\(periodStartMs\)\.toISOString\(\)/)
  // The dead table must be gone...
  assert.doesNotMatch(status, /\.from\('billing_ledger'\)/)
  // ...and the settlement ledger must be UNREACHABLE from the route: 202607280007
  // revokes every privilege from service_role and two CI files assert it, so a
  // PostgREST select would be permission-denied and a `grant select` would turn
  // them red.
  assert.doesNotMatch(status, /\.from\('period_settlement_ledger'\)/)
})

test('the fail-closed anchor gate and the response headers survive the repoint', () => {
  const status = read('src/app/api/billing/status/route.ts')
  // MU8 and tests/company_billing_authorization.test.mjs depend on this exact gate.
  assert.match(status, /periodEndMs - periodStartMs === 7 \* 24 \* 60 \* 60 \* 1000/)
  assert.match(status, /'Cache-Control': 'private, no-store'/)
  // settlement_pending is no longer a hardcoded constant: production migrations
  // are applied by hand while Vercel deploys on push, so the route must degrade
  // to the stage-one contract (nulls behind settlement_pending: true) when the
  // summary RPC is not deployed/readable yet — and only then.
  assert.doesNotMatch(status, /const SETTLEMENT_PENDING/)
  assert.match(status, /settlement_pending: summaryUnavailable/)
  // The degrade is scoped to schema-drift codes; anything else must still
  // throw (=> 500), never fall through to a zero total.
  assert.match(status, /if \(!summaryUnavailableError\(error\)\) throw error;/)
  for (const code of ['PGRST202', '42883', '42P01', '42501']) {
    assert.match(status, new RegExp(`'${code}'`), code)
  }
})

test('every money field is nullable and derived from micro-dollars', () => {
  const status = read('src/app/api/billing/status/route.ts')
  for (const field of [
    'estimated_fee_usd', 'settled_fee_usd', 'reported_fee_usd', 'committed_fee_usd',
  ]) {
    assert.match(status, new RegExp(`${field}: summaryOk \\? toUsd\\(`), field)
  }
  assert.match(status, /MICRO_USD_PER_USD = 1_000_000/)
  // Anything the RPC did not return as a finite number must become null.
  assert.match(status, /Number\.isFinite\(microUsd\)[\s\S]*?: null/)
  // The two operational-health counts degrade the same way: a state that was
  // never queried must not be published as an authoritative 0.
  for (const field of ['needs_review', 'capped_entries']) {
    assert.match(status, new RegExp(`${field}: summaryOk \\? toCount\\([^)]+\\) : null`), field)
  }
})

// ---------------------------------------------------------------------------
// Executable coverage of the real route handler.
// ---------------------------------------------------------------------------

const STUB_SOURCES = {
  'next/server': 'export class NextRequest {}',
  '@/lib/billing/config': `
    export function billingConfig() { return globalThis.__brevitasStatusStub.config }
    export function billingIsConfigured() { return globalThis.__brevitasStatusStub.configured }
  `,
  '@/lib/billing/supabase': `
    const stub = () => globalThis.__brevitasStatusStub
    export async function authenticatedBillingUser() { return stub().user }
    export async function authorizeActiveBillingCompany() { return stub().authorization }
    export async function getBillingAccount() { return stub().account }
    export function billingDatabase() { return stub().database() }
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
      const resolved = nextResolve(specifier, context)
      if (resolved.url.endsWith('.ts')) return { ...resolved, format: 'module' }
      return resolved
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
        // Next.js compiles the route; here it is type-stripped in-process so the
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

/** Records every RPC the route issues and answers with a canned body. */
function recordingDatabase(response) {
  const calls = []
  return {
    calls,
    factory: () => ({
      rpc(name, args) {
        calls.push([name, args])
        return Promise.resolve(response)
      },
      from(table) {
        calls.push(['from', table])
        throw new Error(`the status route must not select ${table} directly`)
      },
    }),
  }
}

const ORGANIZATION_ID = '11111111-1111-4111-8111-111111111111'
const OWNER_ID = '22222222-2222-4222-8222-222222222222'
const PERIOD_START = '2026-07-20T00:00:00.000Z'
const PERIOD_END = new Date(Date.parse(PERIOD_START) + WEEK_MS).toISOString()

/** The shape public.billing_period_settlement_summary returns for a live week. */
function summaryBody(overrides = {}) {
  return {
    ok: true,
    organization_id: ORGANIZATION_ID,
    period_start: PERIOD_START,
    period_end: PERIOD_END,
    settlement_status: 'accruing',
    settlement_id: null,
    revision: null,
    estimated_fee_microusd: 22500000,
    settled_fee_microusd: 0,
    reported_fee_microusd: 0,
    committed_fee_microusd: 0,
    needs_review_count: 0,
    attention_count: 0,
    billing_arrangement: 'marginal_per_call',
    billable: true,
    evidence: {
      eligible_rows: 1,
      net_verified_savings_usd: 100,
      net_after_warm_savings_usd: 90,
      warm_spend_usd: 10,
      warm_spend_days: 1,
      actual_spend_usd: 40,
      zero_spend_share: 0,
      fee_ceiling_microusd: 22500000,
    },
    ...overrides,
  }
}

function installStub({ rpc, periodEnd, account = {}, configured = true } = {}) {
  const database = recordingDatabase(rpc ?? { data: summaryBody(), error: null })
  globalThis.__brevitasStatusStub = {
    configured,
    config: { weeklyCapUsd: 1 },
    user: { id: OWNER_ID },
    authorization: { ok: true, organizationId: ORGANIZATION_ID, billingOwnerId: OWNER_ID },
    account: {
      subscription_status: 'active',
      current_period_start: PERIOD_START,
      current_period_end: periodEnd ?? PERIOD_END,
      last_invoice_status: 'paid',
      stripe_customer_id: 'cus_notARealCustomer',
      ...account,
    },
    database: database.factory,
  }
  return database
}

async function loadRoute() {
  return import(new URL('../src/app/api/billing/status/route.ts', import.meta.url))
}

test('a synchronized billing week reports the projected estimate, not a zero', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  const database = installStub()
  try {
    const response = await GET(new Request('https://brevitassystems.com/api/billing/status'))
    assert.equal(response.status, 200)
    assert.equal(response.headers.get('cache-control'), 'private, no-store')
    const body = await response.json()

    assert.equal(body.period_tracking_valid, true)
    assert.equal(body.settlement_pending, false)
    // The whole point of FIX-5: a live week must show its real accruing fee.
    assert.equal(body.estimated_fee_usd, 22.5)
    // ...while the LEDGER-derived numbers stay honestly zero until it closes.
    assert.equal(body.settled_fee_usd, 0)
    assert.equal(body.reported_fee_usd, 0)
    assert.equal(body.committed_fee_usd, 0)
    assert.equal(body.settlement_status, 'accruing')
    assert.equal(body.settlement_revision, null)
    assert.equal(body.billing_arrangement, 'marginal_per_call')
    assert.equal(body.billable, true)
    assert.equal(body.needs_review, 0)
    assert.equal(body.capped_entries, 0)
    assert.equal(body.evidence.eligible_rows, 1)
    assert.equal(body.evidence.warm_spend_days, 1)

    assert.deepEqual(database.calls, [[
      'billing_period_settlement_summary',
      { p_organization_id: ORGANIZATION_ID, p_period_start: PERIOD_START },
    ]])
  } finally {
    delete globalThis.__brevitasStatusStub
  }
})

test('per-status money buckets round-trip from micro-dollars', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  installStub({
    rpc: {
      data: summaryBody({
        settlement_status: 'reported',
        settlement_id: 1000000001,
        revision: 2,
        estimated_fee_microusd: 22500000,
        settled_fee_microusd: 22500000,
        reported_fee_microusd: 22500000,
        committed_fee_microusd: 22500000,
        needs_review_count: 3,
        attention_count: 2,
      }),
      error: null,
    },
  })
  try {
    const response = await GET(new Request('https://brevitassystems.com/api/billing/status'))
    const body = await response.json()
    assert.equal(body.reported_fee_usd, 22.5)
    assert.equal(body.settled_fee_usd, 22.5)
    assert.equal(body.committed_fee_usd, 22.5)
    assert.equal(body.settlement_status, 'reported')
    assert.equal(body.settlement_revision, 2)
    assert.equal(body.needs_review, 3)
    assert.equal(body.capped_entries, 2)
  } finally {
    delete globalThis.__brevitasStatusStub
  }
})

test('an unattested organization is not presented as billable', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  installStub({
    rpc: {
      data: summaryBody({
        billing_arrangement: 'unattested',
        billable: false,
        // A zero ceiling is what the guard reports for a period it will refuse.
        estimated_fee_microusd: 0,
      }),
      error: null,
    },
  })
  try {
    const body = await (await GET(new Request('https://brevitassystems.com/api/billing/status'))).json()
    assert.equal(body.billable, false)
    assert.equal(body.billing_arrangement, 'unattested')
    // Zero is the honest number here — the guard will refuse any positive fee —
    // but `billable: false` is what tells the UI not to call it a coming charge.
    assert.equal(body.estimated_fee_usd, 0)
  } finally {
    delete globalThis.__brevitasStatusStub
  }
})

test('an ok:false summary blanks every money field and still answers 200', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  for (const code of ['no_billing_account', 'period_anchor_mismatch', 'guard_unavailable']) {
    installStub({ rpc: { data: { ok: false, code }, error: null } })
    try {
      const response = await GET(new Request('https://brevitassystems.com/api/billing/status'))
      assert.equal(response.status, 200, code)
      const body = await response.json()
      for (const field of [
        'estimated_fee_usd', 'settled_fee_usd', 'reported_fee_usd', 'committed_fee_usd',
      ]) {
        assert.equal(body[field], null, `${code}/${field}`)
        // The specific regression: a disconnected total rendering as a real $0.
        assert.notEqual(body[field], 0, `${code}/${field}`)
        assert.equal(typeof body[field], 'object', `${code}/${field}`)
      }
      assert.equal(body.settlement_status, null, code)
      assert.equal(body.billable, false, code)
      assert.equal(body.evidence, null, code)
      // The review counts are unknown here, not zero: an authoritative 0 would
      // assert "nothing is stuck" in the exact state where an entry stuck in
      // review is most likely and least visible.
      assert.equal(body.needs_review, null, code)
      assert.equal(body.capped_entries, null, code)
    } finally {
      delete globalThis.__brevitasStatusStub
    }
  }
})

test('an unrecognised RPC body yields nulls, never a number', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  for (const data of [null, [], 'ok', 42, { estimated_fee_microusd: 22500000 }]) {
    installStub({ rpc: { data, error: null } })
    try {
      const body = await (await GET(new Request('https://brevitassystems.com/api/billing/status'))).json()
      assert.equal(body.estimated_fee_usd, null, JSON.stringify(data))
      assert.equal(body.reported_fee_usd, null, JSON.stringify(data))
      assert.equal(body.billable, false, JSON.stringify(data))
    } finally {
      delete globalThis.__brevitasStatusStub
    }
  }
})

test('period boundaries one millisecond off a week suppress the RPC entirely', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  for (const drift of [1, -1]) {
    const database = installStub({
      periodEnd: new Date(Date.parse(PERIOD_START) + WEEK_MS + drift).toISOString(),
    })
    try {
      const body = await (await GET(new Request('https://brevitassystems.com/api/billing/status'))).json()
      assert.equal(body.period_tracking_valid, false, `drift ${drift}`)
      assert.equal(body.estimated_fee_usd, null, `drift ${drift}`)
      assert.equal(body.settled_fee_usd, null, `drift ${drift}`)
      assert.equal(body.reported_fee_usd, null, `drift ${drift}`)
      assert.equal(body.committed_fee_usd, null, `drift ${drift}`)
      // Never queried, so never reported as a count.
      assert.equal(body.needs_review, null, `drift ${drift}`)
      assert.equal(body.capped_entries, null, `drift ${drift}`)
      assert.deepEqual(database.calls, [], `drift ${drift}`)
    } finally {
      delete globalThis.__brevitasStatusStub
    }
  }
})

test('unauthenticated and unauthorized callers are refused before any read', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  const request = new Request('https://brevitassystems.com/api/billing/status')

  let database = installStub()
  globalThis.__brevitasStatusStub.user = null
  try {
    const response = await GET(request)
    assert.equal(response.status, 401)
    assert.deepEqual(await response.json(), { error: 'Authentication required' })
    assert.deepEqual(database.calls, [])
  } finally {
    delete globalThis.__brevitasStatusStub
  }

  database = installStub()
  globalThis.__brevitasStatusStub.authorization = { ok: false }
  try {
    const response = await GET(request)
    assert.equal(response.status, 403)
    assert.deepEqual(await response.json(), {
      error: 'Billing permission is required for the active company',
    })
    assert.deepEqual(database.calls, [])
  } finally {
    delete globalThis.__brevitasStatusStub
  }
})

test('an unrecognized RPC failure is a 500, never a zero total', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  // Genuine transport/runtime faults — NOT the schema-drift codes — must still
  // refuse loudly rather than presenting "no settlement" as a measured state.
  for (const error of [
    { code: '', message: 'TypeError: fetch failed', details: '', hint: '' },
    { code: '57014', message: 'canceling statement due to statement timeout', details: '', hint: '' },
    new Error('connection reset by peer'),
  ]) {
    installStub({ rpc: { data: null, error } })
    try {
      const response = await GET(new Request('https://brevitassystems.com/api/billing/status'))
      assert.equal(response.status, 500, JSON.stringify(error))
      assert.deepEqual(await response.json(), { error: 'Could not load billing status' })
    } finally {
      delete globalThis.__brevitasStatusStub
    }
  }
})

test('a database without migration 202607280013 degrades to stage-one nulls, not a 500', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  // wyfz migrations are applied by hand while Vercel deploys this route on
  // push, so code-first is the DEFAULT ordering. In that window PostgREST
  // answers "function not found" (or permission denied if the grant is
  // missing); the Savings page must render 'Unavailable' — the truthful
  // answer — instead of a red load error for every customer.
  for (const error of [
    {
      code: 'PGRST202',
      message: 'Could not find the function public.billing_period_settlement_summary in the schema cache',
      details: '', hint: '',
    },
    { code: '42883', message: 'function billing_period_settlement_summary does not exist', details: '', hint: '' },
    { code: '42P01', message: 'relation "period_settlement_ledger" does not exist', details: '', hint: '' },
    { code: '42501', message: 'permission denied for function billing_period_settlement_summary', details: '', hint: '' },
    // Older PostgREST surfaces schema-cache misses without a stable code.
    new Error('Could not find the function public.billing_period_settlement_summary in the schema cache'),
  ]) {
    installStub({ rpc: { data: null, error } })
    try {
      const response = await GET(new Request('https://brevitassystems.com/api/billing/status'))
      const label = JSON.stringify(error)
      assert.equal(response.status, 200, label)
      const body = await response.json()
      // The stage-one contract: pending, and money is null — NEVER a zero.
      assert.equal(body.settlement_pending, true, label)
      for (const field of [
        'estimated_fee_usd', 'settled_fee_usd', 'reported_fee_usd', 'committed_fee_usd',
      ]) {
        assert.equal(body[field], null, `${label}/${field}`)
        assert.notEqual(body[field], 0, `${label}/${field}`)
      }
      assert.equal(body.settlement_status, null, label)
      assert.equal(body.billable, false, label)
      assert.equal(body.needs_review, null, label)
      assert.equal(body.capped_entries, null, label)
      assert.equal(body.evidence, null, label)
      // The rest of the page's state still answers, which is the point of
      // degrading instead of 500ing.
      assert.equal(body.subscription_status, 'active', label)
      assert.equal(body.period_tracking_valid, true, label)
    } finally {
      delete globalThis.__brevitasStatusStub
    }
  }
})

test('settlement_pending stays false when the summary answered, even ok:false', { skip: skipExecutable }, async () => {
  const { GET } = await loadRoute()
  // An answered refusal (guard_unavailable etc.) is not the "no writer yet"
  // state: the card must not claim settlement is pending when the machinery
  // exists and refused for a specific reason.
  installStub({ rpc: { data: { ok: false, code: 'guard_unavailable' }, error: null } })
  try {
    const body = await (await GET(new Request('https://brevitassystems.com/api/billing/status'))).json()
    assert.equal(body.settlement_pending, false)
    assert.equal(body.estimated_fee_usd, null)
  } finally {
    delete globalThis.__brevitasStatusStub
  }
})
