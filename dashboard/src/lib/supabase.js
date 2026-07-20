import { createClient } from '@supabase/supabase-js'
import { redactBrowserError } from './api.js'

const env = import.meta.env || {}
const url = env.VITE_SUPABASE_URL
const key = env.VITE_SUPABASE_ANON_KEY

const decodeJwtRole = value => {
  const parts = String(value).split('.')
  if (parts.length !== 3 || typeof globalThis.atob !== 'function') return ''
  try {
    const payload = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const parsed = JSON.parse(globalThis.atob(payload.padEnd(Math.ceil(payload.length / 4) * 4, '=')))
    return typeof parsed.role === 'string' ? parsed.role : ''
  } catch {
    return ''
  }
}

export function supabasePublicKeyKind(value) {
  if (typeof value !== 'string' || !value) return 'missing'
  if (value.startsWith('sb_publishable_')) return 'publishable'
  if (value.startsWith('sb_secret_')) return 'service-secret'
  const role = decodeJwtRole(value)
  if (role === 'anon') return 'anon'
  if (role === 'service_role') return 'service-secret'
  return 'invalid'
}

const keyKind = supabasePublicKeyKind(key)
if (keyKind === 'service-secret') {
  throw new Error('Unsafe Supabase browser credential configuration')
}

export const supabaseMisconfigured = !url || !key || !['anon', 'publishable'].includes(keyKind)
export const supabaseCredentialKind = supabaseMisconfigured ? 'missing' : keyKind

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key)

export const authModeForPath = pathname => /^\/(signup|waitlist)\/?$/.test(pathname) ? 'signup' : 'login'

export const SESSION_KEY_CACHE_MAX_ENTRIES = 128
export const SESSION_KEY_CACHE_TTL_MS = 15 * 60 * 1000
export const SESSION_KEY_MINT_MAX_IN_FLIGHT = 128
const inMemorySessionKeys = new Map()
const mintInFlight = new Map()
let sessionCacheGeneration = 0

const pruneSessionKeys = (now = Date.now()) => {
  for (const [userId, entry] of inMemorySessionKeys) {
    if (entry.expiresAt <= now) inMemorySessionKeys.delete(userId)
  }
}

const cachedApiKey = (userId, now = Date.now()) => {
  pruneSessionKeys(now)
  const entry = inMemorySessionKeys.get(userId)
  if (!entry) return null
  // Refresh LRU order without extending the absolute credential lifetime.
  inMemorySessionKeys.delete(userId)
  inMemorySessionKeys.set(userId, entry)
  return entry.apiKey
}

export const clearSessionKeyCache = () => {
  inMemorySessionKeys.clear()
  sessionCacheGeneration = (sessionCacheGeneration + 1) % Number.MAX_SAFE_INTEGER
}
export const sessionKeyCacheSize = () => {
  pruneSessionKeys()
  return inMemorySessionKeys.size
}

/**
 * Mint a fresh Brevitas API key from the backend.
 * @returns {Promise<string>}
 */
async function mintApiKey(accessToken, request = fetch) {
  let res
  try {
    res = await request('/v1/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${accessToken}` },
      body: JSON.stringify({ name: 'dashboard session', purpose: 'dashboard_session' }),
    })
  } catch (error) {
    throw new Error(redactBrowserError(error?.message) || 'Failed to create session credential')
  }
  if (!res.ok) {
    const error = await res.json().catch(() => ({}))
    throw new Error(redactBrowserError(error.detail) || `Failed to create API key (${res.status})`)
  }
  const result = await res.json().catch(() => null)
  const api_key = result?.api_key
  if (typeof api_key !== 'string' || !api_key || api_key.length > 4096) {
    throw new Error('Invalid session credential response')
  }
  return api_key
}

export async function cachedKeyIsValid(apiKey, request = fetch) {
  if (!apiKey) return false
  let res
  try {
    res = await request('/v1/stats', { headers: { 'X-Brevitas-Key': apiKey } })
  } catch (error) {
    throw new Error(redactBrowserError(error?.message) || 'Could not validate session credential')
  }
  if (res.ok) return true
  if (res.status === 401) return false
  const error = await res.json().catch(() => ({}))
  throw new Error(redactBrowserError(error.detail) || `Could not validate API key (${res.status})`)
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
  void client
  if (
    typeof userId !== 'string' || !userId || userId.length > 256
    || typeof apiKey !== 'string' || !apiKey || apiKey.length > 4096
  ) {
    throw new Error('Invalid session credential')
  }
  const now = Date.now()
  pruneSessionKeys(now)
  inMemorySessionKeys.delete(userId)
  inMemorySessionKeys.set(userId, { apiKey, expiresAt: now + SESSION_KEY_CACHE_TTL_MS })
  while (inMemorySessionKeys.size > SESSION_KEY_CACHE_MAX_ENTRIES) {
    inMemorySessionKeys.delete(inMemorySessionKeys.keys().next().value)
  }
}

/**
 * Return a working Brevitas API key for a user, self-healing stale keys.
 *
 * Reuses a key only for the current browser session. Raw credentials are never
 * written to Supabase/Postgres; the backend displays each newly minted secret once.
 *
 * @param {string} userId
 * @returns {Promise<string>}
 */
export async function getOrCreateApiKey(userId, accessToken, client = supabase, request = fetch) {
  void client
  if (
    typeof userId !== 'string' || !userId || userId.length > 256
    || typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384
  ) {
    throw new Error('Invalid authenticated session')
  }
  const existing = mintInFlight.get(userId)
  if (existing) return existing
  if (mintInFlight.size >= SESSION_KEY_MINT_MAX_IN_FLIGHT) {
    throw new Error('Too many concurrent credential requests')
  }

  const generation = sessionCacheGeneration
  const operation = Promise.resolve().then(async () => {
    if (generation !== sessionCacheGeneration) {
      throw new Error('Session credential request was cancelled')
    }
    const cached = cachedApiKey(userId)
    if (cached) {
      const valid = await cachedKeyIsValid(cached, request)
      if (generation !== sessionCacheGeneration) {
        throw new Error('Session credential request was cancelled')
      }
      if (valid) return cached
      inMemorySessionKeys.delete(userId)
    }

    // Missing/stale key -> replace the short-lived dashboard credential. Long-lived
    // organization service keys are created only by an explicit admin action.
    const apiKey = await mintApiKey(accessToken, request)
    if (generation !== sessionCacheGeneration) {
      throw new Error('Session credential request was cancelled')
    }
    await cacheApiKey(userId, apiKey, client)
    return apiKey
  })
  mintInFlight.set(userId, operation)
  try {
    return await operation
  } finally {
    if (mintInFlight.get(userId) === operation) mintInFlight.delete(userId)
  }
}
