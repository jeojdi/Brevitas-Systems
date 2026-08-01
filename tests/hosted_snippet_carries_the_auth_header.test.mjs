// Every hosted-integration snippet we hand a customer must carry
// X-Brevitas-Key.
//
// WHY THIS FILE EXISTS. api/server.py's proxy middleware authenticates on
// `x-brevitas-key` and NOTHING else -- there is no fallback to Authorization,
// and `api_key=` in an SDK constructor becomes an Authorization bearer that the
// gateway never reads. The dashboard snippet, the README and the onboarding doc
// all shipped with only `X-Brevitas-Customer-ID`, so the documented way to
// integrate returned `401 Missing X-Brevitas-Key header` on the customer's very
// first request. A prior audit noted it; nothing enforced it, so it survived.
//
// These are the exact strings a paying customer copies. A test that reads them
// is the only thing standing between a doc edit and a broken first impression.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const read = file => readFileSync(resolve(root, file), 'utf8')

/** Files that show a customer how to reach the hosted gateway. */
const SURFACES = [
  'dashboard/src/components/InstallCommand.jsx',
  'README.md',
  'docs/ONBOARD_HOSTED_CUSTOMER.md',
]

test('the gateway still authenticates on x-brevitas-key alone', () => {
  const server = read('api/server.py')
  // If this ever gains a fallback, the assertions below can relax. Until then
  // they are load-bearing, and this pins the premise rather than assuming it.
  assert.match(
    server,
    /raw_key = request\.headers\.get\("x-brevitas-key", ""\)/,
    'the proxy middleware no longer reads x-brevitas-key the way these ' +
    'snippets assume; re-derive what a customer must send before editing them',
  )
})

for (const file of SURFACES) {
  test(`${file} tells the customer to send X-Brevitas-Key`, () => {
    const source = read(file)
    assert.ok(
      source.includes('X-Brevitas-Key'),
      `${file} documents the hosted integration without X-Brevitas-Key. A ` +
      'customer copying it gets 401 on their first request.',
    )
  })

  test(`${file} never presents the customer id as sufficient on its own`, () => {
    const source = read(file)
    // The exact shape that shipped broken: a default_headers block carrying the
    // customer id and nothing else.
    assert.ok(
      !/default_headers=\{\s*["']X-Brevitas-Customer-ID["']\s*:\s*["'][^"']*["']\s*,?\s*\}/.test(source),
      `${file} still has a default_headers block with only the customer id`,
    )
  })
}

test('the Anthropic shape is documented, not just the OpenAI one', () => {
  // Claude is reached through the Anthropic SDK or ANTHROPIC_BASE_URL; a
  // customer on Claude should not have to translate an OpenAI example.
  const install = read('dashboard/src/components/InstallCommand.jsx')
  assert.match(install, /Anthropic\(/)
  assert.match(install, /ANTHROPIC_BASE_URL/)
  assert.match(read('README.md'), /Anthropic\(/)
})

test('gateway base URLs keep the /v1 suffix', () => {
  // A gateway base URL takes /v1; BREVITAS_BASE_URL must not, or the client
  // produces /v1/v1 and 404s silently. Only the first is asserted here.
  for (const file of SURFACES) {
    const source = read(file)
    if (!source.includes('api.brevitassystems.com')) continue
    assert.ok(
      source.includes('https://api.brevitassystems.com/v1'),
      `${file} points at the gateway without /v1`,
    )
  }
})
