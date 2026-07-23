import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import {
  RELEASE_TARGETS,
  assertPlatformDns,
  runReleasePreflight,
} from '../scripts/ci/release-preflight.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const EXPECTED_SHA = 'a'.repeat(40)

function approvedDns() {
  return {
    resolve4: async hostname => hostname.endsWith('.run.app')
      ? ['34.143.72.2']
      : hostname.startsWith('api.')
        ? ['35.214.10.10']
        : ['76.76.21.21'],
    resolve6: async () => {
      const error = new Error('no AAAA records')
      error.code = 'ENODATA'
      throw error
    },
    resolveCname: async hostname => {
      if (hostname.endsWith('.run.app')) {
        const error = new Error('no CNAME records')
        error.code = 'ENODATA'
        throw error
      }
      return hostname.startsWith('api.')
        ? ['brevitas-release.up.railway.app']
        : ['cname.vercel-dns.com']
    },
  }
}

const ready = Object.freeze({
  status: 'ok',
  accepting_traffic: true,
  database_ready: true,
  redis_ready: true,
  kms_ready: true,
  dependencies: {
    postgres: { status: 'ready', authoritative: true },
    redis: { status: 'ready', authoritative: false, role: 'coordination' },
    kms: { status: 'ready', configured: true, active_probe: true, fresh: true },
    compressor: { status: 'ready', required: false },
  },
})

function approvedFetch(overrides = {}) {
  const calls = []
  const fetchImpl = async (url, options) => {
    calls.push({ url, options })
    const parsed = new URL(url)
    if (parsed.pathname === '/') {
      return new Response('<!doctype html><title>Brevitas</title>', {
        status: 200,
        headers: { 'content-type': 'text/html; charset=utf-8', server: 'Vercel', 'x-vercel-id': 'iad1::test' },
      })
    }
    let payload
    if (parsed.pathname === '/api/version') {
      payload = overrides.dashboardBuild || {
        service: 'dashboard', build: { commit_sha: EXPECTED_SHA },
      }
    } else if (parsed.pathname === '/v1/version') {
      payload = overrides.apiBuild || {
        service: 'api', build: { commit_sha: EXPECTED_SHA },
      }
    } else {
      payload = parsed.pathname === '/v1/health/live'
        ? { status: 'ok' }
        : overrides.readiness || ready
    }
    const dashboardResponse = parsed.pathname === '/api/version'
    const cloudRunResponse = parsed.hostname.endsWith('.run.app')
    return Response.json(payload, {
      status: overrides.status || 200,
      headers: {
        server: dashboardResponse
          ? overrides.dashboardServer || 'Vercel'
          : overrides.server || (cloudRunResponse ? 'Google Frontend' : 'railway-edge'),
        ...(dashboardResponse
          ? { 'x-vercel-id': 'iad1::version-test' }
          : cloudRunResponse
            ? overrides.cloudTraceContext === null
              ? {}
              : { 'x-cloud-trace-context': overrides.cloudTraceContext || 'a'.repeat(32) }
            : overrides.railwayRequestId === null
              ? {}
              : { 'x-railway-request-id': overrides.railwayRequestId || 'test-request' }),
      },
    })
  }
  return { calls, fetchImpl }
}

test('release preflight uses fixed staging and production origins only', async () => {
  assert.deepEqual(Object.keys(RELEASE_TARGETS).sort(), ['production', 'staging'])
  assert.equal(RELEASE_TARGETS.production.dashboard.origin, 'https://brevitassystems.com')
  assert.equal(RELEASE_TARGETS.production.api.origin, 'https://api.brevitassystems.com')
  assert.equal(
    RELEASE_TARGETS.staging.dashboard.origin,
    'https://brevitas-systems-staging.vercel.app',
  )
  assert.equal(
    RELEASE_TARGETS.staging.api.origin,
    'https://brevitas-api-staging-975273324573.us-west1.run.app',
  )
  assert.equal(RELEASE_TARGETS.staging.api.platform, 'cloud-run')
  assert.equal(RELEASE_TARGETS.staging.api.compressorRequired, false)
  assert.equal(RELEASE_TARGETS.production.api.compressorRequired, true)

  for (const target of ['', 'prod', 'preview', 'https://attacker.example']) {
    await assert.rejects(() => runReleasePreflight(target), /exactly "staging" or "production"/)
  }
})

