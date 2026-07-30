// G9: ONE pinned Stripe API version, asserted across all three languages.
//
// Three independent processes call Stripe and each one used to choose its own
// version: the Node SDK client in src/lib/billing/config.ts (which inherited
// whatever the installed `stripe` package defaulted to), the Python recovery
// worker in api/billing_recovery.py (which sent no Stripe-Version at all and so
// inherited the mutable *account* default), and scripts/ci/staging-canary.mjs
// (hard-coded '2025-06-30.basil'). Nothing asserted they agreed, and they did
// not: the canary was validating basil-era response shapes that production
// never received.
//
// Python cannot import the TS constant and the CI script cannot import it
// either (config.ts imports 'server-only'), so agreement is enforced here, by
// reading the three files as text. That makes this test the actual mechanism
// keeping the duplication honest — if it is deleted, the drift silently returns.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import Stripe from 'stripe'

import { STRIPE_API_VERSION as CANARY_VERSION } from '../scripts/ci/staging-canary.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

// The one version the whole system is validated against. Restated literally
// here on purpose: a test that derived it from a source file would pass no
// matter which version the three sites drifted to, as long as they drifted
// together. Bumping Stripe versions must require editing this line, which is
// where the re-validation requirement is written down.
const EXPECTED = '2026-06-24.dahlia'

const CONFIG_TS = 'src/lib/billing/config.ts'
const WORKER_PY = 'api/billing_recovery.py'
const CANARY_MJS = 'scripts/ci/staging-canary.mjs'

// Any Stripe version literal at all: YYYY-MM-DD optionally followed by a
// release codename. Used to prove no *other* version hides in these files.
const ANY_STRIPE_VERSION = /'(\d{4}-\d{2}-\d{2}\.[a-z]+)'|"(\d{4}-\d{2}-\d{2}\.[a-z]+)"/g

