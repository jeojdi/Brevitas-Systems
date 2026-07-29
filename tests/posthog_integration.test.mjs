import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

function analyticsContext() {
  const appendedToHead = []
  const button = { addEventListener() {}, focus() {}, setAttribute() {} }
  const panel = { hidden: true, querySelector: () => button }
  const notice = { hidden: false }
  const wrapper = {
    id: '',
    innerHTML: '',
    querySelector(selector) {
      if (selector === '.bvt-privacy-button') return button
      if (selector === '.bvt-privacy-panel') return panel
      if (selector === '.bvt-privacy-notice') return notice
      return null
    },
    querySelectorAll: () => [],
  }
  const document = {
    readyState: 'complete',
    body: { appendChild() {} },
    head: { appendChild(node) { appendedToHead.push(node) } },
    createElement(tag) {
      if (tag === 'div') return wrapper
      return { tagName: tag.toUpperCase() }
    },
    getElementById: () => null,
    querySelector: () => null,
  }
  const storage = new Map()
  const localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  }
  const location = {
    origin: 'https://brevitassystems.com',
    href: 'https://brevitassystems.com/pricing?campaign=private#plans',
  }
  const window = { doNotTrack: '0', location }
  const context = {
    URL,
    document,
    fetch: async () => ({
      ok: true,
      json: async () => ({
        enabled: true,
        projectToken: 'phc_test_public_token',
        apiHost: '/ingest',
        uiHost: 'https://us.posthog.com',
      }),
    }),
    localStorage,
    location,
    navigator: { doNotTrack: '0', globalPrivacyControl: false },
    window,
  }
  window.window = window
  window.document = document
  window.localStorage = localStorage
  return { appendedToHead, context, window }
}

test('website bootstrap loads PostHog through the proxy with privacy safeguards', async () => {
  const { appendedToHead, context, window } = analyticsContext()
  vm.runInNewContext(read('public/analytics.js'), context)
  await new Promise(resolvePromise => setImmediate(resolvePromise))

  const sdkScript = appendedToHead.find(node => node.src)
  assert.equal(sdkScript.src, '/ingest/static/array.js')
  assert.equal(window.posthog._i.length, 1)

  const [projectToken, options] = window.posthog._i[0]
  assert.equal(projectToken, 'phc_test_public_token')
  assert.equal(options.api_host, '/ingest')
  assert.equal(options.ui_host, 'https://us.posthog.com')
  assert.equal(options.autocapture, true)
  // The SDK's own pageview is disabled on purpose: it sits behind a consent check that a
  // visitor who ignores the privacy banner never satisfies, so it recorded no $pageview at
  // all (verified live). The bootstrap fires exactly one itself from `loaded`.
  assert.equal(options.capture_pageview, false)
  assert.equal(typeof options.loaded, 'function')
  assert.equal(options.capture_exceptions, true)
  assert.equal(options.session_recording.maskAllInputs, true)
  assert.equal(options.session_recording.recordCrossOriginIframes, false)

  window.brevitasAnalytics.capture('integration_test_event', {
    landing_url: 'https://brevitassystems.com/pricing?campaign=private#plans',
    api_key: 'must-not-leave-the-browser',
    safe_value: 'kept',
  })
  const queuedCapture = window.posthog.find(item => item[0] === 'capture')
  assert.equal(queuedCapture[1], 'integration_test_event')
  assert.equal(queuedCapture[2].landing_url, 'https://brevitassystems.com/pricing')
  assert.equal(queuedCapture[2].safe_value, 'kept')
  assert.equal('api_key' in queuedCapture[2], false)
})

test('billing conversions use correlated and flushed server events', () => {
  const expectedEvents = [
    'billing_checkout_started',
    'billing_portal_opened',
    'billing_checkout_completed',
    'billing_subscription_updated',
    'billing_invoice_updated',
  ]

  const checkout = read('src/app/api/billing/checkout/route.ts')
  const portal = read('src/app/api/billing/portal/route.ts')
  const webhook = read('src/app/api/billing/webhook/route.ts')
  const helper = read('src/lib/posthog-server.ts')

  assert.match(checkout, /distinctId: user\.id,[\s\S]+event: 'billing_checkout_started'/)
  assert.match(portal, /distinctId: user\.id,[\s\S]+event: 'billing_portal_opened'/)
  for (const event of expectedEvents.slice(2)) assert.match(webhook, new RegExp(`event: '${event}'`))
  assert.match(helper, /flushAt: 1/)
  assert.match(helper, /flushInterval: 0/)
  assert.match(helper, /enableExceptionAutocapture: true/)
  assert.match(helper, /await client\.flush\(\)/)
})

