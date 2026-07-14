import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_ANON_KEY

export const supabaseMisconfigured = !url || !key

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key)

/**
 * Whether the backend still recognises an API key. The backend stores keys separately
 * from the dashboard's Supabase cache, so a backend redeploy can invalidate a cached key.
 * We must validate before trusting a cached key, otherwise the user is stuck with a dead one.
 * @param {string} apiKey
 * @returns {Promise<boolean>}
 */
async function keyIsValid(apiKey) {
  if (!apiKey) return false
  try {
    const res = await fetch('/v1/stats', { headers: { 'X-Brevitas-Key': apiKey } })
    return res.ok
  } catch {
    return false
  }
}

/**
 * Mint a fresh Brevitas API key from the backend.
 * @returns {Promise<string>}
 */
async function mintApiKey(accessToken) {
  const res = await fetch('/v1/keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${accessToken}` },
    body: JSON.stringify({ name: 'dashboard' }),
  })
  if (!res.ok) throw new Error('Failed to create API key')
  const { api_key } = await res.json()
  return api_key
}

/**
 * Return a working Brevitas API key for a user, self-healing stale keys.
 *
 * Reads any cached key from Supabase, validates it against the backend, and — if it is
 * missing or no longer valid — mints a fresh one and overwrites the cache. This survives
 * backend redeploys that reset the key store (the previous version handed back a dead
 * cached key forever, which showed as "Failed to load stats").
 *
 * @param {string} userId
 * @returns {Promise<string>}
 */
export async function getOrCreateApiKey(userId, accessToken) {
  // 1. Look up any cached key (ignore errors — e.g. a missing user_keys table).
  let cached = null
  try {
    const { data } = await supabase
      .from('user_keys')
      .select('api_key')
      .eq('user_id', userId)
      .maybeSingle()
    cached = data?.api_key ?? null
  } catch {
    cached = null
  }

  // 2. Reuse the cached key only if the backend still accepts it.
  if (cached && (await keyIsValid(cached))) return cached

  // 3. Cached key missing or stale -> mint a fresh one and overwrite the cache.
  const apiKey = await mintApiKey(accessToken)
  try {
    await supabase
      .from('user_keys')
      .upsert({ user_id: userId, api_key: apiKey }, { onConflict: 'user_id' })
  } catch {
    // non-fatal: a missing table or RLS must not block using the freshly minted key
  }
  return apiKey
}
