import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

// `overrides` lets a test start from a non-default visitor: `navigator` (GPC / DNT),
// `windowDoNotTrack`, a stored `preference` ('on' | 'off'), and the initial `pathname` /
// `search` the pageview dedupe keys off.
function analyticsContext(overrides = {}) {
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
  // `appHost: true` reproduces the authenticated SPA, which tags its own body
  // (dashboard/index.html: <body class="dashboard-app">).
  if (overrides.appHost) document.body.className = 'dashboard-app'
  const storage = new Map()
  if (overrides.preference) storage.set('brevitas_analytics', overrides.preference)
  const localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  }
  const location = {
    origin: 'https://brevitassystems.com',
    href: 'https://brevitassystems.com/pricing?campaign=private#plans',
    pathname: overrides.pathname ?? '/pricing',
    search: overrides.search ?? '',
  }
  // Real enough for startPageviewTracking to patch and for a test to drive: the bootstrap
  // wraps history.pushState/replaceState and subscribes to popstate.
  const history = { pushState() {}, replaceState() {} }
  const listeners = new Map()
  const window = {
    doNotTrack: overrides.windowDoNotTrack ?? '0',
    location,
    history,
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, [])
      listeners.get(type).push(handler)
    },
  }
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
    history,
    localStorage,
    location,
    navigator: { doNotTrack: '0', globalPrivacyControl: false, ...overrides.navigator },
    window,
  }
  window.window = window
  window.document = document
  window.localStorage = localStorage
  const fire = type => (listeners.get(type) ?? []).forEach(handler => handler())
  return { appendedToHead, context, fire, window }
}

