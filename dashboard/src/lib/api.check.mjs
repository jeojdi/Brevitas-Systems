import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'

import {
  apiJson, apiKeyId, compress, configureApiAuthenticationRecovery, createKey,
  fetchBillingStatus, openBillingPortal, redactBrowserError, saveProvider,
  startBillingCheckout, streamCompression,
} from './api.js'

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json' },
})

test('dashboard requests match the key, provider, and compression contracts', async () => {
  const calls = []
  const request = async (path, options) => {
    calls.push([path, options])
    if (path === '/v1/keys') return json({ api_key: 'bvt_new' })
    if (path === '/v1/provider') return json({ ok: true })
    return json({ compressed_messages: ['short'], model_response: '' })
  }

  await createKey('bvt_active', 'dashboard', { request })
  await saveProvider('bvt_active', { provider: 'openai', model: 'gpt-4o-mini', provider_api_key: 'sk-test' }, { request })
  await compress('bvt_active', { messages: ['long'], prior_context: [] }, { request })

  assert.deepEqual(calls.map(([path, options]) => [
    path, options.method,
    options.headers.Authorization || options.headers['X-Brevitas-Key'],
    JSON.parse(options.body),
  ]), [
    ['/v1/keys', 'POST', 'Bearer bvt_active', { name: 'dashboard' }],
    ['/v1/provider', 'PUT', 'bvt_active', { provider: 'openai', model: 'gpt-4o-mini', provider_api_key: 'sk-test' }],
    ['/v1/compress', 'POST', 'bvt_active', { messages: ['long'], prior_context: [] }],
  ])
})

test('API errors preserve backend detail', async () => {
  await assert.rejects(
    apiJson('/v1/stats', 'stale', { request: async () => json({ detail: 'Invalid API key' }, 401) }),
    /Invalid API key/,
  )
})

test('authentication recovery retries a safe read once with the replacement key', async () => {
  let recoveries = 0
  const keys = []
  const cleanup = configureApiAuthenticationRecovery(async rejected => {
    recoveries += 1
    assert.equal(rejected, 'bvt_stale')
    return 'bvt_replacement'
  })
  try {
    const result = await apiJson('/v1/stats', 'bvt_stale', {
      request: async (_path, options) => {
        keys.push(options.headers['X-Brevitas-Key'])
        return keys.length === 1
          ? json({ detail: 'Invalid API key' }, 401)
          : json({ total_calls: 1 })
      },
    })
    assert.deepEqual(result, { total_calls: 1 })
    assert.deepEqual(keys, ['bvt_stale', 'bvt_replacement'])
    assert.equal(recoveries, 1)
  } finally {
    cleanup()
  }
})

test('authentication recovery never replays mutations or streams', async () => {
  const rejected = []
  const cleanup = configureApiAuthenticationRecovery(async key => {
    rejected.push(key)
    return 'bvt_replacement'
  })
  try {
    let mutationCalls = 0
    await assert.rejects(
      compress('bvt_stale_mutation', { messages: ['hello'] }, {
        request: async () => {
          mutationCalls += 1
          return json({ detail: 'Invalid API key' }, 401)
        },
      }),
      error => error.status === 401,
    )
    let streamCalls = 0
    await assert.rejects(
      streamCompression('bvt_stale_stream', { messages: ['hello'] }, () => {}, {
        request: async () => {
          streamCalls += 1
          return json({ detail: 'Invalid API key' }, 401)
        },
      }),
      error => error.status === 401,
    )
    assert.equal(mutationCalls, 1)
    assert.equal(streamCalls, 1)
    assert.deepEqual(rejected, ['bvt_stale_mutation', 'bvt_stale_stream'])
  } finally {
    cleanup()
  }
})

test('API errors recursively stop common credential shapes reaching browser logs', async () => {
  const message = redactBrowserError(
    'authorization=Bearer-private bvt_super_secret and Bearer actual-token abcdefgh.ijklmnop.qrstuvwx',
  )
  assert.doesNotMatch(message, /private|bvt_super_secret|actual-token|abcdefgh/)
  await assert.rejects(
    apiJson('/v1/stats', 'stale', {
      request: async () => json({ detail: 'provider failed with sk_private_value' }, 502),
    }),
    error => !String(error).includes('sk_private_value'),
  )
})

