import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  STAGING_CANARY_LIMITATIONS,
  STAGING_CANARY_TARGETS,
  assertStagingCanaryGuard,
  runStagingCanary,
  stagingCanaryConfig,
} from '../scripts/ci/staging-canary.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const runId = '10000000-0000-4000-8000-000000000001'
const jobId = '20000000-0000-4000-8000-000000000002'
const dashboardKeyId = '30000000-0000-4000-8000-000000000003'
const serviceAccountId = '40000000-0000-4000-8000-000000000004'
const companyId = '50000000-0000-4000-8000-000000000005'

function environment(overrides = {}) {
  return {
    STAGING_CANARY_ALLOWED: 'mutating-staging-canary',
    STAGING_CANARY_CONFIRMATION: 'RUN MUTATING STAGING CANARY',
    STAGING_REPOSITORY: 'jeojdi/Brevitas-Systems',
    STAGING_REPOSITORY_FORK: 'false',
    STAGING_GITHUB_EVENT: 'workflow_dispatch',
    STAGING_GITHUB_REF: 'refs/heads/main',
    STAGING_GITHUB_WORKFLOW_REF: 'jeojdi/Brevitas-Systems/.github/workflows/staging-canary.yml@refs/heads/main',
    GITHUB_ACTIONS: 'true',
    RUNNER_ENVIRONMENT: 'github-hosted',
    STAGING_CANARY_USER_TOKEN: `eyJ${'a'.repeat(48)}.${'b'.repeat(48)}.${'c'.repeat(48)}`,
    STAGING_CANARY_PROVIDER: 'openai',
    STAGING_CANARY_MODEL: 'gpt-4o-mini',
    STAGING_CANARY_PROVIDER_API_KEY: 'sk-canary-provider-not-real',
    STAGING_CANARY_BILLING_MODE: 'required',
    STAGING_CANARY_RECOVERY_MODE: 'parser-only',
    STAGING_CANARY_STRIPE_SECRET_KEY: 'sk_test_canary_not_real_1234567890',
    STAGING_CANARY_STRIPE_WEBHOOK_SECRET: 'whsec_canary_not_real_1234567890',
    STAGING_BILLING_RECOVERY_SECRET: 'recovery_canary_not_real_1234567890',
    ...overrides,
  }
}

function json(payload, status = 200) {
  return Response.json(payload, { status })
}

