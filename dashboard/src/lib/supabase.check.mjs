import assert from 'node:assert/strict'
import test from 'node:test'

import { cachedKeyIsValid, resendSignupConfirmation } from './supabase.js'

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
