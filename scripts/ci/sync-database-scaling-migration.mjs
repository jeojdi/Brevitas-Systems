import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

export const canonicalPath = resolve(root, 'api/migrations/004_database_scaling.sql')
export const deploymentPath = resolve(
  root,
  'supabase/migrations/202607170006_database_scaling.sql',
)

export function expectedDeploymentMigration(canonical = readFileSync(canonicalPath, 'utf8')) {
  const body = canonical.endsWith('\n') ? canonical : `${canonical}\n`
  const digest = createHash('sha256').update(body).digest('hex')
  const header = [
    '-- GENERATED DEPLOYMENT COPY. DO NOT EDIT BY HAND.',
    '-- Source: api/migrations/004_database_scaling.sql',
    `-- Source-SHA256: ${digest}`,
    '-- Release order: after 202607170005_company_administration.sql.',
    '',
  ].join('\n')
  return `${header}${body}`
}

export function checkDeploymentMigration() {
  const expected = expectedDeploymentMigration()
  let actual
  try {
    actual = readFileSync(deploymentPath, 'utf8')
  } catch (error) {
    if (error && error.code === 'ENOENT') {
      throw new Error('Missing generated migration 202607170006_database_scaling.sql')
    }
    throw error
  }
  if (actual !== expected) {
    throw new Error(
      'Database-scaling deployment migration drifted; run npm run release:migrations:sync',
    )
  }
}

function main() {
  if (process.argv.includes('--write')) {
    writeFileSync(deploymentPath, expectedDeploymentMigration(), { encoding: 'utf8', mode: 0o644 })
    return
  }
  if (process.argv.length > 2 && !process.argv.includes('--check')) {
    throw new Error('Usage: sync-database-scaling-migration.mjs [--check|--write]')
  }
  checkDeploymentMigration()
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main()
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