test('streaming parser handles split chunks and the optional model response', async () => {
  const payload = [
    'data: {"stage":"retrieving","task":"demo"}\n\n',
    'data: {"stage":"compressed","baseline_tokens":10,"optimized_tokens":5}\n\n',
    'data: {"stage":"model_response","provider":"openai","model":"gpt-4o-mini","text":"OK"}\n\n',
    'data: {"stage":"done","result":{"provider":"openai","model":"gpt-4o-mini"}}',
  ].join('')
  const bytes = new TextEncoder().encode(payload)
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(bytes.slice(0, 31))
      controller.enqueue(bytes.slice(31, 117))
      controller.enqueue(bytes.slice(117))
      controller.close()
    },
  })
  const events = []

  await streamCompression('bvt_active', { messages: ['demo'] }, event => events.push(event), {
    request: async (path, options) => {
      assert.equal(path, '/v1/compress/stream')
      assert.equal(options.headers['X-Brevitas-Key'], 'bvt_active')
      return new Response(body, { status: 200 })
    },
  })

  assert.deepEqual(events.map(event => event.stage), ['retrieving', 'compressed', 'model_response', 'done'])
  assert.equal(events[2].text, 'OK')
})

test('streaming failures redact transport, server-event, and callback credentials', async () => {
  await assert.rejects(
    streamCompression('bvt_active', { messages: ['demo'] }, () => {}, {
      request: async () => { throw new Error('Bearer transport-secret') },
    }),
    error => !String(error).includes('transport-secret'),
  )

  const responseFor = payload => new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload))
      controller.close()
    },
  }), { status: 200 })
  await assert.rejects(
    streamCompression('bvt_active', { messages: ['demo'] }, () => {}, {
      request: async () => responseFor('data: {"stage":"error","message":"sk_stream_private"}\n\n'),
    }),
    error => !String(error).includes('sk_stream_private'),
  )
  await assert.rejects(
    streamCompression('bvt_active', { messages: ['demo'] }, () => {
      throw new Error('api_key=bvt_callback_private')
    }, {
      request: async () => responseFor('data: {"stage":"done"}\n\n'),
    }),
    error => !String(error).includes('bvt_callback_private'),
  )
})

test('active key hash can match the backend fingerprint prefix', async () => {
  assert.equal(await apiKeyId('bvt_active'), createHash('sha256').update('bvt_active').digest('hex'))
})

// ---------------------------------------------------------------------------
// U11 (docs/STRIPE_TEST_PLAN.md §2.1) — the billing transport contract.
//
// Before these tests this suite had ZERO billing coverage, even though
// `billingJson` is the only reason the Savings tab works at all. Three
// properties are load-bearing and each fails silently if broken:
//
//   1. RELATIVE paths. The billing routes live on the Next origin that serves
//      the dashboard; an absolute URL would send a Supabase access token to
//      another host, and the Vite dev server (which proxies only /v1) would not
//      reach them either.
//   2. `Authorization: Bearer <supabase access token>` and NEVER
//      `X-Brevitas-Key`. The API-key header is the FastAPI transport; the Next
//      billing routes authenticate the Supabase user (they would 401), and
//      sending an API key to them leaks a credential to the wrong audience.
//   3. `error.status` copied onto the thrown Error. This is the ONLY reason the
//      409 -> portal fallback in components/Billing.jsx:88-95 works: it branches
//      on `e.status === 409`. `apiJson`/`managementJson` do not copy the status,
//      so this property is unique to billingJson and invisible to every other
//      test in this file.
// ---------------------------------------------------------------------------

const billingRecorder = (respond = () => json({ ok: true })) => {
  const calls = []
  return {
    calls,
    request: async (path, options) => {
      calls.push([path, options])
      return respond(path, options)
    },
  }
}

