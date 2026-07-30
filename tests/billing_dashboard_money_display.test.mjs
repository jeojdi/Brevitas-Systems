import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

// The dashboard is the last mile of FIX-5. /api/billing/status now returns null
// (never 0) for every money field whenever it cannot state an amount, but the
// card's own `fmt()` helper is `Number(n || 0).toFixed(decimals)`, so a null
// rendered through it is the string "0.000000" -- a confident wrong $0 on a
// billing page. dashboard/** is excluded from the root eslint config and has no
// component test runner, so nothing else in the repo would catch a regression
// here. Pin it from the root suite, which CI always runs.
//
// `fmtMoney` is pure JavaScript inside a .jsx file. Extract and evaluate the real
// declaration rather than re-implementing it, so this test cannot drift from the
// shipped behaviour.
const billingUi = read('dashboard/src/components/Billing.jsx')

function loadFmtMoney() {
  const start = billingUi.indexOf('function fmtMoney(')
  assert.ok(start >= 0, 'dashboard/src/components/Billing.jsx lost fmtMoney()')
  // Balanced-brace scan from the declaration's opening brace.
  let depth = 0
  let end = -1
  for (let index = billingUi.indexOf('{', start); index < billingUi.length; index += 1) {
    if (billingUi[index] === '{') depth += 1
    else if (billingUi[index] === '}') {
      depth -= 1
      if (depth === 0) { end = index + 1; break }
    }
  }
  assert.ok(end > start, 'fmtMoney() is unbalanced and could not be extracted')
  const source = billingUi.slice(start, end)
  assert.doesNotMatch(source, /</, 'fmtMoney() must stay JSX-free so it can be verified directly')
  return new Function(`${source}; return fmtMoney;`)()
}

const valid = { period_tracking_valid: true, settlement_pending: false }

test('the billing card never renders unstated money as a number', () => {
  const fmtMoney = loadFmtMoney()

  // The whole point: null/undefined must not become "$0.000000".
  for (const value of [null, undefined, NaN, Infinity, -Infinity, '7.92', {}, [], true]) {
    assert.equal(
      fmtMoney(valid, value),
      'Unavailable',
      `a ${typeof value} money value (${String(value)}) must render as Unavailable`,
    )
  }

  // A real number still renders, at micro-dollar precision by default.
  assert.equal(fmtMoney(valid, 7.92), '$7.920000')
  assert.equal(fmtMoney(valid, 0), '$0.000000')
  assert.equal(fmtMoney(valid, 22.5), '$22.500000')
  assert.equal(fmtMoney(valid, 7.92, 2), '$7.92')

  // A genuine, measured zero is distinguishable from an unstated one: the API
  // reports 0 only when it has actually computed a zero fee.
  assert.notEqual(fmtMoney(valid, 0), fmtMoney(valid, null))
})

test('unsynchronized or pending periods suppress every amount', () => {
  const fmtMoney = loadFmtMoney()
  for (const billing of [
    undefined,
    null,
    {},
    { period_tracking_valid: false, settlement_pending: false },
    { period_tracking_valid: true, settlement_pending: true },
    { period_tracking_valid: false, settlement_pending: true },
  ]) {
    assert.equal(
      fmtMoney(billing, 7.92),
      'Unavailable',
      `billing=${JSON.stringify(billing)} must not display an amount`,
    )
  }
})

test('every money field in the card goes through the guarded formatter', () => {
  // fmt() is still used for non-money and for values that are never null
  // (the weekly cap), but no *_fee_usd field may reach it.
  assert.doesNotMatch(
    billingUi,
    /fmt\(\s*billing\.\w*fee\w*_usd/,
    'a fee field is formatted with fmt(), which turns null into "0.000000"',
  )
  for (const field of ['estimated_fee_usd', 'reported_fee_usd']) {
    assert.match(
      billingUi,
      new RegExp(`fmtMoney\\(billing, billing\\.${field}\\)`),
      `${field} must be rendered through fmtMoney`,
    )
  }
  // The operator-facing explanations for the two states that legitimately show
  // no money must exist, so "Unavailable" is never unexplained.
  assert.match(billingUi, /Weekly totals are pending settlement/)
  assert.match(billingUi, /not billable pending a signed billing arrangement/)
  assert.match(billingUi, /billing\.settlement_pending/)
  assert.match(billingUi, /billing\.billable === false/)
})

test('the preview payload carries the repointed shape', () => {
  // ?preview=billing is the only way the card is exercised without Stripe. If
  // the preview lacks the new keys it renders a different shape from production
  // -- and with settlement_pending absent (undefined is falsy) it would still
  // show numbers, so the interesting failure is the opposite one: a preview that
  // says pending and therefore demonstrates nothing.
  const app = read('dashboard/src/App.jsx')
  const preview = app.slice(app.indexOf('configured: true,'), app.indexOf('const emptyCompanyContext'))
  assert.ok(preview.length > 0, 'the ?preview=billing payload could not be located')
  for (const entry of [
    /settlement_pending: false/,
    /billable: true/,
    /settlement_status: 'accruing'/,
    /billing_arrangement: 'marginal_per_call'/,
    /settled_fee_usd: [\d.]+/,
    /committed_fee_usd: [\d.]+/,
    /evidence: \{/,
  ]) assert.match(preview, entry)
})

test('the status route and the card agree on which fields exist', () => {
  // A field the card reads but the route never emits is permanently
  // 'Unavailable'; the reverse is silently dropped money state.
  const route = read('src/app/api/billing/status/route.ts')
  for (const field of [
    'estimated_fee_usd',
    'reported_fee_usd',
    'settlement_pending',
    'billable',
    'period_tracking_valid',
  ]) {
    assert.ok(route.includes(field), `the status route no longer emits ${field}`)
    assert.ok(billingUi.includes(field), `the billing card no longer reads ${field}`)
  }
})