function successfulCanaryFetch({ streamError = false } = {}) {
  const state = {
    calls: [],
    dashboardRevoked: false,
    serviceRevoked: false,
    usageWrites: 0,
    jobPosts: 0,
    jobReceiptRecorded: false,
    totalCalls: 0,
    webhookDeliveries: 0,
  }
  const dashboardKey = 'bvt_dashboard_canary_secret'
  const serviceKey = 'bvt_service_canary_secret'

  const fetchImpl = async (url, options = {}) => {
    const target = new URL(url)
    const method = options.method || 'GET'
    const body = options.body ? String(options.body) : ''
    state.calls.push({ url, method, headers: options.headers || {}, body })

    if (target.origin === STAGING_CANARY_TARGETS.stripeApi) {
      assert.match(options.headers.authorization, /^Bearer sk_test_/)
      if (target.pathname.endsWith('/expire')) {
        return json({
          id: 'cs_test_canary123', livemode: false, mode: 'subscription', status: 'expired',
        })
      }
      return json({
        id: 'cs_test_canary123', livemode: false, mode: 'subscription', status: 'open',
      })
    }

    if (target.origin === STAGING_CANARY_TARGETS.dashboard) {
      if (target.pathname === '/api/billing/status') {
        return json({ configured: true, subscription_status: 'not_started' })
      }
      if (target.pathname === '/api/billing/checkout') {
        return json({ url: 'https://checkout.stripe.com/c/pay/cs_test_canary123' })
      }
      if (target.pathname === '/api/billing/webhook') {
        assert.match(options.headers['stripe-signature'], /^t=\d+,v1=[0-9a-f]{64}$/)
        state.webhookDeliveries += 1
        return json({ received: true, ...(state.webhookDeliveries > 1 ? { duplicate: true } : {}) })
      }
      if (target.pathname === '/api/billing/sync') {
        const user = options.headers.authorization?.startsWith('Bearer eyJ')
        const second = options.headers['x-billing-recovery-secret']?.startsWith('recovery_')
        if (!user || !second) return json({ error: 'Authentication required' }, 401)
        const parsed = JSON.parse(body)
        if (!parsed.entry_id) return json({ error: 'Invalid manual recovery request' }, 400)
        return json({ resolved: true, audit_id: 99 })
      }
    }

    assert.equal(target.origin, STAGING_CANARY_TARGETS.api)
    if (target.pathname === '/v1/organization/bootstrap') {
      return json({
        company_id: companyId, company_name: 'Release staging canary',
        role: 'company_owner', account_type: 'company', created: false,
      })
    }
    if (target.pathname === '/v1/keys' && method === 'POST') {
      return json({
        api_key: dashboardKey, key_id: dashboardKeyId,
        purpose: 'dashboard_session', secret_available_once: true,
      })
    }
    if (target.pathname === `/v1/keys/${dashboardKeyId}` && method === 'DELETE') {
      state.dashboardRevoked = true
      return json({ revoked: true })
    }
    if (target.pathname === '/v1/company/service-accounts' && method === 'POST') {
      return json({
        id: serviceAccountId, api_key: serviceKey,
        secret_available_once: true, status: 'active',
      })
    }
    if (target.pathname === `/v1/company/service-accounts/${serviceAccountId}` &&
        method === 'DELETE') {
      state.serviceRevoked = true
      return json({ id: serviceAccountId, status: 'revoked' })
    }
    if (target.pathname === '/v1/provider' && method === 'PUT') {
      const parsed = JSON.parse(body)
      assert.equal(parsed.provider_api_key, environment().STAGING_CANARY_PROVIDER_API_KEY)
      return json({ ok: true, provider: parsed.provider, model: parsed.model })
    }
    if (target.pathname === '/v1/compress/stream') {
      assert.equal(options.headers['x-brevitas-max-output-tokens'], '64')
      state.totalCalls += 1
      const events = streamError
        ? [{ stage: 'error', code: 'provider_stream_failed', message: 'Model provider stream failed' }]
        : [
            { stage: 'routed', provider: 'openai', model: 'gpt-4o-mini' },
            { stage: 'model_response', text: 'CANARY_OK' },
            { stage: 'done', result: { model_response: 'CANARY_OK' } },
          ]
      return new Response(events.map(event => `data: ${JSON.stringify(event)}\n\n`).join(''), {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      })
    }
    if (target.pathname === '/v1/usage' && method === 'POST') {
      state.usageWrites += 1
      if (state.usageWrites === 1) state.totalCalls += 1
      const requestId = JSON.parse(body).request_id
      return json(state.usageWrites === 1
        ? { request_id: requestId, tokens_saved: 0 }
        : { duplicate: true, request_id: requestId, tokens_saved: 0 })
    }
    if (target.pathname === '/v1/jobs' && method === 'POST') {
      state.jobPosts += 1
      return json({
        id: jobId, status: 'queued', created: state.jobPosts === 1,
      }, 202)
    }
    if (target.pathname === `/v1/jobs/${jobId}` && method === 'GET') {
      if (!state.jobReceiptRecorded) {
        state.jobReceiptRecorded = true
        state.totalCalls += 1
      }
      return json({
        id: jobId, status: 'succeeded', attempts: 1,
        result: { compressed_messages: ['release canary worker recovery evidence'] },
      })
    }
    if (target.pathname === `/v1/jobs/${jobId}/cancel` && method === 'POST') {
      return json({ id: jobId, status: 'cancelled' })
    }
    if (target.pathname === '/v1/stats') {
      const key = options.headers['x-brevitas-key']
      if ((key === dashboardKey && state.dashboardRevoked) ||
          (key === serviceKey && state.serviceRevoked)) {
        return json({ detail: 'Invalid API key' }, 401)
      }
      return json({ total_calls: key === serviceKey ? state.totalCalls : 0 })
    }
    throw new Error(`Unhandled mocked path: ${method} ${target.pathname}`)
  }
  return { fetchImpl, state, dashboardKey, serviceKey }
}

