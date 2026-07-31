// The dress-rehearsal defect: a week that was settled, promoted and REPORTED to
// Stripe was invisible to the customer (docs/STRIPE_BUILD_REPORT.md, "CUSTOMER
// DRESS REHEARSAL").
//
// /api/billing/status asked `billing_period_settlement_summary` only at
// `billing_accounts.current_period_start`, and that RPC hard-refuses any other
// anchor with `period_anchor_mismatch` (202607280013:1060-1073). On caestus the
// account anchor is 2026-07-30T18:03:53Z while settlement 1000000005 covers the
// PRIOR week [2026-07-23, 2026-07-30) — so the summary was asked about a week
// with no settlement row and correctly answered zero/'accruing', and the closed,
// billed week had no route to the screen at all. The current-period numbers were
// never wrong; the omission was.
//
// The fix is additive and anchor-free: `billing_period_settlement_history`
// (202607280029) plus two new response fields, `settlement_history` and
// `prior_settlement`. The anchor test on the summary is untouched.
//
// This file is the first EXECUTABLE coverage of that money path. It runs the
// real handler through tests/helpers/billing-route-loader.mjs (the seam built
// for the webhook/sync suites) rather than grepping it, so every assertion below
// is about behaviour. A text-pin section that always runs covers the runtime
// where the seam is unavailable.
//
// THE CONTRACT UNDER TEST, in one line: money that was not measured is `null`,
// and a history that was not read is `null` — never `[]`, because `[]` is a
// positive claim that this customer has never been billed.
//
// Run: node --test tests/billing_status_settlement_history.test.mjs

import assert from 'node:assert/strict'
import test from 'node:test'

import {
  loadBillingRoute,
  loaderUnavailable,
  readRepositoryFile,
  recordingBillingDatabase,
  stubModule,
} from './helpers/billing-route-loader.mjs'

const WEEK_MS = 7 * 24 * 60 * 60 * 1000

// The live caestus values, so this suite and the acceptance run speak about the
// same money. Settlement 1000000005: fee 1235092 micro-dollars for the week
// ending at the account's current anchor.
const ORGANIZATION_ID = 'f1305475-bc00-4ea0-bfc7-502cd2025191'
const OWNER_ID = '1827866c-7df6-4816-abd5-a8824b7e2584'
const CURRENT_PERIOD_START = '2026-07-30T18:03:53.000Z'
const CURRENT_PERIOD_END = new Date(Date.parse(CURRENT_PERIOD_START) + WEEK_MS).toISOString()
const PRIOR_PERIOD_START = '2026-07-23T18:03:53.000Z'
const PRIOR_PERIOD_END = CURRENT_PERIOD_START
const OLDER_PERIOD_START = '2026-07-16T18:03:53.000Z'
const OLDER_PERIOD_END = PRIOR_PERIOD_START
const SETTLED_MICRO_USD = 1235092
const SETTLED_USD = 1.235092

// ---------------------------------------------------------------------------
// Text pins. These run on every runtime, including one without the loader.
// ---------------------------------------------------------------------------

const routeSource = readRepositoryFile('src/app/api/billing/status/route.ts')
const cardSource = readRepositoryFile('dashboard/src/components/Billing.jsx')