test('billing requests use relative paths, a bearer token, and no API key header', async () => {
  const recorder = billingRecorder((path) => json({
    configured: true, url: `https://checkout.stripe.com/c/pay/${path.slice(1)}`,
  }))

  await fetchBillingStatus('supabase-access-token', { request: recorder.request })
  await startBillingCheckout('supabase-access-token', { request: recorder.request })
  await openBillingPortal('supabase-access-token', { request: recorder.request })

  assert.deepEqual(recorder.calls.map(([path, options]) => [path, options.method]), [
    ['/api/billing/status', undefined],
    ['/api/billing/checkout', 'POST'],
    ['/api/billing/portal', 'POST'],
  ])
  for (const [path, options] of recorder.calls) {
    // Relative, same-origin, no scheme and no authority.
    assert.ok(path.startsWith('/api/billing/'), path)
    assert.doesNotMatch(path, /^[a-z]+:|^\/\//, path)
    // The exact header set: bearer only. No API key, no content type (these
    // requests have no body), nothing else.
    assert.deepEqual(options.headers, { Authorization: 'Bearer supabase-access-token' })
    assert.equal(options.headers['X-Brevitas-Key'], undefined)
    assert.equal(options.body, undefined)
  }
})

test('billing errors carry the HTTP status, which is what makes the 409 portal fallback work', async () => {
  // The real shape: POST /api/billing/checkout answers 409 {action:'portal'}
  // when an occupying subscription already exists, and the UI must convert that
  // into a portal redirect instead of showing an error.
  const conflict = await startBillingCheckout('supabase-access-token', {
    request: async () => json({ error: 'Billing is already active', action: 'portal' }, 409),
  }).then(() => null, thrown => thrown)

  assert.ok(conflict instanceof Error)
  assert.equal(conflict.status, 409)
  assert.equal(conflict.message, 'Billing is already active')

  // Every status the billing routes can produce must round-trip, because the UI
  // distinguishes 409 from everything else.
  for (const status of [400, 401, 403, 409, 415, 429, 500, 503]) {
    const error = await fetchBillingStatus('supabase-access-token', {
      request: async () => json({ error: `failure ${status}` }, status),
    }).then(() => null, thrown => thrown)
    assert.ok(error instanceof Error, String(status))
    assert.equal(error.status, status, String(status))
    assert.equal(error.message, `failure ${status}`, String(status))
  }

  // A non-JSON error body must still produce a usable status rather than
  // throwing inside the error path.
  const opaque = await openBillingPortal('supabase-access-token', {
    request: async () => new Response('<html>upstream</html>', { status: 502 }),
  }).then(() => null, thrown => thrown)
  assert.equal(opaque.status, 502)
  assert.equal(opaque.message, 'Request failed (502)')
})

test('billing error text is redacted and cannot smuggle a credential into the UI', async () => {
  const error = await openBillingPortal('supabase-access-token', {
    request: async () => json({ error: 'Stripe rejected sk_test_abcdefgh12345678' }, 500),
  }).then(() => null, thrown => thrown)
  assert.equal(error.status, 500)
  assert.ok(!error.message.includes('sk_test_abcdefgh12345678'), error.message)
  assert.match(error.message, /\[REDACTED\]/)
})

test('a successful billing response is returned as parsed JSON', async () => {
  const body = {
    configured: true,
    subscription_status: 'active',
    period_tracking_valid: true,
    estimated_fee_usd: 22.5,
    settlement_pending: false,
  }
  assert.deepEqual(
    await fetchBillingStatus('supabase-access-token', { request: async () => json(body) }),
    body,
  )
  // Caller-supplied headers are merged, and cannot drop the Authorization
  // header by accident (spread order: Authorization first, options.headers
  // after — so a caller CAN override it; pinned as measured behaviour).
  const recorder = billingRecorder()
  await fetchBillingStatus('supabase-access-token', {
    request: recorder.request,
    headers: { 'X-Request-ID': 'dashboard-1234' },
  })
  assert.deepEqual(recorder.calls[0][1].headers, {
    Authorization: 'Bearer supabase-access-token',
    'X-Request-ID': 'dashboard-1234',
  })
})
