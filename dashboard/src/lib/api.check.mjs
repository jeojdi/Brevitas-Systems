import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'

import {
  apiJson, apiKeyId, compress, configureApiAuthenticationRecovery, createKey,
  redactBrowserError, saveProvider, streamCompression,
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
