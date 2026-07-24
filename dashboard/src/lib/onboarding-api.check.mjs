import assert from 'node:assert/strict'
import test from 'node:test'

import {
  bootstrapWorkspace,
  completeOnboarding,
  fetchOnboardingStatus,
  normalizeOnboardingStatus,
} from './onboarding-api.js'

const COMPANY_ID = '11111111-1111-4111-8111-111111111111'

test('personal onboarding bootstraps an individual workspace with bearer authentication', async () => {
  const calls = []
  const result = await bootstrapWorkspace('verified-session-token', {
    workspaceType: 'personal', workspaceName: '',
  }, async (path, options) => {
    calls.push([path, options])
    return Response.json({
      company_id: COMPANY_ID,
      company_name: 'My workspace',
      role: 'company_owner',
      account_type: 'individual',
      created: true,
    })
  })

  assert.equal(result.company_id, COMPANY_ID)
  assert.equal(calls[0][0], '/v1/organization/bootstrap')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer verified-session-token')
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    account_type: 'individual', name: 'My workspace',
  })
})

test('company onboarding requires a name before sending a request', async () => {
  let requested = false
  await assert.rejects(bootstrapWorkspace('verified-session-token', {
    workspaceType: 'company', workspaceName: '',
  }, async () => {
    requested = true
    return Response.json({})
  }), /Enter your company name/)
  assert.equal(requested, false)
})

test('onboarding status is server-authoritative, tenant-bound, and uncached', async () => {
  const calls = []
  const status = await fetchOnboardingStatus('verified-session-token', {
    requestId: () => 'request-onboarding-status',
    request: async (path, options) => {
      calls.push([path, options])
      return Response.json({
        company_id: COMPANY_ID,
        status: 'pending',
        cli_connected: true,
        proxied_request_observed: false,
        completed_at: '',
      })
    },
  })

  assert.deepEqual(status, {
    companyId: COMPANY_ID,
    status: 'pending',
    cliConnected: true,
    proxiedRequestObserved: false,
    completedAt: '',
  })
  assert.equal(calls[0][0], '/v1/organization/onboarding')
  assert.equal(calls[0][1].method, 'GET')
  assert.equal(calls[0][1].cache, 'no-store')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer verified-session-token')
  assert.equal(calls[0][1].headers['X-Request-ID'], 'request-onboarding-status')
})

test('completion asks the server to verify evidence and rejects self-attestation', async () => {
  const calls = []
  const completed = await completeOnboarding('verified-session-token', {
    requestId: () => 'request-complete-onboarding',
    request: async (path, options) => {
      calls.push([path, options])
      return Response.json({
        company_id: COMPANY_ID,
        status: 'complete',
        cli_connected: true,
        proxied_request_observed: true,
        completed_at: '2026-07-20T12:00:00+00:00',
      })
    },
  })

  assert.equal(completed.status, 'complete')
  assert.equal(calls[0][0], '/v1/organization/onboarding/complete')
  assert.equal(calls[0][1].method, 'POST')
  assert.equal(calls[0][1].body, undefined)
  assert.equal(calls[0][1].headers['X-Request-ID'], 'request-complete-onboarding')

  await assert.rejects(completeOnboarding('verified-session-token', {
    request: async () => Response.json({
      detail: 'No successful request from a BVX-configured tool has reached the proxy yet.',
    }, { status: 409 }),
  }), error => error.status === 409 && /No successful request/.test(error.message))

  assert.throws(() => normalizeOnboardingStatus({
    company_id: COMPANY_ID,
    status: 'complete',
    cli_connected: false,
    proxied_request_observed: false,
    completed_at: '2026-07-20T12:00:00+00:00',
  }), /Invalid onboarding response/)
})
