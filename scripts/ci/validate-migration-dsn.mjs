import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const allowedHosts = new Set(['127.0.0.1', '::1', 'localhost'])

export function validateMigrationDsn(rawValue) {
  const raw = String(rawValue || '').trim()
  if (!raw) throw new Error('DATABASE_URL is required')
  let parsed
  try {
    parsed = new URL(raw)
  } catch {
    throw new Error('DATABASE_URL is not a valid PostgreSQL URI')
  }
  if (!['postgres:', 'postgresql:'].includes(parsed.protocol)) {
    throw new Error('DATABASE_URL must use postgres:// or postgresql://')
  }
  if (parsed.search || parsed.hash) {
    throw new Error('Migration DATABASE_URL may not contain query parameters or a fragment')
  }
  const hostname = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '')
  if (!allowedHosts.has(hostname)) {
    throw new Error('Migration DATABASE_URL hostname must be an explicit loopback target')
  }
  const database = decodeURIComponent(parsed.pathname.slice(1))
  if (!database || database.includes('/') || /[\r\n\0]/.test(database)) {
    throw new Error('Migration DATABASE_URL must name exactly one database')
  }
  return Object.freeze({ hostname, database, protocol: parsed.protocol })
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    validateMigrationDsn(process.env.DATABASE_URL)
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 2
  }
}
