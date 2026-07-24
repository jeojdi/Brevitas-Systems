import { isIP } from 'node:net'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

import { billingMaintenanceVersionEndpoints } from './verify-billing-maintenance-deployment.mjs'

const FULL_SHA = /^[0-9a-f]{40}(?:[0-9a-f]{24})?$/
const DATABASE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}$/
const HOSTNAME = /^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$/

export const BILLING_IDENTITY_MIGRATIONS = Object.freeze([
  'supabase/migrations/202607200004_stripe_event_ordering.sql',
  'supabase/migrations/202607200005_initial_service_key.sql',
  'supabase/migrations/202607200006_company_billing_authorization.sql',
])

export function assertBillingMigrationMaintenance(environment = process.env) {
  if (environment.BREVITAS_BILLING_ENABLED !== 'false') {
    throw new Error('BREVITAS_BILLING_ENABLED must be exactly false during migrations 200004-200006')
  }
  if (environment.BREVITAS_BILLING_MIGRATION_PHASE !== 'api-worker-quiesced') {
    throw new Error(
      'BREVITAS_BILLING_MIGRATION_PHASE must be api-worker-quiesced during migrations 200004-200006',
    )
  }
  const maintenanceSha = String(
    environment.BREVITAS_BILLING_MAINTENANCE_SHA || '',
  ).trim().toLowerCase()
  if (!FULL_SHA.test(maintenanceSha)) {
    throw new Error('BREVITAS_BILLING_MAINTENANCE_SHA must be a full deployed maintenance commit SHA')
  }

  const expectedHost = String(
    environment.BREVITAS_BILLING_MIGRATION_EXPECTED_HOST || '',
  ).trim().toLowerCase().replace(/^\[|\]$/g, '')
  const expectedDatabase = String(
    environment.BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE || '',
  ).trim()
  if ((!HOSTNAME.test(expectedHost) && isIP(expectedHost) === 0) ||
      !DATABASE_NAME.test(expectedDatabase)) {
    throw new Error('Expected billing migration host and database must be explicit safe identifiers')
  }

  let parsed
  try {
    parsed = new URL(String(environment.DATABASE_URL || '').trim())
  } catch {
    throw new Error('DATABASE_URL must be a valid PostgreSQL URI')
  }
  if (!['postgres:', 'postgresql:'].includes(parsed.protocol) || parsed.hash ||
      !parsed.username || !parsed.password) {
    throw new Error('DATABASE_URL must be an authenticated PostgreSQL URI without a fragment')
  }
  const actualHost = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '')
  let actualDatabase
  try {
    actualDatabase = decodeURIComponent(parsed.pathname.slice(1))
  } catch {
    throw new Error('DATABASE_URL database name is not valid percent-encoding')
  }
  if (actualHost !== expectedHost || actualDatabase !== expectedDatabase) {
    throw new Error('DATABASE_URL does not match the explicitly approved host and database')
  }
  for (const [name, value] of parsed.searchParams) {
    if (name !== 'sslmode' || !['require', 'verify-ca', 'verify-full'].includes(value)) {
      throw new Error('DATABASE_URL contains an unsupported billing-migration connection option')
    }
  }
  const versionEndpoints = billingMaintenanceVersionEndpoints(environment)
  return Object.freeze({
    host: actualHost,
    database: actualDatabase,
    maintenanceSha,
    versionEndpoints,
    migrations: BILLING_IDENTITY_MIGRATIONS,
  })
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const contract = assertBillingMigrationMaintenance()
    console.log(
      `Billing migration maintenance gate passed for ${contract.host}/${contract.database} at ${contract.maintenanceSha}`,
    )
  } catch (error) {
    console.error(
      `Billing migration maintenance gate failed: ${error instanceof Error ? error.message : String(error)}`,
    )
    process.exitCode = 2
  }
}
