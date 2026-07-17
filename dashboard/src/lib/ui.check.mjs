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

test('customer savings UI shows signed verified savings without a measured duplicate', async () => {
  const [billing, overview, projects] = await Promise.all([
    source('Billing'), source('Overview'), source('Projects'),
  ])
  for (const component of [billing, overview, projects]) {
    assert.doesNotMatch(component, /Measured savings/i)
    assert.doesNotMatch(component, /Math\.abs/)
  }
  assert.match(billing, /Verified savings/)
  assert.match(overview, /verified savings/)
  assert.match(projects, /Verified savings/)
})

test('dashboard navigation is separated and exposes its active section', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /aria-label="Dashboard sections"/)
  assert.match(app, /aria-current=\{activeTab === tab \? 'page' : undefined\}/)
})

test('overview uses a cumulative saved and not-saved area chart', async () => {
  const overview = await source('Overview')
  assert.match(overview, /AreaChart, Area/)
  assert.match(overview, /dataKey="totalSaved"/)
  assert.match(overview, /dataKey="totalNotSaved"/)
  assert.match(overview, /fill="url\(#savedArea\)"/)
  assert.match(overview, /fill="url\(#notSavedArea\)"/)
  assert.equal((overview.match(/type="monotone"/g) || []).length, 2)
  assert.match(overview, /dot=\{\{ r: 5,/)
  assert.match(overview, /dot=\{\{ r: 5\.5,/)
  assert.match(overview, /savedBeforeRange/)
  assert.match(overview, /notSavedBeforeRange/)
  assert.match(overview, /const savedColor\s*=\s*'#4f5fc4'/)
  assert.match(overview, /const notSavedColor\s*=\s*darkMode \? '#737373' : '#9ca3af'/)
  assert.doesNotMatch(overview, /BarChart|LineChart|<Bar\b|<Line\b/)
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
