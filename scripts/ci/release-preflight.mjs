import { resolve4, resolve6, resolveCname } from 'node:dns/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const VERCEL_APEX_IPV4 = new Set(['76.76.21.21'])
const FULL_COMMIT_SHA = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/
const IMAGE_DIGEST = /^sha256:[0-9a-f]{64}$/
const RELEASE_VERSION = /^(?=.{1,64}$)v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
const RFC3339_TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/

export const RELEASE_TARGETS = Object.freeze({
  staging: Object.freeze({
    dashboard: Object.freeze({
      origin: 'https://staging.brevitassystems.com',
      platform: 'vercel',
    }),
    api: Object.freeze({
      origin: 'https://staging-api.brevitassystems.com',
      platform: 'railway',
    }),
  }),
  production: Object.freeze({
    dashboard: Object.freeze({
      origin: 'https://brevitassystems.com',
      platform: 'vercel',
    }),
    api: Object.freeze({
      origin: 'https://api.brevitassystems.com',
      platform: 'railway',
    }),
  }),
})

function targetFor(name) {
  if (name !== 'staging' && name !== 'production') {
    throw new Error('Release target must be exactly "staging" or "production"')
  }
  return RELEASE_TARGETS[name]
}

function formatDnsError(error) {
  if (!(error instanceof Error)) return String(error)
  const code = typeof error.code === 'string' ? `${error.code}: ` : ''
  return `${code}${error.message}`
}

async function optionalRecords(query, hostname) {
  try {
    return { records: await query(hostname), error: null }
  } catch (error) {
    return { records: [], error }
  }
}

export async function inspectDns(hostname, dns = {}) {
  const lookup4 = dns.resolve4 || resolve4
  const lookup6 = dns.resolve6 || resolve6
  const lookupCname = dns.resolveCname || resolveCname
  const [ipv4, ipv6, cname] = await Promise.all([
    optionalRecords(lookup4, hostname),
    optionalRecords(lookup6, hostname),
    optionalRecords(lookupCname, hostname),
  ])
  const addresses = [...ipv4.records, ...ipv6.records]
  if (addresses.length === 0) {
    const failures = [ipv4.error, ipv6.error].filter(Boolean).map(formatDnsError).join('; ')
    throw new Error(`DNS ${hostname} did not resolve to an A or AAAA address (${failures || 'no records'})`)
  }
  return {
    addresses,
    cnames: cname.records.map(value => value.toLowerCase().replace(/\.$/, '')),
  }
}

export function assertPlatformDns(hostname, platform, records) {
  if (platform === 'vercel') {
    const vercelCname = records.cnames.some(value =>
      value === 'cname.vercel-dns.com' || value.endsWith('.vercel-dns.com'))
    const vercelApex = records.addresses.some(value => VERCEL_APEX_IPV4.has(value))
    if (!vercelCname && !vercelApex) {
      throw new Error(
        `DNS ${hostname} is not routed to Vercel (expected *.vercel-dns.com or the approved Vercel apex address)`,
      )
    }
    return
  }
  if (platform === 'railway') {
    const railwayCname = records.cnames.some(value => value.endsWith('.up.railway.app'))
    if (!railwayCname) {
      throw new Error(`DNS ${hostname} is not routed to Railway (expected a *.up.railway.app CNAME)`)
    }
    return
  }
  throw new Error(`Unsupported release platform ${platform}`)
}

function assertPlatformResponse(hostname, platform, response) {
  const server = (response.headers.get('server') || '').toLowerCase()
  if (platform === 'vercel') {
    if (server !== 'vercel' && !response.headers.get('x-vercel-id')) {
      throw new Error(`HTTPS ${hostname} did not return a Vercel routing signature`)
    }
    return
  }
  if (platform === 'railway') {
    if (!server.includes('railway') && !response.headers.get('x-railway-request-id')) {
      throw new Error(`HTTPS ${hostname} did not return a Railway routing signature`)
    }
    return
  }
  throw new Error(`Unsupported release platform ${platform}`)
}

async function get(fetchImpl, url, platform) {
  let response
  try {
    response = await fetchImpl(url, {
      method: 'GET',
      redirect: 'manual',
      cache: 'no-store',
      signal: AbortSignal.timeout(8_000),
      headers: {
        accept: 'application/json, text/html;q=0.5',
        'user-agent': 'brevitas-release-preflight/1',
        'x-brevitas-release-preflight': 'non-mutating',
      },
    })
  } catch (error) {
    throw new Error(`HTTPS ${url} failed: ${error instanceof Error ? error.message : String(error)}`)
  }
  if (response.status >= 300 && response.status < 400) {
    throw new Error(`HTTPS ${url} unexpectedly redirected with HTTP ${response.status}`)
  }
  if (response.status !== 200) {
    throw new Error(`HTTPS ${url} returned HTTP ${response.status}; expected 200`)
  }
  assertPlatformResponse(new URL(url).hostname, platform, response)
  return response
}

async function jsonContract(response, label) {
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.toLowerCase().includes('application/json')) {
    throw new Error(`${label} did not return application/json`)
  }
  try {
    return await response.json()
  } catch {
    throw new Error(`${label} did not return valid JSON`)
  }
}

export function assertLivenessContract(payload) {
  if (payload?.status !== 'ok') {
    throw new Error('API liveness contract requires status="ok"')
  }
}

