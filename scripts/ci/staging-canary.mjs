import { createHmac, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

export const STAGING_CANARY_TARGETS = Object.freeze({
  api: 'https://staging-api.brevitassystems.com',
  dashboard: 'https://staging.brevitassystems.com',
  stripeApi: 'https://api.stripe.com',
})

export const STAGING_CANARY_LIMITATIONS = Object.freeze([
  'public-signup-and-email-delivery',
  'browser-rendering-and-interaction',
  'released-cli-artifact',
  'worker-death-and-reclaim',
  'subscription-state-mutation',
  'invoice-state-mutation',
  'billing-end-to-end',
])

const APPROVED_PROVIDER_MODELS = Object.freeze({
  anthropic: 'claude-haiku-4-5-20251001',
  deepseek: 'deepseek-chat',
  grok: 'grok-3-mini',
  groq: 'llama-3.1-8b-instant',
  openai: 'gpt-4o-mini',
})
const USER_TOKEN_MAX = 4096
const RESPONSE_MAX_BYTES = 64 * 1024
const MODEL_OUTPUT_MAX_TOKENS = 64
const MODEL_OUTPUT_MAX_CHARS = 4096
const JOB_POLL_ATTEMPTS = 45
const SAFE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function required(environment, name) {
  const value = String(environment[name] || '').trim()
  if (!value) throw new Error(`${name} is required for the approved staging canary`)
  return value
}

function boundedSecret(environment, name, minimum = 12) {
  const value = required(environment, name)
  if (value.length < minimum || value.length > USER_TOKEN_MAX || /\s/.test(value)) {
    throw new Error(`${name} is not a bounded single-token credential`)
  }
  return value
}

export function assertStagingCanaryGuard(environment) {
  if (environment.STAGING_CANARY_ALLOWED !== 'mutating-staging-canary') {
    throw new Error('Staging canary requires the explicit mutating workflow guard')
  }
  if (environment.STAGING_CANARY_CONFIRMATION !== 'RUN MUTATING STAGING CANARY') {
    throw new Error('Staging canary confirmation is missing')
  }
  if (environment.STAGING_REPOSITORY !== 'jeojdi/Brevitas-Systems') {
    throw new Error('Staging canary refuses an unapproved repository or fork')
  }
  if (environment.STAGING_REPOSITORY_FORK !== 'false') {
    throw new Error('Staging canary refuses fork execution')
  }
  if (environment.STAGING_GITHUB_EVENT !== 'workflow_dispatch') {
    throw new Error('Staging canary is manual workflow_dispatch only')
  }
  if (environment.STAGING_GITHUB_REF !== 'refs/heads/main') {
    throw new Error('Staging canary must run from the protected main branch')
  }
  if (environment.STAGING_GITHUB_WORKFLOW_REF !==
      'jeojdi/Brevitas-Systems/.github/workflows/staging-canary.yml@refs/heads/main') {
    throw new Error('Staging canary refuses an unapproved workflow identity')
  }
  if (environment.GITHUB_ACTIONS !== 'true' ||
      environment.RUNNER_ENVIRONMENT !== 'github-hosted') {
    throw new Error('Staging canary requires a GitHub-hosted Actions runner')
  }
  for (const origin of Object.values(STAGING_CANARY_TARGETS)) {
    const target = new URL(origin)
    if (target.protocol !== 'https:' || /(^|[.-])(prod|production)([.-]|$)/i.test(target.hostname)) {
      throw new Error('Staging canary target contract is not staging-safe')
    }
  }
}

export function stagingCanaryConfig(environment) {
  assertStagingCanaryGuard(environment)
  const provider = required(environment, 'STAGING_CANARY_PROVIDER')
  const model = required(environment, 'STAGING_CANARY_MODEL')
  if (APPROVED_PROVIDER_MODELS[provider] !== model) {
    throw new Error('Staging canary provider/model is not in the low-cost allowlist')
  }
  const billingMode = required(environment, 'STAGING_CANARY_BILLING_MODE')
  if (!['required', 'skip'].includes(billingMode)) {
    throw new Error('STAGING_CANARY_BILLING_MODE must be required or skip')
  }
  const recoveryMode = required(environment, 'STAGING_CANARY_RECOVERY_MODE')
  if (!['parser-only', 'resolve-test-ledger'].includes(recoveryMode)) {
    throw new Error('STAGING_CANARY_RECOVERY_MODE is invalid')
  }

  const config = {
    userToken: boundedSecret(environment, 'STAGING_CANARY_USER_TOKEN', 20),
    provider,
    model,
    providerApiKey: boundedSecret(environment, 'STAGING_CANARY_PROVIDER_API_KEY'),
    billingMode,
    recoveryMode,
    stripeSecretKey: '',
    stripeWebhookSecret: '',
    billingRecoverySecret: '',
    recoveryEntryId: 0,
  }
  if (billingMode === 'required') {
    config.stripeSecretKey = boundedSecret(
      environment, 'STAGING_CANARY_STRIPE_SECRET_KEY', 20)
    config.stripeWebhookSecret = boundedSecret(
      environment, 'STAGING_CANARY_STRIPE_WEBHOOK_SECRET', 20)
    config.billingRecoverySecret = boundedSecret(
      environment, 'STAGING_BILLING_RECOVERY_SECRET', 20)
    if (!config.stripeSecretKey.startsWith('sk_test_')) {
      throw new Error('Staging canary refuses a non-test Stripe secret key')
    }
    if (!config.stripeWebhookSecret.startsWith('whsec_')) {
      throw new Error('Staging canary requires a Stripe test endpoint signing secret')
    }
    if (recoveryMode === 'resolve-test-ledger') {
      config.recoveryEntryId = Number(required(
        environment, 'STAGING_CANARY_RECOVERY_ENTRY_ID'))
      if (!Number.isSafeInteger(config.recoveryEntryId) || config.recoveryEntryId <= 0) {
        throw new Error('STAGING_CANARY_RECOVERY_ENTRY_ID must be a positive integer')
      }
    }
  } else if (recoveryMode !== 'parser-only') {
    throw new Error('Ledger recovery cannot run when billing is skipped')
  }
  return config
}

async function boundedText(response, limit = RESPONSE_MAX_BYTES) {
  if (!response.body?.getReader) {
    const value = await response.text()
    if (Buffer.byteLength(value) > limit) throw new Error('Canary response exceeded its byte bound')
    return value
  }
  const reader = response.body.getReader()
  const chunks = []
  let total = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > limit) {
      await reader.cancel()
      throw new Error('Canary response exceeded its byte bound')
    }
    chunks.push(value)
  }
  return Buffer.concat(chunks.map(value => Buffer.from(value))).toString('utf8')
}

