import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'

import {
  billingMaintenanceVersionEndpoints,
  verifyBillingMaintenanceDeployment,
} from '../scripts/ci/verify-billing-maintenance-deployment.mjs'

const SHA = 'a'.repeat(40)
const read = path => readFileSync(resolve(import.meta.dirname, '..', path), 'utf8')
const environment = Object.freeze({
  BREVITAS_BILLING_MAINTENANCE_SHA: SHA,
  BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL:
    'https://dashboard.example.invalid/api/version',
  BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL:
    'http://127.0.0.1:43119/version',
})

function versionResponse(service, commitSha = SHA, init = {}) {
  return Response.json(
    { service, build: { commit_sha: commitSha } },
    { status: 200, ...init },
  )
}

function approvedFetch(calls = []) {
  return async (url, options) => {
    calls.push({ url, options })
    return versionResponse(new URL(url).pathname === '/api/version' ? 'dashboard' : 'worker')
  }
}

test('billing maintenance verifies both deployed versions with read-only bounded requests', async () => {
  const calls = []
  const result = await verifyBillingMaintenanceDeployment(environment, {
    fetchImpl: approvedFetch(calls),
  })
  assert.equal(result.commit_sha, SHA)
  assert.equal(result.build_identity, 'self-reported-sha-match')
  assert.equal(result.cryptographic_provenance, false)
  assert.deepEqual(calls.map(({ url }) => url), [
    environment.BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL,
    environment.BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL,
  ])
  for (const { options } of calls) {
    assert.equal(options.method, 'GET')
    assert.equal(options.redirect, 'manual')
    assert.equal(options.cache, 'no-store')
    assert.equal(options.body, undefined)
    assert.equal(options.headers.accept, 'application/json')
    assert.equal(options.headers['x-brevitas-billing-maintenance'], 'read-only-version-check')
    assert.equal(options.headers.authorization, undefined)
    assert.equal(options.headers.cookie, undefined)
    assert.ok(options.signal instanceof AbortSignal)
  }
})

test('billing maintenance rejects a deployed SHA mismatch', async () => {
  await assert.rejects(
    () => verifyBillingMaintenanceDeployment(environment, {
      fetchImpl: async url => versionResponse(
        new URL(url).pathname === '/api/version' ? 'dashboard' : 'worker',
        new URL(url).pathname === '/api/version' ? SHA : 'b'.repeat(40),
      ),
    }),
    /worker self-reported commit does not match|exact maintenance SHA match/,
  )
})

test('billing maintenance rejects redirects before accepting a version body', async () => {
  await assert.rejects(
    () => verifyBillingMaintenanceDeployment(environment, {
      fetchImpl: async url => new URL(url).pathname === '/api/version'
        ? new Response(null, { status: 302, headers: { location: 'https://attacker.invalid/' } })
        : versionResponse('worker'),
    }),
    /dashboard deployed-version request redirected/,
  )
})

test('billing maintenance rejects malformed or overclaimed self-reported contracts', async () => {
  for (const dashboardPayload of [
    { service: 'api', build: { commit_sha: SHA } },
    { service: 'dashboard' },
    { service: 'dashboard', build: { commit_sha: SHA, branch: 'main' } },
    { service: 'dashboard', build: { commit_sha: SHA }, signed: true },
  ]) {
    await assert.rejects(
      () => verifyBillingMaintenanceDeployment(environment, {
        fetchImpl: async url => new URL(url).pathname === '/api/version'
          ? Response.json(dashboardPayload)
          : versionResponse('worker'),
      }),
      /contract|unsupported field/,
    )
  }
})

test('billing maintenance fails closed when a deployed-version request times out', async () => {
  const neverCompletes = (_url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true })
  })
  await assert.rejects(
    () => verifyBillingMaintenanceDeployment(environment, {
      fetchImpl: neverCompletes,
      timeoutMs: 1,
    }),
    /deployed-version request failed.*timeout/i,
  )
})

test('billing maintenance requires public dashboard HTTPS and permits only an explicit worker tunnel', () => {
  assert.deepEqual(billingMaintenanceVersionEndpoints(environment), {
    dashboard: environment.BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL,
    worker: environment.BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL,
  })
  for (const override of [
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'http://dashboard.example/api/version' },
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'https://127.0.0.1/api/version' },
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'https://bad..example/api/version' },
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'https://user@dashboard.example/api/version' },
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'https://dashboard.example/api/version?sha=a' },
    { BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL: 'https://dashboard.example/version' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'https://worker.example/v1/version' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'http://worker.example/version' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'http://127.0.0.1/version' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'http://127.0.0.1:80/version' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'http://localhost:43119/version' },
  ]) {
    assert.throws(
      () => billingMaintenanceVersionEndpoints({ ...environment, ...override }),
      /must use exact path|public endpoint must use HTTPS/,
    )
  }
  assert.throws(
    () => billingMaintenanceVersionEndpoints({
      ...environment,
      BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL:
        'https://same.example/api/version',
      BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL:
        'https://same.example/version',
    }),
    /must use distinct origins/,
  )
})

test('operator runbook uses authenticated SSH forwarding without publishing the worker', () => {
  const documentation = read('docs/STRIPE_BILLING.md')
  assert.match(documentation, /railway ssh config[\s\S]+--service <authoritative-billing-worker-service>/)
  assert.match(documentation, /--environment <staging-or-production>/)
  assert.match(documentation, /ssh -N[\s\S]+-L 127\.0\.0\.1:43119:127\.0\.0\.1:<worker-health-port>/)
  assert.match(documentation, /do not use `railway tcp-proxy`/)
  assert.match(documentation, /remove the generated `brevitas-billing-maintenance` alias/)
  assert.match(documentation, /self-reported deployment identity, not signed artifacts or cryptographic provenance/)
})
