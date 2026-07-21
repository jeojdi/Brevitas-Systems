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
  'supabase/migrations/202607200001_stripe_webhook_durability.sql',
  'supabase/migrations/202607200002_waitlist_security.sql',
  'supabase/migrations/202607200003_billing_owner_attribution.sql',
  'supabase/migrations/202607200004_stripe_event_ordering.sql',
  'supabase/migrations/202607200005_initial_service_key.sql',
  'supabase/migrations/202607200006_company_billing_authorization.sql',
  'supabase/migrations/202607200007_billing_recovery_scope.sql',
  'supabase/migrations/202607200008_provider_credential_cleanup.sql',
  'supabase/migrations/202607200009_multitab_dashboard_sessions.sql',
  'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql',
  'supabase/migrations/202607200011_compliance_billing_isolation.sql',
  'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql',
  'supabase/migrations/202607200013_billing_control_rate_limits.sql',
  'supabase/migrations/202607200014_billing_checkout_session_reservations.sql',
  'supabase/migrations/202607200015_provider_outbound_ambiguity.sql',
  'supabase/migrations/202607200016_durable_onboarding.sql',
  'supabase/migrations/202607200017_billing_customer_owner_fencing.sql',
]

export const expectedUpgradeMigrationOrder = expectedFreshMigrationOrder.slice(12)

const atomicForwardMigrationPaths = expectedFreshMigrationOrder.slice(-17)

