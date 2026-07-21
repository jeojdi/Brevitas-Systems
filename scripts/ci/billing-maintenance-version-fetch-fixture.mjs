// Offline fetch injection for the ephemeral PostgreSQL migration job only.
// The production maintenance script does not reference or select this module.
const expectedDashboard = 'https://dashboard.example.invalid/api/version'
const expectedWorker = 'http://127.0.0.1:43119/version'
const loopbackHosts = new Set(['127.0.0.1', '::1', 'localhost'])

let databaseUrl
try {
  databaseUrl = new URL(String(process.env.DATABASE_URL || ''))
} catch {
  throw new Error('Offline billing-version fixture requires a valid loopback DATABASE_URL')
}
const databaseHost = databaseUrl.hostname.toLowerCase().replace(/^\[|\]$/g, '')
const expectedHost = String(
  process.env.BREVITAS_BILLING_MIGRATION_EXPECTED_HOST || '',
).trim().toLowerCase().replace(/^\[|\]$/g, '')
if (process.env.BREVITAS_BILLING_MAINTENANCE_OFFLINE_LOOPBACK_TEST !== 'true' ||
    !loopbackHosts.has(databaseHost) || expectedHost !== databaseHost ||
    process.env.BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL !== expectedDashboard ||
    process.env.BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL !== expectedWorker) {
  throw new Error('Offline billing-version fixture is restricted to the explicit loopback CI contract')
}

globalThis.fetch = async (url, options) => {
  if ((url !== expectedDashboard && url !== expectedWorker) ||
      options?.method !== 'GET' || options?.redirect !== 'manual' || options?.body !== undefined) {
    throw new Error('Offline billing-version fixture received an unauthorized request')
  }
  return Response.json({
    service: url === expectedDashboard ? 'dashboard' : 'worker',
    build: { commit_sha: process.env.BREVITAS_BILLING_MAINTENANCE_SHA },
  })
}
