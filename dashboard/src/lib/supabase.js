import { createClient } from '@supabase/supabase-js'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_ANON_KEY

export const supabaseMisconfigured = !url || !key

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key)

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
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(error.detail || `Failed to create API key (${res.status})`)
  }
  const { api_key } = await res.json()
  return api_key
}

/**
 * Return a working Brevitas API key for a user, self-healing stale keys.
 *
 * Reuses the account's key from Supabase or mints one for a new account.
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

  // The API-key store is also persisted in Supabase, so a cached account key remains valid.
  if (cached) return cached

  // Cached key missing -> mint a fresh one and save it for later sessions.
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
