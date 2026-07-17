import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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
  assert.equal(options.capture_pageview, true)
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