const expectedFrozenChecksumPaths = [
  'supabase/migrations/202607170007_compliance_workflows.sql',
  'scripts/dr/compliance-workflow-assertions.sql',
  'supabase/migrations/202607170009_key_listing_security.sql',
  'supabase/migrations/202607170010_device_delivery_idempotency.sql',
  'supabase/migrations/202607170011_active_memberships.sql',
  'supabase/migrations/202607170012_receipt_accounting_alignment.sql',
  'supabase/migrations/202607170013_active_company_selection.sql',
  'supabase/migrations/202607200001_stripe_webhook_durability.sql',
  'supabase/migrations/202607200002_waitlist_security.sql',
  'supabase/migrations/202607200003_billing_owner_attribution.sql',
  'supabase/migrations/202607200004_stripe_event_ordering.sql',
  'supabase/migrations/202607200005_initial_service_key.sql',
  'supabase/migrations/202607200006_company_billing_authorization.sql',
  'supabase/migrations/202607200007_billing_recovery_scope.sql',
  'supabase/migrations/202607200008_provider_credential_cleanup.sql',
  'supabase/migrations/202607200009_multitab_dashboard_sessions.sql',
  'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql',
  'supabase/migrations/202607200011_compliance_billing_isolation.sql',
  'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql',
  'supabase/migrations/202607200013_billing_control_rate_limits.sql',
  'supabase/migrations/202607200014_billing_checkout_session_reservations.sql',
  'supabase/migrations/202607200015_provider_outbound_ambiguity.sql',
  'supabase/migrations/202607200016_durable_onboarding.sql',
  'supabase/migrations/202607200017_billing_customer_owner_fencing.sql',
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

function verifyAtomicForwardMigrations() {
  const nonTransactionalSql = /\b(?:create|drop)\s+index\s+concurrently\b|\bvacuum\b|\breindex\b|\balter\s+type\b[\s\S]*\badd\s+value\b/i
  for (const path of atomicForwardMigrationPaths) {
    const migration = read(path)
    const executableLines = migration
      .split(/\r?\n/)
      .map((line) => line.trim().toLowerCase())
      .filter((line) => line && !line.startsWith('--'))
    const boundaries = executableLines.filter(
      (line) => line === 'begin;' || line === 'commit;',
    )
    if (executableLines[0] !== 'begin;' || executableLines.at(-1) !== 'commit;' ||
        JSON.stringify(boundaries) !== JSON.stringify(['begin;', 'commit;'])) {
      fail(`${path} must have one explicit transaction enclosing the complete migration body`)
    }
    if (nonTransactionalSql.test(migration)) {
      fail(`${path} contains SQL that cannot use the required per-file transaction`)
    }
  }
}

function verifyBillingIdentityRolloutContract() {
  const serviceKeyMigration = read(
    'supabase/migrations/202607200005_initial_service_key.sql',
  ).toLowerCase()
  for (const contract of [
    'client_upgrade_required',
    "'required_contract','initial_service_key'",
    'uuid,uuid,uuid,text,text,text[],timestamptz,text',
    'uuid,uuid,uuid,text,text,text[],text,text,timestamptz,text',
  ]) {
    if (!serviceKeyMigration.includes(contract)) {
      fail(`Initial service-key rollout compatibility misses ${contract}`)
    }
  }

  const gate = read('scripts/ci/billing-migration-maintenance-gate.mjs')
  const apply = read('scripts/ci/apply-billing-identity-migrations.sh')
  const deployedVersion = read('scripts/ci/verify-billing-maintenance-deployment.mjs')
  const offlineVersionFixture = read(
    'scripts/ci/billing-maintenance-version-fetch-fixture.mjs',
  )
  const expectedRollout = atomicForwardMigrationPaths.slice(3, 6)
  for (const path of expectedRollout) {
    if (!gate.includes(`'${path}'`)) fail(`Billing rollout gate misses ${path}`)
  }
  if (!gate.includes("BREVITAS_BILLING_ENABLED !== 'false'") ||
      !gate.includes("BREVITAS_BILLING_MIGRATION_PHASE !== 'api-worker-quiesced'") ||
      !gate.includes('BREVITAS_BILLING_MAINTENANCE_SHA') ||
      !gate.includes('billingMaintenanceVersionEndpoints(environment)')) {
    fail('Billing rollout gate does not require disabled, quiesced, SHA-bound maintenance')
  }
  if (!apply.includes('for migration in "${billing_identity_migrations[@]}"') ||
      !apply.includes('psql "${DATABASE_URL}" --no-psqlrc --set ON_ERROR_STOP=1 --file "${migration}"') ||
      apply.includes('--single-transaction')) {
    fail('Billing rollout must execute each explicitly transactional file with stop-on-error')
  }
  if (!apply.includes("then 'complete'") ||
      !apply.includes("then 'inconsistent-company-scoped'") ||
      !apply.includes('initial_state="$(read_billing_identity_state)"') ||
      !apply.includes('validated the company-scoped postcondition and skipped reapplication') ||
      apply.indexOf('initial_state="$(read_billing_identity_state)"') >
        apply.indexOf('for migration in "${billing_identity_migrations[@]}"')) {
    fail('Billing rollout does not validate and skip completed company-scoped state before replay')
  }
  const versionCommand = 'node scripts/ci/verify-billing-maintenance-deployment.mjs'
  if (!apply.includes(versionCommand) ||
      apply.indexOf(versionCommand) > apply.indexOf('psql "${DATABASE_URL}"') ||
      !deployedVersion.includes("redirect: 'manual'") ||
      !deployedVersion.includes('AbortSignal.timeout(timeoutMs)') ||
      !deployedVersion.includes("expectedSha, 'dashboard'") ||
      !deployedVersion.includes("expectedSha, 'worker'") ||
      !deployedVersion.includes("cryptographic_provenance: false") ||
      !deployedVersion.includes('new URL(dashboard).origin === new URL(worker).origin') ||
      !deployedVersion.includes("hostname === '127.0.0.1'") ||
      !deployedVersion.includes('port >= 1_024 && port <= 65_535')) {
    fail('Billing rollout does not verify bounded deployed dashboard/worker versions before PostgreSQL')
  }
  if (!offlineVersionFixture.includes("new Set(['127.0.0.1', '::1', 'localhost'])") ||
      !offlineVersionFixture.includes("BREVITAS_BILLING_MAINTENANCE_OFFLINE_LOOPBACK_TEST !== 'true'") ||
      !offlineVersionFixture.includes('expectedHost !== databaseHost')) {
    fail('Offline billing-version fixture is not restricted to explicit loopback migration tests')
  }

  const stripeOrderingMigration = read(
    'supabase/migrations/202607200004_stripe_event_ordering.sql',
  ).toLowerCase()
  for (const functionName of [
    'compare_and_set_stripe_subscription_snapshot',
    'compare_and_set_stripe_invoice_snapshot',
  ]) {
    const drop = stripeOrderingMigration.indexOf(`drop function if exists public.${functionName}(`)
    const create = stripeOrderingMigration.indexOf(`create function public.${functionName}(`)
    if (drop < 0 || create < 0 || drop > create) {
      fail(`Billing migration 200004 cannot transactionally rename ${functionName} inputs on rerun`)
    }
  }

  const webhook = read('src/app/api/billing/webhook/route.ts')
  if (!webhook.includes('if (!billingIsConfigured())') ||
      !webhook.includes("'Retry-After': '30'") ||
      webhook.indexOf('if (!billingIsConfigured())') > webhook.indexOf('await request.text()')) {
    fail('Stripe webhook does not fail closed before body processing during billing maintenance')
  }
}

function verifyStripeSnapshotCasContract() {
  for (const [path, identityColumn, identityParameter] of [
    ['supabase/migrations/202607200004_stripe_event_ordering.sql', 'user_id', 'p_user_id'],
    ['supabase/migrations/202607200006_company_billing_authorization.sql', 'organization_id', 'p_organization_id'],
  ]) {
    const migration = read(path).toLowerCase()
    if (/stripe_event_sequence|stripe_event_order_is_newer|apply_stripe_(?:subscription|invoice)_event|collate\s+"c"/.test(migration)) {
      fail(`${path} retains event-order authorization instead of authoritative snapshot CAS`)
    }
    for (const [domain, signature] of [
      ['subscription', 'uuid,bigint,bigint,text,text,text,text,timestamptz,timestamptz,timestamptz'],
      ['invoice', 'uuid,bigint,bigint,text,text,text,text'],
    ]) {
      const name = `compare_and_set_stripe_${domain}_snapshot`
      const start = migration.indexOf(`create function public.${name}(`) >= 0
        ? migration.indexOf(`create function public.${name}(`)
        : migration.indexOf(`create or replace function public.${name}(`)
      const end = migration.indexOf('\n$$;', start)
      if (start < 0 || end < 0) fail(`${path} misses ${name}`)
      const body = migration.slice(start, end).replace(/\s+/g, '')
      const where = body.slice(body.indexOf('whereaccount.'))
      if (!where.includes(`whereaccount.${identityColumn}=${identityParameter}`) ||
          !where.includes(`account.stripe_${domain}_reconcile_revision=p_expected_revision`) ||
          /p_event_(?:created|id|type)/.test(where.slice(0, where.indexOf('returning'))) ||
          !body.includes(`stripe_${domain}_reconcile_revision=account.stripe_${domain}_reconcile_revision+1`) ||
          !body.includes('returnv_revision;')) {
        fail(`${path} ${name} is not identity-and-revision-only compare-and-set`)
      }
      if (!migration.includes(`grant execute on function public.${name}(\n    ${signature}\n) to service_role;`) ||
          !migration.includes(`revoke all on function public.${name}(\n    ${signature}\n) from public, anon, authenticated;`)) {
        fail(`${path} ${name} is not service-role-only`)
      }
    }
  }
}

function verifyWebhookLeaseRenewalContract() {
  const migration = read(
    'supabase/migrations/202607200012_stripe_webhook_lease_renewal.sql',
  ).toLowerCase()
  const route = read('src/app/api/billing/webhook/route.ts')
  const inbox = read('src/lib/billing/webhook-inbox.mjs')
  const persistence = read('src/lib/billing/canonical-persistence.ts')
  for (const contract of [
    'renew_stripe_webhook_event_lease',
    'lease_owner = p_lease_owner',
    'lease_expires_at > renewal_time',
    'lease_expires_at > completion_time',
    'lease_expires_at > failure_time',
    'expired webhook lease was resurrected',
    'stale webhook owner completed a reclaimed event',
    'stale webhook owner failed a reclaimed event',
    'compare_and_set_stripe_subscription_snapshot_for_webhook',
    'compare_and_set_stripe_invoice_snapshot_for_webhook',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Stripe webhook lease-renewal migration misses ${contract}`)
    }
  }
  if (!route.includes("rpc('renew_stripe_webhook_event_lease'") ||
      (route.match(/await lease\.fence\(\)/g) || []).length !== 2 ||
      !route.includes('heartbeatIntervalMs: WEBHOOK_HEARTBEAT_INTERVAL_MS')) {
    fail('Stripe webhook runtime does not heartbeat and fence both canonical database writers')
  }
  if (!inbox.includes('await heartbeat.renewAndStop()') ||
      !inbox.includes('abortController.abort(lostError)') ||
      !inbox.includes('await fail(error)')) {
    fail('Stripe webhook inbox does not fail closed across renewal loss and cleanup')
  }
  if (!persistence.includes("'compare_and_set_stripe_subscription_snapshot_for_webhook'") ||
      !persistence.includes("'compare_and_set_stripe_invoice_snapshot_for_webhook'") ||
      !persistence.includes('...webhookLeaseParameters(lease, diagnostic)')) {
    fail('Canonical Stripe persistence bypasses the atomic webhook lease fence')
  }
}

function verifyBillingControlRateLimitContract() {
  const migration = read(
    'supabase/migrations/202607200013_billing_control_rate_limits.sql',
  ).toLowerCase()
  const helper = read('src/lib/billing/supabase.ts')
  const routes = [
    ['checkout', read('src/app/api/billing/checkout/route.ts')],
    ['portal', read('src/app/api/billing/portal/route.ts')],
  ]
  for (const contract of [
    'consume_billing_control_attempt',
    "v_operation not in ('checkout', 'portal')",
    'pg_advisory_xact_lock',
    'billing_control.global',
    'v_identity_hash',
    'v_global_limit integer := 120',
    'from public, anon, authenticated, service_role',
    'to service_role',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Billing control rate-limit migration misses ${contract}`)
    }
  }
  if (!helper.includes("rpc(\n      'consume_billing_control_attempt'") ||
      !helper.includes('parseBillingControlAdmission(data)') ||
      !helper.includes('throw new BillingControlAdmissionError()')) {
    fail('Billing control shared-admission helper is not fail closed')
  }
  for (const [operation, route] of routes) {
    const maintenance = route.indexOf('billingMaintenanceResponse()')
    const authentication = route.indexOf('authenticatedBillingUser(request)')
    const authorization = route.indexOf('authorizeActiveBillingCompany(user.id)')
    const admission = route.indexOf('consumeBillingControlAttempt(')
    if (maintenance < 0 || !(maintenance < authentication &&
        authentication < authorization && authorization < admission) ||
        route.indexOf(`'${operation}'`, admission) < admission ||
        /withRateLimit|RATE_LIMITS|x-forwarded-for|x-real-ip|x-client-ip/i.test(route)) {
      fail(`${operation} does not use verified shared admission in the required order`)
    }
  }
}

