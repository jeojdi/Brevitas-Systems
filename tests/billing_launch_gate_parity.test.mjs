// U4 (docs/STRIPE_TEST_PLAN.md §2.1) — launch-gate parity between the Next.js
// surface and the Python worker, driven from ONE shared fixture.
//
// `BREVITAS_BILLING_ENABLED` is the launch switch for real money. It is read by
// two independent implementations in two languages:
//
//   * src/lib/billing/maintenance-gate.mjs:4  -> 503s checkout/portal/sync
//   * api/billing_recovery.py:834             -> whether the worker meters at all
//   * src/lib/billing/config.ts:16            -> config.enabled
//
// Nothing in the repository previously compared them. The two existing tests
// each cover their own side with a hand-typed list, and the lists already
// differ: tests/billing_maintenance_surface.test.mjs:13 includes `' true '` but
// not `'true '`, and tests/test_billing_recovery.py:984 includes `'TRUE'` but
// not `'true '`. tests/fixtures/billing-gate-values.json is now the single list,
// and this file asserts BOTH implementations against it — the Python half by
// executing the real predicate in a subprocess, so the parity claim is measured
// rather than asserted about source text.
//
// The Python half self-skips when ./.venv/bin/python is absent (it is present in
// this repo and in CI's Python job), and the JS half always runs.
//
// Run: node --test tests/billing_launch_gate_parity.test.mjs

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { billingMaintenanceResponse } from '../src/lib/billing/maintenance-gate.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

const fixture = JSON.parse(read('tests/fixtures/billing-gate-values.json'))
const { variable, enabledValue, disabledValues } = fixture

test('the shared fixture is a real fixture, not a vacuous one', () => {
  assert.equal(variable, 'BREVITAS_BILLING_ENABLED')
  assert.equal(enabledValue, 'true')
  assert.equal(fixture.absentIsDisabled, true)
  // A floor, so the list cannot be silently emptied and leave this suite green.
  assert.ok(disabledValues.length >= 10, `only ${disabledValues.length} disabled values`)
  assert.ok(!disabledValues.includes(enabledValue))
  assert.equal(new Set(disabledValues).size, disabledValues.length, 'duplicate fixture values')
  // The two the test plan calls out by name: the realistic paste errors.
  for (const pasted of [' true ', 'true ']) {
    assert.ok(disabledValues.includes(pasted), JSON.stringify(pasted))
  }
})

test('the Next.js maintenance gate enables on exactly one value', async () => {
  // Enabled: the gate returns null so the route proceeds.
  assert.equal(billingMaintenanceResponse({ [variable]: enabledValue }), null)

  const cases = [['<absent>', {}], ...disabledValues.map(value => [JSON.stringify(value), { [variable]: value }])]
  for (const [label, environment] of cases) {
    const response = billingMaintenanceResponse(environment)
    assert.ok(response instanceof Response, label)
    assert.equal(response.status, 503, label)
    assert.equal(response.headers.get('cache-control'), 'no-store', label)
    assert.equal(response.headers.get('retry-after'), '30', label)
    assert.deepEqual(await response.json(), { error: 'Billing is temporarily unavailable' }, label)
  }
})

test('config.ts derives config.enabled with the identical exact-string comparison', () => {
  // config.ts imports `server-only` and so cannot be imported here; the
  // comparison is pinned textually. If it ever diverges from the gate (e.g. a
  // `.trim()` or a `.toLowerCase()` is added to one side), that is the exact
  // drift this file exists to catch, and it will show up as a diff here.
  const config = read('src/lib/billing/config.ts')
  assert.match(config, /enabled: process\.env\.BREVITAS_BILLING_ENABLED === 'true'/)
  const gate = read('src/lib/billing/maintenance-gate.mjs')
  assert.match(gate, /environment\.BREVITAS_BILLING_ENABLED === 'true'/)
  for (const source of [config, gate]) {
    assert.doesNotMatch(source, /BREVITAS_BILLING_ENABLED[^\n]*(trim|toLowerCase|toUpperCase|includes)/)
  }
})

// ---------------------------------------------------------------------------
// The Python half, executed.
// ---------------------------------------------------------------------------

const pythonBinary = resolve(root, '.venv/bin/python')
const pythonSkip = existsSync(pythonBinary)
  ? false
  : 'requires ./.venv/bin/python (create it with the project venv)'

// Everything billing_recovery_is_configured() needs BESIDES the gate, so the
// gate is provably the only variable under test.
const WORKER_ENVIRONMENT = {
  STRIPE_SECRET_KEY: 'sk_test_localFixture',
  STRIPE_PRICE_ID: 'price_localFixture',
  STRIPE_METER_EVENT_NAME: 'brevitas_fee_microusd',
  BREVITAS_BILLING_WEEKLY_CAP_USD: '1',
  SUPABASE_SERVICE_ROLE_KEY: 'service-role-fixture',
  SUPABASE_URL: 'https://local.invalid',
}

const PROBE = `
import json, os, sys
from api.billing_recovery import billing_recovery_is_configured

fixture = json.load(open(sys.argv[1]))
name = fixture["variable"]
results = {}

os.environ.pop(name, None)
results["<absent>"] = billing_recovery_is_configured()

os.environ[name] = fixture["enabledValue"]
results["<enabled>"] = billing_recovery_is_configured()

for value in fixture["disabledValues"]:
    os.environ[name] = value
    results[value] = billing_recovery_is_configured()

print(json.dumps(results))
`

test('the Python worker gate enables on exactly the same single value', { skip: pythonSkip }, () => {
  const probe = spawnSync(pythonBinary, ['-c', PROBE, 'tests/fixtures/billing-gate-values.json'], {
    cwd: root,
    encoding: 'utf8',
    // A clean environment plus only the names the predicate requires. `PATH` is
    // kept so the interpreter can start.
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...WORKER_ENVIRONMENT },
  })
  assert.equal(probe.status, 0, probe.stderr)
  const results = JSON.parse(probe.stdout.trim().split('\n').at(-1))

  // The one enabled value, and nothing else.
  assert.equal(results['<enabled>'], true, 'the worker must enable on exactly "true"')
  assert.equal(results['<absent>'], false, 'an unset gate must never enable the worker')
  for (const value of disabledValues) {
    assert.equal(results[value], false, `worker enabled on ${JSON.stringify(value)}`)
  }
  // Every fixture value was actually exercised — a probe that silently dropped
  // entries would otherwise pass.
  assert.equal(Object.keys(results).length, disabledValues.length + 2)
})

test('both implementations agree value-for-value', { skip: pythonSkip }, async () => {
  const probe = spawnSync(pythonBinary, ['-c', PROBE, 'tests/fixtures/billing-gate-values.json'], {
    cwd: root,
    encoding: 'utf8',
    env: { PATH: process.env.PATH, HOME: process.env.HOME, ...WORKER_ENVIRONMENT },
  })
  assert.equal(probe.status, 0, probe.stderr)
  const python = JSON.parse(probe.stdout.trim().split('\n').at(-1))

  const javascript = {
    '<absent>': billingMaintenanceResponse({}) === null,
    '<enabled>': billingMaintenanceResponse({ [variable]: enabledValue }) === null,
  }
  for (const value of disabledValues) {
    javascript[value] = billingMaintenanceResponse({ [variable]: value }) === null
  }

  // The whole property in one comparison: identical keys, identical verdicts.
  assert.deepEqual(python, javascript)
})