test('journey canary has fixed staging-only targets and fails closed on authority or secret gaps', () => {
  assert.deepEqual(STAGING_CANARY_TARGETS, {
    api: 'https://brevitas-api-staging-975273324573.us-west1.run.app',
    dashboard: 'https://brevitas-systems-staging.vercel.app',
    stripeApi: 'https://api.stripe.com',
  })
  assert.throws(() => assertStagingCanaryGuard({}), /explicit mutating workflow guard/)
  assert.throws(
    () => stagingCanaryConfig(environment({ STAGING_REPOSITORY_FORK: 'true' })),
    /fork/,
  )
  assert.throws(
    () => stagingCanaryConfig(environment({ STAGING_CANARY_MODEL: 'gpt-4o' })),
    /low-cost allowlist/,
  )
  assert.throws(
    () => stagingCanaryConfig(environment({
      STAGING_CANARY_STRIPE_SECRET_KEY: 'sk_live_canary_not_real_1234567890',
    })),
    /non-test Stripe/,
  )
  assert.throws(
    () => stagingCanaryConfig(environment({
      STAGING_CANARY_BILLING_MODE: 'skip',
      STAGING_CANARY_RECOVERY_MODE: 'resolve-test-ledger',
    })),
    /cannot run when billing is skipped/,
  )
})

