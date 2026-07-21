import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = name => readFile(new URL(`../components/${name}.jsx`, import.meta.url), 'utf8')

test('savings UI explains Stripe-hosted billing and the exact fee boundary', async () => {
  const [billing, app] = await Promise.all([
    source('Billing'), readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
  ])
  assert.match(billing, /25% of verified savings/)
  assert.match(app, /estimated_fee_usd: 7\.92/)
  assert.match(billing, /Stripe hosts card collection/)
  assert.match(billing, /monthly safety cap/)
  assert.doesNotMatch(billing, /card(number|_number)|payment_method_data/i)
})

test('customer UI separates input reduction, native caching, avoided calls, and controls', async () => {
  const [billing, overview, projects] = await Promise.all([
    source('Billing'), source('Overview'), source('Projects'),
  ])
  for (const component of [billing, overview, projects]) {
    assert.doesNotMatch(component, /Measured savings/i)
    assert.doesNotMatch(component, /Math\.abs/)
  }
  assert.match(billing, /Verified savings/)
  assert.match(overview, /provider input tokens avoided/)
  assert.match(overview, /model calls avoided/)
  assert.match(overview, /net native-cache discount/)
  assert.match(overview, /paired control/)
  assert.match(projects, /Input avoided/)
  assert.match(projects, /Calls avoided/)
})

test('dashboard navigation is separated and exposes its active section', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /aria-label="Dashboard sections"/)
  assert.match(app, /aria-current=\{activeTab === tab \? 'page' : undefined\}/)
})

test('overview uses an input-avoidance per-call area chart', async () => {
  const overview = await source('Overview')
  assert.match(overview, /AreaChart, Area/)
  assert.match(overview, /dataKey="inputAvoided"/)
  assert.match(overview, /fill="url\(#savedArea\)"/)
  assert.doesNotMatch(overview, /notSavedArea|totalNotSaved/)
  assert.equal((overview.match(/type="monotone"/g) || []).length, 1)
  assert.match(overview, /dot=\{\{ r: 5\.5,/)
  assert.match(overview, /fmtAxis/)
  assert.match(overview, /width=\{58\}/)
  assert.match(overview, /const savedColor\s*=\s*'#4f5fc4'/)
  assert.doesNotMatch(overview, /cached input rate|cachedInputRate/i)
  assert.doesNotMatch(overview, /BarChart|LineChart|ComposedChart|<Bar\b|<Line\b/)
})

test('dashboard preview is restricted to localhost and keeps production auth intact', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /\['localhost', '127\.0\.0\.1'\]\.includes\(window\.location\.hostname\)/)
  assert.match(app, /\['dashboard', 'billing'\]\.includes\(PREVIEW_SECTION\)/)
  assert.match(app, /previewStats=\{PREVIEW_STATS\}/)
  assert.match(app, /previewBilling=\{PREVIEW_BILLING\}/)
  assert.match(app, /if \(PREVIEW_MODE\) \{\s*return <DashboardPreview/)
  assert.match(app, /if \(!session\) \{\s*return <Auth/)
})

test('admin UI combines protected PostHog and financial operations without secrets', async () => {
  const admin = await source('Admin')
  assert.match(admin, /\/v1\/admin\/keys/)
  assert.match(admin, /\/v1\/admin\/analytics/)
  assert.match(admin, /\/v1\/admin\/stats\/breakdown/)
  assert.match(admin, /\/v1\/admin\/billing/)
  assert.match(admin, /Billing · Amount owed/)
  assert.match(admin, /data-ph-sensitive/)
  assert.doesNotMatch(admin, /POSTHOG_PERSONAL_API_KEY|X-Brevitas-Admin/)
})
