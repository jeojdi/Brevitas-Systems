import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { billingMaintenanceResponse } from '../src/lib/billing/maintenance-gate.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

test('billing maintenance response fails closed with a bounded retry contract', async () => {
  for (const value of [undefined, '', 'false', 'FALSE', '1', 'yes', ' true ']) {
    const response = billingMaintenanceResponse(
      value === undefined ? {} : { BREVITAS_BILLING_ENABLED: value },
    )

    assert.ok(response instanceof Response)
    assert.equal(response.status, 503)
    assert.equal(response.headers.get('cache-control'), 'no-store')
    const retryAfter = Number(response.headers.get('retry-after'))
    assert.ok(Number.isSafeInteger(retryAfter) && retryAfter >= 1 && retryAfter <= 300)
    assert.deepEqual(await response.json(), { error: 'Billing is temporarily unavailable' })
  }

  assert.equal(
    billingMaintenanceResponse({ BREVITAS_BILLING_ENABLED: 'true' }),
    null,
  )
})

test('billing controls gate maintenance before request or external work', () => {
  const routes = [
    {
      name: 'checkout',
      source: read('src/app/api/billing/checkout/route.ts'),
      forbiddenBeforeGate: [
        'authenticatedBillingUser(',
        'authorizeActiveBillingCompany(',
        'billingIsConfigured(',
        'validateStripeCatalog(',
        'getBillingAccount(',
        'getStripe(',
        'captureServerEvent(',
      ],
    },
    {
      name: 'portal',
      source: read('src/app/api/billing/portal/route.ts'),
      forbiddenBeforeGate: [
        'authenticatedBillingUser(',
        'authorizeActiveBillingCompany(',
        'getBillingAccount(',
        'getStripe(',
        'captureServerEvent(',
      ],
    },
    {
      name: 'manual recovery',
      source: read('src/app/api/billing/sync/route.ts'),
      forbiddenBeforeGate: [
        'authenticatedBillingUser(',
        'authorizeActiveBillingCompany(',
        'consumeBillingRecoveryAttempt(',
        'billingConfig(',
        'request.headers',
        'request.text(',
        'manuallyResolveBillingLedgerEntry(',
      ],
    },
  ]

  for (const route of routes) {
    const handler = route.source.slice(route.source.indexOf('export async function POST'))
    const body = handler.slice(handler.indexOf('{') + 1)
    const gateIndex = body.indexOf('billingMaintenanceResponse()')
    const returnIndex = body.indexOf('if (maintenanceResponse) return maintenanceResponse;')

    assert.match(
      body,
      /^\s*const maintenanceResponse = billingMaintenanceResponse\(\);\s*if \(maintenanceResponse\) return maintenanceResponse;/,
      `${route.name} maintenance gate is not the first executable handler statement`,
    )
    assert.ok(gateIndex >= 0, `${route.name} maintenance gate is missing`)
    assert.ok(returnIndex > gateIndex, `${route.name} does not immediately return the gate response`)
    const beforeMaintenanceReturn = body.slice(0, returnIndex)
    assert.doesNotMatch(beforeMaintenanceReturn, /\brequest\b/, `${route.name} reads the request before maintenance returns`)
    for (const externalAction of route.forbiddenBeforeGate) {
      const actionIndex = body.indexOf(externalAction)
      assert.ok(actionIndex > returnIndex, `${route.name} reaches ${externalAction} before maintenance returns`)
    }
  }
})