async function canaryRequest(fetchImpl, url, {
  label,
  expected = 200,
  method = 'GET',
  headers = {},
  json,
  rawBody,
  parse = 'json',
  timeoutMs = 15_000,
} = {}) {
  const body = json === undefined ? rawBody : JSON.stringify(json)
  let response
  try {
    response = await fetchImpl(url, {
      method,
      redirect: 'manual',
      signal: AbortSignal.timeout(timeoutMs),
      headers: {
        accept: parse === 'sse' ? 'text/event-stream' : 'application/json',
        'user-agent': 'brevitas-staging-journey-canary/1',
        'x-brevitas-release-smoke': 'mutating-staging-canary',
        ...(json === undefined ? {} : { 'content-type': 'application/json' }),
        ...headers,
      },
      ...(body === undefined ? {} : { body }),
    })
  } catch (error) {
    throw new Error(`${label} request failed (${error instanceof Error ? error.name : 'Error'})`)
  }
  const allowed = Array.isArray(expected) ? expected : [expected]
  if (!allowed.includes(response.status)) {
    throw new Error(`${label} returned HTTP ${response.status}`)
  }
  if (parse === 'none') return { response, payload: null, text: '' }
  const text = await boundedText(response)
  if (parse === 'sse') return { response, payload: null, text }
  if (!text) return { response, payload: {}, text }
  try {
    return { response, payload: JSON.parse(text), text }
  } catch {
    throw new Error(`${label} returned invalid JSON`)
  }
}

