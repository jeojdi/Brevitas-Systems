import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'

import {
  apiJson, apiKeyId, compress, createKey, saveProvider, streamCompression,
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
    path, options.method, options.headers['X-Brevitas-Key'], JSON.parse(options.body),
  ]), [
    ['/v1/keys', 'POST', 'bvt_active', { name: 'dashboard' }],
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

test('active key id matches the backend SHA-256 identifier', async () => {
  assert.equal(await apiKeyId('bvt_active'), createHash('sha256').update('bvt_active').digest('hex'))
})
