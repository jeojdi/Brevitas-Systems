// Credentialed, read-only production schema-drift gate.
//
// Unlike scripts/ci/verify-migrations.mjs (a credential-free static check of the
// migration files themselves) this connects to a live database over DATABASE_URL
// and asserts that what is *deployed* matches what the migrations describe:
//
//   1. the applied-migration head recorded in public.brevitas_schema_migrations
//      equals the last entry of scripts/ci/migration-fresh-manifest.txt, and
//   2. the billing/usage money columns are the numeric(p,s) types the migrations
//      declare, not the double-precision floats a hand-assembled prod project
//      created (the drift documented in supabase/migrations/20260710_cloud_usage.sql
//      and MEMORY: "production-schema-drift" — migrations cannot self-repair it).
//
// The session is opened with default_transaction_read_only=on and only issues
// SELECTs, so it can never mutate the target. When DATABASE_URL is absent it
// no-ops with a clear message so credential-free runs are never blocked; it only
// exits non-zero on real drift.

import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { resolve, dirname, join } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const MANIFEST_PATH = join(HERE, 'migration-fresh-manifest.txt')
const LEDGER = 'public.brevitas_schema_migrations'

// Money columns and the numeric(precision, scale) the migrations declare.
// billing_events: created numeric(12,8) in 20260626_create_billing.sql, widened to
// numeric(18,10) by 202607270002_widen_billing_events_money.sql (this is the applied head).
// usage_log:      supabase/migrations/20260710_cloud_usage.sql
export const EXPECTED_MONEY_COLUMNS = Object.freeze([
  { table: 'billing_events', column: 'cost_saved_usd', precision: 18, scale: 10 },
  { table: 'billing_events', column: 'brevitas_fee_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'baseline_cost_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'actual_cost_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'measured_savings_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'verified_savings_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'cost_saved_usd', precision: 18, scale: 10 },
  { table: 'usage_log', column: 'brevitas_fee_usd', precision: 18, scale: 10 },
])

export function parseManifest(text) {
  return String(text)
    .split('\n')
    .map(line => line.trim())
    .filter(line => line.length > 0 && !line.startsWith('#'))
}

export function manifestHead(manifestPaths) {
  if (!Array.isArray(manifestPaths) || manifestPaths.length === 0) {
    throw new Error('Fresh migration manifest is empty; cannot determine expected head')
  }
  return manifestPaths[manifestPaths.length - 1]
}

// The manifest — not lexical or filesystem order — is authoritative, so the
// applied head is the latest manifest entry that is recorded as applied.
export function appliedHead(manifestPaths, appliedPaths) {
  const applied = appliedPaths instanceof Set ? appliedPaths : new Set(appliedPaths)
  for (let i = manifestPaths.length - 1; i >= 0; i -= 1) {
    if (applied.has(manifestPaths[i])) return manifestPaths[i]
  }
  return null
}

export function assertHead(manifestPaths, appliedPaths, logger = console) {
  const expected = manifestHead(manifestPaths)
  const head = appliedHead(manifestPaths, appliedPaths)
  if (head === null) {
    throw new Error(
      `No fresh-manifest migration is recorded in ${LEDGER}; the database is unmigrated or drifted`,
    )
  }
  if (head !== expected) {
    throw new Error(
      `Applied migration head drift: ${LEDGER} head is "${head}" but the fresh manifest ends at "${expected}". ` +
        'The deployed schema is behind (or ahead of) the release manifest.',
    )
  }
  // Applied paths outside the manifest are unexpected but not head drift; surface
  // them without failing so a benign historical ledger row cannot block a release.
  const manifestSet = new Set(manifestPaths)
  const applied = appliedPaths instanceof Set ? [...appliedPaths] : [...new Set(appliedPaths)]
  const extras = applied.filter(path => !manifestSet.has(path))
  if (extras.length > 0) {
    logger.warn?.(
      `note: ${extras.length} applied migration(s) are not in the fresh manifest: ${extras.join(', ')}`,
    )
  }
  return expected
}

export function assertMoneyColumns(rows, expected = EXPECTED_MONEY_COLUMNS) {
  const byKey = new Map()
  for (const row of rows) {
    byKey.set(`${row.table_name}.${row.column_name}`, row)
  }
  const problems = []
  for (const spec of expected) {
    const key = `${spec.table}.${spec.column}`
    const row = byKey.get(key)
    if (!row) {
      problems.push(`${key}: expected numeric(${spec.precision},${spec.scale}) but the column is missing`)
      continue
    }
    if (row.data_type !== 'numeric') {
      // double precision / real here is the known float drift the migrations
      // intend to coerce away but cannot repair once prod data exists.
      problems.push(
        `${key}: expected numeric(${spec.precision},${spec.scale}) but found ${row.data_type} — ` +
          'this is the known money-column float drift',
      )
      continue
    }
    const precision = Number(row.numeric_precision)
    const scale = Number(row.numeric_scale)
    if (precision !== spec.precision || scale !== spec.scale) {
      problems.push(
        `${key}: expected numeric(${spec.precision},${spec.scale}) but found numeric(${row.numeric_precision},${row.numeric_scale})`,
      )
    }
  }
  if (problems.length > 0) {
    throw new Error(`Money-column type drift detected:\n  - ${problems.join('\n  - ')}`)
  }
}

function moneyColumnsQuery(expected = EXPECTED_MONEY_COLUMNS) {
  const tuples = expected
    .map(spec => `('${spec.table}','${spec.column}')`)
    .join(', ')
  return (
    'select table_name, column_name, data_type, ' +
    "coalesce(numeric_precision::text,''), coalesce(numeric_scale::text,'') " +
    'from information_schema.columns ' +
    "where table_schema = 'public' " +
    `and (table_name, column_name) in (${tuples}) ` +
    'order by table_name, column_name'
  )
}

// Field separator: ASCII unit separator, never present in identifiers or types.
const FIELD_SEP = '\x1f'

function psqlQuery(databaseUrl, sql) {
  let stdout
  try {
    stdout = execFileSync(
      'psql',
      [databaseUrl, '-v', 'ON_ERROR_STOP=1', '-qtAX', '-F', FIELD_SEP, '-c', sql],
      {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        // Enforce a read-only session for the whole connection, independent of
        // the fact that this script only issues SELECTs.
        env: { ...process.env, PGOPTIONS: '-c default_transaction_read_only=on' },
      },
    )
  } catch (error) {
    const detail = error?.stderr
      ? String(error.stderr).trim()
      : error instanceof Error
        ? error.message
        : String(error)
    throw new Error(`psql query failed: ${detail}`)
  }
  return stdout
    .split('\n')
    .filter(line => line.length > 0)
    .map(line => line.split(FIELD_SEP))
}

export function runSchemaDriftCheck({
  env = process.env,
  manifestText = readFileSync(MANIFEST_PATH, 'utf8'),
  query,
  logger = console,
} = {}) {
  const databaseUrl = String(env.DATABASE_URL || '').trim()
  if (!databaseUrl) {
    logger.log?.(
      'schema-drift: DATABASE_URL is not set; skipping the credentialed read-only drift check.',
    )
    return { skipped: true }
  }

  const runQuery = query || (sql => psqlQuery(databaseUrl, sql))
  const manifestPaths = parseManifest(manifestText)

  const appliedRows = runQuery(`select path from ${LEDGER} order by path`)
  const appliedPaths = new Set(appliedRows.map(row => row[0]))
  const head = assertHead(manifestPaths, appliedPaths, logger)

  const moneyRows = runQuery(moneyColumnsQuery()).map(row => ({
    table_name: row[0],
    column_name: row[1],
    data_type: row[2],
    numeric_precision: row[3],
    numeric_scale: row[4],
  }))
  assertMoneyColumns(moneyRows)

  logger.log?.(
    `schema-drift: OK — applied head "${head}" matches the fresh manifest and all ` +
      'billing/usage money columns are the declared numeric types.',
  )
  return { skipped: false, head }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    runSchemaDriftCheck()
  } catch (error) {
    console.error(`schema-drift check failed: ${error instanceof Error ? error.message : String(error)}`)
    process.exitCode = 1
  }
}
