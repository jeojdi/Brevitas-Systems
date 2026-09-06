import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

// The SPA is unlinted and has no component test harness (JSX cannot be loaded by
// `node --test`), so these are source assertions in the style of ui.check.mjs. They
// pin the shapes that stop a failing backend from becoming a UI outage.
const component = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')
const app = () => readFile(new URL('../App.jsx', import.meta.url), 'utf8')

test('a failed stats load can be retried without white-screening the dashboard', async () => {
  const overview = await component('Overview')
  // A cached payload outlives a later failure, so the panel can render with
  // pending=false/error=''/stats=null: nothing ever arrived for this key and the last
  // attempt was aborted rather than failed. Without this third guard the stat reads
  // dereference null and React unmounts the whole root.
  assert.match(overview, /if \(!stats\) return/)
  assert.match(overview, /stats unavailable/)
  const guards = overview.indexOf('statsResource.pending) return')
  const statsRow = overview.indexOf("label: '// ai calls'")
  const nullGuard = overview.indexOf('if (!stats) return')
  assert.ok(guards >= 0, 'the skeleton guard must key off the cache entry being empty')
  assert.ok(nullGuard > guards, 'null guard must sit with the other early returns')
  assert.ok(statsRow >= 0 && nullGuard < statsRow, 'null guard must precede the big-stat reads')
  // Every `stats` dereference is either optional-chained or sits behind an optional
  // read of the SAME field — the redesign's conditional cards are written as
  // `(stats?.x || 0) > 0 && … fmt(stats.x)`, where the `&&` short-circuit is the
  // guard and it may wrap onto the next line. A bare `stats.x` whose field is never
  // optional-read anywhere is how this panel used to take the whole SPA down.
  const bare = [...overview.matchAll(/[^?.]\bstats\.([a-z_]+)/g)].map(match => match[1])
  for (const field of new Set(bare)) {
    assert.ok(
      overview.includes(`stats?.${field}`),
      `stats.${field} is dereferenced without ever being optional-read`,
    )
  }
})

test('one throwing panel degrades to a message instead of unmounting the SPA', async () => {
  const [boundary, shell] = await Promise.all([component('PanelErrorBoundary'), app()])
  assert.match(boundary, /static getDerivedStateFromError/)
  assert.match(boundary, /componentDidCatch/)
  assert.match(boundary, /role="alert"/)
  assert.match(boundary, /reload this section/)
  // Message only, and only to the local console: panel props carry usage and
  // billing figures, so the boundary must not become an egress path.
  assert.doesNotMatch(boundary, /fetch\(|capture\(|posthog/)
  // Keyed on the rendered tab so navigating away resets a crashed panel. renderTab
  // rather than activeTab: while the API key is pending the shell pins the panel to
  // the Overview skeleton, and the boundary key has to follow what actually renders
  // or a crash in the skeleton would survive into the loaded panel.
  assert.match(shell, /<PanelErrorBoundary key=\{renderTab\}>/)
  assert.match(shell, /<\/PanelErrorBoundary>/)
  const boundaryStart = shell.indexOf('<PanelErrorBoundary')
  const boundaryEnd = shell.indexOf('</PanelErrorBoundary>')
  for (const tab of ['Overview', 'Savings', 'Admin', 'Playground']) {
    const at = shell.indexOf(`renderTab === '${tab}'`, boundaryStart)
    assert.ok(at > boundaryStart && at < boundaryEnd, `${tab} panel must render inside the boundary`)
  }
})

test('billing separates poll failures from Stripe action failures', async () => {
  const billing = await component('Billing')
  // One shared slot meant the 10s poll erased a "checkout failed" message within
  // seconds, and a single transient poll failure pinned its red line for the whole
  // session even after later polls succeeded.
  assert.doesNotMatch(billing, /setBillingError|\bbillingError\b/)
  assert.match(billing, /setBillingLoadError\(''\)/)
  assert.match(billing, /setBillingActionError\(''\)/)
  const load = billing.slice(billing.indexOf('const loadBilling'), billing.indexOf('const goToStripe'))
  assert.doesNotMatch(load, /setBillingActionError/)
  const action = billing.slice(billing.indexOf('const goToStripe'), billing.indexOf('if (!apiKey) return'))
  assert.doesNotMatch(action, /setBillingLoadError/)
  // The load error is cleared on success, never on entry.
  assert.ok(
    load.indexOf("setBillingLoadError('')") > load.indexOf('await fetchBillingStatus'),
    'the poll must clear its error only after a successful fetch',
  )
})

test('billing money figures state their own age when a poll fails', async () => {
  const billing = await component('Billing')
  assert.match(billing, /setBillingStale\(true\)/)
  assert.match(billing, /setBillingStale\(false\)/)
  assert.match(billing, /setBillingCheckedAt\(new Date\(\)\.toLocaleTimeString\(\)\)/)
  assert.match(billing, /Last updated \{billingCheckedAt\} — the latest refresh failed/)
  assert.match(billing, /Last updated \{billingCheckedAt\}\./)
  const note = billing.indexOf('the latest refresh failed')
  const estimate = billing.indexOf('Accruing this week')
  assert.ok(estimate >= 0 && note > estimate, 'the staleness note belongs with the fee figures')
})

test('billing panels survive a field the API stops sending', async () => {
  const [billing, admin] = await Promise.all([component('Billing'), component('Admin')])
  // api/ and dashboard/ deploy separately, so an additive or renamed field must not
  // throw and must never render as a confident $0.
  assert.match(billing, /String\(billing\.subscription_status \|\| 'unknown'\)/)
  assert.match(billing, /if \(typeof value !== 'number' \|\| !Number\.isFinite\(value\)\) return 'Unavailable'/)
  // Admin's two dollar formatters now share the finiteness guard in spend.js
  // instead of each re-deriving it. Both of them — not just the billing panel —
  // must route through it: `usd` fills the whole customer breakdown table.
  assert.match(admin, /^const usd = value => usdOrUnavailable\(value, 4\)$/m)
  assert.match(admin, /^const billingUsd = value => usdOrUnavailable\(value, 2\)$/m)
  assert.doesNotMatch(admin, /Number\(value \|\| 0\)\.toFixed/)
  const spend = await readFile(new URL('./spend.js', import.meta.url), 'utf8')
  assert.match(spend, /Number\.isFinite\(parsed\) \? `\$\$\{parsed\.toFixed\(decimals\)\}` : UNAVAILABLE/)
  assert.match(admin, /billing\.accounts\?\.length/)
})