test('journey canary reports its bounded API scope without claiming untested paths', async () => {
  const { fetchImpl, state } = successfulCanaryFetch()
  const result = await runStagingCanary(environment(), fetchImpl, {
    randomUUID: () => runId,
    now: () => 1_782_000_000_000,
    sleep: async () => {},
  })

  assert.deepEqual(result, {
    target: 'staging',
    journeyScope: 'api-only-preprovisioned-user',
    authentication: 'pre-provisioned-canary',
    workspace: true,
    dashboardKey: true,
    provider: {
      configured: true, streamed: true, maxOutputTokens: 64,
      events: 3, outputChars: 9,
    },
    usage: { attributed: true, idempotent: true },
    jobs: {
      submitted: true, idempotent: true, normalCompletion: true,
      workerDeathReclaim: false,
    },
    billing: {
      mode: 'required',
      checkoutSession: 'created-uncompleted',
      unmatchedWebhookReplay: 'duplicate-observed',
      recovery: 'parser-only',
      subscriptionMutation: false,
      invoiceMutation: false,
      endToEnd: false,
    },
    limitations: [...STAGING_CANARY_LIMITATIONS],
    revocation: true,
    cleanup: true,
  })
  assert.equal(state.webhookDeliveries, 2)
  assert.equal(state.jobPosts, 2)
  assert.equal(state.usageWrites, 2)
  assert.equal(state.dashboardRevoked, true)
  assert.equal(state.serviceRevoked, true)

  const streamCalls = state.calls.filter(call => new URL(call.url).pathname === '/v1/compress/stream')
  assert.equal(streamCalls.length, 1)
  assert.equal(streamCalls[0].headers['x-brevitas-max-output-tokens'], '64')
  assert.ok(Buffer.byteLength(streamCalls[0].body) < 2048)
  const jobBodies = state.calls
    .filter(call => new URL(call.url).pathname === '/v1/jobs' && call.method === 'POST')
    .map(call => JSON.parse(call.body))
  assert.ok(jobBodies.every(body => body.operation === 'compress' && body.max_attempts === 2))
  assert.ok(state.calls.some(call => new URL(call.url).pathname.endsWith('/expire')))
  assert.ok(state.calls.every(call => [
    STAGING_CANARY_TARGETS.api,
    STAGING_CANARY_TARGETS.dashboard,
    STAGING_CANARY_TARGETS.stripeApi,
    'https://checkout.stripe.com',
  ].includes(new URL(call.url).origin)))
  for (const secret of [
    environment().STAGING_CANARY_USER_TOKEN,
    environment().STAGING_CANARY_PROVIDER_API_KEY,
    environment().STAGING_CANARY_STRIPE_SECRET_KEY,
    environment().STAGING_CANARY_STRIPE_WEBHOOK_SECRET,
    environment().STAGING_BILLING_RECOVERY_SECRET,
  ]) assert.doesNotMatch(JSON.stringify(result), new RegExp(secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
})

test('canary limitations permanently exclude signup browser CLI failover and billing E2E claims', () => {
  assert.deepEqual(STAGING_CANARY_LIMITATIONS, [
    'public-signup-and-email-delivery',
    'browser-rendering-and-interaction',
    'released-cli-artifact',
    'worker-death-and-reclaim',
    'subscription-state-mutation',
    'invoice-state-mutation',
    'billing-end-to-end',
  ])
  const source = read('scripts/ci/staging-canary.mjs')
  assert.doesNotMatch(source, /\/signup|child_process|execFile|spawn\(/)
})

test('journey canary revokes credentials when the provider journey fails', async () => {
  const { fetchImpl, state } = successfulCanaryFetch({ streamError: true })
  await assert.rejects(
    () => runStagingCanary(environment(), fetchImpl, {
      randomUUID: () => runId,
      now: () => 1_782_000_000_000,
      sleep: async () => {},
    }),
    /sanitized error event/,
  )
  assert.equal(state.serviceRevoked, true)
  assert.equal(state.dashboardRevoked, true)
  assert.equal(state.calls.some(call => new URL(call.url).hostname === 'api.stripe.com'), false)
})

test('workflow is approval-gated with no host input and server enforces provider output ceilings', () => {
  const workflow = read('.github/workflows/staging-canary.yml')
  const server = read('api/server.py')
  const documentation = read('docs/STAGING_CANARY.md')
  assert.match(workflow, /workflow_dispatch:/)
  assert.match(workflow, /^\s{4}environment: staging$/m)
  assert.match(workflow, /github\.event\.repository\.fork == false/)
  assert.match(workflow, /github\.repository == 'jeojdi\/Brevitas-Systems'/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
  assert.match(workflow, /STAGING_GITHUB_WORKFLOW_REF: \$\{\{ github\.workflow_ref \}\}/)
  assert.doesNotMatch(workflow, /api_url|dashboard_url|target_url/i)
  assert.match(workflow, /STAGING_CANARY_ALLOWED: mutating-staging-canary/)
  assert.match(workflow, /secrets\.STAGING_CANARY_USER_TOKEN/)
  assert.match(workflow, /secrets\.STAGING_CANARY_PROVIDER_API_KEY/)
  assert.match(workflow, /secrets\.STAGING_CANARY_STRIPE_SECRET_KEY/)
  assert.match(workflow, /secrets\.STAGING_CANARY_STRIPE_WEBHOOK_SECRET/)
  assert.match(workflow, /secrets\.STAGING_BILLING_RECOVERY_SECRET/)
  assert.match(server, /x-brevitas-max-output-tokens/)
  assert.match(server, /"max_tokens": _provider_output_token_limit\(request\)/)
  assert.match(server, /token_field: _provider_output_token_limit\(request\)/)
  assert.match(server, /"num_predict": _provider_output_token_limit\(request\)/)
  assert.match(documentation, /public `\/signup`, email confirmation, or email delivery/i)
  assert.match(documentation, /operator still must\s+inspect/i)
  assert.match(documentation, /packaged\/released CLI artifact/i)
  assert.match(documentation, /worker death, lease expiry, cross-worker reclaim/i)
  assert.match(documentation, /matched Stripe subscription or invoice state mutation/i)
  assert.match(documentation, /billing end to end/i)
})
