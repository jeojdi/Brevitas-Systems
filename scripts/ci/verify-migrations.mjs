import { readFileSync, readdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

import { checkDeploymentMigration } from './sync-database-scaling-migration.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const read = (path) => readFileSync(resolve(root, path), 'utf8')
const fail = (message) => { throw new Error(message) }

export const expectedFreshMigrationOrder = [
  'supabase/migrations/20260611_create_user_keys.sql',
  'supabase/migrations/20260624_create_profiles.sql',
  'supabase/migrations/20260626_create_billing.sql',
  'supabase/migrations/20260627_add_tracking_labels.sql',
  'supabase/migrations/20260710_cloud_usage.sql',
  'supabase/migrations/20260711_bvx_device_auth.sql',
  'supabase/migrations/20260714_legal_acceptances.sql',
  'supabase/migrations/20260715_analytics_privacy.sql',
  'supabase/migrations/20260716_bvx_repository_registry.sql',
  'supabase/migrations/20260716_posthog_warehouse_view.sql',
  'supabase/migrations/20260716_stripe_billing.sql',
  'supabase/migrations/20260716_stripe_billing_rate_25pct.sql',
  'supabase/migrations/202607170001_enterprise_tenancy.sql',
  'supabase/migrations/202607170002_cache_security.sql',
  'supabase/migrations/202607170003_durable_jobs.sql',
  'supabase/migrations/202607170004_billing_recovery.sql',
  'supabase/migrations/202607170005_company_administration.sql',
  'supabase/migrations/202607170006_database_scaling.sql',
  'supabase/migrations/202607170007_compliance_workflows.sql',
  'supabase/migrations/202607170008_atomic_key_audit.sql',
  'supabase/migrations/202607170009_key_listing_security.sql',
  'supabase/migrations/202607170010_device_delivery_idempotency.sql',
  'supabase/migrations/202607170011_active_memberships.sql',
  'supabase/migrations/202607170012_receipt_accounting_alignment.sql',
  'supabase/migrations/202607170013_active_company_selection.sql',
]

export const expectedUpgradeMigrationOrder = expectedFreshMigrationOrder.slice(12)

const expectedFrozenChecksumPaths = [
  'supabase/migrations/202607170007_compliance_workflows.sql',
  'scripts/dr/compliance-workflow-assertions.sql',
  'supabase/migrations/202607170009_key_listing_security.sql',
  'supabase/migrations/202607170010_device_delivery_idempotency.sql',
  'supabase/migrations/202607170011_active_memberships.sql',
  'supabase/migrations/202607170012_receipt_accounting_alignment.sql',
  'supabase/migrations/202607170013_active_company_selection.sql',
]

function manifestEntries(path) {
  return read(path)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
}

function verifyManifest() {
  const manifests = [
    ['scripts/ci/migration-fresh-manifest.txt', expectedFreshMigrationOrder],
    ['scripts/ci/migration-upgrade-manifest.txt', expectedUpgradeMigrationOrder],
  ]
  for (const [path, expected] of manifests) {
    const actual = manifestEntries(path)
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
      fail(`${path} differs from the release contract:\n${actual.join('\n')}`)
    }
    if (new Set(actual).size !== actual.length) fail(`${path} contains duplicates`)
    if (actual.some((entry) => !entry.startsWith('supabase/migrations/')
        || /rollback|api\/migrations/i.test(entry))) {
      fail(`${path} contains a non-forward or non-deployable migration`)
    }
    for (const migrationPath of actual) read(migrationPath)
  }

  const inventory = readdirSync(resolve(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .sort()
    .map((name) => `supabase/migrations/${name}`)
  if (JSON.stringify(inventory) !== JSON.stringify(expectedFreshMigrationOrder)) {
    fail(`Fresh manifest must cover every forward Supabase migration exactly once:\n${inventory.join('\n')}`)
  }
  const enterprise = inventory
    .map((path) => path.slice('supabase/migrations/'.length))
    .filter((name) => /^2026071700(?:0[1-9]|1[0-3])_.+\.sql$/.test(name))
  const timestamps = enterprise.map((name) => name.slice(0, 12))
  const expected = Array.from(
    { length: 13 },
    (_, index) => `20260717${String(index + 1).padStart(4, '0')}`,
  )
  if (JSON.stringify(timestamps) !== JSON.stringify(expected)) {
      fail(`Enterprise migration timestamps must be unique and contiguous: ${enterprise.join(', ')}`)
  }
  const suffix = expectedFreshMigrationOrder.slice(-expectedUpgradeMigrationOrder.length)
  if (JSON.stringify(suffix) !== JSON.stringify(expectedUpgradeMigrationOrder)) {
    fail('Upgrade manifest must be the exact suffix after the known production baseline')
  }
}

function verifyFrozenChecksums() {
  const entries = read('scripts/ci/migration-frozen-checksums.txt')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
    .map((line) => {
      const match = line.match(/^([0-9a-f]{64})\s{2}(.+)$/)
      if (!match) fail(`Invalid frozen migration checksum entry: ${line}`)
      return { digest: match[1], path: match[2] }
    })
  if (JSON.stringify(entries.map(({ path }) => path)) !==
      JSON.stringify(expectedFrozenChecksumPaths)) {
    fail('Frozen migration checksum inventory is incomplete or reordered')
  }
  for (const { digest, path } of entries) {
    if (!expectedUpgradeMigrationOrder.includes(path)
        && path !== 'scripts/dr/compliance-workflow-assertions.sql') {
      fail(`Frozen checksum is outside the enterprise upgrade chain: ${path}`)
    }
    const actual = createHash('sha256').update(read(path)).digest('hex')
    if (actual !== digest) fail(`Frozen migration checksum drift: ${path}`)
  }
}

function verifyDatabaseScalingRollback() {
  const migration = read('api/migrations/004_database_scaling.sql').toLowerCase()
  const concurrent = read('api/migrations/004_database_scaling.concurrent_indexes.sql').toLowerCase()
  const rollback = read('api/migrations/004_database_scaling.rollback.sql').toLowerCase()
  const functions = [
    'usage_page',
    'usage_stats',
    'usage_breakdown',
    'usage_grouped',
    'admin_usage_report',
    'admin_key_repository_usage',
    'admin_usage_report_page',
  ]
  const indexes = [
    'usage_log_org_page_idx',
    'usage_log_owner_page_idx',
    'usage_log_key_page_idx',
    'usage_log_org_customer_page_idx',
    'usage_log_org_pipeline_idx',
    'usage_log_org_agent_idx',
    'usage_log_org_run_idx',
    'usage_log_admin_project_idx',
    'usage_log_admin_client_idx',
    'usage_log_admin_provider_idx',
    'usage_log_admin_model_idx',
  ]
  for (const name of functions) {
    if (!migration.includes(`function public.${name}`)) fail(`Scaling migration misses ${name}`)
    if (!rollback.includes(`function if exists public.${name}`)) fail(`Scaling rollback misses ${name}`)
  }
  for (const name of indexes) {
    if (!concurrent.includes(`if not exists ${name}`)) fail(`Concurrent index migration misses ${name}`)
    if (!rollback.includes(`if exists public.${name}`)) fail(`Scaling rollback misses ${name}`)
  }
  if (/\b(drop table|truncate|delete from|update|insert into)\b/.test(rollback)) {
    fail('Scaling rollback must never mutate or remove authoritative records')
  }
  if (!rollback.includes('drop index concurrently')) {
    fail('Scaling rollback must preserve non-blocking index removal')
  }
}

function verifyBillingRollbackAndSelfChecks() {
  const billing = read('supabase/migrations/202607170004_billing_recovery.sql').toLowerCase()
  for (const contract of [
    'rollback procedure',
    'prevent_billing_ledger_delete',
    'prevent_billing_ledger_identity_change',
    "p_anchor_end - p_anchor_start <> interval '7 days'",
    "week_offset * interval '7 days'",
    'end boundary must enter next period',
    'utc period changed across dst',
    'for update skip locked',
    'lease_expires_at',
  ]) {
    if (!billing.includes(contract)) fail(`Billing recovery migration misses ${contract}`)
  }
  const rollback = billing.slice(billing.indexOf('-- rollback procedure'))
  if (/\b(drop table|truncate|delete from public\.billing_ledger)\b/.test(rollback)) {
    fail('Billing rollback documentation may not delete financial records')
  }
}

function verifyCacheUpgradeAndRollback() {
  const compatibility = read('api/migrations/002_semantic_cache.sql').toLowerCase()
  const cache = read('supabase/migrations/202607170002_cache_security.sql').toLowerCase()
  const rollback = read('scripts/ci/migration-cache-rollback.sql').toLowerCase()
  for (const contract of [
    'pg_advisory_xact_lock',
    'clock_timestamp()',
    'semantic_cache_no_plaintext',
    'revoke insert, update on table public.semantic_cache from service_role',
    'set search_path = pg_catalog, public, extensions',
    'drop function if exists public.semantic_cache_lookup(vector, text, float)',
    'p_tenant_namespace',
    'p_model_id',
  ]) {
    if (!cache.includes(contract)) fail(`Cache security migration misses ${contract}`)
  }
  if (!compatibility.includes('deprecated compatibility guard')
      || !compatibility.includes('drop function if exists public.semantic_cache_lookup')) {
    fail('Legacy cache migration is not a safe compatibility/signature guard')
  }
  for (const contract of [
    'semantic_cache_store_bounded',
    'semantic_cache_lookup',
    'semantic_cache_absolute_bound',
    'semantic_cache_normalize_write',
    'semantic_cache_no_plaintext',
  ]) {
    if (!rollback.includes(contract)) fail(`Cache rollback misses ${contract}`)
  }
  if (/\b(drop table|truncate|delete from)\b/.test(rollback)) {
    fail('Cache rollback may not remove encrypted cache records')
  }
}

function verifyAdministrationContract() {
  const administration = read(
    'supabase/migrations/202607170005_company_administration.sql',
  ).toLowerCase()
  for (const contract of [
    'organization_members',
    'service_accounts',
    'audit_events',
    'immutable',
    'service_role',
    'revoke',
  ]) {
    if (!administration.includes(contract)) fail(`Administration migration misses ${contract}`)
  }
  if (/drop table\s+(?:if exists\s+)?public\.audit_events/.test(administration)) {
    fail('Administration migration may not discard the existing audit history')
  }
}

function verifyDeviceAndMembershipContracts() {
  const device = read(
    'supabase/migrations/202607170010_device_delivery_idempotency.sql',
  ).toLowerCase()
  for (const contract of [
    'bvx_device_consumption_receipts',
    'approver_id uuid references auth.users',
    "set encrypted_key='',quarantined_at=now()",
    'pg_advisory_xact_lock',
    'company_selection_required',
    'company_access_denied',
    'company_admin',
    'device_key.activated',
    'receipt_invalid',
    'digest_mismatch',
    'revoke all on function public.consume_bvx_device(text)',
  ]) {
    if (!device.includes(contract)) fail(`Device delivery migration misses ${contract}`)
  }
  if ((device.match(/set search_path = pg_catalog, public, pg_temp/g) || []).length < 4) {
    fail('Device delivery RPCs do not all use the hardened search path')
  }

  const memberships = read(
    'supabase/migrations/202607170011_active_memberships.sql',
  ).toLowerCase()
  for (const contract of [
    'company_admin_active_memberships',
    'lock_company_actor_role',
    "member.status='active'",
    'member.user_id=p_actor_user_id',
    'limit 100',
    'for share of member',
    'organization_members_actor_active_idx',
    'from public, anon, authenticated, service_role',
  ]) {
    if (!memberships.includes(contract)) fail(`Active membership migration misses ${contract}`)
  }

  const selection = read(
    'supabase/migrations/202607170013_active_company_selection.sql',
  ).toLowerCase()
  for (const contract of [
    'active_company_selections',
    'company_admin_resolve_active_membership',
    'company_admin_select_active_membership',
    'p_requested_organization_id',
    "member.status = 'active'",
    'member.user_id = p_actor_user_id',
    'pg_advisory_xact_lock',
    'for update',
    'enable row level security',
    'from public, anon, authenticated, service_role',
  ]) {
    if (!selection.includes(contract)) fail(`Active company selection misses ${contract}`)
  }
}

function verifyReceiptAccountingAlignment() {
  const canonical = read('api/migrations/003_receipt_accounting.sql').toLowerCase()
  const deployment = read(
    'supabase/migrations/202607170012_receipt_accounting_alignment.sql',
  ).toLowerCase()
  for (const [column, type] of [
    ['cache_write_5m_tokens', 'bigint not null default 0'],
    ['cache_write_1h_tokens', 'bigint not null default 0'],
    ['cache_attributable', 'boolean not null default false'],
  ]) {
    const definition = `add column if not exists ${column} ${type}`
    if (!canonical.includes(definition)) fail(`Canonical receipt migration misses ${column}`)
    if (!deployment.includes(definition)) fail(`Supabase receipt migration misses ${column}`)
    if (!canonical.includes(`comment on column public.usage_log.${column}`)
        || !deployment.includes(`comment on column public.usage_log.${column}`)) {
      fail(`Receipt migration comment drift for ${column}`)
    }
  }
  for (const contract of [
    'usage_log_receipt_cache_tiers_check',
    'cache_write_tokens >= 0',
    'queue_brevitas_fee_after_usage',
    'validate constraint usage_log_receipt_cache_tiers_check',
    'revoke all on table public.usage_log from public, anon, authenticated, service_role',
    'grant select, insert on table public.usage_log to service_role',
    'grant usage, select on sequence public.usage_log_id_seq to service_role',
    'evidence-preserving rollback procedure',
  ]) {
    if (!deployment.includes(contract)) fail(`Receipt alignment migration misses ${contract}`)
  }
  const rollback = read('scripts/ci/migration-receipt-accounting-rollback.sql').toLowerCase()
  if (!rollback.includes('drop constraint if exists usage_log_receipt_cache_tiers_check')) {
    fail('Receipt rollback does not remove only its validation layer')
  }
  if (/\b(drop table|drop column|truncate|delete from|update|insert into)\b/.test(rollback)) {
    fail('Receipt rollback may not mutate or remove persisted usage/billing evidence')
  }
}

function verifyHashedLocks() {
  for (const path of [
    'scripts/ci/python-runtime.lock',
    'scripts/ci/python-test.lock',
    'scripts/ci/python-compressor.lock',
    'scripts/ci/python-audit.lock',
    'scripts/ci/python-sast.lock',
  ]) {
    const lock = read(path)
    if (!lock.includes('--hash=sha256:')) fail(`${path} is not hash locked`)
    for (const line of lock.split(/\r?\n/)) {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('#') || trimmed.startsWith('--hash=')) continue
      if (/^[a-z0-9][a-z0-9._-]*[<>=!~]/i.test(trimmed) && !trimmed.includes('==')) {
        fail(`${path} contains an unpinned requirement: ${trimmed}`)
      }
    }
  }
}

export function verifyMigrations() {
  verifyManifest()
  verifyFrozenChecksums()
  checkDeploymentMigration()
  verifyDatabaseScalingRollback()
  verifyBillingRollbackAndSelfChecks()
  verifyCacheUpgradeAndRollback()
  verifyAdministrationContract()
  verifyDeviceAndMembershipContracts()
  verifyReceiptAccountingAlignment()
  verifyHashedLocks()
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    verifyMigrations()
    console.log('Migration order, drift, rollback, and lock contracts verified.')
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error))
    process.exitCode = 1
  }
}