test('release preflight probes only non-mutating allowlisted paths and validates full contracts', async () => {
  const { calls, fetchImpl } = approvedFetch()
  const result = await runReleasePreflight('staging', {
    dns: approvedDns(), fetchImpl, expectedSha: EXPECTED_SHA,
  })

  assert.equal(result.target, 'staging')
  assert.equal(result.commit_sha, EXPECTED_SHA)
  assert.equal(result.build_identity, 'self-reported-sha-match')
  assert.equal(result.cryptographic_provenance, false)
  assert.deepEqual(calls.map(call => call.url), [
    'https://brevitas-systems-staging.vercel.app/',
    'https://brevitas-systems-staging.vercel.app/api/version',
    'https://brevitas-api-staging-975273324573.us-west1.run.app/v1/version',
    'https://brevitas-api-staging-975273324573.us-west1.run.app/v1/health/live',
    'https://brevitas-api-staging-975273324573.us-west1.run.app/v1/health/ready',
  ])
  for (const { options } of calls) {
    assert.equal(options.method, 'GET')
    assert.equal(options.redirect, 'manual')
    assert.equal(options.headers['x-brevitas-release-preflight'], 'non-mutating')
    assert.equal(options.body, undefined)
  }
})

test('release preflight fails clearly on unresolved and misrouted DNS', async () => {
  const unresolved = {
    resolve4: async () => {
      const error = new Error('queryA ENOTFOUND brevitas-systems-staging.vercel.app')
      error.code = 'ENOTFOUND'
      throw error
    },
    resolve6: async () => {
      const error = new Error('queryAaaa ENOTFOUND brevitas-systems-staging.vercel.app')
      error.code = 'ENOTFOUND'
      throw error
    },
    resolveCname: async () => [],
  }
  await assert.rejects(
    () => runReleasePreflight('staging', {
      dns: unresolved, fetchImpl: approvedFetch().fetchImpl, expectedSha: EXPECTED_SHA,
    }),
    /DNS brevitas-systems-staging\.vercel\.app did not resolve.*ENOTFOUND/,
  )

  assert.throws(
    () => assertPlatformDns('staging.brevitassystems.com', 'vercel', {
      addresses: ['203.0.113.20'], cnames: ['attacker.example'],
    }),
    /not routed to Vercel/,
  )
  assert.doesNotThrow(
    () => assertPlatformDns('brevitas-systems-staging.vercel.app', 'vercel', {
      addresses: ['216.198.79.1'], cnames: [],
    }),
  )
  assert.throws(
    () => assertPlatformDns('staging-api.brevitassystems.com', 'railway', {
      addresses: ['203.0.113.21'], cnames: ['attacker.example'],
    }),
    /not routed to Railway/,
  )
  assert.throws(
    () => assertPlatformDns('attacker.run.app', 'cloud-run', {
      addresses: ['203.0.113.22'], cnames: [],
    }),
    /not a deterministic Cloud Run service hostname/,
  )
})

test('release preflight rejects legacy degraded readiness and incomplete dependency shapes', async () => {
  for (const readiness of [
    { status: 'degraded' },
    { ...ready, status: 'degraded' },
    { ...ready, dependencies: { ...ready.dependencies, postgres: { status: 'ready' } } },
    { ...ready, dependencies: { ...ready.dependencies, kms: { status: 'ready' } } },
    { ...ready, dependencies: { ...ready.dependencies, compressor: { status: 'unavailable' } } },
  ]) {
    const { fetchImpl } = approvedFetch({ readiness })
    await assert.rejects(
      () => runReleasePreflight('production', {
        dns: approvedDns(), fetchImpl, expectedSha: EXPECTED_SHA,
      }),
      /readiness contract requires status="ok"/,
    )
  }
})

test('staging permits only the documented optional-compressor degradation', async () => {
  const optionalCompressorReadiness = {
    ...ready,
    status: 'degraded',
    dependencies: {
      ...ready.dependencies,
      compressor: { status: 'unavailable', required: false },
    },
  }
  const accepted = approvedFetch({ readiness: optionalCompressorReadiness })
  await assert.doesNotReject(() => runReleasePreflight('staging', {
    dns: approvedDns(), fetchImpl: accepted.fetchImpl, expectedSha: EXPECTED_SHA,
  }))

  const rejected = approvedFetch({ readiness: optionalCompressorReadiness })
  await assert.rejects(
    () => runReleasePreflight('production', {
      dns: approvedDns(), fetchImpl: rejected.fetchImpl, expectedSha: EXPECTED_SHA,
    }),
    /requires status="ok"/,
  )

  for (const readiness of [
    { ...optionalCompressorReadiness, status: 'ok' },
    {
      ...optionalCompressorReadiness,
      dependencies: {
        ...optionalCompressorReadiness.dependencies,
        compressor: { status: 'unavailable', required: true },
      },
    },
    { ...optionalCompressorReadiness, database_ready: false },
  ]) {
    const { fetchImpl } = approvedFetch({ readiness })
    await assert.rejects(
      () => runReleasePreflight('staging', {
        dns: approvedDns(), fetchImpl, expectedSha: EXPECTED_SHA,
      }),
      /permits only a compressor-only/,
    )
  }
})

