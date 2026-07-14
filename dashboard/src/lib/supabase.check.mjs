import assert from 'node:assert/strict'
import test from 'node:test'

import { authModeForPath, cachedKeyIsValid, getOrCreateApiKey, resendSignupConfirmation } from './supabase.js'

test('signup routes open account creation while login routes stay login', () => {
  assert.equal(authModeForPath('/signup'), 'signup')
  assert.equal(authModeForPath('/waitlist/'), 'signup')
  assert.equal(authModeForPath('/login'), 'login')
  assert.equal(authModeForPath('/dashboard'), 'login')
})

test('cached keys self-heal only when authentication rejects them', async () => {
  assert.equal(await cachedKeyIsValid('valid', async () => ({ ok: true, status: 200 })), true)
  assert.equal(await cachedKeyIsValid('stale', async () => ({ ok: false, status: 401 })), false)
  await assert.rejects(
    cachedKeyIsValid('unknown', async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'Authentication store unavailable' }),
    })),
    /Authentication store unavailable/,
  )
})

test('confirmation resend uses the signup flow and requested redirect', async () => {
  let request
  await resendSignupConfirmation('person@example.com', 'https://example.com/confirmed', {
    resend: async value => { request = value; return { error: null } },
  })
  assert.deepEqual(request, {
    type: 'signup',
    email: 'person@example.com',
    options: { emailRedirectTo: 'https://example.com/confirmed' },
  })
})

test('stale cached keys are replaced using the Supabase bearer token', async () => {
  let saved
  const table = {
    select() { return this },
    eq() { return this },
    async maybeSingle() { return { data: { api_key: 'stale' } } },
    async upsert(value) { saved = value; return { error: null } },
  }
  const calls = []
  const request = async (path, options) => {
    calls.push([path, options])
    if (options.method === 'POST') return new Response(JSON.stringify({ api_key: 'fresh' }), { status: 200 })
    return new Response(JSON.stringify({ detail: 'Invalid API key' }), { status: 401 })
  }

  const key = await getOrCreateApiKey('user-1', 'supabase-jwt', { from: () => table }, request)

  assert.equal(key, 'fresh')
  assert.equal(calls[1][1].headers.Authorization, 'Bearer supabase-jwt')
  assert.deepEqual(saved, { user_id: 'user-1', api_key: 'fresh' })
})
