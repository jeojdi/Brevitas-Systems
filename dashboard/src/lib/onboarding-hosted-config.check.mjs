import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'

// The SPA has no JSX test harness (`node --test` cannot load .jsx), so the pure snippet
// builders live behind sentinel comments in the component and are executed here with
// node:vm — the same technique email-confirmed.check.mjs uses, and the one the repository
// SAST policy permits in place of eval / new Function. Everything that is genuinely JSX is
// asserted against the source text, in the style of company-service-account.check.mjs.
const component = readFileSync(
  new URL('../components/CompanyAdministration.jsx', import.meta.url), 'utf8')

const START = '// --- hosted integration snippets (pure; exercised by onboarding-hosted-config.check.mjs) ---'
const END = '// --- end hosted integration snippets ---'
const startIndex = component.indexOf(START)
const endIndex = component.indexOf(END)
assert.ok(startIndex >= 0 && endIndex > startIndex, 'snippet builders must stay inside their sentinels')

const { customerIdSlug, hostedIntegrationSnippets } = runInNewContext(
  `${component.slice(startIndex, endIndex).replaceAll('export function', 'function')}
  ;({ customerIdSlug, hostedIntegrationSnippets })`,
  Object.create(null),
)

const KEY = 'bvt_Vg3lQ-8pZx_TESTONLY'
const byId = (snippets, id) => snippets.find(snippet => snippet.id === id).code

test('every language block carries the key, the hosted base URL, and both required headers', () => {
  const snippets = hostedIntegrationSnippets({ apiKey: KEY, customerId: 'Acme, Inc.' })
  // Joined rather than deepEqual: the builders run in a vm realm, so their arrays are not
  // reference-equal to this realm's Array.prototype.
  assert.equal(snippets.map(snippet => snippet.id).join(','), 'python,node,curl')

  for (const snippet of snippets) {
    assert.ok(snippet.code.includes(KEY), `${snippet.id} must embed the minted key`)
    assert.ok(snippet.code.includes('https://api.brevitassystems.com/v1'),
      `${snippet.id} must point at the hosted endpoint, not the local proxy`)
    // api/server.py reads the credential from X-Brevitas-Key only — an Authorization
    // bearer token is never consulted on the proxy path, so a block that omits this
    // header 401s no matter how correct the rest of it looks.
    assert.ok(snippet.code.includes('X-Brevitas-Key'), `${snippet.id} must send X-Brevitas-Key`)
    // An organization_service key hard-400s without this header.
    assert.ok(snippet.code.includes('X-Brevitas-Customer-ID'),
      `${snippet.id} must send X-Brevitas-Customer-ID`)
    assert.ok(snippet.code.includes('acme-inc'), `${snippet.id} must use the slugged customer id`)
  }

  assert.match(byId(snippets, 'python'), /base_url="https:\/\/api\.brevitassystems\.com\/v1"/)
  assert.match(byId(snippets, 'node'), /baseURL: 'https:\/\/api\.brevitassystems\.com\/v1'/)
  assert.match(byId(snippets, 'curl'), /^curl https:\/\/api\.brevitassystems\.com\/v1\/chat\/completions/)
})