function bearer(token) {
  return { authorization: `Bearer ${token}` }
}

function keyHeaders(key, customerId = '') {
  return {
    'x-brevitas-key': key,
    ...(customerId ? { 'x-brevitas-customer-id': customerId } : {}),
  }
}

function requireObject(payload, label) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error(`${label} returned an invalid object contract`)
  }
  return payload
}

function parseModelStream(text, provider, model) {
  const events = []
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith('data: ')) continue
    if (events.length >= 100) throw new Error('Provider stream exceeded its event bound')
    try {
      events.push(JSON.parse(line.slice(6)))
    } catch {
      throw new Error('Provider stream emitted invalid JSON')
    }
  }
  if (events.some(event => event?.stage === 'error')) {
    throw new Error('Provider stream returned a sanitized error event')
  }
  const routed = events.find(event => event?.stage === 'routed')
  const modelResponse = events.find(event => event?.stage === 'model_response')
  const done = events.find(event => event?.stage === 'done')
  const output = String(modelResponse?.text || '')
  if (routed?.provider !== provider || routed?.model !== model) {
    throw new Error('Provider stream routing evidence did not match the canary configuration')
  }
  if (!output || output.length > MODEL_OUTPUT_MAX_CHARS || !done?.result) {
    throw new Error('Provider stream output did not satisfy its bounded completion contract')
  }
  return { events: events.length, outputChars: output.length }
}

function stripeSignature(payload, secret, timestamp) {
  const digest = createHmac('sha256', secret)
    .update(`${timestamp}.${payload}`, 'utf8')
    .digest('hex')
  return `t=${timestamp},v1=${digest}`
}

