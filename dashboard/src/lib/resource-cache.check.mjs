import assert from 'node:assert/strict'
import test from 'node:test'

import {
  cacheGeneration, clearResourceCache, peekResource, resourceCacheSize, resourceKey,
  revalidateResource,
} from './resource-cache.js'

const deferred = () => {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

test('a cached read is shared, deduped, and reused inside its TTL', async () => {
  clearResourceCache()
  let calls = 0
  const fetcher = async () => { calls += 1; return { total_calls: 7 } }
  const key = resourceKey('/v1/stats', 'bvt_alpha')

  // Two panels mounting at once must produce one request, not two.
  const [a, b] = await Promise.all([
    revalidateResource(key, fetcher),
    revalidateResource(key, fetcher),
  ])
  assert.deepEqual(a, { total_calls: 7 })
  assert.deepEqual(b, { total_calls: 7 })
  assert.equal(calls, 1)

  // A revisit inside the TTL is served from memory — this is the tab switch.
  await revalidateResource(key, fetcher, { ttlMs: 60_000 })
  assert.equal(calls, 1)
  assert.deepEqual(peekResource(key).data, { total_calls: 7 })

  // An explicit reload ignores the TTL.
  await revalidateResource(key, fetcher, { ttlMs: 60_000, force: true })
  assert.equal(calls, 2)
})

test('one identity never reads another identity cache entry', async () => {
  clearResourceCache()
  const billed = resourceKey('/v1/stats', 'bvt_has_billing')
  const member = resourceKey('/v1/stats', 'bvt_no_billing')
  assert.notEqual(billed, member)

  await revalidateResource(billed, async () => ({ total_actual_cost_usd: 74.26 }))
  // A member without billing access gets the redacted payload the API sent them,
  // never the priced one already resident for the privileged credential.
  await revalidateResource(member, async () => ({ spend_redacted: true }))

  assert.deepEqual(peekResource(billed).data, { total_actual_cost_usd: 74.26 })
  assert.deepEqual(peekResource(member).data, { spend_redacted: true })
  // A read with no credential is not cacheable at all.
  assert.equal(resourceKey('/v1/stats', ''), '')
})

test('a fetch in flight across sign-out is discarded, not published', async () => {
  clearResourceCache()
  const key = resourceKey('/v1/stats', 'bvt_departing')
  const gate = deferred()
  const pending = revalidateResource(key, () => gate.promise)

  const before = cacheGeneration()
  // Sign-out: the map empties and the generation moves.
  clearResourceCache()
  assert.notEqual(cacheGeneration(), before)
  assert.equal(resourceCacheSize(), 0)

  // The in-flight request now resolves with the departed session's dollars.
  gate.resolve({ total_actual_cost_usd: 999.99 })
  assert.equal(await pending, undefined)
  // It must not have seeded the cache for whoever is here now.
  assert.equal(resourceCacheSize(), 0)
  assert.equal(peekResource(key), undefined)
})

test('a failed refresh keeps the last good payload on screen', async () => {
  clearResourceCache()
  const key = resourceKey('/v1/stats/cache', 'bvt_alpha')
  await revalidateResource(key, async () => ({ cache_hit_rate_pct: 97.47 }))

  await assert.rejects(
    revalidateResource(key, async () => { throw new Error('upstream 500') }, { force: true }),
    /upstream 500/,
  )

  const entry = peekResource(key)
  // The live tick failing must not blank a populated dashboard.
  assert.deepEqual(entry.data, { cache_hit_rate_pct: 97.47 })
  assert.match(entry.error.message, /upstream 500/)
})

test('an aborted read leaves no error for the UI to render', async () => {
  clearResourceCache()
  const key = resourceKey('/v1/stats/activity', 'bvt_alpha')
  const abort = Object.assign(new Error('aborted'), { name: 'AbortError' })
  assert.equal(await revalidateResource(key, async () => { throw abort }), undefined)
  assert.equal(peekResource(key)?.error, undefined)
})

test('the shell drops cached payloads wherever it drops the API key', async () => {
  const { readFile } = await import('node:fs/promises')
  const shell = await readFile(new URL('../App.jsx', import.meta.url), 'utf8')
  // The key and the payloads it authorised share one lifetime. Pairing them in one
  // helper is what stops a future sign-out/switch path from clearing the credential
  // and leaving another account's dollar figures resident in memory.
  assert.match(shell, /const clearIdentityCaches = \(\) => \{\s*clearSessionKeyCache\(\)\s*clearResourceCache\(\)\s*\}/)
  // Every call site goes through the pair, so neither can be cleared alone. Sliced
  // past the helper's own closing brace: the two calls inside it are the definition.
  const helperStart = shell.indexOf('const clearIdentityCaches')
  const body = shell.slice(shell.indexOf('}', shell.indexOf('clearResourceCache()', helperStart)) + 1)
  assert.doesNotMatch(body, /clearSessionKeyCache\(\)/)
  assert.doesNotMatch(body, /clearResourceCache\(\)/)
  // Sign-out, auth-state loss, identity change, company switch, invitation accept.
  assert.ok(
    body.match(/clearIdentityCaches\(\)/g)?.length >= 5,
    'every credential-clearing path must clear both caches',
  )
})

test('the cache is memory-only: no dollar figure is written to web storage', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('./resource-cache.js', import.meta.url), 'utf8')
  // Comments stripped first: the preamble names these APIs to explain why they are
  // not used, and asserting over prose would fail on the explanation itself.
  const code = source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')
  // Money must not outlive the tab. See supabase.js on why this app keeps sensitive
  // values in memory rather than origin-scoped storage.
  assert.doesNotMatch(code, /localStorage|sessionStorage|indexedDB|document\.cookie/)
})
