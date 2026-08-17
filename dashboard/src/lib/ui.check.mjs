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
  assert.match(billing, /weekly safety cap/)
  assert.match(billing, /every seven days/)
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
  assert.match(overview, /Saved through Brevitas/)
  assert.match(overview, /cache hit rate/)
  assert.match(overview, /provider input tokens avoided/)
  assert.match(overview, /model calls avoided/)
  assert.match(overview, /verified savings/)
  // Projects surfaces the measured native-cache discount, not the always-zero
  // avoidance/verified fields it used to.
  assert.match(projects, /Cache savings/)
})

test('dashboard navigation is separated and exposes its active section', async () => {
  const [app, sidebar] = await Promise.all([
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
    source('Sidebar'),
  ])
  // The nav lives in the left sidebar now; App wires the tab list and the active
  // section (renderTab, not activeTab — the highlight must follow what is shown).
  assert.match(sidebar, /aria-label="Dashboard sections"/)
  assert.match(sidebar, /aria-current=\{active \? 'page' : undefined\}/)
  assert.match(app, /tabs=\{visibleTabs\}/)
  assert.match(app, /activeTab=\{renderTab\}/)
})

test('overview leads with a savings hero and hit-rate ring, with no chart library', async () => {
  const overview = await source('Overview')
  assert.match(overview, /Saved through Brevitas/)
  assert.match(overview, /cache hit rate/)
  assert.match(overview, /const savedColor\s*=\s*'#2f2df5'/)
  // The recharts per-call area chart was removed; nothing pulls a chart library now.
  assert.doesNotMatch(overview, /from 'recharts'/)
  assert.doesNotMatch(overview, /AreaChart|BarChart|LineChart|ComposedChart|<Bar\b|<Line\b/)
})

test('dashboard preview is restricted to localhost and keeps production auth intact', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  assert.match(app, /\['localhost', '127\.0\.0\.1'\]\.includes\(window\.location\.hostname\)/)
  assert.match(app, /'onboarding-personal'/)
  assert.match(app, /'onboarding-enterprise'/)
  assert.match(app, /'personal', 'enterprise'/)
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
  // The billing panel must carry the honest un-netted label, not "Amount owed":
  // per-row fees are floored at zero and warm spend is never deducted, so the
  // figure is not an invoiceable settlement amount.
  assert.match(admin, /Billing · Row fees/)
  assert.match(admin, /Gross positive row fees \(un-netted\)/)
  assert.match(admin, /data-ph-sensitive/)
  assert.doesNotMatch(admin, /POSTHOG_PERSONAL_API_KEY|X-Brevitas-Admin/)
})

test('the admin tab survives the unknown-tab reset guard', async () => {
  const app = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  // `Admin` is not in PERSONAL_TABS/ENTERPRISE_TABS, so the guard that snaps unknown
  // sections back to Overview has to test the rendered list, not dashboardTabs — the
  // tab rendered but was unreachable when those two lists diverged.
  assert.match(app, /visibleTabs = useMemo\(\s*\(\) => \(isAdmin \? \[\.\.\.dashboardTabs, 'Admin'\] : dashboardTabs\)/)
  assert.match(app, /if \(!visibleTabs\.includes\(activeTab\)\) setActiveTab\('Overview'\)/)
  // The rendered list is now handed to the sidebar rather than mapped inline.
  assert.match(app, /tabs=\{visibleTabs\}/)
  assert.doesNotMatch(app, /!dashboardTabs\.includes\(activeTab\)/)
})

test('device connection consumes only authenticated company choices and handles denials safely', async () => {
  const [device, app, company] = await Promise.all([
    source('DeviceConnect'),
    readFile(new URL('../App.jsx', import.meta.url), 'utf8'),
    source('CompanyAdministration'),
  ])
  assert.match(device, /companies\.find\(company => company\.company_id === selectedCompanyId\)/)
  assert.match(device, /JSON\.stringify\(\{ device_code: deviceCode, company_id: selected\.company_id \}\)/)
  assert.match(device, /response\.status === 409/)
  assert.match(device, /response\.status === 403/)
  assert.doesNotMatch(device, /X-Brevitas-Company-ID/)
  assert.match(app, /fetchCompanyContext\(session\.access_token/)
  assert.match(app, /setCompanyContext\(emptyCompanyContext\(\)\)/)
  assert.match(app, /selectedCompanyId:[\s\S]*context\.activeCompanyId/)
  assert.match(app, /company => company\.company_id === companyId/)
  assert.match(company, /onCompanyContextChange\?\.\(value\)/)
})