async function runBillingCanary({
  config, fetchImpl, userToken, runToken, nowSeconds, onCheckoutSession,
}) {
  if (config.billingMode === 'skip') {
    return {
      mode: 'skipped',
      checkoutSession: 'not-run',
      unmatchedWebhookReplay: 'not-run',
      recovery: 'skipped',
      subscriptionMutation: false,
      invoiceMutation: false,
      endToEnd: false,
    }
  }
  const dashboard = STAGING_CANARY_TARGETS.dashboard
  const statusBefore = requireObject((await canaryRequest(
    fetchImpl, `${dashboard}/api/billing/status`, {
      label: 'billing status before canary', headers: bearer(userToken),
    })).payload, 'billing status')
  if (statusBefore.configured !== true) {
    throw new Error('Staging billing is not configured')
  }

  const checkout = requireObject((await canaryRequest(
    fetchImpl, `${dashboard}/api/billing/checkout`, {
      label: 'Stripe test checkout', method: 'POST', headers: bearer(userToken), json: {},
    })).payload, 'Stripe test checkout')
  let checkoutUrl
  try {
    checkoutUrl = new URL(String(checkout.url || ''))
  } catch {
    throw new Error('Stripe test checkout returned an invalid URL')
  }
  if (checkoutUrl.protocol !== 'https:' || checkoutUrl.hostname !== 'checkout.stripe.com') {
    throw new Error('Stripe test checkout returned an unapproved host')
  }
  const sessionMatch = decodeURIComponent(checkoutUrl.pathname).match(/cs_test_[A-Za-z0-9_]+/)
  if (!sessionMatch) throw new Error('Stripe test checkout did not expose a test session id')
  const checkoutSessionId = sessionMatch[0]

  const stripeHeaders = {
    authorization: `Bearer ${config.stripeSecretKey}`,
    'stripe-version': '2025-06-30.basil',
  }
  onCheckoutSession({ checkoutSessionId, stripeHeaders })
  const session = requireObject((await canaryRequest(
    fetchImpl,
    `${STAGING_CANARY_TARGETS.stripeApi}/v1/checkout/sessions/${encodeURIComponent(checkoutSessionId)}`,
    { label: 'Stripe test session evidence', headers: stripeHeaders },
  )).payload, 'Stripe test session evidence')
  if (session.id !== checkoutSessionId || session.livemode !== false ||
      session.mode !== 'subscription' || session.status !== 'open') {
    throw new Error('Stripe checkout evidence was not an uncompleted test subscription session')
  }

  const event = {
    id: `evt_canary_${runToken}`,
    object: 'event',
    api_version: '2025-06-30.basil',
    created: nowSeconds,
    livemode: false,
    pending_webhooks: 1,
    type: 'invoice.payment_failed',
    data: {
      object: {
        id: `in_canary_${runToken}`,
        object: 'invoice',
        customer: `cus_canary_unmatched_${runToken}`,
        status: 'open',
        livemode: false,
      },
    },
  }
  const eventBody = JSON.stringify(event)
  const signature = stripeSignature(
    eventBody, config.stripeWebhookSecret, nowSeconds)
  const webhookOptions = {
    label: 'signed Stripe test webhook', method: 'POST', rawBody: eventBody,
    headers: {
      'content-type': 'application/json',
      'stripe-signature': signature,
    },
  }
  const firstWebhook = requireObject((await canaryRequest(
    fetchImpl, `${dashboard}/api/billing/webhook`, webhookOptions)).payload,
  'signed Stripe test webhook')
  const duplicateWebhook = requireObject((await canaryRequest(
    fetchImpl, `${dashboard}/api/billing/webhook`, webhookOptions)).payload,
  'duplicate Stripe test webhook')
  if (firstWebhook.received !== true || duplicateWebhook.duplicate !== true) {
    throw new Error('Stripe webhook inbox did not report the immediate replay as a duplicate')
  }

  await canaryRequest(fetchImpl, `${dashboard}/api/billing/sync`, {
    label: 'billing recovery user-only denial', expected: 401, method: 'POST',
    headers: bearer(userToken), json: {},
  })
  await canaryRequest(fetchImpl, `${dashboard}/api/billing/sync`, {
    label: 'billing recovery secret-only denial', expected: 401, method: 'POST',
    headers: bearer(config.billingRecoverySecret), json: {},
  })
  await canaryRequest(fetchImpl, `${dashboard}/api/billing/sync`, {
    label: 'billing recovery two-factor parser', expected: 400, method: 'POST',
    headers: {
      ...bearer(userToken),
      'x-billing-recovery-secret': config.billingRecoverySecret,
    },
    json: {},
  })

  let recovery = 'parser-only'
  if (config.recoveryMode === 'resolve-test-ledger') {
    const resolved = requireObject((await canaryRequest(
      fetchImpl, `${dashboard}/api/billing/sync`, {
        label: 'scoped staging ledger recovery', method: 'POST',
        headers: {
          ...bearer(userToken),
          'x-billing-recovery-secret': config.billingRecoverySecret,
          'x-request-id': `canary-recovery-${runToken}`,
        },
        json: {
          entry_id: config.recoveryEntryId,
          resolution: 'pending',
          note: `Approved staging canary reconciliation ${runToken}`,
        },
      })).payload, 'scoped staging ledger recovery')
    if (resolved.resolved !== true || !Number.isSafeInteger(resolved.audit_id)) {
      throw new Error('Scoped staging ledger recovery did not return immutable audit evidence')
    }
    recovery = 'resolved-test-ledger'
  }

  await canaryRequest(fetchImpl, `${dashboard}/api/billing/status`, {
    label: 'billing status after canary', headers: bearer(userToken),
  })
  return {
    mode: 'required',
    checkoutSession: 'created-uncompleted',
    unmatchedWebhookReplay: 'duplicate-observed',
    recovery,
    subscriptionMutation: false,
    invoiceMutation: false,
    endToEnd: false,
  }
}