// Pages under public/ that are deliberately shipped WITHOUT the analytics bootstrap.
// Intentionally empty: every publicly reachable page — including /404 and the
// post-signup interstitials — must report pageviews, or funnel numbers silently
// under-count the way they did when index.html shipped with no bootstrap at all.
// Adding a name here is a conscious decision to create an analytics blind spot;
// justify it in a comment next to the entry.
const ANALYTICS_EXEMPT_PAGES = new Set([])

test('every public page loads the analytics bootstrap exactly once from <head>', () => {
  // Enumerated dynamically on purpose: a hardcoded list would keep passing when a
  // new page lands without analytics, which is exactly the bug this test exists for.
  const pages = readdirSync(resolve(root, 'public')).filter(name => name.endsWith('.html'))
  assert.ok(pages.length >= 19, `expected the full public site, found ${pages.length} pages`)

  const covered = pages.filter(name => !ANALYTICS_EXEMPT_PAGES.has(name))
  assert.ok(covered.length > 0, 'every public page was allowlisted out of analytics coverage')

  for (const file of covered) {
    const html = read(`public/${file}`)

    // Any mention at all — script tag, preload, inline injection. Two references
    // double-count pageviews, so the bar is exactly one, not "at least one".
    const references = html.match(/\/analytics\.js\b/g) || []
    assert.equal(references.length, 1,
      `${file}: expected exactly 1 analytics bootstrap reference, found ${references.length}`)

    const head = html.match(/<head[^>]*>([\s\S]*?)<\/head>/i)
    assert.ok(head, `${file}: no <head> to host the analytics bootstrap`)
    assert.match(head[1], /\/analytics\.js\b/,
      `${file}: analytics bootstrap must load from <head>, not the body`)

    const tags = head[1].match(/<script\b[^>]*\bsrc=["']\/analytics\.js["'][^>]*>/gi) || []
    assert.equal(tags.length, 1, `${file}: expected exactly 1 analytics <script> tag in <head>`)
    assert.match(tags[0], /\bdefer\b/,
      `${file}: analytics bootstrap must be deferred so it never blocks first paint`)
  }
})

test('PostHog ingest rewrites keep SDK asset routes ahead of the catch-all proxy', () => {
  const config = read('next.config.ts')

  assert.match(config, /const posthogHost = \(process\.env\.POSTHOG_HOST \|\| "https:\/\/us\.i\.posthog\.com"\)/)
  assert.match(config,
    /const posthogAssetsHost = \(process\.env\.POSTHOG_ASSETS_HOST \|\| "https:\/\/us-assets\.i\.posthog\.com"\)/)

  const routes = [...config.matchAll(
    /source:\s*'(\/ingest\/[^']*)'[\s\S]{0,160}?destination:\s*`([^`]+)`/g,
  )].map(([, source, destination]) => ({ source, destination, index: config.indexOf(`'${source}'`) }))

  const sources = routes.map(route => route.source)
  for (const expected of ['/ingest/static/:path*', '/ingest/array/:path*', '/ingest/:path*']) {
    assert.ok(sources.includes(expected), `missing ingest rewrite for ${expected}`)
  }

  // These live in afterFiles so they cannot shadow real files in public/.
  const afterFiles = config.indexOf('afterFiles:')
  assert.ok(afterFiles > -1, 'next.config.ts must declare an afterFiles rewrite group')
  for (const route of routes) {
    assert.ok(route.index > afterFiles, `${route.source} must be declared inside afterFiles`)
  }

  // Next.js matches rewrites in declaration order. If the catch-all is declared
  // first it swallows /ingest/static/* and /ingest/array/*, and the SDK bundle
  // gets proxied to the event-ingestion host instead of the asset host.
  const catchAll = sources.indexOf('/ingest/:path*')
  assert.equal(catchAll, sources.length - 1,
    `catch-all /ingest/:path* must be the last ingest rewrite, got order ${sources.join(' , ')}`)
  assert.ok(sources.indexOf('/ingest/static/:path*') < catchAll, 'asset route ordered after catch-all')
  assert.ok(sources.indexOf('/ingest/array/:path*') < catchAll, 'array route ordered after catch-all')

  const destinations = new Map(routes.map(route => [route.source, route.destination]))
  assert.equal(destinations.get('/ingest/static/:path*'), '${posthogAssetsHost}/static/:path*')
  assert.equal(destinations.get('/ingest/array/:path*'), '${posthogAssetsHost}/array/:path*')
  assert.equal(destinations.get('/ingest/:path*'), '${posthogHost}/:path*')
})
