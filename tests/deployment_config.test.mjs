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

test('homepage install flow continues into full bvx onboarding', () => {
  const homepageSource = `${read('public/index.html')}\n${read('public/components.jsx')}`
  assert.match(homepageSource,
    /brew install Brevitas-ai\/brevitas\/bvx && bvx install/)
  assert.doesNotMatch(homepageSource, /bvx login/)
  assert.doesNotMatch(homepageSource, /bvx install ai/)
})

test('public pages declare a safe mobile viewport and load responsive styles', () => {
  for (const file of readdirSync(resolve(root, 'public')).filter(name => name.endsWith('.html'))) {
    const html = read(`public/${file}`)
    assert.match(html, /<meta name="viewport"[^>]+width=device-width/i, `${file}: mobile viewport`)
    assert.match(html, /<meta name="viewport"[^>]+viewport-fit=cover/i, `${file}: safe-area viewport`)
    if (/href=["'][^"']*theme\.css["']/.test(html)) {
      assert.match(html, /href=["']\/responsive\.css["']/, `${file}: shared responsive stylesheet`)
    }
  }
})

test('mobile carousels never scroll the document on initial render', () => {
  const techniques = read('public/six-techniques-hub.jsx')
  assert.doesNotMatch(techniques, /\.scrollIntoView\(/)
  assert.match(techniques, /targetLeft[\s\S]+strip\.scrollTo\(\{ left:/)

  const responsive = read('public/responsive.css')
  assert.match(responsive, /@media \(max-width: 640px\)/)
  assert.match(responsive, /\.hero-stats[\s\S]+repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(responsive, /\.docs-side[\s\S]+overflow-x: auto/)
  assert.match(responsive, /\.benchmark-table[\s\S]+display: block/)
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
  for (const path of [
    '/dashboard/:path*', '/login', '/login/personal', '/login/enterprise', '/signup', '/waitlist',
  ]) {
    assert.match(config, new RegExp(path.replace(/[/*]/g, '\\$&')))
  }
  assert.match(config, /source: '\/login\/personal', destination: '\/dashboard\/index\.html'/)
  assert.match(config, /source: '\/login\/enterprise', destination: '\/dashboard\/index\.html'/)
  assert.match(config, /X-Robots-Tag.+noindex, nofollow/)
})

test('email confirmation returns only to allowlisted login audiences', () => {
  const page = read('public/email-confirmed.html')
  assert.match(page, /personal: '\/login\/personal'/)
  assert.match(page, /enterprise: '\/login\/enterprise'/)
  assert.match(page, /destinations\[audience\]/)
  assert.doesNotMatch(page, /(?:next|redirect|returnTo).*location\.(?:href|assign)/i)
})

test('waitlist accepts only bounded server-authorized writes', () => {
  const route = read('src/app/api/waitlist/route.ts')
  const server = read('src/lib/waitlist-server.ts')
  const migration = read('supabase/migrations/202607200002_waitlist_security.sql')
  const sharedLimits = read('supabase/migrations/202607200010_shared_endpoint_rate_limits.sql')
  assert.match(route, /submitWaitlistSignup/)
  assert.doesNotMatch(route, /withRateLimit|x-forwarded-for|x-real-ip|x-client-ip/i)
  assert.doesNotMatch(route, /@\/lib\/supabase|NEXT_PUBLIC_SUPABASE_ANON_KEY/)
  assert.doesNotMatch(route, /export async function GET/)
  assert.match(server, /import 'server-only'/)
  assert.match(server, /SUPABASE_SERVICE_ROLE_KEY/)
  assert.match(server, /\.rpc\('submit_waitlist_signup'/)
  assert.doesNotMatch(server, /NEXT_PUBLIC_SUPABASE_ANON_KEY/)
  assert.match(migration, /revoke all on table public\.waitlist[\s\S]+anon[\s\S]+authenticated/i)
  assert.match(migration, /grant execute on function public\.submit_waitlist_signup[\s\S]+to service_role/i)
  assert.match(migration, /waitlist_email_canonical_check/)
  assert.match(migration, /waitlist_field_lengths_check/)
  assert.match(sharedLimits, /shared_endpoint_rate_limits/)
  assert.match(sharedLimits, /v_global_limit integer := 120/)
  assert.match(sharedLimits, /v_identity_limit integer := 3/)

  for (const sql of ['supabase/create_waitlist_table.sql', 'supabase/fix_rls_policy.sql']) {
    const policy = read(sql)
    assert.doesNotMatch(policy, /CREATE POLICY[^;]+FOR SELECT/is)
    assert.doesNotMatch(policy, /FOR INSERT TO anon/i)
    assert.match(policy, /REVOKE ALL ON TABLE[^;]+anon[^;]+authenticated/i)
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
  assert.match(analytics, /cache: 'no-store'/)
  assert.match(analytics, /if \(notice\) notice\.hidden = open/)
  assert.match(analytics, /data-close/)
  assert.match(analytics, /toggle\(false\); button\.focus\(\)/)
  assert.match(read('public/analytics.css'), /\.bvt-privacy-notice\[hidden\] \{ display: none; \}/)
  assert.doesNotMatch(publicConfig, /POSTHOG_PERSONAL_API_KEY/)
  assert.match(publicConfig, /private, no-store, max-age=0, must-revalidate/)
  assert.doesNotMatch(publicConfig, /stale-while-revalidate/)
  assert.match(read('public/privacy.html'), /PostHog/)
})

test('phone layouts show full lossless pipeline responses and keep privacy controls out of content', () => {
  const pipeline = read('public/pipeline-explorer.jsx')
  const analyticsCss = read('public/analytics.css')
  const responsiveCss = read('public/responsive.css')

  assert.match(pipeline, /className="bv-mobile-slides"/)
  assert.match(pipeline, /function MobilePipelineSlides\(/)
  assert.match(pipeline, /function InputRoute\(/)
  assert.doesNotMatch(pipeline, /NO WORDS REMOVED/)
  assert.doesNotMatch(pipeline, /showRemoved/)
  assert.match(pipeline, /Finish this hop/)
  assert.match(pipeline, /Next: \$\{nextRole\}/)
  assert.match(pipeline, /setStartHop\(nextSlide\)/)
  assert.match(pipeline, /@media \(max-width: 640px\)[\s\S]+\.bv-pipe-grid \{ display: none; \}/)
  assert.match(pipeline, /\.bv-mobile-slides \{[\s\S]+display: block;/)
  assert.match(pipeline, /\.bv-mobile-transcript \{[\s\S]+50svh/)
  assert.match(pipeline, /\.section \.bv-cost-readout[\s\S]+repeat\(2, minmax\(0, 1fr\)\)/)
  assert.match(analyticsCss, /@media \(max-width: 640px\)[\s\S]+#brevitas-privacy-controls \{[\s\S]+position: relative;/)
  assert.match(analyticsCss, /\.bvt-privacy-panel \{[\s\S]+position: fixed;/)
  assert.doesNotMatch(responsiveCss, /body \{[\s\S]{0,100}min-width: 320px;/)
})

test('production build compiles the dashboard with Supabase public configuration', () => {
  const pkg = JSON.parse(read('package.json'))
  const vercel = JSON.parse(read('vercel.json'))
  const builder = read('scripts/build-dashboard.mjs')

  assert.match(pkg.scripts.build, /build:dashboard.*next build/)
  assert.match(vercel.installCommand, /npm ci --prefix dashboard/)
  assert.match(builder, /VITE_SUPABASE_URL/)
  assert.match(builder, /VITE_SUPABASE_ANON_KEY/)
  assert.match(builder, /NEXT_PUBLIC_SUPABASE_URL/)
  assert.match(builder, /NEXT_PUBLIC_SUPABASE_ANON_KEY/)
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