export async function runStagingCanary(
  environment = process.env,
  fetchImpl = globalThis.fetch,
  dependencies = {},
) {
  const config = stagingCanaryConfig(environment)
  if (typeof fetchImpl !== 'function') throw new Error('A fetch implementation is required')
  const uuidImpl = dependencies.randomUUID || randomUUID
  const sleep = dependencies.sleep || (milliseconds => new Promise(resolveSleep => {
    setTimeout(resolveSleep, milliseconds)
  }))
  const now = dependencies.now || (() => Date.now())
  const runId = uuidImpl()
  if (!SAFE_UUID.test(runId)) throw new Error('Canary run id generator returned an invalid UUID')
  const runToken = runId.replaceAll('-', '')
  const api = STAGING_CANARY_TARGETS.api
  const userHeaders = bearer(config.userToken)
  const customerId = 'release-canary'
  const cleanup = []
  let dashboardKey = ''
  let dashboardKeyId = ''
  let serviceKey = ''
  let serviceAccountId = ''
  let jobId = ''
  let jobSucceeded = false
  let billingResult = null
  let billingCleanup = null
  let primaryError = null
  let result = null

  try {
    const workspace = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/organization/bootstrap`, {
        label: 'authenticated workspace selection', method: 'POST',
        headers: userHeaders,
        json: { account_type: 'company', name: 'Release staging canary' },
      })).payload, 'authenticated workspace selection')
    if (!SAFE_UUID.test(String(workspace.company_id || '')) ||
        !['company_owner', 'company_admin'].includes(workspace.role)) {
      throw new Error('Canary identity is not an owner/admin of a staging workspace')
    }

    const dashboardCredential = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/keys`, {
        label: 'dashboard session mint', method: 'POST', headers: userHeaders,
        json: {
          name: `Release canary ${runToken.slice(0, 12)}`,
          environment: 'staging',
          purpose: 'dashboard_session',
        },
      })).payload, 'dashboard session mint')
    dashboardKey = String(dashboardCredential.api_key || '')
    dashboardKeyId = String(dashboardCredential.key_id || '')
    if (!dashboardKey || !SAFE_UUID.test(dashboardKeyId) ||
        dashboardCredential.purpose !== 'dashboard_session') {
      throw new Error('Dashboard session mint did not return a revocable one-time credential')
    }
    await canaryRequest(fetchImpl, `${api}/v1/stats`, {
      label: 'dashboard session validation', headers: keyHeaders(dashboardKey),
    })

    const service = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/company/service-accounts`, {
        label: 'staging service account creation', method: 'POST', headers: userHeaders,
        json: {
          name: `Release canary ${runToken.slice(0, 16)}`,
          environment: 'staging',
          expires_in_days: 1,
          scopes: [
            'proxy:invoke', 'usage:write', 'usage:read_own',
            'customer:route', 'customer:auto_provision',
            'provider:read', 'provider:manage',
            'jobs:create', 'jobs:read', 'jobs:cancel',
          ],
        },
      })).payload, 'staging service account creation')
    serviceKey = String(service.api_key || '')
    serviceAccountId = String(service.id || '')
    if (!serviceKey || !SAFE_UUID.test(serviceAccountId) || service.secret_available_once !== true) {
      throw new Error('Service account did not return its usable one-time key')
    }

    const configured = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/provider`, {
        label: 'BYOK provider configuration', method: 'PUT',
        headers: keyHeaders(serviceKey),
        json: {
          provider: config.provider,
          model: config.model,
          provider_api_key: config.providerApiKey,
        },
      })).payload, 'BYOK provider configuration')
    if (configured.ok !== true || configured.provider !== config.provider ||
        configured.model !== config.model) {
      throw new Error('BYOK provider configuration was not persisted safely')
    }

    const stream = await canaryRequest(fetchImpl, `${api}/v1/compress/stream`, {
      label: 'bounded BYOK proxy stream', method: 'POST', parse: 'sse', timeoutMs: 45_000,
      headers: {
        ...keyHeaders(serviceKey, customerId),
        'x-brevitas-max-output-tokens': String(MODEL_OUTPUT_MAX_TOKENS),
      },
      json: {
        task: 'Reply exactly CANARY_OK and nothing else.',
        messages: ['Approved staging release canary. Reply exactly CANARY_OK.'],
        prior_context: [],
        lossy: false,
        retrieval: false,
        meter: true,
        pipeline: 'release-canary',
        agent: 'staging-gate',
        run_id: runToken,
      },
    })
    const providerEvidence = parseModelStream(stream.text, config.provider, config.model)
    const statsAfterProxy = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/stats`, {
        label: 'proxy usage attribution', headers: keyHeaders(serviceKey, customerId),
      })).payload, 'proxy usage attribution')
    if (!Number.isFinite(statsAfterProxy.total_calls) || statsAfterProxy.total_calls < 1) {
      throw new Error('Real proxy usage was not attributed to the canary key')
    }

    const usageRequestId = `canary-usage-${runToken}`
    const usageBody = {
      provider: config.provider,
      model: config.model,
      operation: 'chat',
      baseline_tokens: 20,
      compressed_tokens: 20,
      receipt_available: false,
      request_id: usageRequestId,
      strategy: 'byte_preserving',
      project: 'release-canary',
      environment: 'staging',
      pipeline: 'release-canary',
      run_id: runToken,
    }
    const firstUsage = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/usage`, {
        label: 'idempotent usage write', method: 'POST',
        headers: keyHeaders(serviceKey, customerId), json: usageBody,
      })).payload, 'idempotent usage write')
    const duplicateUsage = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/usage`, {
        label: 'duplicate usage write', method: 'POST',
        headers: keyHeaders(serviceKey, customerId), json: usageBody,
      })).payload, 'duplicate usage write')
    if (firstUsage.duplicate === true || duplicateUsage.duplicate !== true ||
        duplicateUsage.request_id !== usageRequestId) {
      throw new Error('Usage receipt idempotency evidence failed')
    }

    const jobHeaders = {
      ...keyHeaders(serviceKey, customerId),
      'idempotency-key': `canary-job-${runToken}`,
    }
    const jobBody = {
      operation: 'compress',
      task: 'Keep the canary payload lossless.',
      messages: ['release canary worker recovery evidence'],
      context: ['bounded staging context'],
      max_attempts: 2,
      retention_seconds: 300,
    }
    const firstJob = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/jobs`, {
        label: 'durable job submission', expected: 202, method: 'POST',
        headers: jobHeaders, json: jobBody,
      })).payload, 'durable job submission')
    const duplicateJob = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/jobs`, {
        label: 'duplicate durable job submission', expected: 202, method: 'POST',
        headers: jobHeaders, json: jobBody,
      })).payload, 'duplicate durable job submission')
    jobId = String(firstJob.id || '')
    if (!SAFE_UUID.test(jobId) || firstJob.created !== true ||
        duplicateJob.created !== false || duplicateJob.id !== jobId) {
      throw new Error('Durable job idempotency evidence failed')
    }
    let completedJob = firstJob
    for (let attempt = 0; attempt < JOB_POLL_ATTEMPTS; attempt += 1) {
      completedJob = requireObject((await canaryRequest(
        fetchImpl, `${api}/v1/jobs/${jobId}`, {
          label: 'durable job completion', headers: keyHeaders(serviceKey, customerId),
        })).payload, 'durable job completion')
      if (['succeeded', 'failed', 'cancelled', 'dead'].includes(completedJob.status)) break
      await sleep(1000)
    }
    if (completedJob.status !== 'succeeded' || !completedJob.result || completedJob.attempts > 2) {
      throw new Error('Durable worker did not complete the bounded compression job')
    }
    jobSucceeded = true

    billingResult = await runBillingCanary({
      config,
      fetchImpl,
      userToken: config.userToken,
      runToken,
      nowSeconds: Math.floor(now() / 1000),
      onCheckoutSession: value => { billingCleanup = value },
    })

    const finalStats = requireObject((await canaryRequest(
      fetchImpl, `${api}/v1/stats`, {
        label: 'final canary usage evidence', headers: keyHeaders(serviceKey, customerId),
      })).payload, 'final canary usage evidence')
    if (!Number.isFinite(finalStats.total_calls) || finalStats.total_calls < 3) {
      throw new Error('Proxy, idempotent usage, and worker receipts were not all observable')
    }

    result = {
      target: 'staging',
      journeyScope: 'api-only-preprovisioned-user',
      authentication: 'pre-provisioned-canary',
      workspace: true,
      dashboardKey: true,
      provider: {
        configured: true,
        streamed: true,
        maxOutputTokens: MODEL_OUTPUT_MAX_TOKENS,
        ...providerEvidence,
      },
      usage: { attributed: true, idempotent: true },
      jobs: {
        submitted: true,
        idempotent: true,
        normalCompletion: true,
        workerDeathReclaim: false,
      },
      billing: {
        mode: billingResult.mode,
        checkoutSession: billingResult.checkoutSession,
        unmatchedWebhookReplay: billingResult.unmatchedWebhookReplay,
        recovery: billingResult.recovery,
        subscriptionMutation: billingResult.subscriptionMutation,
        invoiceMutation: billingResult.invoiceMutation,
        endToEnd: billingResult.endToEnd,
      },
      limitations: [...STAGING_CANARY_LIMITATIONS],
    }
  } catch (error) {
    primaryError = error instanceof Error ? error : new Error('Staging canary failed')
  } finally {
    if (jobId && !jobSucceeded && serviceKey) {
      try {
        await canaryRequest(fetchImpl, `${api}/v1/jobs/${jobId}/cancel`, {
          label: 'incomplete job cleanup', expected: [200, 404], method: 'POST',
          headers: keyHeaders(serviceKey, customerId),
        })
      } catch {
        cleanup.push('job')
      }
    }
    if (billingCleanup?.checkoutSessionId && billingCleanup?.stripeHeaders) {
      try {
        const expired = requireObject((await canaryRequest(
          fetchImpl,
          `${STAGING_CANARY_TARGETS.stripeApi}/v1/checkout/sessions/${encodeURIComponent(billingCleanup.checkoutSessionId)}/expire`,
          {
            label: 'Stripe test session cleanup', method: 'POST',
            headers: {
              ...billingCleanup.stripeHeaders,
              'content-type': 'application/x-www-form-urlencoded',
            },
            rawBody: '',
          },
        )).payload, 'Stripe test session cleanup')
        if (expired.status !== 'expired' || expired.livemode !== false) cleanup.push('checkout')
      } catch {
        cleanup.push('checkout')
      }
    }
    if (serviceAccountId) {
      try {
        await canaryRequest(
          fetchImpl, `${api}/v1/company/service-accounts/${serviceAccountId}`, {
            label: 'service account cleanup', method: 'DELETE', headers: userHeaders,
          })
      } catch {
        cleanup.push('service-account')
      }
    }
    if (dashboardKeyId) {
      try {
        await canaryRequest(fetchImpl, `${api}/v1/keys/${dashboardKeyId}`, {
          label: 'dashboard session cleanup', method: 'DELETE', headers: userHeaders,
        })
      } catch {
        cleanup.push('dashboard-key')
      }
    }
  }

  if (!primaryError && serviceKey && dashboardKey) {
    try {
      await canaryRequest(fetchImpl, `${api}/v1/stats`, {
        label: 'revoked service credential validation', expected: [401, 403],
        headers: keyHeaders(serviceKey, customerId),
      })
      await canaryRequest(fetchImpl, `${api}/v1/stats`, {
        label: 'revoked dashboard credential validation', expected: [401, 403],
        headers: keyHeaders(dashboardKey),
      })
    } catch (error) {
      primaryError = error instanceof Error ? error : new Error('Credential revocation evidence failed')
    }
  }
  if (cleanup.length) {
    throw new Error(`Staging canary cleanup failed for: ${cleanup.join(', ')}`)
  }
  if (primaryError) throw primaryError
  return { ...result, revocation: true, cleanup: true }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await runStagingCanary()
    console.log(
      `Staging API canary passed within declared scope; not covered: ${result.limitations.join(', ')}`,
    )
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'Staging canary failed')
    process.exitCode = 1
  }
}