// Boots public/analytics.js in a fresh context and waits for the config fetch to resolve.
async function bootAnalytics(overrides) {
  const harness = analyticsContext(overrides)
  vm.runInNewContext(read('public/analytics.js'), harness.context)
  await new Promise(resolvePromise => setImmediate(resolvePromise))
  const [projectToken, options] = harness.window.posthog._i[0]
  return { ...harness, options, projectToken }
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

test('sanitize_properties keeps the keys PostHog owns and still drops application secrets', async () => {
  const { options } = await bootAnalytics()
  const sanitize = options.sanitize_properties
  assert.equal(typeof sanitize, 'function')

  // The load-bearing case. posthog-js carries the project API key as a property literally
  // named `token` and builds its request body as {api_key: properties.token, ...}. Because
  // sanitize_properties REPLACES the property bag, dropping `token` yields api_key:
  // undefined, JSON.stringify omits it, and PostHog rejects the whole batch with a 400 —
  // which is why $lib='web' sat at zero. Narrowing this exemption silently kills ingestion
  // again, so assert every key class the SDK populates for itself.
  const kept = sanitize({
    token: 'phc_test_public_token',
    distinct_id: 'anon-1',
    $feature_flag_response: 'variant-a',
    $session_entry_referrer: 'https://news.example.com/',
    utm_source: 'newsletter',
    utm_content: 'header-link',
    gclid: 'abc123',
    safe_value: 'kept',
    $current_url: 'https://brevitassystems.com/pricing?campaign=private#plans',
  })
  assert.equal(kept.token, 'phc_test_public_token')
  assert.equal(kept.distinct_id, 'anon-1')
  assert.equal(kept.$feature_flag_response, 'variant-a')
  assert.equal(kept.$session_entry_referrer, 'https://news.example.com/')
  assert.equal(kept.utm_source, 'newsletter')
  assert.equal(kept.utm_content, 'header-link')
  assert.equal(kept.gclid, 'abc123')
  assert.equal(kept.safe_value, 'kept')
  // Exempt from the drop filter, but URL-shaped keys are still stripped of query and fragment.
  assert.equal(kept.$current_url, 'https://brevitassystems.com/pricing')

  // The exemption must not become a hole. These are application-owned and must never leave
  // the browser; note `access_token` is NOT exempt because the `token` exemption is anchored.
  const secrets = {
    api_key: 'sk-live',
    apiKey: 'sk-live',
    access_token: 'ghp_x',
    refresh_token: 'ghp_x',
    secret: 'x',
    client_secret: 'x',
    password: 'hunter2',
    authorization: 'Bearer x',
    user_prompt: 'private question',
    model_response: 'private answer',
    message_content: 'private text',
    email: 'someone@example.com',
  }
  // Keys, not the object itself: sanitize_properties builds its result inside the vm realm,
  // so deepStrictEqual would fail on the prototype rather than on the contents.
  const survivors = Object.keys(sanitize(secrets))
  assert.deepEqual(survivors, [],
    `expected every secret-shaped key to be dropped, kept ${survivors.join(', ')}`)
})

test('GPC opts a visitor out by default and Do Not Track alone does not', async () => {
  const gpc = await bootAnalytics({ navigator: { globalPrivacyControl: true } })
  assert.equal(gpc.window.brevitasAnalytics.isEnabled(), false)
  assert.equal(gpc.options.opt_out_capturing_by_default, true)

  // Do Not Track is deliberately not honoured: no legal force, PostHog ignores it by default,
  // and Firefox sets it for users who never chose it. public/privacy.html promises GPC only,
  // and the test below pins the two together — if this flips back, the policy moves with it.
  const dnt = await bootAnalytics({ navigator: { doNotTrack: '1' }, windowDoNotTrack: '1' })
  assert.equal(dnt.window.brevitasAnalytics.isEnabled(), true)
  assert.equal(dnt.options.opt_out_capturing_by_default, false)

  const off = await bootAnalytics({ preference: 'off' })
  assert.equal(off.window.brevitasAnalytics.isEnabled(), false)
  assert.equal(off.options.opt_out_capturing_by_default, true)
})

test('the authenticated SPA never arms replay and masks all of its text', async () => {
  // The finding: /analytics.js is loaded by the SPA as well as the marketing
  // site, with session_recording on and text masking driven by four element
  // markers that Audit.jsx, Overview.jsx, Projects.jsx and Pipelines.jsx do not
  // carry — so rrweb would ship the scanner's evidence strings (file paths and
  // code excerpts from the customer's own repository) and their spend figures to
  // PostHog's cloud, for a customer engineer's first visit, before the privacy
  // banner is touched.
  const app = await bootAnalytics({ appHost: true })
  assert.equal(app.options.disable_session_recording, true)
  assert.equal(app.options.session_recording.maskAllInputs, true)
  // '*' is the mask-everything selector. `maskAllText` is NOT a posthog-js
  // option and would be silently ignored, which is why it is not used here.
  assert.equal(app.options.session_recording.maskTextSelector, '*')

  // Explicit consent must not re-arm the recorder on this host:
  // startSessionRecording() overrides disable_session_recording.
  app.window.brevitasAnalytics.setEnabled(true)
  const methods = app.window.posthog.map(item => item[0])
  assert.equal(methods.includes('opt_in_capturing'), true)
  assert.equal(methods.includes('startSessionRecording'), false)

  // An SPA alias reaches the same conclusion from the path alone, because
  // next.config.ts rewrites all of them onto the same document.
  for (const pathname of ['/dashboard', '/login/enterprise', '/signup', '/email-confirmed', '/welcome']) {
    const alias = await bootAnalytics({ pathname })
    assert.equal(alias.options.disable_session_recording, true, pathname)
    assert.equal(alias.options.session_recording.maskTextSelector, '*', pathname)
  }

  // Marketing pages keep counting every visit exactly as before: no host flag,
  // no behaviour change.
  const site = await bootAnalytics()
  assert.equal(site.options.disable_session_recording, false)
  assert.equal(site.options.session_recording.maskTextSelector,
    '[data-ph-sensitive],.ph-sensitive,.ph-no-capture,[data-private]')
  assert.equal(site.options.autocapture, true)
  site.window.brevitasAnalytics.setEnabled(true)
  assert.equal(site.window.posthog.map(item => item[0]).includes('startSessionRecording'), true)
})

test('the published privacy policy describes the signals the bootstrap actually honours', () => {
  const bootstrap = read('public/analytics.js')
  const policy = read('public/privacy.html')

  assert.match(bootstrap, /navigator\.globalPrivacyControl === true/,
    'the bootstrap must honour Global Privacy Control')
  assert.match(policy, /Global Privacy Control/,
    'the policy must tell visitors GPC is honoured')

  // These two drifted apart once already: DNT was removed from the bootstrap while /privacy
  // kept promising it was respected. Whichever way that decision goes, both files move together.
  const checksDoNotTrack = /(?:navigator|window)\.doNotTrack/.test(bootstrap)
  assert.equal(checksDoNotTrack, false,
    'public/analytics.js checks Do Not Track again — restore the promise in public/privacy.html')
  assert.doesNotMatch(policy, /Do Not Track/i,
    'public/privacy.html promises Do Not Track is honoured, but public/analytics.js does not check it')
})

test('the bootstrap fires exactly one $pageview per page load and per SPA navigation', async () => {
  const { context, fire, options } = await bootAnalytics({ pathname: '/pricing' })

  // capture_pageview is false, so nothing records a $pageview unless `loaded` installs it.
  assert.equal(options.capture_pageview, false)
  const captured = []
  options.loaded({ capture: event => captured.push(event) })
  assert.deepEqual(captured, ['$pageview'], 'one $pageview for the initial page load')

  // An SPA navigation to a new path counts once...
  context.location.pathname = '/dashboard'
  context.history.pushState(null, '', '/dashboard')
  assert.deepEqual(captured, ['$pageview', '$pageview'])

  // ...and a router rewriting the same URL must not inflate the count.
  context.history.replaceState(null, '', '/dashboard')
  context.history.pushState(null, '', '/dashboard')
  assert.deepEqual(captured, ['$pageview', '$pageview'], 'same URL must not re-count')

  // Back/forward counts, and the query string is part of the identity.
  context.location.search = '?tab=audit'
  fire('popstate')
  assert.deepEqual(captured, ['$pageview', '$pageview', '$pageview'])
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
