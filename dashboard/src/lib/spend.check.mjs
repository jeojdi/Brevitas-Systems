import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  UNAVAILABLE, WITHHELD, moneyOrWithheld, spendRedacted, statsRows, sumMoney, usdOrUnavailable,
} from './spend.js'

const source = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

test('spendRedacted reads only the explicit API flag', () => {
  assert.equal(spendRedacted({ spend_redacted: true }), true)
  assert.equal(spendRedacted({ spend_redacted: false }), false)
  assert.equal(spendRedacted({}), false)
  assert.equal(spendRedacted(null), false)
  assert.equal(spendRedacted(undefined), false)
  // Never coerce truthy junk into "withheld".
  assert.equal(spendRedacted({ spend_redacted: 1 }), false)
})

test('moneyOrWithheld never renders withheld money as $0.00', () => {
  const render = v => `$${Number(v || 0).toFixed(2)}`
  // Redacted flag wins even when a number is present.
  assert.equal(moneyOrWithheld(3.5, render, true), WITHHELD)
  // A stripped list row lacks the key entirely: undefined is withheld.
  assert.equal(moneyOrWithheld(undefined, render), WITHHELD)
  // A measured zero is real money and must still render.
  assert.equal(moneyOrWithheld(0, render), '$0.00')
  assert.equal(moneyOrWithheld(1.234, render), '$1.23')
  // null passes through so renderers keep their own 'Not measured' semantics.
  assert.equal(moneyOrWithheld(null, v => (v == null ? 'Not measured' : 'x')), 'Not measured')
})

test('statsRows reads CONTRACT A and the bare list it replaces', () => {
  // New shape: rows plus an explicit flag.
  assert.deepEqual(
    statsRows({ rows: [{ a: 1 }], spend_redacted: true }),
    { rows: [{ a: 1 }], redacted: true },
  )
  assert.deepEqual(
    statsRows({ rows: [], spend_redacted: false }),
    { rows: [], redacted: false },
  )
  // Old shape, still on Railway until the API lane deploys: a bare array is
  // {rows: array, spend_redacted: false}. Either deploy order must be correct.
  assert.deepEqual(statsRows([{ a: 1 }, { a: 2 }]), { rows: [{ a: 1 }, { a: 2 }], redacted: false })
  assert.deepEqual(statsRows([]), { rows: [], redacted: false })
  // Not loaded, failed, or an unrecognized shape: no rows and no claim.
  assert.deepEqual(statsRows(null), { rows: [], redacted: false })
  assert.deepEqual(statsRows(undefined), { rows: [], redacted: false })
  assert.deepEqual(statsRows({ detail: 'forbidden' }), { rows: [], redacted: false })
  // A flag on a payload whose rows never arrived is still honoured.
  assert.deepEqual(statsRows({ spend_redacted: true }), { rows: [], redacted: true })
  // Never coerce truthy junk into "withheld".
  assert.equal(statsRows({ rows: [], spend_redacted: 'yes' }).redacted, false)
})

test('usdOrUnavailable renders a measured zero but never invents one', () => {
  assert.equal(usdOrUnavailable(0), '$0.0000')
  assert.equal(usdOrUnavailable(0, 2), '$0.00')
  assert.equal(usdOrUnavailable(1.23456), '$1.2346')
  assert.equal(usdOrUnavailable('2.5', 2), '$2.50')
  // Everything the API did not state is '—', not zero.
  assert.equal(usdOrUnavailable(undefined), UNAVAILABLE)
  assert.equal(usdOrUnavailable(null), UNAVAILABLE)
  assert.equal(usdOrUnavailable(''), UNAVAILABLE)
  assert.equal(usdOrUnavailable('n/a'), UNAVAILABLE)
  assert.equal(usdOrUnavailable(NaN), UNAVAILABLE)
  assert.equal(usdOrUnavailable(Infinity), UNAVAILABLE)
})

test('sumMoney refuses to present a partial sum as a total', () => {
  assert.equal(sumMoney([{ v: 1 }, { v: 2.5 }], 'v'), 3.5)
  assert.equal(sumMoney([], 'v'), 0)
  assert.equal(sumMoney(undefined, 'v'), 0)
  // One stripped row poisons the whole total: the caller renders Withheld/'—'.
  assert.equal(sumMoney([{ v: 1 }, {}], 'v'), null)
  assert.equal(sumMoney([{ v: 1 }, { v: null }], 'v'), null)
  assert.equal(sumMoney([{ v: 1 }, { v: 'x' }], 'v'), null)
  // A measured zero is a real term and does not poison anything.
  assert.equal(sumMoney([{ v: 0 }, { v: 2 }], 'v'), 2)
})