test('the route reaches closed periods through an RPC, never a table select', () => {
  assert.match(routeSource, /\.rpc\('billing_period_settlement_history'/)
  // 202607280007 revokes every privilege on the settlement ledger from all three
  // PostgREST roles, and two CI files assert it; a select would be
  // permission-denied and a `grant select` would turn them red.
  assert.doesNotMatch(routeSource, /\.from\('period_settlement_ledger'\)/)
  // The anchored summary must NOT have been widened to fix this. Its p_period_start
  // is still the account's own current anchor.
  assert.match(routeSource, /p_period_start: new Date\(periodStartMs\)\.toISOString\(\)/)
  assert.match(routeSource, /periodEndMs - periodStartMs === 7 \* 24 \* 60 \* 60 \* 1000/)
})

test('the billing card reads both new fields and formats their money defensively', () => {
  for (const field of ['prior_settlement', 'settlement_history']) {
    assert.ok(routeSource.includes(field), `the status route no longer emits ${field}`)
    assert.ok(cardSource.includes(field), `the billing card no longer reads ${field}`)
  }
  // fmt() is Number(n || 0).toFixed(), which renders an unstated amount as
  // "$0.000000" on a billing page. No settled-period money may reach it.
  assert.doesNotMatch(cardSource, /fmt\(\s*priorSettlement\./)
  assert.doesNotMatch(cardSource, /fmt\(\s*period\?\./)
  assert.match(cardSource, /fmtPeriodMoney\(priorSettlement\.reported_fee_usd\)/)
})

// `fmtPeriodMoney` is pure JavaScript inside a .jsx file; extract and evaluate
// the shipped declaration rather than re-implementing it (same technique as
// tests/billing_dashboard_money_display.test.mjs uses for fmtMoney).
function loadCardHelper(name) {
  const start = cardSource.indexOf(`function ${name}(`)
  assert.ok(start >= 0, `dashboard/src/components/Billing.jsx lost ${name}()`)
  let depth = 0
  let end = -1
  for (let index = cardSource.indexOf('{', start); index < cardSource.length; index += 1) {
    if (cardSource[index] === '{') depth += 1
    else if (cardSource[index] === '}') {
      depth -= 1
      if (depth === 0) { end = index + 1; break }
    }
  }
  assert.ok(end > start, `${name}() is unbalanced and could not be extracted`)
  const source = cardSource.slice(start, end)
  assert.doesNotMatch(source, /</, `${name}() must stay JSX-free so it can be verified directly`)
  return new Function(`${source}; return ${name};`)()
}

test('settled-period money is never rendered as a confident zero', () => {
  const fmtPeriodMoney = loadCardHelper('fmtPeriodMoney')
  for (const value of [null, undefined, NaN, Infinity, -Infinity, '1.235092', {}, [], true]) {
    assert.equal(
      fmtPeriodMoney(value),
      'Unavailable',
      `a ${typeof value} amount (${String(value)}) must not render as money`,
    )
  }
  assert.equal(fmtPeriodMoney(SETTLED_USD), '$1.235092')
  // A measured zero stays distinguishable from an unstated one.
  assert.equal(fmtPeriodMoney(0), '$0.000000')
  assert.notEqual(fmtPeriodMoney(0), fmtPeriodMoney(null))
})

test('a closed-period boundary the API did not state is labelled, not blanked', () => {
  // fmtDate() answers '' for null/unparseable. The current-period line can only
  // render behind period_tracking_valid, which already proved both dates parse;
  // the settled-period lines have no such proof — prior_settlement is selected on
  // period_start alone — so every one of them must carry an explicit fallback. A
  // bare '' renders "2026-07-23 → " and reads as a formatting glitch rather than
  // as a value the API did not state.
  const fmtDate = loadCardHelper('fmtDate')
  for (const value of [null, undefined, '', 'not-a-date', {}]) {
    assert.equal(fmtDate(value), '', `fmtDate(${String(value)}) is no longer the empty string`)
  }
  assert.equal(fmtDate(PRIOR_PERIOD_START), '2026-07-23')

  for (const expression of [
    "fmtDate(priorSettlement.period_start) || 'Unavailable'",
    "fmtDate(priorSettlement.period_end) || 'Unavailable'",
    "fmtDate(period?.period_start) || 'Unavailable'",
  ]) {
    assert.ok(cardSource.includes(expression), `a settled-period date can render blank: ${expression}`)
  }
})

test('closed-period money is not gated on the CURRENT period anchor', () => {
  // fmtMoney blanks everything when period_tracking_valid is false or
  // settlement_pending is true. Both describe the anchored summary for the live
  // week. Applying them to an anchor-free history would hide already-reported
  // weeks in exactly the state where the customer most needs them.
  const source = cardSource.slice(
    cardSource.indexOf('function fmtPeriodMoney('),
    cardSource.indexOf('function fmtK('),
  )
  assert.doesNotMatch(source, /period_tracking_valid/)
  assert.doesNotMatch(source, /settlement_pending/)
})

// ---------------------------------------------------------------------------
// Executable coverage of the real handler.
// ---------------------------------------------------------------------------

// Every stub must be registered BEFORE the first import of the route: module
// resolution is cached for the life of the process, and the loader throws rather
// than let a late stub silently exercise the real module (which, with no env,
// would answer 503 and make every assertion below vacuous). The supabase stub is
// declared here with its full export shape and its VALUES are swapped per test.
if (loaderUnavailable === false) {
  stubModule('next/server', { NextRequest: class NextRequest {} })
  stubModule('@/lib/billing/config', {
    billingConfig: () => ({ weeklyCapUsd: 25 }),
    billingIsConfigured: () => true,
  })
  stubModule('@/lib/billing/supabase', {
    authenticatedBillingUser: undefined,
    authorizeActiveBillingCompany: undefined,
    billingDatabase: undefined,
    getBillingAccount: undefined,
  })
}

/** The shape billing_period_settlement_summary returns for the live week. */
function summaryBody(overrides = {}) {
  return {
    ok: true,
    organization_id: ORGANIZATION_ID,
    period_start: CURRENT_PERIOD_START,
    period_end: CURRENT_PERIOD_END,
    settlement_status: 'accruing',
    settlement_id: null,
    revision: null,
    estimated_fee_microusd: 0,
    settled_fee_microusd: 0,
    reported_fee_microusd: 0,
    committed_fee_microusd: 0,
    needs_review_count: 0,
    attention_count: 0,
    billing_arrangement: 'marginal_per_call',
    billable: true,
    evidence: { eligible_rows: 0 },
    ...overrides,
  }
}

/** One `periods` element of billing_period_settlement_history. */
function historyPeriod(overrides = {}) {
  return {
    period_start: PRIOR_PERIOD_START,
    period_end: PRIOR_PERIOD_END,
    settlement_id: 1000000005,
    revision: 1,
    settlement_status: 'reported',
    settled_fee_microusd: SETTLED_MICRO_USD,
    reported_fee_microusd: SETTLED_MICRO_USD,
    committed_fee_microusd: SETTLED_MICRO_USD,
    ...overrides,
  }
}

function historyBody(periods, overrides = {}) {
  return { ok: true, organization_id: ORGANIZATION_ID, periods, ...overrides }
}

/**
 * Install the per-test stub. Every RPC is answered by name, so a test can make
 * the history read fail while the summary still succeeds — the mid-rollout case
 * that must not take the page down.
 */
function installStub({
  summary = { data: summaryBody(), error: null },
  history = { data: historyBody([historyPeriod()]), error: null },
  account = {},
  periodEnd = CURRENT_PERIOD_END,
} = {}) {
  const database = recordingBillingDatabase({
    rpc: {
      billing_period_settlement_summary: () => summary,
      billing_period_settlement_history: () => history,
    },
    // The settlement ledger is unreachable by design; a direct select here is a
    // test failure, not a fallback.
    forbidFrom: true,
  })
  stubModule('@/lib/billing/supabase', {
    authenticatedBillingUser: async () => ({ id: OWNER_ID }),
    authorizeActiveBillingCompany: async () => ({
      ok: true, organizationId: ORGANIZATION_ID, billingOwnerId: OWNER_ID,
    }),
    billingDatabase: () => database.client,
    getBillingAccount: async () => ({
      subscription_status: 'active',
      current_period_start: CURRENT_PERIOD_START,
      current_period_end: periodEnd,
      last_invoice_status: 'paid',
      stripe_customer_id: 'cus_UywBHVTvSfUPdo',
      ...account,
    }),
  })
  return database
}

const request = () => new Request('https://brevitassystems.com/api/billing/status')

async function statusBody(options) {
  const { GET } = await loadBillingRoute('status')
  const database = installStub(options)
  const response = await GET(request())
  return { response, body: await response.json(), database }
}

const skip = loaderUnavailable

test('THE DEFECT: a reported prior week is visible while the live week accrues', { skip }, async () => {
  const { response, body, database } = await statusBody()
  assert.equal(response.status, 200)

  // The current-period contract is unchanged. These numbers were never wrong —
  // they are correct for a week in progress with no settlement row yet.
  assert.equal(body.period_tracking_valid, true)
  assert.equal(body.settlement_pending, false)
  assert.equal(body.settlement_status, 'accruing')
  assert.equal(body.settled_fee_usd, 0)
  assert.equal(body.reported_fee_usd, 0)

  // ...and the week that WAS billed is now reachable, which it previously was
  // not at any anchor this endpoint would accept.
  assert.notEqual(body.prior_settlement, null)
  assert.equal(body.prior_settlement.period_start, PRIOR_PERIOD_START)
  assert.equal(body.prior_settlement.period_end, PRIOR_PERIOD_END)
  assert.equal(body.prior_settlement.settlement_id, 1000000005)
  assert.equal(body.prior_settlement.settlement_revision, 1)
  assert.equal(body.prior_settlement.settlement_status, 'reported')
  assert.equal(body.prior_settlement.reported_fee_usd, SETTLED_USD)
  assert.equal(body.prior_settlement.settled_fee_usd, SETTLED_USD)
  assert.equal(body.prior_settlement.committed_fee_usd, SETTLED_USD)
  assert.deepEqual(body.settlement_history, [body.prior_settlement])

  // Exactly two reads, both RPCs, summary first; no table select at all.
  assert.deepEqual(database.rpcNames(), [
    'billing_period_settlement_summary',
    'billing_period_settlement_history',
  ])
  assert.deepEqual(database.calls[1].args, { p_organization_id: ORGANIZATION_ID, p_limit: 8 })
})

test('history money round-trips from micro-dollars, per status bucket', { skip }, async () => {
  const { body } = await statusBody({
    history: {
      data: historyBody([historyPeriod({
        settlement_status: 'sending',
        settled_fee_microusd: 2470184,
        reported_fee_microusd: 0,
        committed_fee_microusd: 2470184,
      })]),
      error: null,
    },
  })
  const [period] = body.settlement_history
  assert.equal(period.settled_fee_usd, 2.470184)
  // A send whose outcome Stripe has not confirmed is committed but not reported.
  assert.equal(period.reported_fee_usd, 0)
  assert.equal(period.committed_fee_usd, 2.470184)
  assert.equal(period.settlement_status, 'sending')
})

test('an unreadable history is null, never an empty list', { skip }, async () => {
  // wyfz applies migrations by hand while Vercel deploys on push, so the window
  // in which this RPC does not exist yet is the DEFAULT ordering, not an edge
  // case. `[]` here would tell every customer they have never been billed.
  for (const error of [
    { code: 'PGRST202', message: 'Could not find the function public.billing_period_settlement_history in the schema cache' },
    { code: '42883', message: 'function billing_period_settlement_history does not exist' },
    { code: '42P01', message: 'relation "period_settlement_ledger" does not exist' },
    { code: '42501', message: 'permission denied for function billing_period_settlement_history' },
    new Error('Could not find the function public.billing_period_settlement_history in the schema cache'),
  ]) {
    const label = JSON.stringify(error) || String(error)
    const { response, body } = await statusBody({ history: { data: null, error } })
    assert.equal(response.status, 200, label)
    assert.equal(body.settlement_history, null, label)
    assert.notDeepEqual(body.settlement_history, [], label)
    assert.equal(body.prior_settlement, null, label)
    // The rest of the page still answers — that is the point of degrading.
    assert.equal(body.subscription_status, 'active', label)
    assert.equal(body.settlement_status, 'accruing', label)
    assert.equal(body.settlement_pending, false, label)
  }
})

test('an ok:false history refusal blanks the fields, and still answers 200', { skip }, async () => {
  for (const code of ['no_billing_account', 'guard_unavailable']) {
    const { response, body } = await statusBody({
      history: { data: { ok: false, code }, error: null },
    })
    assert.equal(response.status, 200, code)
    assert.equal(body.settlement_history, null, code)
    assert.equal(body.prior_settlement, null, code)
  }
})

test('an ANSWERED empty history is [], which is a different sentence from null', { skip }, async () => {
  // jsonb_agg over zero rows is NULL, so both shapes must be treated as the
  // measured "no settlements" — the RPC did answer ok:true.
  for (const periods of [[], null, undefined]) {
    const { body } = await statusBody({ history: { data: historyBody(periods), error: null } })
    assert.deepEqual(body.settlement_history, [], JSON.stringify(periods ?? null))
    assert.equal(body.prior_settlement, null, JSON.stringify(periods ?? null))
  }
})

test('an unrecognised history shape or element is unknown, not partial', { skip }, async () => {
  // A body we cannot parse is not evidence of anything.
  for (const data of [null, [], 'ok', 42, { ok: true, periods: 'nope' }, { ok: true, periods: 7 }]) {
    const { body } = await statusBody({ history: { data, error: null } })
    assert.equal(body.settlement_history, null, JSON.stringify(data))
    assert.equal(body.prior_settlement, null, JSON.stringify(data))
  }
  // One unreadable element invalidates the whole list rather than being dropped:
  // the dropped period could be the one holding the money.
  const { body } = await statusBody({
    history: { data: historyBody([historyPeriod(), 'not-a-period']), error: null },
  })
  assert.equal(body.settlement_history, null)
  assert.equal(body.prior_settlement, null)
})

test('a money value the RPC did not state as a number becomes null, never 0', { skip }, async () => {
  const { body } = await statusBody({
    history: {
      data: historyBody([historyPeriod({
        reported_fee_microusd: '1235092',
        committed_fee_microusd: null,
        settled_fee_microusd: Number.NaN,
      })]),
      error: null,
    },
  })
  const [period] = body.settlement_history
  for (const field of ['reported_fee_usd', 'committed_fee_usd', 'settled_fee_usd']) {
    assert.equal(period[field], null, field)
    assert.notEqual(period[field], 0, field)
  }
  // The period itself still appears — hiding it would be a second omission.
  assert.equal(period.settlement_id, 1000000005)
})

test('an unrecognised history failure is a 500, never a silent omission', { skip }, async () => {
  for (const error of [
    { code: '57014', message: 'canceling statement due to statement timeout' },
    { code: '', message: 'TypeError: fetch failed' },
    new Error('connection reset by peer'),
  ]) {
    const { GET } = await loadBillingRoute('status')
    installStub({ history: { data: null, error } })
    const response = await GET(request())
    assert.equal(response.status, 500, JSON.stringify(error) || String(error))
    assert.deepEqual(await response.json(), { error: 'Could not load billing status' })
  }
})

test('the history read survives a desynchronized current period', { skip }, async () => {
  // The anchored summary is suppressed when the boundaries are not exactly one
  // week — and that is precisely when a customer most needs to see what was
  // already charged. Periods here are long closed, so the fallback boundary
  // (now) is unambiguous.
  const closedStart = '2026-07-02T18:03:53.000Z'
  const closedEnd = '2026-07-09T18:03:53.000Z'
  const { body, database } = await statusBody({
    periodEnd: new Date(Date.parse(CURRENT_PERIOD_START) + WEEK_MS + 1).toISOString(),
    history: {
      data: historyBody([historyPeriod({ period_start: closedStart, period_end: closedEnd })]),
      error: null,
    },
  })
  assert.equal(body.period_tracking_valid, false)
  // Current-week money stays null — the anchor gate is untouched.
  for (const field of ['estimated_fee_usd', 'settled_fee_usd', 'reported_fee_usd', 'committed_fee_usd']) {
    assert.equal(body[field], null, field)
  }
  // ...and the closed week is still reported.
  assert.deepEqual(database.rpcNames(), ['billing_period_settlement_history'])
  assert.equal(body.settlement_history.length, 1)
  assert.equal(body.prior_settlement.reported_fee_usd, SETTLED_USD)
  assert.equal(body.prior_settlement.period_start, closedStart)
})

test('prior_settlement is the newest STRICTLY earlier week, whatever the row order', { skip }, async () => {
  const { body } = await statusBody({
    history: {
      data: historyBody([
        // A row for the week in progress must never be presented as the last
        // billed week, no matter where it sits in the list.
        historyPeriod({
          period_start: CURRENT_PERIOD_START,
          period_end: CURRENT_PERIOD_END,
          settlement_id: 1000000009,
          settlement_status: 'draft',
          settled_fee_microusd: 0,
          reported_fee_microusd: 0,
          committed_fee_microusd: 0,
        }),
        historyPeriod({ period_start: OLDER_PERIOD_START, period_end: OLDER_PERIOD_END, settlement_id: 1000000003 }),
        historyPeriod({ settlement_id: 1000000005 }),
      ]),
      error: null,
    },
  })
  assert.equal(body.settlement_history.length, 3)
  assert.equal(body.prior_settlement.settlement_id, 1000000005)
  assert.equal(body.prior_settlement.period_start, PRIOR_PERIOD_START)
})

test('a period with no parseable boundaries is never chosen as the prior week', { skip }, async () => {
  const { body } = await statusBody({
    history: {
      data: historyBody([historyPeriod({ period_start: null, period_end: null, settlement_id: 1000000011 })]),
      error: null,
    },
  })
  assert.equal(body.settlement_history.length, 1)
  assert.equal(body.prior_settlement, null)
})

test('unauthenticated and unauthorized callers read no settlement history', { skip }, async () => {
  const { GET } = await loadBillingRoute('status')

  let database = installStub()
  stubModule('@/lib/billing/supabase', {
    authenticatedBillingUser: async () => null,
    authorizeActiveBillingCompany: async () => ({ ok: true, organizationId: ORGANIZATION_ID, billingOwnerId: OWNER_ID }),
    billingDatabase: () => database.client,
    getBillingAccount: async () => ({}),
  })
  let response = await GET(request())
  assert.equal(response.status, 401)
  assert.deepEqual(database.calls, [])

  database = installStub()
  stubModule('@/lib/billing/supabase', {
    authenticatedBillingUser: async () => ({ id: OWNER_ID }),
    authorizeActiveBillingCompany: async () => ({ ok: false }),
    billingDatabase: () => database.client,
    getBillingAccount: async () => ({}),
  })
  response = await GET(request())
  assert.equal(response.status, 403)
  assert.deepEqual(database.calls, [])
})
