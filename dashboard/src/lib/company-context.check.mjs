import assert from 'node:assert/strict'
import test from 'node:test'

import {
  activateCompany, ACTIVE_COMPANY_MAX, fetchCompanyContext, normalizeCompanyContext,
} from './company-context.js'

const FIRST = '11111111-1111-4111-8111-111111111111'
const SECOND = '22222222-2222-4222-8222-222222222222'

test('authenticated company context keeps the active company first and supports multi-org choice', () => {
  const context = normalizeCompanyContext({
    company_id: SECOND,
    companies: [
      { company_id: FIRST, company_name: 'Alpha', role: 'company_owner', account_type: 'individual' },
      { company_id: SECOND, company_name: 'Beta', role: 'member', account_type: 'company' },
    ],
  })
  assert.equal(context.activeCompanyId, SECOND)
  assert.deepEqual(context.companies.map(company => company.company_id), [SECOND, FIRST])
  assert.equal(context.companies[0].account_type, 'company')
})

test('company context rejects unbound, duplicate, unbounded, and invalid-role choices', () => {
  assert.throws(() => normalizeCompanyContext({
    company_id: SECOND,
    companies: [{ company_id: FIRST, company_name: 'Alpha', role: 'member', account_type: 'company' }],
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: [
      { company_id: FIRST, company_name: 'Alpha', role: 'member', account_type: 'company' },
      { company_id: FIRST, company_name: 'Duplicate', role: 'member', account_type: 'company' },
    ],
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: Array.from({ length: ACTIVE_COMPANY_MAX + 1 }, (_, index) => ({
      company_id: `${String(index).padStart(8, '0')}-1111-4111-8111-111111111111`,
      company_name: `Company ${index}`,
      role: 'member',
      account_type: 'company',
    })),
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: [{ company_id: FIRST, company_name: 'Alpha', role: 'super_admin', account_type: 'company' }],
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: [{ company_id: FIRST, company_name: 'Alpha', role: 'member', account_type: 'consumer' }],
  }), /Invalid company access response/)
})

test('company context comes only from the authenticated capabilities endpoint', async () => {
  const calls = []
  const result = await fetchCompanyContext('verified-session-token', {
    requestId: () => 'request-company-context',
    request: async (path, options) => {
      calls.push([path, options])
      if (path === '/v1/organization/onboarding') {
        return Response.json({
          company_id: FIRST,
          status: 'pending',
          cli_connected: false,
          proxied_request_observed: false,
          completed_at: '',
        })
      }
      return Response.json({
        company_id: FIRST,
        companies: [{
          company_id: FIRST, company_name: 'Verified company', role: 'company_admin', account_type: 'company',
        }],
      })
    },
  })

  assert.equal(result.activeCompanyId, FIRST)
  assert.equal(result.onboarding.status, 'pending')
  assert.equal(calls[0][0], '/api/admin/company/capabilities')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer verified-session-token')
  assert.equal(calls[0][1].headers['X-Request-ID'], 'request-company-context')
  assert.equal(calls[0][1].cache, 'no-store')
  assert.equal(calls[1][0], '/v1/organization/onboarding')
  assert.equal(calls[1][1].headers.Authorization, 'Bearer verified-session-token')

  await assert.rejects(fetchCompanyContext('verified-session-token', {
    request: async () => Response.json({ detail: 'foreign company private' }, { status: 403 }),
  }), error => error.message === 'Company access denied' && error.status === 403)

  await assert.rejects(fetchCompanyContext('verified-session-token', {
    request: async path => path === '/api/admin/company/capabilities'
      ? Response.json({
        company_id: FIRST,
        companies: [{ company_id: FIRST, company_name: 'First', role: 'company_owner', account_type: 'individual' }],
      })
      : Response.json({
        company_id: SECOND, status: 'pending', cli_connected: false,
        proxied_request_observed: false, completed_at: '',
      }),
  }), /Invalid company access response/)
})

test('active company switching sends only an authenticated membership target', async () => {
  const calls = []
  const result = await activateCompany('verified-session-token', SECOND, {
    requestId: () => 'request-switch-company',
    request: async (path, options) => {
      calls.push([path, options])
      return Response.json({ company_id: SECOND, role: 'member' })
    },
  })

  assert.deepEqual(result, { company_id: SECOND, role: 'member' })
  assert.equal(calls[0][0], '/api/admin/company/active')
  assert.equal(calls[0][1].method, 'POST')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer verified-session-token')
  assert.equal(calls[0][1].headers['X-Request-ID'], 'request-switch-company')
  assert.deepEqual(JSON.parse(calls[0][1].body), { company_id: SECOND })

  await assert.rejects(
    activateCompany('verified-session-token', 'not-a-company-id'),
    /Invalid company selection/,
  )
  await assert.rejects(activateCompany('verified-session-token', SECOND, {
    request: async () => Response.json({ company_id: FIRST, role: 'member' }),
  }), /Invalid company access response/)
})
