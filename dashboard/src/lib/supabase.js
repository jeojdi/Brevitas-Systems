import { createClient } from '@supabase/supabase-js'

const env = import.meta.env || {}
const url = env.VITE_SUPABASE_URL
const key = env.VITE_SUPABASE_ANON_KEY

export const supabaseMisconfigured = !url || !key

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key)

export const authModeForPath = pathname => /^\/(signup|waitlist)\/?$/.test(pathname) ? 'signup' : 'login'

/**
 * Mint a fresh Brevitas API key from the backend.
 * @returns {Promise<string>}
 */
async function mintApiKey(accessToken, request = fetch) {
  const res = await request('/v1/keys', {
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

export async function cachedKeyIsValid(apiKey, request = fetch) {
  if (!apiKey) return false
  const res = await request('/v1/stats', { headers: { 'X-Brevitas-Key': apiKey } })
  if (res.ok) return true
  if (res.status === 401) return false
  const error = await res.json().catch(() => ({}))
  throw new Error(error.detail || `Could not validate API key (${res.status})`)
}

export async function resendSignupConfirmation(email, redirectTo, auth = supabase.auth) {
  const { error } = await auth.resend({
    type: 'signup',
    email,
    options: { emailRedirectTo: redirectTo },
  })
  if (error) throw error
}

export async function cacheApiKey(userId, apiKey, client = supabase) {
  const { error } = await client
    .from('user_keys')
    .upsert({ user_id: userId, api_key: apiKey }, { onConflict: 'user_id' })
  if (error) throw error
}

/**
 * Return a working Brevitas API key for a user, self-healing stale keys.
 *
 * Reuses the account's key from Supabase or mints one for a new account.
 *
 * @param {string} userId
 * @returns {Promise<string>}
 */
export async function getOrCreateApiKey(userId, accessToken, client = supabase, request = fetch) {
  // 1. Look up any cached key (ignore errors — e.g. a missing user_keys table).
  let cached = null
  try {
    const { data } = await client
      .from('user_keys')
      .select('api_key')
      .eq('user_id', userId)
      .maybeSingle()
    cached = data?.api_key ?? null
  } catch {
    cached = null
  }

  if (cached && await cachedKeyIsValid(cached, request)) return cached

  // Missing/stale key -> mint a fresh one and replace the dashboard cache.
  const apiKey = await mintApiKey(accessToken, request)
  try {
    await cacheApiKey(userId, apiKey, client)
  } catch {
    // non-fatal: a missing table or RLS must not block using the freshly minted key
  }
  return apiKey
}