test('pipelines consume CONTRACT A and tolerate the bare list', async () => {
  const pipelines = await source('Pipelines')
  assert.match(pipelines, /statsRows/)
  // All three list endpoints go through the normalizer, so the flag reaches the
  // money renderer on every one of them.
  assert.match(pipelines, /const \{ rows: pipelines, redacted: pipelinesRedacted \} = statsRows\(stats\)/)
  assert.match(pipelines, /const \{ rows: agents, redacted: agentsRedacted \} = statsRows\(agentStats\)/)
  assert.match(pipelines, /const \{ rows: runs, redacted: runsRedacted \} = statsRows\(runStats\)/)
  // No surface reads the payload as a bare array any more.
  assert.doesNotMatch(pipelines, /=\s*stats \|\| \[\]/)
  assert.doesNotMatch(pipelines, /=\s*agentStats \|\| \[\]/)
  assert.doesNotMatch(pipelines, /=\s*runStats \|\| \[\]/)
  // Every money cell is passed the flag for the payload it came from.
  for (const [prefix, flag] of [['p', 'pipelinesRedacted'], ['a', 'agentsRedacted'], ['r', 'runsRedacted']]) {
    assert.match(pipelines, new RegExp(`money\\(${prefix}\\.cost_saved_usd, ${flag}\\)`))
  }
  assert.match(pipelines, /money\(pipelineData\?\.cost_saved_usd, pipelinesRedacted\)/)
  assert.match(pipelines, /money\(pipelineData\?\.brevitas_fee_usd, pipelinesRedacted\)/)
  // The roll-up refuses to sum around a stripped row.
  assert.match(pipelines, /const totalCost = sumMoney\(pipelines, 'cost_saved_usd'\)/)
  assert.match(pipelines, /spendWithheld \|\| totalCost === null \? WITHHELD/)
  assert.doesNotMatch(pipelines, /reduce\(\(sum, p\) => sum \+ \(p\.cost_saved_usd \|\| 0\)/)
  assert.doesNotMatch(pipelines, /reduce\(\(sum, p\) => sum \+ \(p\.brevitas_fee_usd \|\| 0\)/)
})

test('projects never present a short roll-up as a repository total', async () => {
  const projects = await source('Projects')
  // A row missing its money column used to contribute a silent 0.
  assert.doesNotMatch(projects, /project\.spend \+= Number\(row\.actual_cost_usd \|\| 0\)/)
  assert.doesNotMatch(projects, /project\.verified \+= Number\(row\.verified_savings_usd \|\| 0\)/)
  assert.match(projects, /else project\.spendKnown = false/)
  assert.match(projects, /else project\.verifiedKnown = false/)
  assert.match(projects, /!project\.spendKnown \? UNAVAILABLE/)
  assert.match(projects, /!project\.verifiedKnown \? UNAVAILABLE/)
  // Row cells: an explicit null is 'Unpriced', anything unstateable is '—'.
  assert.match(projects, /const usd = n => n === null \? 'Unpriced' : usdOrUnavailable\(n\)/)
})

test('the playground prices a turn or says it did not — never $0.00', async () => {
  const playground = await source('Playground')
  // The `?? 0` defeated the `costSaved != null` guard in SavingsStrip.
  assert.doesNotMatch(playground, /r\.cost_saved_usd \?\? 0/)
  assert.match(playground, /costSaved: r\.estimated_cost_delta_usd \?\? r\.cost_saved_usd \?\? null/)
  assert.match(playground, /meta\.costSaved != null \? fmtUsd\(meta\.costSaved\) : '—'/)
  // fmtUsd itself no longer folds null/undefined/NaN into '$0.00'.
  assert.doesNotMatch(playground, /if \(!v\) return '\$0\.00'/)
  assert.match(playground, /if \(v === null \|\| v === undefined \|\| !Number\.isFinite\(value\)\) return UNAVAILABLE/)
  // The session total counts only priced turns and discloses the rest.
  assert.match(playground, /totals\.priced === 0 \? UNAVAILABLE : fmtUsd\(totals\.cost\)/)
  assert.match(playground, /turn\$\{totals\.unpriced === 1 \? '' : 's'\} not priced/)
  assert.doesNotMatch(playground, /acc\.cost {2}\+= t\.meta\.costSaved \?\? 0/)
})

test('every dashboard surface that renders *_usd gates on spend redaction', async () => {
  const [overview, billing, projects, pipelines] = await Promise.all([
    source('Overview'), source('Billing'), source('Projects'), source('Pipelines'),
  ])
  for (const component of [overview, billing, projects]) {
    assert.match(component, /spendRedacted/)
    assert.match(component, /WITHHELD/)
  }
  // Overview: all three flag checks plus the money chart hidden when withheld.
  assert.match(overview, /spendRedacted\(stats\)/)
  assert.match(overview, /spendRedacted\(cacheStats\)/)
  assert.match(overview, /\{!cacheSpendWithheld && cacheHistory\.length > 0 &&/)
  assert.match(overview, /spendWithheld \? WITHHELD : `\$\$\{Number\(stats\?\.total_actual_cost_usd \|\| 0\)\.toFixed\(2\)\}`/)
  // Billing: stats and cache money both gated.
  assert.match(billing, /spendWithheld \? WITHHELD : allUnpriced \? 'Unpriced'/)
  assert.match(billing, /cacheSpendWithheld \? WITHHELD/)
  assert.match(billing, /spendWithheld \? WITHHELD : `\$\$\{fmt\(m\.actual_cost_usd, 4\)\}`/)
  // Projects: breakdown flag captured in state and used on every money cell.
  assert.match(projects, /setWithheld\(spendRedacted\(data\)\)/)
  assert.match(projects, /withheld \? WITHHELD/)
  // Pipelines: list payloads carry no flag, so absent keys read as withheld.
  assert.match(pipelines, /moneyOrWithheld/)
  assert.doesNotMatch(pipelines, /\$\$\{fmt\([apr]\.(?:cost_saved|brevitas_fee)_usd/)
})

test('admin billing shows gross positive row fees, not "amount owed"', async () => {
  const admin = await source('Admin')
  assert.match(admin, /Gross positive row fees \(un-netted\)/)
  assert.match(admin, /gross_positive_row_fees_usd \?\? record\?\.amount_owed_usd/)
  assert.match(admin, /billingUsd\(grossRowFees\(billing\)\)/)
  assert.match(admin, /billingUsd\(grossRowFees\(account\)\)/)
  assert.match(admin, /settlement_pending === true \|\| billing\.netted === false/)
  assert.doesNotMatch(admin, /Amount owed to Brevitas/)
  assert.doesNotMatch(admin, /'Amount owed'/)
})
