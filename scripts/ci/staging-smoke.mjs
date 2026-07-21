import { isIP } from 'node:net'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const allowedHosts = Object.freeze({
  api: new Set(['staging-api.brevitassystems.com', 'api-staging.brevitassystems.com']),
  dashboard: new Set(['staging.brevitassystems.com', 'dashboard-staging.brevitassystems.com']),
})

function required(environment, name) {
  const value = String(environment[name] || '').trim()
  if (!value) throw new Error(`${name} is required by the approved staging environment`)
  return value
}

export function assertStagingTarget(rawValue, kind) {
  let target
  try {
    target = new URL(rawValue)
  } catch {
    throw new Error(`${kind} staging URL is invalid`)
  }
  const hostname = target.hostname.toLowerCase()
  if (target.protocol !== 'https:') throw new Error(`${kind} staging URL must use HTTPS`)
  if (target.username || target.password) throw new Error(`${kind} staging URL may not contain credentials`)
  if (target.port || target.pathname !== '/' || target.search || target.hash) {
    throw new Error(`${kind} staging URL must be an origin without port, path, query, or fragment`)
  }
  if (isIP(hostname) || hostname === 'localhost' || hostname.endsWith('.localhost')) {
    throw new Error(`${kind} staging URL may not resolve through a local literal host`)
  }
  if (/(^|[.-])(prod|production)([.-]|$)/i.test(hostname)) {
    throw new Error(`${kind} staging URL contains a production marker`)
  }
  if (!allowedHosts[kind]?.has(hostname)) {
    throw new Error(`${kind} staging host is not in the repository allowlist`)
  }
  return target.origin
}

function assertExecutionGuard(environment) {
  if (environment.STAGING_SMOKE_ALLOWED !== 'true') {
    throw new Error('Staging smoke requires the explicit workflow guard')
  }
  if (environment.STAGING_ENVIRONMENT_CONFIRMATION !== 'staging') {
    throw new Error('Staging environment confirmation is missing')
  }
  if (environment.STAGING_REPOSITORY !== 'jeojdi/Brevitas-Systems') {
    throw new Error('Staging smoke refuses an unapproved repository or fork')
  }
  if (environment.STAGING_REPOSITORY_FORK !== 'false') {
    throw new Error('Staging smoke refuses fork execution')
  }
  if (environment.STAGING_GITHUB_EVENT !== 'workflow_dispatch') {
    throw new Error('Staging smoke is manual workflow_dispatch only')
  }
  if (environment.STAGING_GITHUB_REF !== 'refs/heads/main') {
    throw new Error('Staging smoke must run from the protected main branch')
  }
}

async function expectStatus(fetchImpl, url, expected, options = {}) {
  const response = await fetchImpl(url, {
    redirect: 'manual',
    signal: AbortSignal.timeout(8_000),
    ...options,
    headers: {
      accept: 'application/json',
      'user-agent': 'brevitas-release-staging-smoke/1',
      'x-brevitas-release-smoke': 'non-mutating',
      ...(options.headers || {}),
    },
  })
  const allowed = Array.isArray(expected) ? expected : [expected]
  if (!allowed.includes(response.status)) {
    throw new Error(`Staging check ${new URL(url).pathname} returned HTTP ${response.status}`)
  }
  return response
}

async function expectDeniedNonleak(fetchImpl, url, forbiddenValues, options = {}) {
  const response = await expectStatus(fetchImpl, url, [403, 404], options)
  const body = await response.text()
  for (const value of forbiddenValues) {
    if (value && body.includes(value)) {
      throw new Error(`Staging tenant-isolation denial leaked fixture identity at ${new URL(url).pathname}`)
    }
  }
}

async function assertReady(response) {
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error('Staging readiness response is not valid JSON')
  }
  if (
    payload?.accepting_traffic !== true ||
    payload?.database_ready !== true ||
    payload?.redis_ready !== true ||
    payload?.kms_ready !== true ||
    payload?.dependencies?.kms?.status !== 'ready' ||
    payload?.dependencies?.kms?.configured !== true ||
    payload?.dependencies?.kms?.active_probe !== true ||
    payload?.dependencies?.kms?.fresh !== true ||
    payload?.dependencies?.compressor?.status !== 'ready'
  ) {
    throw new Error(
      'Staging readiness did not confirm API, Postgres, Redis, fresh active KMS, and compressor',
    )
  }
}

