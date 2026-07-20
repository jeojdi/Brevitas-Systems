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
      { company_id: FIRST, company_name: 'Alpha', role: 'company_owner' },
      { company_id: SECOND, company_name: 'Beta', role: 'member' },
    ],
  })
  assert.equal(context.activeCompanyId, SECOND)
  assert.deepEqual(context.companies.map(company => company.company_id), [SECOND, FIRST])
})

test('company context rejects unbound, duplicate, unbounded, and invalid-role choices', () => {
  assert.throws(() => normalizeCompanyContext({
    company_id: SECOND,
    companies: [{ company_id: FIRST, company_name: 'Alpha', role: 'member' }],
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: [
      { company_id: FIRST, company_name: 'Alpha', role: 'member' },
      { company_id: FIRST, company_name: 'Duplicate', role: 'member' },
    ],
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: Array.from({ length: ACTIVE_COMPANY_MAX + 1 }, (_, index) => ({
      company_id: `${String(index).padStart(8, '0')}-1111-4111-8111-111111111111`,
      company_name: `Company ${index}`,
      role: 'member',
    })),
  }), /Invalid company access response/)
  assert.throws(() => normalizeCompanyContext({
    company_id: FIRST,
    companies: [{ company_id: FIRST, company_name: 'Alpha', role: 'super_admin' }],
  }), /Invalid company access response/)
})

test('company context comes only from the authenticated capabilities endpoint', async () => {
  const calls = []
  const result = await fetchCompanyContext('verified-session-token', {
    requestId: () => 'request-company-context',
    request: async (path, options) => {
      calls.push([path, options])
      return Response.json({
        company_id: FIRST,
        companies: [{
          company_id: FIRST, company_name: 'Verified company', role: 'company_admin',
        }],
      })
    },
  })

  assert.equal(result.activeCompanyId, FIRST)
  assert.equal(calls[0][0], '/api/admin/company/capabilities')
  assert.equal(calls[0][1].headers.Authorization, 'Bearer verified-session-token')
  assert.equal(calls[0][1].headers['X-Request-ID'], 'request-company-context')
  assert.equal(calls[0][1].cache, 'no-store')

  await assert.rejects(fetchCompanyContext('verified-session-token', {
    request: async () => Response.json({ detail: 'foreign company private' }, { status: 403 }),
  }), error => error.message === 'Company access denied' && error.status === 403)
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