function verifyBillingCheckoutReservationContract() {
  const migration = read(
    'supabase/migrations/202607200014_billing_checkout_session_reservations.sql',
  ).toLowerCase()
  const route = read('src/app/api/billing/checkout/route.ts')
  const helper = read('src/lib/billing/checkout-reservation.mjs')
  for (const contract of [
    'billing_checkout_reservations',
    'reserve_billing_checkout_generation',
    'persist_billing_checkout_session',
    'advance_billing_checkout_generation',
    'release_billing_checkout_generation',
    "generation_started_at + interval '23 hours'",
    "v_mode := 'recover_only'",
    'v_reservation.generation <> p_generation',
    'v_reservation.reservation_token is distinct from p_reservation_token',
    'v_reservation.lease_expires_at <= v_now',
    'checkout_session_id <> p_checkout_session_id',
    'from public, anon, authenticated, service_role',
    'to service_role',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Billing Checkout reservation migration misses ${contract}`)
    }
  }
  if (route.includes('Date.now()') || route.includes('300_000') ||
      !route.includes('checkoutIdempotencyKey(organizationId, generation)') ||
      !route.includes("status: 'open'") || !route.includes('limit: 100') ||
      !route.includes('persistBillingCheckoutSession({') ||
      !route.includes('releaseBillingCheckoutGeneration({') ||
      !route.includes('returningCheckoutUrl && !released')) {
    fail('Billing Checkout route bypasses generation recovery or live-token fencing')
  }
  if (!helper.includes('openSubscriptionSessions.length > 1') ||
      !helper.includes('generationMetadata(matching) !== String(generation)') ||
      !helper.includes('page.has_more')) {
    fail('Billing Checkout open-session recovery is not bounded and ambiguity-safe')
  }
}

function verifyProviderOutboundFenceContract() {
  const migration = read(
    'supabase/migrations/202607200015_provider_outbound_ambiguity.sql',
  ).toLowerCase()
  for (const contract of [
    'ai_jobs_provider_outbound_identity_check',
    'validate constraint ai_jobs_provider_outbound_identity_check',
    'ai_jobs_provider_outbound_ambiguity_idx',
    'create or replace function public.mark_ai_job_provider_outbound_started',
    'and lease_owner = p_worker_id',
    "and status = 'running'",
    'and lease_expires_at > pg_catalog.now()',
    "and operation = 'chat'",
    'and provider_outbound_started_at is null',
    "last_error_code = 'provider_outcome_ambiguous'",
    'set search_path = pg_catalog, public, pg_temp',
    'from public, anon, authenticated, service_role',
    'to service_role',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Provider outbound fencing migration misses ${contract}`)
    }
  }
  const marker = migration.slice(
    migration.indexOf('create or replace function public.mark_ai_job_provider_outbound_started'),
    migration.indexOf('create or replace function public.claim_ai_job'),
  )
  if (!marker.includes('provider_outbound_attempt = attempts') ||
      !marker.includes('and cancel_requested = false') ||
      /grant execute[\s\S]*to (?:public|anon|authenticated)/.test(marker)) {
    fail('Provider outbound marker is not ownership-fenced and service-only')
  }
  const claim = migration.slice(
    migration.indexOf('create or replace function public.claim_ai_job'),
  )
  const terminalize = claim.indexOf("last_error_code = 'provider_outcome_ambiguous'")
  const selectCandidate = claim.indexOf('select id into selected_id')
  if (terminalize < 0 || selectCandidate < 0 || terminalize > selectCandidate ||
      !claim.includes('and provider_outbound_started_at is null')) {
    fail('Provider outbound ambiguity does not terminalize before candidate claim')
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

function verifyDurableOnboardingContract() {
  const migration = read(
    'supabase/migrations/202607200016_durable_onboarding.sql',
  ).toLowerCase()
  for (const contract of [
    'register_bvx_installation',
    'registration_key_hash',
    'registration_key_id',
    'device_auth_receipt_id',
    'installation.registration_key_hash = usage.key_hash',
    "credential.key_type = 'device'",
    "usage.authoritative is true",
    "usage.receipt_source = 'proxy'",
    "activation.action = 'device_key.activated'",
    'pg_advisory_xact_lock',
    'revoke all on function public.register_bvx_installation',
    'drop function if exists public.register_bvx_installation',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Durable onboarding migration misses ${contract}`)
    }
  }
  if ((migration.match(/set search_path = pg_catalog, public, pg_temp/g) || []).length < 3) {
    fail('Durable onboarding RPCs do not all use the hardened search path')
  }

  const assertions = read(
    'scripts/ci/migration-durable-onboarding-assertions.sql',
  ).toLowerCase()
  for (const contract of [
    'forged unbound installation became onboarding evidence',
    'sdk telemetry completed onboarding',
    'non-device authoritative receipt completed onboarding',
    'registration-key/usage-key mismatch completed onboarding',
    'cross-tenant actor completed onboarding',
    'valid receipt-bound bvx proxy request did not complete onboarding',
  ]) {
    if (!assertions.includes(contract)) {
      fail(`Durable onboarding assertions miss ${contract}`)
    }
  }
}

function verifyBillingCustomerOwnerFencingContract() {
  const migration = read(
    'supabase/migrations/202607200017_billing_customer_owner_fencing.sql',
  ).toLowerCase()
  for (const contract of [
    'create or replace function public.save_billing_customer_identity',
    'member.user_id = organization.billing_owner_id',
    "member.status = 'active'",
    'for update of organization, member',
    'v_billing_owner_id',
    'on conflict (organization_id) do update',
    'account.stripe_customer_id is null',
    'account.stripe_customer_id = excluded.stripe_customer_id',
    'from public, anon, authenticated, service_role',
    'to service_role',
  ]) {
    if (!migration.includes(contract)) {
      fail(`Billing customer owner fencing migration misses ${contract}`)
    }
  }
  if (/p_(?:billing_)?owner_id|p_user_id/.test(migration)) {
    fail('Billing customer persistence still accepts caller-owned attribution')
  }

  const route = read('src/app/api/billing/checkout/route.ts')
  const database = read('src/lib/billing/supabase.ts')
  if (!route.includes('saveBillingCustomerIdentity(organizationId, customerId)') ||
      route.includes('const billingOwnerId = authorization.billingOwnerId') ||
      !database.includes("'save_billing_customer_identity'") ||
      !database.includes('p_organization_id: organizationId') ||
      !database.includes('p_stripe_customer_id: stripeCustomerId')) {
    fail('Checkout bypasses database-derived billing-owner persistence')
  }

  const race = read('scripts/ci/run-billing-owner-transfer-race-test.sh')
  if (!race.includes('pg_advisory_lock(170017)') ||
      !race.includes("set lock_timeout='750ms'") ||
      !race.includes('canceling statement due to lock timeout')) {
    fail('Billing owner-transfer race fixture does not prove lock serialization')
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
  verifyAtomicForwardMigrations()
  verifyBillingIdentityRolloutContract()
  verifyStripeSnapshotCasContract()
  verifyWebhookLeaseRenewalContract()
  verifyBillingControlRateLimitContract()
  verifyBillingCheckoutReservationContract()
  verifyProviderOutboundFenceContract()
  verifyFrozenChecksums()
  checkDeploymentMigration()
  verifyDatabaseScalingRollback()
  verifyBillingRollbackAndSelfChecks()
  verifyCacheUpgradeAndRollback()
  verifyAdministrationContract()
  verifyDeviceAndMembershipContracts()
  verifyDurableOnboardingContract()
  verifyBillingCustomerOwnerFencingContract()
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
