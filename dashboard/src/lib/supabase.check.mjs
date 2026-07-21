import assert from 'node:assert/strict'
import test from 'node:test'

import {
  SESSION_KEY_CACHE_MAX_ENTRIES,
  SESSION_KEY_MINT_MAX_IN_FLIGHT,
  SESSION_KEY_CACHE_TTL_MS,
  LOGIN_AUDIENCE,
  authModeForPath,
  cacheApiKey,
  cachedKeyIsValid,
  clearSessionKeyCache,
  confirmationPathForLoginAudience,
  getOrCreateApiKey,
  invalidateCachedApiKey,
  loginAudienceForPath,
  resendSignupConfirmation,
  sessionKeyCacheSize,
  supabasePublicKeyKind,
} from './supabase.js'

const COMPANY_A = '11111111-1111-4111-8111-111111111111'
const COMPANY_B = '22222222-2222-4222-8222-222222222222'

const jwtForRole = role => {
  const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'none' })}.${encode({ role })}.signature-value`
}

test('signup routes open account creation while login routes stay login', () => {
  assert.equal(authModeForPath('/signup'), 'signup')
  assert.equal(authModeForPath('/waitlist/'), 'signup')
  assert.equal(authModeForPath('/login'), 'login')
  assert.equal(authModeForPath('/dashboard'), 'login')
})

test('personal and enterprise login routes are exact presentation hints', () => {
  assert.equal(loginAudienceForPath('/login/personal'), LOGIN_AUDIENCE.PERSONAL)
  assert.equal(loginAudienceForPath('/login/personal/'), LOGIN_AUDIENCE.PERSONAL)
  assert.equal(loginAudienceForPath('/login/enterprise'), LOGIN_AUDIENCE.ENTERPRISE)
  assert.equal(loginAudienceForPath('/login/enterprise/'), LOGIN_AUDIENCE.ENTERPRISE)
  for (const pathname of ['/login', '/login/company', '/login/personal/admin', '/dashboard']) {
    assert.equal(loginAudienceForPath(pathname), '')
  }
  assert.equal(
    confirmationPathForLoginAudience(LOGIN_AUDIENCE.PERSONAL),
    '/email-confirmed?audience=personal',
  )
  assert.equal(
    confirmationPathForLoginAudience(LOGIN_AUDIENCE.ENTERPRISE),
    '/email-confirmed?audience=enterprise',
  )
  assert.equal(confirmationPathForLoginAudience('https://example.com'), '/email-confirmed')
})

test('browser Supabase config distinguishes public keys from service credentials', () => {
  assert.equal(supabasePublicKeyKind('sb_publishable_project-value'), 'publishable')
  assert.equal(supabasePublicKeyKind(jwtForRole('anon')), 'anon')
  assert.equal(supabasePublicKeyKind(['sb', 'secret_project-value'].join('_')), 'service-secret')
  assert.equal(supabasePublicKeyKind(jwtForRole('service_role')), 'service-secret')
  assert.equal(supabasePublicKeyKind('opaque-or-malformed'), 'invalid')
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
  await assert.rejects(
    cachedKeyIsValid('unknown', async () => ({
      ok: false,
      status: 503,
      json: async () => ({ detail: 'Bearer validation-private' }),
    })),
    error => !String(error).includes('validation-private'),
  )
  await assert.rejects(
    cachedKeyIsValid('unknown', async () => { throw new Error('sk_transport_private') }),
    error => !String(error).includes('sk_transport_private'),
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

test('new keys use the Supabase bearer token and never enter a database table', async () => {
  clearSessionKeyCache()
  let tableAccessed = false
  const calls = []
  const request = async (path, options) => {
    calls.push([path, options])
    if (options.method === 'POST') return new Response(JSON.stringify({
      api_key: 'fresh', organization_id: COMPANY_A,
    }), { status: 200 })
    return new Response(JSON.stringify({ detail: 'Invalid API key' }), { status: 401 })
  }

  const key = await getOrCreateApiKey('new-user-no-db-key', 'supabase-jwt', COMPANY_A, {
    from: () => { tableAccessed = true; throw new Error('raw key database access forbidden') },
  }, request)

  assert.equal(key, 'fresh')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer supabase-jwt')
  assert.equal(JSON.parse(calls[0][1].body).purpose, 'dashboard_session')
  assert.equal(tableAccessed, false)
  clearSessionKeyCache()
})

test('browser session credential cache has TTL and LRU size bounds', async () => {
  clearSessionKeyCache()
  const originalNow = Date.now
  let now = 10_000
  Date.now = () => now
  try {
    for (let index = 0; index <= SESSION_KEY_CACHE_MAX_ENTRIES; index += 1) {
      await cacheApiKey(`user-${index}`, COMPANY_A, `bvt_key_${index}`)
    }
    assert.equal(sessionKeyCacheSize(), SESSION_KEY_CACHE_MAX_ENTRIES)

    const methods = []
    const replacement = await getOrCreateApiKey('user-0', 'session-token', COMPANY_A, null, async (_path, options) => {
      methods.push(options.method || 'GET')
      return new Response(JSON.stringify({
        api_key: 'bvt_replaced', organization_id: COMPANY_A,
      }), { status: 200 })
    })
    assert.equal(replacement, 'bvt_replaced')
    assert.deepEqual(methods, ['POST'])

    now += SESSION_KEY_CACHE_TTL_MS + 1
    assert.equal(sessionKeyCacheSize(), 0)
  } finally {
    Date.now = originalNow
    clearSessionKeyCache()
  }
})

test('per-user single-flight mints one credential for concurrent callers', async () => {
  clearSessionKeyCache()
  await cacheApiKey('single-flight-user', COMPANY_A, 'bvt_revoked_stale')
  let release
  const gate = new Promise(resolve => { release = resolve })
  let posts = 0
  let validations = 0
  const request = async (_path, options) => {
    if (!options.method) {
      validations += 1
      return new Response(JSON.stringify({ detail: 'Invalid API key' }), { status: 401 })
    }
    posts += 1
    await gate
    return new Response(JSON.stringify({
      api_key: 'bvt_single_flight', organization_id: COMPANY_A,
    }), { status: 200 })
  }

  const first = getOrCreateApiKey('single-flight-user', 'session-token', COMPANY_A, null, request)
  const second = getOrCreateApiKey('single-flight-user', 'session-token', COMPANY_A, null, request)
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(validations, 1)
  assert.equal(posts, 1)
  release()
  assert.deepEqual(await Promise.all([first, second]), ['bvt_single_flight', 'bvt_single_flight'])
  assert.equal(sessionKeyCacheSize(), 1)
  clearSessionKeyCache()
})

test('clearSessionKeyCache is idempotent and invalidates in-flight mint responses', async () => {
  clearSessionKeyCache()
  let release
  const gate = new Promise(resolve => { release = resolve })
  let posts = 0
  const pending = getOrCreateApiKey('signout-user', 'session-token', COMPANY_A, null, async () => {
    posts += 1
    await gate
    return new Response(JSON.stringify({
      api_key: 'bvt_minted_after_signout', organization_id: COMPANY_A,
    }), { status: 200 })
  })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(posts, 1)

  clearSessionKeyCache()
  clearSessionKeyCache()
  release()
  await assert.rejects(pending, /request was cancelled/)
  assert.equal(sessionKeyCacheSize(), 0)
})

test('mint and getOrCreate error paths redact backend and transport credentials', async () => {
  clearSessionKeyCache()
  await assert.rejects(
    getOrCreateApiKey('error-user', 'session-token', COMPANY_A, null, async () => new Response(
      JSON.stringify({ detail: 'provider rejected sk_backend_private' }), { status: 502 },
    )),
    error => !String(error).includes('sk_backend_private'),
  )
  await assert.rejects(
    getOrCreateApiKey('transport-user', 'session-token', COMPANY_A, null, async () => {
      throw new Error('Bearer mint-transport-private')
    }),
    error => !String(error).includes('mint-transport-private'),
  )
  assert.equal(sessionKeyCacheSize(), 0)
  assert.equal(SESSION_KEY_MINT_MAX_IN_FLIGHT, SESSION_KEY_CACHE_MAX_ENTRIES)
})

test('dashboard cache is company scoped and invalidates only the rejected credential', async () => {
  clearSessionKeyCache()
  await cacheApiKey('multi-company-user', COMPANY_A, 'bvt_company_a')
  await cacheApiKey('multi-company-user', COMPANY_B, 'bvt_company_b')
  invalidateCachedApiKey('multi-company-user', COMPANY_A, 'bvt_company_a')
  assert.equal(sessionKeyCacheSize(), 1)

  const companyB = await getOrCreateApiKey(
    'multi-company-user', 'session-token', COMPANY_B, null,
    async (_path, options) => {
      assert.equal(options.headers['X-Brevitas-Key'], 'bvt_company_b')
      return new Response('{}', { status: 200 })
    },
  )
  assert.equal(companyB, 'bvt_company_b')

  const companyA = await getOrCreateApiKey(
    'multi-company-user', 'session-token', COMPANY_A, null,
    async () => new Response(JSON.stringify({
      api_key: 'bvt_company_a_replacement', organization_id: COMPANY_A,
    }), { status: 200 }),
  )
  assert.equal(companyA, 'bvt_company_a_replacement')
  assert.equal(sessionKeyCacheSize(), 2)
  clearSessionKeyCache()
  assert.equal(sessionKeyCacheSize(), 0)
})

test('mint rejects a credential bound to a different company', async () => {
  clearSessionKeyCache()
  await assert.rejects(
    getOrCreateApiKey(
      'tenant-mismatch-user', 'session-token', COMPANY_A, null,
      async () => new Response(JSON.stringify({
        api_key: 'bvt_wrong_tenant', organization_id: COMPANY_B,
      }), { status: 200 }),
    ),
    /tenant mismatch/,
  )
  assert.equal(sessionKeyCacheSize(), 0)
})