export function assertBuildIdentityContract(payload, expectedSha, expectedService) {
  if (!FULL_COMMIT_SHA.test(expectedSha)) {
    throw new Error('Expected release SHA must be a full immutable Git commit SHA')
  }
  if (payload?.service !== expectedService || !payload?.build ||
      typeof payload.build !== 'object' || Array.isArray(payload.build)) {
    throw new Error(`${expectedService} build identity contract is incomplete`)
  }
  const allowedFields = new Set(['commit_sha', 'built_at', 'version', 'image_digest'])
  if (Object.keys(payload.build).some(field => !allowedFields.has(field))) {
    throw new Error(`${expectedService} build identity exposes an unsupported field`)
  }
  const deployedSha = String(payload.build.commit_sha || '').toLowerCase()
  if (!FULL_COMMIT_SHA.test(deployedSha) || deployedSha !== expectedSha) {
    throw new Error(
      `${expectedService} self-reported commit does not match the expected workflow SHA`,
    )
  }
  if (payload.build.built_at !== undefined &&
      (typeof payload.build.built_at !== 'string' ||
       !RFC3339_TIMESTAMP.test(payload.build.built_at) ||
       Number.isNaN(Date.parse(payload.build.built_at)))) {
    throw new Error(`${expectedService} build timestamp is invalid`)
  }
  if (payload.build.version !== undefined &&
      (typeof payload.build.version !== 'string' ||
       !RELEASE_VERSION.test(payload.build.version))) {
    throw new Error(`${expectedService} build version is invalid`)
  }
  if (payload.build.image_digest !== undefined &&
      (typeof payload.build.image_digest !== 'string' ||
       !IMAGE_DIGEST.test(payload.build.image_digest))) {
    throw new Error(`${expectedService} image digest is invalid`)
  }
}

export function assertReadinessContract(payload) {
  const ready =
    payload?.status === 'ok' &&
    payload?.accepting_traffic === true &&
    payload?.database_ready === true &&
    payload?.redis_ready === true &&
    payload?.kms_ready === true &&
    payload?.dependencies?.postgres?.status === 'ready' &&
    payload?.dependencies?.postgres?.authoritative === true &&
    payload?.dependencies?.redis?.status === 'ready' &&
    payload?.dependencies?.redis?.role === 'coordination' &&
    payload?.dependencies?.kms?.status === 'ready' &&
    payload?.dependencies?.kms?.configured === true &&
    payload?.dependencies?.kms?.active_probe === true &&
    payload?.dependencies?.kms?.fresh === true &&
    payload?.dependencies?.compressor?.status === 'ready'
  if (!ready) {
    throw new Error(
      'API readiness contract requires status="ok", traffic acceptance, ready Postgres/Redis/KMS/compressor, authoritative Postgres, coordination Redis, and fresh active KMS evidence',
    )
  }
}

export async function runReleasePreflight(targetName, dependencies = {}) {
  const target = targetFor(targetName)
  const fetchImpl = dependencies.fetchImpl || globalThis.fetch
  if (typeof fetchImpl !== 'function') throw new Error('A fetch implementation is required')
  const dns = dependencies.dns || {}
  const expectedSha = String(
    dependencies.expectedSha || process.env.BREVITAS_EXPECTED_RELEASE_SHA || '',
  ).trim().toLowerCase()
  if (!FULL_COMMIT_SHA.test(expectedSha)) {
    throw new Error('BREVITAS_EXPECTED_RELEASE_SHA must be a full immutable Git commit SHA')
  }

  for (const service of [target.dashboard, target.api]) {
    const hostname = new URL(service.origin).hostname
    const records = await inspectDns(hostname, dns)
    assertPlatformDns(hostname, service.platform, records)
  }

  const dashboard = await get(fetchImpl, `${target.dashboard.origin}/`, target.dashboard.platform)
  if (!(dashboard.headers.get('content-type') || '').toLowerCase().includes('text/html')) {
    throw new Error('Dashboard root did not return text/html')
  }

  const dashboardVersion = await jsonContract(
    await get(fetchImpl, `${target.dashboard.origin}/api/version`, target.dashboard.platform),
    'Dashboard build identity',
  )
  assertBuildIdentityContract(dashboardVersion, expectedSha, 'dashboard')

  const apiVersion = await jsonContract(
    await get(fetchImpl, `${target.api.origin}/v1/version`, target.api.platform),
    'API build identity',
  )
  assertBuildIdentityContract(apiVersion, expectedSha, 'api')

  const liveness = await jsonContract(
    await get(fetchImpl, `${target.api.origin}/v1/health/live`, target.api.platform),
    'API liveness',
  )
  assertLivenessContract(liveness)
  const readiness = await jsonContract(
    await get(fetchImpl, `${target.api.origin}/v1/health/ready`, target.api.platform),
    'API readiness',
  )
  assertReadinessContract(readiness)

  return {
    target: targetName,
    dashboard: target.dashboard.origin,
    api: target.api.origin,
    dns: 'verified',
    https: 'verified',
    routing: 'verified',
    commit_sha: expectedSha,
    build_identity: 'self-reported-sha-match',
    cryptographic_provenance: false,
    liveness: 'ok',
    readiness: 'ok',
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2)
    if (args.length !== 1) throw new Error('Usage: npm run release:preflight -- staging|production')
    const result = await runReleasePreflight(args[0])
    console.log(
      `Release preflight passed for ${result.target}: DNS, HTTPS, routing, liveness, readiness; ` +
      'build identity is a self-reported SHA match, not cryptographic provenance',
    )
  } catch (error) {
    console.error(`Release preflight failed: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  }
}
