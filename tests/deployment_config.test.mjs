import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const sha256 = path => createHash('sha256').update(readFileSync(resolve(root, path))).digest('hex')

test('internal artifacts are archived outside public hosting', () => {
  for (const name of ['design-canvas.html', 'design-canvas.jsx', 'hero-demo.html',
    'test.html', 'traverse-demo.html', 'waitlist.html', 'google-verification-example.html']) {
    assert.equal(existsSync(resolve(root, 'public', name)), false, name)
    assert.equal(existsSync(resolve(root, 'archive/public-internal', name)), true, name)
  }
  assert.equal(existsSync(resolve(root, 'public/uploads')), false)
  assert.equal(existsSync(resolve(root, 'archive/public-internal/uploads')), true)
})

test('public discovery files contain only live public routes and assets', () => {
  const sitemap = read('public/sitemap.xml')
  for (const path of ['/product', '/pricing', '/benchmarks', '/docs', '/blog']) {
    assert.match(sitemap, new RegExp(`<loc>https://brevitassystems\\.com${path}</loc>`))
  }
  for (const path of ['/index', '/features', '/about', '/contact', '/waitlist']) {
    assert.doesNotMatch(sitemap, new RegExp(`<loc>[^<]+${path}</loc>`))
  }
  assert.doesNotMatch(read('public/site.webmanifest'), /\/screenshots\/|"\/share"/)
  assert.doesNotMatch(read('public/index.html'), /\/og-image\.png/)
  assert.doesNotMatch(read('public/pricing.html'), /site\.css/)
})

test('homepage install flow explicitly authenticates bvx before setup', () => {
  const homepageSource = `${read('public/index.html')}\n${read('public/components.jsx')}`
  assert.match(homepageSource,
    /brew install brevitas-ai\/brevitas\/bvx && bvx login && bvx install ai/)
})

test('live HTML references only existing static assets', () => {
  const asset = /(?:src|href)=["']([^"']+\.(?:css|ico|js|jsx|jpg|png|svg|webmanifest|mp4))["']/g
  for (const file of readdirSync(resolve(root, 'public')).filter(name => name.endsWith('.html'))) {
    for (const [, reference] of read(`public/${file}`).matchAll(asset)) {
      if (/^https?:/.test(reference)) continue
      assert.equal(existsSync(resolve(root, 'public', reference.replace(/^\//, ''))), true,
        `${file}: ${reference}`)
    }
  }
})

test('favicon routes use the Brevitas mark without a stale Next override', () => {
  assert.equal(existsSync(resolve(root, 'src/app/favicon.ico')), false)
  assert.equal(existsSync(resolve(root, 'src/app/icon.ico')), true)

  const expected = sha256('public/brevitas-mark.ico')
  assert.equal(sha256('public/favicon.ico'), expected)
  assert.equal(sha256('src/app/icon.ico'), expected)

  for (const file of readdirSync(resolve(root, 'public')).filter(name => name.endsWith('.html'))) {
    const html = read(`public/${file}`)
    assert.match(html, /\/brevitas-mark\.(?:ico|svg)/, file)
    assert.doesNotMatch(html, /href=["']\/favicon(?:\.|-)/, file)
  }

  const dashboard = read('public/dashboard/index.html')
  assert.match(dashboard, /\/brevitas-mark\.(?:ico|svg)/)
  assert.doesNotMatch(dashboard, /href=["']\/favicon(?:\.|-)/)
  assert.match(read('public/site.webmanifest'), /\/brevitas-(?:mark|app|touch)-/)
})

test('dashboard aliases receive CSP and are excluded from indexing', () => {
  const config = read('next.config.ts')
  for (const path of ['/dashboard/:path*', '/login', '/signup', '/waitlist']) {
    assert.match(config, new RegExp(path.replace(/[/*]/g, '\\$&')))
  }
  assert.match(config, /X-Robots-Tag.+noindex, nofollow/)
})

test('waitlist accepts writes without granting browser reads', () => {
  const route = read('src/app/api/waitlist/route.ts')
  assert.doesNotMatch(route, /\.insert\(\[row\]\)[\s\S]{0,80}\.select\(/)
  assert.doesNotMatch(route, /export async function GET/)

  for (const sql of ['supabase/create_waitlist_table.sql', 'supabase/fix_rls_policy.sql']) {
    const policy = read(sql)
    assert.doesNotMatch(policy, /CREATE POLICY[^;]+FOR SELECT/is)
    assert.match(policy, /FOR INSERT TO anon WITH CHECK \(true\)/i)
  }
})

test('legal acceptance migration backfills existing signup metadata', () => {
  const migration = read('supabase/migrations/20260714_legal_acceptances.sql')
  assert.match(migration, /insert into public\.legal_acceptances/i)
  assert.match(migration, /from auth\.users/i)
  assert.match(migration, /accepted_terms_at/)
  assert.match(migration, /on conflict \(user_id\) do nothing/i)
})

test('PostHog analytics is proxied, privacy controlled, and never exposes the personal key', () => {
  const config = read('next.config.ts')
  const analytics = read('public/analytics.js')
  const publicConfig = read('src/app/api/analytics-config/route.ts')
  assert.match(config, /source: '\/ingest\/static\/:path\*'/)
  assert.match(config, /source: '\/ingest\/:path\*'/)
  assert.match(analytics, /maskAllInputs: true/)
  assert.match(analytics, /maskCapturedNetworkRequestFn/)
  assert.match(analytics, /globalPrivacyControl/)
  assert.match(analytics, /opt_out_capturing/)
  assert.doesNotMatch(publicConfig, /POSTHOG_PERSONAL_API_KEY/)
  assert.match(read('public/privacy.html'), /PostHog/)
})

test('new signup records the versioned analytics privacy notice', () => {
  const auth = read('dashboard/src/components/Auth.jsx')
  const migration = read('supabase/migrations/20260715_analytics_privacy.sql')
  assert.match(auth, /privacy_version: '2026-07-15'/)
  assert.match(auth, /analytics_notice_acknowledged_at/)
  assert.match(migration, /add column if not exists privacy_version/i)
  assert.match(migration, /Existing rows intentionally remain null/)
})

test('PostHog warehouse role can read only the approved analytics view', () => {
  const migration = read('supabase/migrations/20260716_posthog_warehouse_view.sql')
  assert.match(migration, /create role posthog_reader nologin/i)
  assert.match(migration, /grant select on analytics\.posthog_usage to posthog_reader/i)
  assert.doesNotMatch(migration, /grant select on (?:public|auth)\./i)
  for (const sensitive of ['key_hash', 'provider_api_key', 'usage_raw', 'request_id']) {
    const viewBody = migration.match(/create or replace view[\s\S]+?from public\.usage_log/i)?.[0] || ''
    assert.doesNotMatch(viewBody, new RegExp(`\\b${sensitive}\\b`, 'i'))
  }
})