test('the customer id is slugged so nothing typed can reshape a generated snippet', () => {
  assert.equal(customerIdSlug('Acme, Inc.'), 'acme-inc')
  assert.equal(customerIdSlug('  Northwind Traders  '), 'northwind-traders')
  assert.equal(customerIdSlug('ALREADY-fine_1.2'), 'already-fine_1.2')
  assert.equal(customerIdSlug(''), '')
  assert.equal(customerIdSlug(null), '')
  assert.equal(customerIdSlug(undefined), '')
  assert.equal(customerIdSlug('---'), '')
  assert.equal(customerIdSlug('x'.repeat(200)).length, 128)

  // Quote, brace, backslash and newline injection must not survive into the literals the
  // three blocks interpolate into.
  const hostile = `a"}, "X-Brevitas-Key": "stolen`
  assert.equal(customerIdSlug(hostile), 'a-x-brevitas-key-stolen')
  const hostileSnippets = hostedIntegrationSnippets({ apiKey: KEY, customerId: hostile })
  for (const snippet of hostileSnippets) {
    // One key header, holding the real key — the injected second one did not survive.
    assert.equal(snippet.code.split('X-Brevitas-Key').length - 1, 1, `${snippet.id} key header count`)
    assert.equal(snippet.code.split('X-Brevitas-Customer-ID').length - 1, 1,
      `${snippet.id} customer header count`)
  }
  // The whole hostile string collapsed into one inert value on one line.
  assert.ok(byId(hostileSnippets, 'python').includes(
    '        "X-Brevitas-Customer-ID": "a-x-brevitas-key-stolen",\n'))
  assert.ok(byId(hostileSnippets, 'node').includes(
    "    'X-Brevitas-Customer-ID': 'a-x-brevitas-key-stolen',\n"))
  assert.ok(byId(hostileSnippets, 'curl').includes(
    '-H "X-Brevitas-Customer-ID: a-x-brevitas-key-stolen"'))

  for (const raw of ['a\nb', "it's", 'a`b', 'a\\b', 'a$b', 'a{b}']) {
    assert.match(customerIdSlug(raw), /^[a-z0-9._-]*$/)
  }
})

test('an empty customer id degrades to a visible placeholder rather than a broken block', () => {
  const snippets = hostedIntegrationSnippets({ apiKey: KEY })
  for (const snippet of snippets) {
    assert.ok(snippet.code.includes('your-customer-id'),
      `${snippet.id} must show a placeholder id instead of an empty header value`)
  }
  assert.ok(!byId(snippets, 'curl').includes('X-Brevitas-Customer-ID: "'),
    'the curl header must never be emitted empty')
})

test('the config block only exists while the one-time secret is in memory', () => {
  // It is rendered from the same state as the one-time reveal and takes no default, so
  // clearing or leaving the page takes the pasteable copy with it.
  assert.match(component, /<HostedIntegration\s+apiKey=\{serviceSecret\}/)
  assert.match(component, /if \(!apiKey\) return null/)
  assert.match(component, /const snippets = hostedIntegrationSnippets\(\{ apiKey, customerId \}\)/)
  assert.doesNotMatch(component, /localStorage|sessionStorage|indexedDB|document\.cookie/)
  // The pasteable block contains the raw key, so it must carry the same analytics
  // suppression as the one-time reveal.
  const block = component.slice(component.indexOf('id="hosted-config-panel"'))
  assert.match(block.slice(0, 500), /ph-no-capture/)
  assert.match(block.slice(0, 500), /data-ph-sensitive/)
})

test('the UI states the header requirement and the hosted-vs-bvx billing truth once each', () => {
  const ui = component.slice(component.indexOf('function HostedIntegration'))
  assert.match(ui, /required on every single request/)
  assert.match(ui, /400 Organization service proxy calls require/)
  assert.match(ui, /401 Missing/)
  // bvx install must be described as analytics-only and unbillable here, not discovered
  // from a $0 invoice.
  assert.match(ui, /bvx install/)
  assert.match(ui, /analytics only/)
  assert.match(ui, /never billed/)
  assert.match(ui, /only one that produces billable usage/)
})

test('the language tabs are real tabs and the copy control degrades honestly', () => {
  const ui = component.slice(component.indexOf('function HostedIntegration'))
  assert.match(ui, /role="tablist"/)
  assert.match(ui, /role="tab"/)
  assert.match(ui, /role="tabpanel"/)
  assert.match(ui, /aria-selected=\{snippet\.id === active\.id\}/)
  assert.match(ui, /aria-controls="hosted-config-panel"/)
  assert.match(ui, /navigator\.clipboard\?\.writeText/)
  assert.match(ui, /Copy failed/)
  assert.match(ui, /writeText\(active\.code\)/)
})