test('release preflight rejects HTTPS misrouting, redirects, and missing health routes', async () => {
  const misrouted = approvedFetch({ server: 'nginx', cloudTraceContext: null }).fetchImpl
  await assert.rejects(
    () => runReleasePreflight('staging', {
      dns: approvedDns(), fetchImpl: misrouted, expectedSha: EXPECTED_SHA,
    }),
    /Cloud Run routing signature/,
  )

  const redirecting = async url => new Response(null, {
    status: 307,
    headers: { location: new URL('/legacy-health', url), server: 'Vercel' },
  })
  await assert.rejects(
    () => runReleasePreflight('production', {
      dns: approvedDns(), fetchImpl: redirecting, expectedSha: EXPECTED_SHA,
    }),
    /unexpectedly redirected/,
  )

  const missingRoute = async (url, options) => {
    if (new URL(url).pathname === '/v1/health/live') {
      return Response.json({ detail: 'Not Found' }, {
        status: 404,
        headers: { server: 'railway-edge' },
      })
    }
    return approvedFetch().fetchImpl(url, options)
  }
  await assert.rejects(
    () => runReleasePreflight('production', {
      dns: approvedDns(), fetchImpl: missingRoute, expectedSha: EXPECTED_SHA,
    }),
    /health\/live returned HTTP 404/,
  )
})

test('release preflight rejects missing, mismatched, and overexposed build identity', async () => {
  await assert.rejects(
    () => runReleasePreflight('staging', {
      dns: approvedDns(), fetchImpl: approvedFetch().fetchImpl,
    }),
    /BREVITAS_EXPECTED_RELEASE_SHA must be a full immutable Git commit SHA/,
  )

  for (const overrides of [
    { dashboardBuild: { service: 'dashboard', build: { commit_sha: 'b'.repeat(40) } } },
    { apiBuild: { service: 'api', build: {} } },
    {
      apiBuild: {
        service: 'api',
        build: { commit_sha: EXPECTED_SHA, branch: 'main' },
      },
    },
  ]) {
    await assert.rejects(
      () => runReleasePreflight('production', {
        dns: approvedDns(), fetchImpl: approvedFetch(overrides).fetchImpl,
        expectedSha: EXPECTED_SHA,
      }),
      /build identity|self-reported commit/,
    )
  }
})

test('preflight documentation rejects cryptographic or deployment-provenance claims', () => {
  const documentation = read('docs/RELEASE_PREFLIGHT.md')
  const releaseSecurity = read('docs/RELEASE_SECURITY.md')
  assert.match(documentation, /version fields are self-reported/i)
  assert.match(documentation, /does not bind[\s\S]+served bytes or container to the[\s\S]+SHA/i)
  assert.match(documentation, /not independent proof/i)
  assert.match(documentation, /cryptographic_attestation=false/)
  assert.match(documentation, /deployment_verified=false/)
  assert.match(releaseSecurity, /self-reported full SHA against the workflow SHA/i)
  assert.match(releaseSecurity, /does not cryptographically bind served bytes or images/i)
})

test('manual workflow exposes only fixed target choices and no arbitrary URL input', () => {
  const workflow = read('.github/workflows/release-preflight.yml')
  assert.match(workflow, /workflow_dispatch:/)
  assert.match(workflow, /options:\s*\n\s*- staging\s*\n\s*- production/)
  assert.doesNotMatch(workflow, /api_url|dashboard_url/i)
  assert.match(workflow, /github\.repository == 'jeojdi\/Brevitas-Systems'/)
  assert.match(workflow, /github\.event\.repository\.fork == false/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
  assert.match(workflow, /BREVITAS_EXPECTED_RELEASE_SHA: \$\{\{ github\.sha \}\}/)
  assert.match(workflow, /npm run release:preflight -- \$\{\{ inputs\.target \}\}/)
})
