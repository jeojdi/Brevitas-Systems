import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  COMPANY_INVITATION_ENDPOINT,
  acceptCompanyInvitation,
  consumeCompanyInvitationFragment,
  isCompanyInvitationToken,
} from './company-invitation.js'

const TOKEN = `bvi_${'a'.repeat(43)}`
const COMPANY = '11111111-1111-4111-8111-111111111111'
const ACCESS_TOKEN = 'verified-session-token-value'

test('invitation tokens use the bounded backend format', () => {
  assert.equal(isCompanyInvitationToken(TOKEN), true)
  assert.equal(isCompanyInvitationToken('bvi_short'), false)
  assert.equal(isCompanyInvitationToken(`bvi_${'a'.repeat(125)}`), false)
  assert.equal(isCompanyInvitationToken(`bvi_${'a'.repeat(42)}!`), false)
})

test('fragment capture scrubs the secret while preserving unrelated fragment state', () => {
  const location = {
    pathname: '/dashboard/',
    search: '?source=email',
    hash: `#view=company&invite=${TOKEN}`,
  }
  const calls = []
  const result = consumeCompanyInvitationFragment({
    location,
    history: {
      state: { safe: true },
      replaceState: (...values) => calls.push(values),
    },
  })

  assert.deepEqual(result, { found: true, token: TOKEN })
  assert.deepEqual(calls, [[{ safe: true }, '', '/dashboard/?source=email#view=company']])
  assert.doesNotMatch(calls[0][2], /bvi_/)
})

test('malformed and ambiguous invitation fragments are scrubbed but rejected', () => {
  for (const hash of ['#invite=bvi_too_short', `#invite=${TOKEN}&invitation=${TOKEN}`]) {
    const calls = []
    const result = consumeCompanyInvitationFragment({
      location: { pathname: '/dashboard/', search: '', hash },
      history: { replaceState: (...values) => calls.push(values) },
    })
    assert.deepEqual(result, { found: true, token: '' })
    assert.equal(calls.length, 1)
    assert.doesNotMatch(calls[0][2], /bvi_/)
  }
})

test('fragment capture falls back to clearing the hash when History is unavailable', () => {
  const location = { pathname: '/dashboard/', search: '', hash: `#invite=${TOKEN}` }
  const result = consumeCompanyInvitationFragment({ location, history: null })
  assert.deepEqual(result, { found: true, token: TOKEN })
  assert.equal(location.hash, '')
})

test('acceptance uses authenticated no-store same-origin POST with a request id', async () => {
  const calls = []
  const result = await acceptCompanyInvitation(ACCESS_TOKEN, TOKEN, {
    requestId: () => 'request-invite-accept-001',
    request: async (...values) => {
      calls.push(values)
      return Response.json({ company_id: COMPANY, role: 'member', status: 'accepted' })
    },
  })

  assert.deepEqual(result, { company_id: COMPANY, role: 'member', status: 'accepted' })
  assert.equal(calls[0][0], COMPANY_INVITATION_ENDPOINT)
  const options = calls[0][1]
  assert.equal(options.method, 'POST')
  assert.equal(options.headers.Authorization, `Bearer ${ACCESS_TOKEN}`)
  assert.equal(options.headers['X-Request-ID'], 'request-invite-accept-001')
  assert.equal(options.cache, 'no-store')
  assert.equal(options.credentials, 'same-origin')
  assert.equal(options.redirect, 'error')
  assert.equal(options.referrerPolicy, 'no-referrer')
  assert.deepEqual(JSON.parse(options.body), { invitation_token: TOKEN })
})

test('acceptance requires authentication and never exposes backend secret details', async () => {
  let requested = false
  await assert.rejects(
    acceptCompanyInvitation('', TOKEN, { request: async () => { requested = true } }),
    error => error.code === 'authentication_required' && !error.message.includes(TOKEN),
  )
  assert.equal(requested, false)

  await assert.rejects(
    acceptCompanyInvitation(ACCESS_TOKEN, TOKEN, {
      requestId: () => 'request-invite-denied-001',
      request: async () => Response.json(
        { detail: `denied token=${TOKEN} Bearer backend-private` },
        { status: 403 },
      ),
    }),
    error => error.code === 'invitation_forbidden'
      && !error.message.includes(TOKEN)
      && !error.message.includes('backend-private'),
  )
})

test('acceptance rejects malformed success payloads', async () => {
  await assert.rejects(
    acceptCompanyInvitation(ACCESS_TOKEN, TOKEN, {
      requestId: () => 'request-invalid-response-001',
      request: async () => Response.json({
        company_id: COMPANY,
        role: 'company_owner',
        status: 'accepted',
      }),
    }),
    error => error.code === 'invalid_response',
  )
})

test('invitation UI keeps secrets out of storage and analytics capture', async () => {
  const source = await readFile(
    new URL('../components/InvitationAcceptance.jsx', import.meta.url),
    'utf8',
  )
  assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage)\b/)
  assert.match(source, /data-ph-sensitive/)
  assert.match(source, /ph-no-capture/)
  assert.match(source, /onUseDifferentAccount/)
})
