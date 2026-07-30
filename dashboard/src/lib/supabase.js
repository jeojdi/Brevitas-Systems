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

export const AUTH_SERVER_ERROR_TTL_MS = 30 * 1000
export const AUTH_UNKNOWN_ERROR_MESSAGE =
  'Something went wrong on our end. Please try again in a moment.'

let lastAuthServerError = null

export const clearCapturedAuthServerError = () => { lastAuthServerError = null }

const serverErrorMessage = body => {
  if (typeof body !== 'object' || body === null) return ''
  for (const field of ['msg', 'message', 'error_description', 'error']) {
    if (typeof body[field] === 'string' && body[field]) return body[field]
  }
  return ''
}

/**
 * Wrap fetch so 5xx bodies from GoTrue survive.
 *
 * `@supabase/auth-js` treats every status in its NETWORK_ERROR_CODES list
 * (500-504, 520-530) as retryable and builds the thrown error from the raw
 * `Response` rather than the parsed body. `Response` has no enumerable own
 * properties, so the message degrades to `JSON.stringify(response)` — the
 * literal string `{}` — and the server's explanation is lost. Stash it here so
 * `authErrorMessage` can put it back.
 */
export function createAuthErrorCapturingFetch(baseFetch = fetch, now = Date.now) {
  return async (input, init) => {
    const response = await baseFetch(input, init)
    if (response.status >= 500) {
      const body = await response.clone().json().catch(() => null)
      const message = redactBrowserError(serverErrorMessage(body))
      lastAuthServerError = message ? { message, at: now() } : null
    }
    return response
  }
}

/**
 * Resolve the message to show a user for a failed Supabase auth call.
 *
 * Auth requests are serialized behind a disabled submit button, so the single
 * captured slot is read back by the call that produced it.
 */
export function authErrorMessage(error, now = Date.now()) {
  const raw = typeof error?.message === 'string' ? error.message.trim() : ''
  if (raw && raw !== '{}') return raw
  const captured = lastAuthServerError
  if (captured && now - captured.at <= AUTH_SERVER_ERROR_TTL_MS) return captured.message
  return AUTH_UNKNOWN_ERROR_MESSAGE
}

/**
 * Session storage for the browser client, stated rather than inherited.
 *
 * auth-js persists the access token AND the long-lived refresh token in
 * `localStorage` under `sb-<ref>-auth-token`. That is origin-scoped, so any script
 * that executes anywhere on brevitassystems.com can read it — which is why the
 * Brevitas API key is kept in the in-memory Map below instead, and why the real
 * mitigation for this credential is an origin-wide script-src CSP plus SRI on the
 * marketing pages' CDN scripts (next.config.ts applies a CSP to /dashboard and the
 * auth routes only). There is no httpOnly option here: `storage` accepts a
 * synchronous key/value adapter, and every alternative (sessionStorage, in-memory)
 * ends session persistence across tab close, which is a functional regression, not
 * a fix.
 *
 * These options are all the current auth-js defaults, pinned deliberately so a
 * library default flip cannot silently move where the session lives or how it is
 * recovered. `flowType` in particular must stay 'implicit': public/email-confirmed.html
 * forwards GoTrue's `#access_token=` fragment to /login for `detectSessionInUrl` to
 * consume, and a pkce client ignores that fragment (it wants `?code=` plus a stored
 * verifier), so flipping it without repointing the Supabase email templates lands
 * every newly confirmed user back on the login screen.
 */
export const AUTH_CLIENT_OPTIONS = Object.freeze({
  flowType: 'implicit',
  persistSession: true,
  autoRefreshToken: true,
  detectSessionInUrl: true,
})

export const supabase = supabaseMisconfigured
  ? null
  : createClient(url, key, {
      auth: { ...AUTH_CLIENT_OPTIONS },
      global: { fetch: createAuthErrorCapturingFetch() },
    })

export const authModeForPath = pathname => /^\/(signup|waitlist)\/?$/.test(pathname) ? 'signup' : 'login'

export const LOGIN_AUDIENCE = Object.freeze({
  PERSONAL: 'personal',
  ENTERPRISE: 'enterprise',
})

export const loginAudienceForPath = pathname => {
  const match = /^\/login\/(personal|enterprise)\/?$/.exec(String(pathname || ''))
  return match?.[1] || ''
}

export const confirmationPathForLoginAudience = audience => (
  Object.values(LOGIN_AUDIENCE).includes(audience)
    ? `/email-confirmed?audience=${audience}`
    : '/email-confirmed'
)

