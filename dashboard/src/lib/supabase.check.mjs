import assert from 'node:assert/strict'
import test from 'node:test'

import { cachedKeyIsValid } from './supabase.js'

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