// Comments are stripped before scanning for stray version literals. The
// comments in these files deliberately quote '2025-06-30.basil' as the value
// that USED to be here — that history is the most useful thing a future reader
// can find, and it must not be the reason this test fails. Only executable
// version strings count.
const stripComments = source => source
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n')
  .map(line => line.replace(/(^|\s)(\/\/|#).*$/, '$1'))
  .join('\n')

const versionLiterals = source =>
  [...stripComments(source).matchAll(ANY_STRIPE_VERSION)]
    .map(match => match[1] ?? match[2])

test('all three Stripe callers pin the same API version', () => {
  const config = read(CONFIG_TS)
  const worker = read(WORKER_PY)
  const canary = read(CANARY_MJS)

  // 1. Node SDK side: the constant is exported (so it is a real single source
  //    of truth other modules can use, not a comment) and it is the value.
  const configMatch = config.match(
    /export const STRIPE_API_VERSION = '(\d{4}-\d{2}-\d{2}\.[a-z]+)'/)
  assert.ok(configMatch, `${CONFIG_TS} must export a STRIPE_API_VERSION literal`)
  assert.equal(configMatch[1], EXPECTED)

  // 2. Python worker side: a module-level constant, i.e. at column 0, not a
  //    per-call string buried in one code path.
  const workerMatch = worker.match(
    /^STRIPE_API_VERSION = "(\d{4}-\d{2}-\d{2}\.[a-z]+)"$/m)
  assert.ok(workerMatch,
    `${WORKER_PY} must define a module-level STRIPE_API_VERSION`)
  assert.equal(workerMatch[1], EXPECTED)

  // 3. CI canary side: exported and importable, so this test compares the
  //    value the script actually evaluates, not just its source text.
  const canaryMatch = canary.match(
    /export const STRIPE_API_VERSION = '(\d{4}-\d{2}-\d{2}\.[a-z]+)'/)
  assert.ok(canaryMatch, `${CANARY_MJS} must export a STRIPE_API_VERSION literal`)
  assert.equal(canaryMatch[1], EXPECTED)
  assert.equal(CANARY_VERSION, EXPECTED)

  // The three agree with each other, stated directly so a failure message
  // names the drift rather than only the expected constant.
  assert.deepEqual(
    { config: configMatch[1], worker: workerMatch[1], canary: canaryMatch[1] },
    { config: EXPECTED, worker: EXPECTED, canary: EXPECTED },
  )
})

test('no second Stripe version literal survives anywhere in the three callers', () => {
  // The original bug was not a missing pin, it was a *second* pin. Pinning one
  // site while another keeps its own literal is the failure mode, so assert
  // that every version-shaped string in these files is the pinned one.
  for (const path of [CONFIG_TS, WORKER_PY, CANARY_MJS]) {
    const found = new Set(versionLiterals(read(path)))
    found.delete(EXPECTED)
    assert.deepEqual([...found], [],
      `${path} still contains a non-pinned Stripe API version literal`)
  }
})

test('the pinned version is what the Stripe SDK client and worker requests carry', () => {
  const config = read(CONFIG_TS)
  const worker = read(WORKER_PY)

  // Passing apiVersion is the whole point: with it omitted the SDK silently
  // uses its packaged default, so a `npm update stripe` moves production off
  // the validated shapes with no code change and no test failure.
  assert.match(config, /new Stripe\(key, \{[\s\S]*?apiVersion: STRIPE_API_VERSION/)

  // The worker must send the header from its shared _request helper. If it were
  // set only on the session's default headers, a caller passing headers=... —
  // send() passes Idempotency-Key — could shadow it depending on merge order.
  assert.match(worker, /"Stripe-Version": STRIPE_API_VERSION/)
  assert.match(
    worker,
    /def _request\(self[\s\S]*?headers = \{\*\*\(kwargs\.pop\("headers", None\) or \{\}\),\s*\n?\s*"Stripe-Version": STRIPE_API_VERSION\}[\s\S]*?self\.session\.request\([\s\S]*?headers=headers,/,
  )
  // And every outbound Stripe path must go through that helper, or the pin has
  // holes. These are the four Stripe endpoints the worker touches.
  for (const call of [
    /self\._request\(\s*\n?\s*"POST",\s*\n?\s*"\/v1\/billing\/meter_events"/,
    /self\._request\("GET", f"\/v1\/prices\//,
    /self\._request\(\s*\n?\s*"GET", f"\/v1\/billing\/meters\//,
    /self\._request\(\s*\n?\s*"GET",\s*\n?\s*f"\/v1\/billing\/meters\/\{quote\(meter_id, safe=''\)\}\/event_summaries"/,
  ]) {
    assert.match(worker, call)
  }
  // No direct session/requests call may bypass _request inside the gateway.
  const gateway = worker.slice(worker.indexOf('class StripeRestBillingGateway'))
  const stripeCalls = [...gateway.matchAll(/self\.session\.request\(/g)]
  assert.equal(stripeCalls.length, 1,
    'the Stripe gateway must make all REST calls through the single _request helper')
  assert.doesNotMatch(gateway, /requests\.(get|post|put|delete|patch)\(/)
})

test('the reason for this particular version is documented at the source of truth', () => {
  // A bare pin invites a "just bump it" edit. The two shapes below are the ones
  // Tier-3 proved the code depends on, so the constant carries its own
  // upgrade-cost note; keep them greppable.
  const config = read(CONFIG_TS)
  assert.match(config, /current_period_start/)
  assert.match(config, /invoice\.parent/)
  assert.match(config, /stripe-state\.mjs/)
  // Each duplicate names where the other two live, so whoever edits one finds
  // the rest without needing to know this test exists.
  assert.match(read(WORKER_PY), /src\/lib\/billing\/config\.ts/)
  assert.match(read(WORKER_PY), /staging-canary\.mjs/)
  assert.match(read(CANARY_MJS), /api\/billing_recovery\.py/)
  assert.match(read(CANARY_MJS), /src\/lib\/billing\/config\.ts/)
})

test('the SDK accepts the pinned literal and the installed SDK agrees with it', () => {
  // Executable, not textual: a client built with the pinned literal really does
  // report that version, so a typo'd or retired version cannot pass review just
  // because it looks plausible. No network — getApiField reads local config.
  const pinned = new Stripe('sk_test_offline_dummy', { apiVersion: EXPECTED })
  assert.equal(pinned.getApiField('version'), EXPECTED)

  // And the installed `stripe` package's own default must equal the pin. This is
  // deliberately strict: if a dependency bump changes the default, the two would
  // silently diverge for anything that forgets to pass apiVersion, so the bump
  // should fail here and force someone to re-validate the shapes named on
  // STRIPE_API_VERSION in src/lib/billing/config.ts before moving the literal.
  const sdkDefault = new Stripe('sk_test_offline_dummy').getApiField('version')
  assert.equal(sdkDefault, EXPECTED,
    `installed stripe SDK defaults to ${sdkDefault} but the system pins ${EXPECTED}`)
})

test('no other Stripe client in the tree pins a conflicting version', () => {
  // scripts/setup-stripe-billing.mjs builds its own Stripe client. It is a
  // one-off operator tool, not a request path, so it is left on the SDK
  // default — but it must never pin a DIFFERENT version, which would recreate
  // the exact multi-version split G9 closes.
  const setup = read('scripts/setup-stripe-billing.mjs')
  assert.deepEqual(
    versionLiterals(setup).filter(version => version !== EXPECTED), [])
})