export const SESSION_KEY_CACHE_MAX_ENTRIES = 128
export const SESSION_KEY_CACHE_TTL_MS = 15 * 60 * 1000
export const SESSION_KEY_MINT_MAX_IN_FLIGHT = 128
const inMemorySessionKeys = new Map()
const mintInFlight = new Map()
let sessionCacheGeneration = 0

const sessionScope = (userId, companyId) => `${userId}\u0000${companyId}`

const pruneSessionKeys = (now = Date.now()) => {
  for (const [userId, entry] of inMemorySessionKeys) {
    if (entry.expiresAt <= now) inMemorySessionKeys.delete(userId)
  }
}

const cachedApiKey = (scope, now = Date.now()) => {
  pruneSessionKeys(now)
  const entry = inMemorySessionKeys.get(scope)
  if (!entry) return null
  // Refresh LRU order without extending the absolute credential lifetime.
  inMemorySessionKeys.delete(scope)
  inMemorySessionKeys.set(scope, entry)
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
async function mintApiKey(accessToken, companyId, request = fetch) {
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
  if (result?.organization_id !== companyId) {
    throw new Error('Session credential tenant mismatch')
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

export async function cacheApiKey(userId, companyId, apiKey, client = supabase) {
  void client
  if (
    typeof userId !== 'string' || !userId || userId.length > 256
    || typeof companyId !== 'string' || !companyId || companyId.length > 256
    || typeof apiKey !== 'string' || !apiKey || apiKey.length > 4096
  ) {
    throw new Error('Invalid session credential')
  }
  const scope = sessionScope(userId, companyId)
  const now = Date.now()
  pruneSessionKeys(now)
  inMemorySessionKeys.delete(scope)
  inMemorySessionKeys.set(scope, { apiKey, expiresAt: now + SESSION_KEY_CACHE_TTL_MS })
  while (inMemorySessionKeys.size > SESSION_KEY_CACHE_MAX_ENTRIES) {
    inMemorySessionKeys.delete(inMemorySessionKeys.keys().next().value)
  }
}

export function invalidateCachedApiKey(userId, companyId, rejectedApiKey) {
  const scope = sessionScope(userId, companyId)
  const entry = inMemorySessionKeys.get(scope)
  if (entry?.apiKey === rejectedApiKey) inMemorySessionKeys.delete(scope)
}

/**
 * Return a working Brevitas API key for a user, self-healing stale keys.
 *
 * Reuses a key only for the current browser session. Raw credentials are never
 * written to Supabase/Postgres; the backend displays each newly minted secret once.
 *
 * @param {string} userId
 * @param {string} companyId
 * @returns {Promise<string>}
 */
export async function getOrCreateApiKey(
  userId, accessToken, companyId, client = supabase, request = fetch,
) {
  void client
  if (
    typeof userId !== 'string' || !userId || userId.length > 256
    || typeof accessToken !== 'string' || !accessToken || accessToken.length > 16_384
    || typeof companyId !== 'string' || !companyId || companyId.length > 256
  ) {
    throw new Error('Invalid authenticated session')
  }
  const scope = sessionScope(userId, companyId)
  const existing = mintInFlight.get(scope)
  if (existing) return existing
  if (mintInFlight.size >= SESSION_KEY_MINT_MAX_IN_FLIGHT) {
    throw new Error('Too many concurrent credential requests')
  }

  const generation = sessionCacheGeneration
  const operation = Promise.resolve().then(async () => {
    if (generation !== sessionCacheGeneration) {
      throw new Error('Session credential request was cancelled')
    }
    const cached = cachedApiKey(scope)
    if (cached) {
      const valid = await cachedKeyIsValid(cached, request)
      if (generation !== sessionCacheGeneration) {
        throw new Error('Session credential request was cancelled')
      }
      if (valid) return cached
      inMemorySessionKeys.delete(scope)
    }

    // Missing/stale key -> replace the short-lived dashboard credential. Long-lived
    // organization service keys are created only by an explicit admin action.
    const apiKey = await mintApiKey(accessToken, companyId, request)
    if (generation !== sessionCacheGeneration) {
      throw new Error('Session credential request was cancelled')
    }
    await cacheApiKey(userId, companyId, apiKey, client)
    return apiKey
  })
  mintInFlight.set(scope, operation)
  try {
    return await operation
  } finally {
    if (mintInFlight.get(scope) === operation) mintInFlight.delete(scope)
  }
}