export async function runStagingSmoke(
  environment = process.env,
  fetchImpl = globalThis.fetch,
) {
  assertExecutionGuard(environment)
  if (typeof fetchImpl !== 'function') throw new Error('A fetch implementation is required')

  const apiOrigin = assertStagingTarget(required(environment, 'STAGING_API_URL'), 'api')
  const dashboardOrigin = assertStagingTarget(
    required(environment, 'STAGING_DASHBOARD_URL'),
    'dashboard',
  )
  const tenantAKey = required(environment, 'STAGING_TENANT_A_API_KEY')
  const tenantBKey = required(environment, 'STAGING_TENANT_B_API_KEY')
  const tenantAJob = required(environment, 'STAGING_TENANT_A_JOB_ID')
  const tenantBJob = required(environment, 'STAGING_TENANT_B_JOB_ID')
  const tenantACustomer = required(environment, 'STAGING_TENANT_A_CUSTOMER_ID')
  const tenantBCustomer = required(environment, 'STAGING_TENANT_B_CUSTOMER_ID')
  const billingUserToken = required(environment, 'STAGING_BILLING_USER_TOKEN')
  const billingRecoverySecret = required(environment, 'STAGING_BILLING_RECOVERY_SECRET')
  if (tenantAKey === tenantBKey) {
    throw new Error('Staging tenant fixtures require two distinct API keys')
  }
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
  if (!uuid.test(tenantAJob) || !uuid.test(tenantBJob) || tenantAJob === tenantBJob) {
    throw new Error('Staging tenant fixtures require two distinct UUID job IDs')
  }
  const customerId = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/
  if (
    !customerId.test(tenantACustomer) || !customerId.test(tenantBCustomer) ||
    tenantACustomer === tenantBCustomer
  ) {
    throw new Error('Staging tenant fixtures require two distinct safe customer IDs')
  }

  await expectStatus(fetchImpl, `${apiOrigin}/v1/health/live`, 200)
  await assertReady(await expectStatus(fetchImpl, `${apiOrigin}/v1/health/ready`, 200))
  await expectStatus(fetchImpl, `${apiOrigin}/v1/stats`, 401)

  const apiKey = (value, customer) => ({
    'x-brevitas-key': value,
    'x-brevitas-customer-id': customer,
  })
  const forbiddenTenantContent = [
    tenantAKey, tenantBKey, tenantAJob, tenantBJob, tenantACustomer, tenantBCustomer,
  ]
  await expectStatus(fetchImpl, `${apiOrigin}/v1/jobs/${tenantAJob}`, 200, {
    headers: apiKey(tenantAKey, tenantACustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantAJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantBKey, tenantBCustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantAJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantAKey, tenantBCustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantAJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantBKey, tenantACustomer),
  })
  await expectStatus(fetchImpl, `${apiOrigin}/v1/jobs/${tenantBJob}`, 200, {
    headers: apiKey(tenantBKey, tenantBCustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantBJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantAKey, tenantACustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantBJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantBKey, tenantACustomer),
  })
  await expectDeniedNonleak(fetchImpl, `${apiOrigin}/v1/jobs/${tenantBJob}`, forbiddenTenantContent, {
    headers: apiKey(tenantAKey, tenantBCustomer),
  })

  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/status`, 401)
  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/status`, 200, {
    headers: { authorization: `Bearer ${billingUserToken}` },
  })
  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/sync`, 401, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  })
  // The global recovery secret is a second factor, never a user identity.
  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/sync`, 401, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${billingRecoverySecret}`,
      'content-type': 'application/json',
    },
    body: '{}',
  })
  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/sync`, 401, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${billingUserToken}`,
      'content-type': 'application/json',
    },
    body: '{}',
  })
  await expectStatus(fetchImpl, `${dashboardOrigin}/api/billing/sync`, 400, {
    method: 'POST',
    headers: {
      authorization: `Bearer ${billingUserToken}`,
      'x-billing-recovery-secret': billingRecoverySecret,
      'content-type': 'application/json',
    },
    // Deliberately invalid: verifies both factors without mutating a ledger.
    body: '{}',
  })

  return {
    liveness: true,
    readiness: true,
    auth: true,
    tenantIsolation: true,
    billing: true,
    manualRecovery: true,
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await runStagingSmoke()
    console.log(`Staging smoke passed: ${Object.keys(result).join(', ')}`)
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
