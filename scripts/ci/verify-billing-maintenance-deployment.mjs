import { isIP } from 'node:net'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

import { assertBuildIdentityContract } from './release-preflight.mjs'

const HOSTNAME = /^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/
const FULL_SHA = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/
const MAX_RESPONSE_BYTES = 4_096
const DEFAULT_TIMEOUT_MS = 8_000

function parsedEndpoint(rawValue, expectedPath, label) {
  let parsed
  try {
    parsed = new URL(String(rawValue || '').trim())
  } catch {
    throw new Error(`${label} must be an explicit HTTPS URL`)
  }
  const hostname = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (parsed.username || parsed.password || parsed.search || parsed.hash ||
      parsed.pathname !== expectedPath) {
    throw new Error(
      `${label} must use exact path ${expectedPath} with no credentials, query, or fragment`,
    )
  }
  return { parsed, hostname }
}

function publicHttpsEndpoint(rawValue, expectedPath, label) {
  const { parsed, hostname } = parsedEndpoint(rawValue, expectedPath, label)
  if (parsed.protocol !== 'https:' || (parsed.port && parsed.port !== '443') ||
      !HOSTNAME.test(hostname) || isIP(hostname) !== 0 ||
      hostname === 'localhost' || hostname.endsWith('.local')) {
    throw new Error(
      `${label} public endpoint must use HTTPS on the default port with a DNS hostname`,
    )
  }
  return parsed.href
}

function workerEndpoint(rawValue) {
  const label = 'BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL'
  const { parsed, hostname } = parsedEndpoint(rawValue, '/version', label)
  if (parsed.protocol === 'http:' && hostname === '127.0.0.1' &&
      /^[0-9]+$/.test(parsed.port)) {
    const port = Number(parsed.port)
    if (port >= 1_024 && port <= 65_535) return parsed.href
  }
  return publicHttpsEndpoint(rawValue, '/version', label)
}

export function billingMaintenanceVersionEndpoints(environment = process.env) {
  const dashboard = publicHttpsEndpoint(
    environment.BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL,
    '/api/version',
    'BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL',
  )
  const worker = workerEndpoint(environment.BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL)
  if (dashboard === worker || new URL(dashboard).origin === new URL(worker).origin) {
    throw new Error('Dashboard and worker billing-maintenance version endpoints must use distinct origins')
  }
  return Object.freeze({ dashboard, worker })
}

async function boundedJson(response, label) {
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.toLowerCase().includes('application/json')) {
    throw new Error(`${label} did not return application/json`)
  }
  const declaredLength = Number(response.headers.get('content-length'))
  if (Number.isFinite(declaredLength) && declaredLength > MAX_RESPONSE_BYTES) {
    throw new Error(`${label} response exceeds ${MAX_RESPONSE_BYTES} bytes`)
  }
  if (!response.body || typeof response.body.getReader !== 'function') {
    throw new Error(`${label} response body is unavailable`)
  }

  const reader = response.body.getReader()
  const chunks = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > MAX_RESPONSE_BYTES) {
        await reader.cancel()
        throw new Error(`${label} response exceeds ${MAX_RESPONSE_BYTES} bytes`)
      }
      chunks.push(value)
    }
  } finally {
    reader.releaseLock()
  }

  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  let payload
  try {
    payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
  } catch {
    throw new Error(`${label} did not return bounded valid UTF-8 JSON`)
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload) ||
      JSON.stringify(Object.keys(payload).sort()) !== JSON.stringify(['build', 'service'])) {
    throw new Error(`${label} has an unsupported top-level contract`)
  }
  return payload
}

async function verifyOne(fetchImpl, url, expectedSha, expectedService, signalFactory, timeoutMs) {
  let response
  try {
    response = await fetchImpl(url, {
      method: 'GET',
      redirect: 'manual',
      cache: 'no-store',
      signal: signalFactory(timeoutMs),
      headers: {
        accept: 'application/json',
        'user-agent': 'brevitas-billing-maintenance/1',
        'x-brevitas-billing-maintenance': 'read-only-version-check',
      },
    })
  } catch (error) {
    throw new Error(
      `${expectedService} deployed-version request failed: ${error instanceof Error ? error.message : String(error)}`,
    )
  }
  if (response.redirected || (response.url && response.url !== url) ||
      (response.status >= 300 && response.status < 400)) {
    throw new Error(`${expectedService} deployed-version request redirected`)
  }
  if (response.status !== 200) {
    throw new Error(`${expectedService} deployed-version request returned HTTP ${response.status}`)
  }

  const payload = await boundedJson(response, `${expectedService} deployed version`)
  assertBuildIdentityContract(payload, expectedSha, expectedService)
  if (payload.build.commit_sha !== expectedSha) {
    throw new Error(`${expectedService} self-reported commit is not an exact maintenance SHA match`)
  }
}

export async function verifyBillingMaintenanceDeployment(environment = process.env, dependencies = {}) {
  const expectedSha = String(environment.BREVITAS_BILLING_MAINTENANCE_SHA || '')
    .trim().toLowerCase()
  if (!FULL_SHA.test(expectedSha)) {
    throw new Error('BREVITAS_BILLING_MAINTENANCE_SHA must be a full immutable commit SHA')
  }
  const endpoints = billingMaintenanceVersionEndpoints(environment)
  const fetchImpl = dependencies.fetchImpl || globalThis.fetch
  const signalFactory = dependencies.signalFactory || (timeoutMs => AbortSignal.timeout(timeoutMs))
  const timeoutMs = dependencies.timeoutMs ?? DEFAULT_TIMEOUT_MS
  if (typeof fetchImpl !== 'function' || typeof signalFactory !== 'function' ||
      !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > DEFAULT_TIMEOUT_MS) {
    throw new Error('Billing maintenance deployed-version dependencies are invalid')
  }

  await Promise.all([
    verifyOne(fetchImpl, endpoints.dashboard, expectedSha, 'dashboard', signalFactory, timeoutMs),
    verifyOne(fetchImpl, endpoints.worker, expectedSha, 'worker', signalFactory, timeoutMs),
  ])
  return Object.freeze({
    dashboard: endpoints.dashboard,
    worker: endpoints.worker,
    commit_sha: expectedSha,
    build_identity: 'self-reported-sha-match',
    cryptographic_provenance: false,
  })
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await verifyBillingMaintenanceDeployment()
    console.log(
      `Billing maintenance deployed-version check passed for dashboard and worker at ${result.commit_sha}; identity is self-reported, not cryptographic provenance.`,
    )
  } catch (error) {
    console.error(
      `Billing maintenance deployed-version check failed: ${error instanceof Error ? error.message : String(error)}`,
    )
    process.exitCode = 2
  }
}
