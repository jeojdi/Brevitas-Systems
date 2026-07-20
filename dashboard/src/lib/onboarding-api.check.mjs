import assert from 'node:assert/strict'
import test from 'node:test'

import { bootstrapWorkspace } from './onboarding-api.js'

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
