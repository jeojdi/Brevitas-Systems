import assert from 'node:assert/strict'
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

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
