import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { runStagingSmoke, assertStagingTarget } from '../scripts/ci/staging-smoke.mjs'
import { validateMigrationDsn } from '../scripts/ci/validate-migration-dsn.mjs'
import {
  BILLING_IDENTITY_MIGRATIONS,
  assertBillingMigrationMaintenance,
} from '../scripts/ci/billing-migration-maintenance-gate.mjs'
import {
  expectedFreshMigrationOrder,
  expectedUpgradeMigrationOrder,
  verifyMigrations,
} from '../scripts/ci/verify-migrations.mjs'

const root = resolve(import.meta.dirname, '..')
const read = (path) => readFileSync(resolve(root, path), 'utf8')

function runBillingMaintenanceWithFakePostgres(t, stateMode) {
  const directory = mkdtempSync(join(tmpdir(), 'brevitas-billing-maintenance-'))
  const fakePsql = join(directory, 'psql')
  const fakeNode = join(directory, 'node')
  const appliedLog = join(directory, 'applied.log')
  const commandLog = join(directory, 'commands.log')
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  writeFileSync(fakeNode, [
    '#!/usr/bin/env bash',
    'set -eu',
    'if [[ "${1:-}" == scripts/ci/verify-billing-maintenance-deployment.mjs ]]; then',
    "  printf '%s\\n' version >>\"${FAKE_COMMAND_LOG}\"",
    '  if [[ "${FAKE_BILLING_STATE_MODE}" == version-failure ]]; then',
    "    printf '%s\\n' 'simulated deployed-version rejection' >&2",
    '    exit 2',
    '  fi',
    '  exit 0',
    'fi',
    'exec "${FAKE_REAL_NODE}" "$@"',
  ].join('\n'))
  chmodSync(fakeNode, 0o700)
  writeFileSync(fakePsql, [
    '#!/usr/bin/env bash',
    'set -eu',
    "printf '%s\\n' psql >>\"${FAKE_COMMAND_LOG}\"",
    'arguments=" $* "',
    'if [[ "${arguments}" == *"select current_database()"* ]]; then',
    "  printf '%s\\n' 'brevitas_ci'",
    'elif [[ "${arguments}" == *"claim_stripe_webhook_event"* ]]; then',
    "  printf '%s\\n' 't'",
    'elif [[ "${arguments}" == *"select case"* && "${arguments}" == *"company_billing_authorize_actor"* ]]; then',
    '  if [[ "${FAKE_BILLING_STATE_MODE}" == complete ]]; then',
    "    printf '%s\\n' 'complete'",
    '  elif [[ "${FAKE_BILLING_STATE_MODE}" == inconsistent ]]; then',
    "    printf '%s\\n' 'inconsistent-company-scoped'",
    '  elif [[ -s "${FAKE_PSQL_LOG}" ]]; then',
    "    printf '%s\\n' 'complete'",
    '  else',
    "    printf '%s\\n' 'pending'",
    '  fi',
    'elif [[ "${arguments}" == *" --file "* ]]; then',
    '  previous=""',
    '  for argument in "$@"; do',
    '    if [[ "${previous}" == --file ]]; then',
    "      printf '%s\\n' \"${argument}\" >>\"${FAKE_PSQL_LOG}\"",
    '      exit 0',
    '    fi',
    '    previous="${argument}"',
    '  done',
    '  exit 91',
    'else',
    "  printf '%s\\n' 'unexpected fake psql invocation' >&2",
    '  exit 90',
    'fi',
  ].join('\n'))
  chmodSync(fakePsql, 0o700)

  const result = spawnSync('bash', ['scripts/ci/apply-billing-identity-migrations.sh'], {
    cwd: root,
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: `${directory}:${process.env.PATH}`,
      DATABASE_URL: 'postgresql://operator:secret@127.0.0.1:5432/brevitas_ci',
      BREVITAS_BILLING_ENABLED: 'false',
      BREVITAS_BILLING_MIGRATION_PHASE: 'api-worker-quiesced',
      BREVITAS_BILLING_MAINTENANCE_SHA: 'a'.repeat(40),
      BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL:
        'https://dashboard.example.invalid/api/version',
      BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL:
        'http://127.0.0.1:43119/version',
      BREVITAS_BILLING_MIGRATION_EXPECTED_HOST: '127.0.0.1',
      BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE: 'brevitas_ci',
      FAKE_BILLING_STATE_MODE: stateMode,
      FAKE_PSQL_LOG: appliedLog,
      FAKE_COMMAND_LOG: commandLog,
      FAKE_REAL_NODE: process.execPath,
    },
  })
  const applied = existsSync(appliedLog)
    ? readFileSync(appliedLog, 'utf8').trim().split(/\r?\n/).filter(Boolean)
    : []
  const commands = existsSync(commandLog)
    ? readFileSync(commandLog, 'utf8').trim().split(/\r?\n/).filter(Boolean)
    : []
  return { ...result, applied, commands }
}

function workflowFiles() {
  const directory = resolve(root, '.github/workflows')
  return readdirSync(directory)
    .filter((name) => /\.ya?ml$/.test(name))
    .map((name) => join(directory, name))
}

test('every third-party action is immutable and workflows use least privilege', () => {
  const shaRef = /^[^\s@]+@[0-9a-f]{40}(?:\s+#.+)?$/
  for (const path of workflowFiles()) {
    const workflow = readFileSync(path, 'utf8')
    const uses = [...workflow.matchAll(/^\s*uses:\s*(.+)$/gm)].map((match) => match[1].trim())
    assert.ok(uses.length > 0, `${path} has no pinned actions`)
    for (const action of uses) assert.match(action, shaRef, `${path}: ${action}`)
    assert.match(workflow, /^permissions:\n\s+contents:\s+read\s*$/m, path)
    assert.doesNotMatch(workflow, /continue-on-error|\|\|\s*true/i, path)
    for (const checkout of workflow.matchAll(/uses:\s*actions\/checkout@[\s\S]+?(?=\n\s*- name:|\n\s*uses:|\n\S|$)/g)) {
      assert.match(checkout[0], /persist-credentials:\s*false/, path)
    }
  }
})

test('dependency, SAST, secret, and image scans are blocking at defined severity', () => {
  const workflow = read('.github/workflows/security.yml')
  assert.match(workflow, /npm audit --audit-level=high/)
  assert.match(workflow, /npm audit --prefix dashboard --audit-level=high/)
  assert.match(workflow, /pip install --require-hashes -r scripts\/ci\/python-audit\.lock/)
  assert.equal((workflow.match(/pip-audit --strict --require-hashes --disable-pip/g) || []).length, 2)
  assert.match(workflow, /pip install --require-hashes -r scripts\/ci\/python-sast\.lock/)
  assert.match(workflow, /semgrep scan --config \.github\/semgrep\.yml --severity ERROR --error --metrics off/)
  assert.match(workflow, /trufflehog@[0-9a-f]{40}/)
  assert.match(workflow, /--only-verified/)
  assert.equal((workflow.match(/version: 3\.90\.9@sha256:[0-9a-f]{64}/g) || []).length, 3)
  assert.equal((workflow.match(/scan-type: image/g) || []).length, 2)
  assert.equal((workflow.match(/exit-code: '1'/g) || []).length, 2)
  assert.equal((workflow.match(/severity: HIGH,CRITICAL/g) || []).length, 2)
  assert.doesNotMatch(workflow, /if-no-files-found:\s*ignore|severity:\s*UNKNOWN/)
  const semgrep = read('.github/semgrep.yml')
  assert.ok((semgrep.match(/severity: ERROR/g) || []).length >= 5)
})

test('JavaScript and Python installs are lock-based and reproducible', () => {
  for (const path of ['package-lock.json', 'dashboard/package-lock.json']) {
    const lock = JSON.parse(read(path))
    assert.equal(lock.lockfileVersion, 3)
  }
  const workflow = read('.github/workflows/security.yml')
  assert.match(workflow, /npm ci --ignore-scripts/)
  assert.match(workflow, /pip install --require-hashes -r scripts\/ci\/python-test\.lock/)
  for (const path of [
    'scripts/ci/python-runtime.lock',
    'scripts/ci/python-test.lock',
    'scripts/ci/python-compressor.lock',
    'scripts/ci/python-audit.lock',
    'scripts/ci/python-sast.lock',
  ]) {
    const lock = read(path)
    assert.match(lock, /--hash=sha256:[0-9a-f]{64}/)
    assert.doesNotMatch(lock, /^[a-z0-9_.-]+(?:>=|~=|>|<)[^=]/im)
  }
})

test('blocking build covers application, backend, compressor, and core engine suites', () => {
  const workflow = read('.github/workflows/security.yml')
  assert.match(workflow, /npm test\n/)
  assert.match(workflow, /npm test --prefix dashboard/)
  assert.match(
    workflow,
    /python -m pytest -q tests unit-tests token_efficiency_model\/lossless\/tests/,
  )
  assert.match(workflow, /npm run build\n/)
  assert.match(workflow, /npm run build --prefix dashboard/)
  assert.match(
    read('tests/test_production_topology.py'),
    /def test_hosted_dashboard_keys_use_atomic_store_contract\(/,
  )
})

test('release Docker bases and Python installs are immutable', () => {
  for (const [path, lockPath] of [
    ['Dockerfile', 'scripts/ci/python-runtime.lock'],
    ['services/compress/Dockerfile', 'scripts/ci/python-compressor.lock'],
  ]) {
    const dockerfile = read(path)
    const normalized = dockerfile.replace(/\\\r?\n\s*/g, ' ')
    const fromLines = dockerfile.split(/\r?\n/).filter((line) => /^FROM\s+/i.test(line))
    assert.ok(fromLines.length > 0, path)
    for (const line of fromLines) {
      assert.match(line, /^FROM\s+[^\s@]+@sha256:[0-9a-f]{64}(?:\s+AS\s+\w+)?$/i, `${path}: ${line}`)
    }
    const escapedLock = lockPath.replaceAll('/', '\\/').replaceAll('.', '\\.')
    assert.match(dockerfile, new RegExp(`^COPY\\s+${escapedLock}\\s+`, 'm'), path)
    assert.match(
      normalized,
      new RegExp(`python -m pip install[^\\n]*--require-hashes[^\\n]*-r ${escapedLock}`),
      `${path} must install ${lockPath} with hash enforcement`,
    )
    assert.doesNotMatch(normalized, /pip install[^\n]*(?:requirements\.txt|--upgrade\s+pip)/)
    assert.doesNotMatch(normalized, /\b(?:apt-get|apk|dnf|yum)\s+(?:install|update)\b/)
  }
})

test('migration order, generated drift, idempotence, and rollback contracts pass', () => {
  verifyMigrations()
  const securitySuffix = Array.from(
    { length: 18 },
    (_, index) => `supabase/migrations/20260720${String(index + 1).padStart(4, '0')}_${[
      'stripe_webhook_durability',
      'waitlist_security',
      'billing_owner_attribution',
      'stripe_event_ordering',
      'initial_service_key',
      'company_billing_authorization',
      'billing_recovery_scope',
      'provider_credential_cleanup',
      'multitab_dashboard_sessions',
      'shared_endpoint_rate_limits',
      'compliance_billing_isolation',
      'stripe_webhook_lease_renewal',
      'billing_control_rate_limits',
      'billing_checkout_session_reservations',
      'provider_outbound_ambiguity',
      'durable_onboarding',
      'billing_customer_owner_fencing',
      'workspace_experiences',
    ][index]}.sql`,
  )
  assert.equal(expectedFreshMigrationOrder.length, 43)
  assert.equal(expectedUpgradeMigrationOrder.length, 31)
  assert.deepEqual(expectedFreshMigrationOrder.slice(-18), securitySuffix)
  assert.deepEqual(expectedUpgradeMigrationOrder.slice(-18), securitySuffix)
  const workflow = read('.github/workflows/migrations.yml')
  assert.match(workflow, /pgvector\/pgvector:pg16-bookworm@sha256:[0-9a-f]{64}/)
  assert.match(workflow, /docker run --detach --rm --network host/)
  assert.match(workflow, /postgres -c listen_addresses=127\.0\.0\.1/)
  assert.match(workflow, /brevitas-restore-postgres/)
  assert.match(workflow, /postgres -p 5433 -c listen_addresses=127\.0\.0\.1/)
  assert.match(workflow, /bash scripts\/ci\/run-migration-tests\.sh/)
  assert.match(workflow, /bash scripts\/ci\/run-waitlist-shared-limit-test\.sh/)
  assert.match(workflow, /scripts\/ci\/\*billing-owner-transfer\*/)
  assert.match(workflow, /bash scripts\/ci\/run-restore-integration\.sh/)
  assert.match(workflow, /- 'scripts\/ci\/run-restore-integration\.sh'/)
  assert.match(workflow, /- 'scripts\/dr\/\*\*'/)
  const runner = read('scripts/ci/run-migration-tests.sh')
  assert.ok(
    runner.indexOf('node scripts/ci/validate-migration-dsn.mjs') <
      runner.indexOf("PGOPTIONS='-c default_transaction_read_only=on'"),
    'the URI must be parsed before the read-only database preflight',
  )
  assert.ok(
    runner.indexOf("PGOPTIONS='-c default_transaction_read_only=on'") <
      runner.indexOf('migration-bootstrap.sql'),
    'the read-only loopback check must precede all DDL',
  )
  assert.match(runner, /select pg_catalog\.host\(pg_catalog\.inet_server_addr\(\)\)/)
  assert.match(runner, /127\.0\.0\.1\|::1/)
  assert.match(runner, /migration-fresh-manifest\.txt/)
  assert.match(runner, /migration-upgrade-manifest\.txt/)
  assert.match(runner, /bash scripts\/ci\/apply-billing-identity-migrations\.sh/)
  assert.match(runner, /billing-maintenance-version-fetch-fixture\.mjs/)
  assert.match(runner, /BREVITAS_BILLING_MAINTENANCE_OFFLINE_LOOPBACK_TEST=true/)
  assert.match(runner, /004_database_scaling\.concurrent_indexes\.sql/)
  assert.match(runner, /004_database_scaling\.rollback\.sql/)
  assert.match(runner, /migration-rollback-assertions\.sql/)
  assert.match(runner, /migration-reapply-assertions\.sql/)
  assert.match(runner, /migration-cache-legacy-fixture\.sql/)
  assert.match(runner, /migration-cache-fresh-assertions\.sql/)
  assert.match(runner, /migration-cache-guard-assertions\.sql/)
  assert.match(runner, /migration-cache-concurrent-write\.sql/)
  assert.match(runner, /migration-cache-rollback\.sql/)
  assert.match(runner, /migration-key-audit-assertions\.sql/)
  assert.match(runner, /migration-device-null-upgrade-fixture\.sql/)
  assert.match(runner, /migration-device-null-upgrade-assertions\.sql/)
  assert.match(runner, /migration-device-membership-assertions\.sql/)
  assert.match(runner, /migration-receipt-accounting-assertions\.sql/)
  assert.match(runner, /migration-active-company-assertions\.sql/)
  assert.match(runner, /migration-waitlist-security-assertions\.sql/)
  assert.match(runner, /migration-initial-service-key-assertions\.sql/)
  assert.match(runner, /migration-company-billing-assertions\.sql/)
  assert.match(runner, /migration-billing-recovery-scope-assertions\.sql/)
  assert.match(runner, /migration-provider-credential-cleanup-assertions\.sql/)
  assert.match(runner, /migration-multitab-dashboard-session-assertions\.sql/)
  assert.match(runner, /waitlist-shared-rate-limit-assertions\.sql/)
  assert.match(runner, /billing-control-shared-rate-limit-assertions\.sql/)
  assert.match(runner, /migration-checkout-session-reservation-assertions\.sql/)
  assert.match(runner, /run-billing-control-shared-limit-test\.sh/)
  assert.match(runner, /migration-billing-customer-owner-fencing-assertions\.sql/)
  assert.match(runner, /run-billing-owner-transfer-race-test\.sh/)
  assert.match(runner, /migration-compliance-billing-isolation-assertions\.sql/)
  assert.match(runner, /migration-receipt-accounting-rollback\.sql/)
  assert.match(runner, /migration-receipt-accounting-rollback-assertions\.sql/)
  assert.match(runner, /scripts\/dr\/compliance-workflow-assertions\.sql/)
  assert.match(runner, /cache_pids/)
  assert.match(runner, /127\.0\.0\.1/)
  assert.match(runner, /#fresh_migrations\[@\]\}" -ne 43/)
  assert.match(runner, /#upgrade_migrations\[@\]\}" -ne 31/)
  assert.equal((runner.match(/apply_migration "\$\{device_migration\}"/g) || []).length, 3)
  assert.equal((runner.match(/apply_migration "\$\{membership_migration\}"/g) || []).length, 3)
  assert.equal((runner.match(/apply_migration "\$\{receipt_migration\}"/g) || []).length, 4)
  assert.equal((runner.match(/apply_migration "\$\{selection_migration\}"/g) || []).length, 3)
  for (const variable of [
    'webhook_migration',
    'waitlist_migration',
    'billing_owner_migration',
    'billing_recovery_scope_migration',
    'provider_cleanup_migration',
    'multitab_sessions_migration',
    'shared_limits_migration',
    'compliance_billing_isolation_migration',
    'webhook_lease_renewal_migration',
    'billing_control_limits_migration',
    'checkout_reservation_migration',
    'provider_outbound_migration',
    'durable_onboarding_migration',
    'billing_customer_owner_migration',
  ]) {
    assert.equal(
      (runner.match(new RegExp(`apply_migration "\\$\\{${variable}\\}"`, 'g')) || []).length,
      3,
      `${variable} must be applied twice on upgrade and reapplied on fresh install`,
    )
  }
  for (const variable of [
    'stripe_ordering_migration',
    'initial_service_key_migration',
    'company_billing_migration',
  ]) {
    assert.equal(
      (runner.match(new RegExp(`apply_migration "\\$\\{${variable}\\}"`, 'g')) || []).length,
      1,
      `${variable} must use the guarded maintenance runner on upgrade and direct apply on fresh install`,
    )
  }
  const frozenChecksums = read('scripts/ci/migration-frozen-checksums.txt')
  assert.match(
    frozenChecksums,
    /a1ec546eed185128b545093a0ea3de6567ca0629bf157995d2012b4a620b3f62  supabase\/migrations\/202607170007_compliance_workflows\.sql/,
  )
  assert.match(
    frozenChecksums,
    /d6a0a37952f3c539da8e1ddbe44133a9fda11a6faab1af843380c05bf4452f8a  scripts\/dr\/compliance-workflow-assertions\.sql/,
  )
  assert.match(
    frozenChecksums,
    /4525eb31944b46b4b69f37c3f88b35f67968bca2262a50157f34f85ba44aeb0f  supabase\/migrations\/202607170010_device_delivery_idempotency\.sql/,
  )
  assert.match(
    frozenChecksums,
    /22e7a42dcf3075b2f324f7cba9cc06e7aa230203a6bf160c717f2eed9fc6ff7c  supabase\/migrations\/202607170013_active_company_selection\.sql/,
  )
  const deviceUpgradeAssertions = read(
    'scripts/ci/migration-device-null-upgrade-assertions.sql',
  )
  const deviceUpgradeFixture = read(
    'scripts/ci/migration-device-null-upgrade-fixture.sql',
  )
  assert.match(deviceUpgradeFixture, /'legacy-kms-ciphertext'/)
  assert.match(deviceUpgradeFixture, /'c1000000-0000-4000-8000-000000000001',\s*'c1000000-0000-4000-8000-000000000001'/)
  assert.match(deviceUpgradeFixture, /'release-quarantined-ciphertext',now\(\)/)
  assert.match(deviceUpgradeAssertions, /encrypted_key=''/)
  assert.match(deviceUpgradeAssertions, /constraint_state\.convalidated/)
  const restoreRunner = read('scripts/ci/run-restore-integration.sh')
  assert.match(restoreRunner, /pg_dump[\s\S]*--format=custom[\s\S]*--schema=public --schema=auth/)
  assert.match(restoreRunner, /pg_restore[\s\S]*--single-transaction[\s\S]*--no-owner --no-privileges/)
  assert.match(restoreRunner, /bootstrap-restore-target\.sh/)
  assert.match(restoreRunner, /verify-logical\.sh/)
  assert.match(restoreRunner, /replay-deletion-artifact\.sh/)
  assert.match(restoreRunner, /raw_verified_at < replay_verified_at/)
  assert.match(restoreRunner, /replay_verified_at <= ready_at/)
  assert.match(restoreRunner, /tombstones":\[\]/)
  for (const refusal of ['missing-control', 'wrong-control', 'wrong-hash', 'wrong-reference']) {
    assert.match(restoreRunner, new RegExp(refusal))
  }
  assert.ok(
    restoreRunner.indexOf('expect_failure missing-control') <
      restoreRunner.indexOf('bootstrap-restore-target.sh'),
    'missing restore control must fail before target bootstrap',
  )
  assert.ok(
    restoreRunner.indexOf('pg_restore') < restoreRunner.indexOf('verify-logical.sh'),
    'the independently restored database must be verified after restore',
  )
  const keyAssertions = read('scripts/ci/migration-key-audit-assertions.sql')
  assert.match(keyAssertions, /company_admin_dashboard_keys_page/)
  assert.match(keyAssertions, /company_admin_revoke_dashboard_session_key/)
  assert.match(keyAssertions, /company_admin_revoke_key[\s\S]*is not null/)
  assert.match(keyAssertions, /like '%fingerprint%'/)
  assert.match(keyAssertions, /dashboard_session\.revoke\.noop/)
  assert.match(keyAssertions, /strict dashboard revoke addressed a service-account key/)
  assert.match(keyAssertions, /multi-tab dashboard key coexistence failed/)
  const initialKeyAssertions = read('scripts/ci/migration-initial-service-key-assertions.sql')
  assert.match(initialKeyAssertions, /initial service credential is not immediately usable/)
  assert.match(initialKeyAssertions, /duplicate initial key left a keyless service account/)
  const providerCleanupAssertions = read(
    'scripts/ci/migration-provider-credential-cleanup-assertions.sql',
  )
  assert.match(providerCleanupAssertions, /provider configuration survived key revocation or deletion/)
  assert.match(providerCleanupAssertions, /bounded provider configuration expiry purge failed/)
  const multitabAssertions = read(
    'scripts/ci/migration-multitab-dashboard-session-assertions.sql',
  )
  assert.match(multitabAssertions, /dashboard session rotated before the actor cap/)
  assert.match(multitabAssertions, /actor-scoped dashboard session cap is incorrect/)
  const deviceAssertions = read('scripts/ci/migration-device-membership-assertions.sql')
  assert.match(deviceAssertions, /revoked-key replay did not quarantine its receipt/)
  assert.match(deviceAssertions, /removed approver replay did not quarantine its receipt/)
  assert.match(deviceAssertions, /active membership actor\/role\/cap contract failed/)
  const activeCompanyAssertions = read('scripts/ci/migration-active-company-assertions.sql')
  assert.match(activeCompanyAssertions, /foreign active company selection was accepted/)
  assert.match(activeCompanyAssertions, /stale active company selection was not repaired/)
  const receiptMigration = read(
    'supabase/migrations/202607170012_receipt_accounting_alignment.sql',
  )
  assert.match(receiptMigration, /cache_write_5m_tokens bigint not null default 0/)
  assert.match(receiptMigration, /cache_write_1h_tokens bigint not null default 0/)
  assert.match(receiptMigration, /cache_attributable boolean not null default false/)
  assert.match(receiptMigration, /cache_write_tokens >= 0/)
  assert.doesNotMatch(receiptMigration, /cache_write_tokens=0\s+or/)
  assert.match(receiptMigration, /queue_brevitas_fee_after_usage/)
  assert.match(receiptMigration, /grant select, insert on table public\.usage_log to service_role/)
  const receiptAssertions = read('scripts/ci/migration-receipt-accounting-assertions.sql')
  for (const refusal of [
    'release-invalid-zero-total-tier', 'release-invalid-negative-total',
    'release-invalid-negative-5m', 'release-invalid-negative-1h',
  ]) assert.match(receiptAssertions, new RegExp(refusal))
})

test('migration DSN validation rejects endpoint bait outside the hostname', () => {
  assert.deepEqual(
    validateMigrationDsn('postgresql://postgres:secret@127.0.0.1:5432/brevitas_ci'),
    { hostname: '127.0.0.1', database: 'brevitas_ci', protocol: 'postgresql:' },
  )
  assert.equal(
    validateMigrationDsn('postgres://postgres:secret@[::1]:5432/brevitas_ci').hostname,
    '::1',
  )
  assert.equal(
    validateMigrationDsn('postgresql://postgres:secret@localhost/brevitas_ci').hostname,
    'localhost',
  )
  const rejected = [
    'postgresql://127.0.0.1@database.example/brevitas_ci',
    'postgresql://postgres:127.0.0.1@database.example/brevitas_ci',
    'postgresql://database.example/127.0.0.1',
    'postgresql://database.example/brevitas_ci?host=127.0.0.1',
    'postgresql://database.example/brevitas_ci#127.0.0.1',
    'postgresql://127.0.0.1.example/brevitas_ci',
    'https://127.0.0.1/brevitas_ci',
    'postgresql://127.0.0.1/',
  ]
  for (const value of rejected) {
    assert.throws(() => validateMigrationDsn(value))
    const result = spawnSync('bash', ['scripts/ci/run-migration-tests.sh'], {
      cwd: root,
      encoding: 'utf8',
      env: { ...process.env, DATABASE_URL: value },
    })
    assert.equal(result.status, 2, value)
    assert.equal(`${result.stdout}${result.stderr}`.includes(value), false)
  }
})

test('billing identity rollout is disabled, quiesced, target-bound, and per-file atomic', () => {
  const approved = {
    DATABASE_URL: 'postgresql://operator:secret@127.0.0.1:5432/brevitas_ci?sslmode=require',
    BREVITAS_BILLING_ENABLED: 'false',
    BREVITAS_BILLING_MIGRATION_PHASE: 'api-worker-quiesced',
    BREVITAS_BILLING_MAINTENANCE_SHA: 'a'.repeat(40),
    BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL:
      'https://dashboard.example.invalid/api/version',
    BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL:
      'http://127.0.0.1:43119/version',
    BREVITAS_BILLING_MIGRATION_EXPECTED_HOST: '127.0.0.1',
    BREVITAS_BILLING_MIGRATION_EXPECTED_DATABASE: 'brevitas_ci',
  }
  const contract = assertBillingMigrationMaintenance(approved)
  assert.equal(contract.database, 'brevitas_ci')
  assert.deepEqual(contract.migrations, BILLING_IDENTITY_MIGRATIONS)
  assert.deepEqual(contract.versionEndpoints, {
    dashboard: approved.BREVITAS_BILLING_MAINTENANCE_DASHBOARD_VERSION_URL,
    worker: approved.BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL,
  })

  for (const override of [
    { BREVITAS_BILLING_ENABLED: 'true' },
    { BREVITAS_BILLING_MIGRATION_PHASE: 'deploying' },
    { BREVITAS_BILLING_MAINTENANCE_SHA: 'abcdef0' },
    { BREVITAS_BILLING_MAINTENANCE_WORKER_VERSION_URL: 'http://worker.example/version' },
    { BREVITAS_BILLING_MIGRATION_EXPECTED_HOST: 'database.example' },
    { DATABASE_URL: 'postgresql://127.0.0.1@database.example/brevitas_ci' },
    { DATABASE_URL: 'postgresql://operator:secret@127.0.0.1/brevitas_ci?host=database.example' },
  ]) assert.throws(() => assertBillingMigrationMaintenance({ ...approved, ...override }))

  const apply = read('scripts/ci/apply-billing-identity-migrations.sh')
  assert.match(apply, /for migration in "\$\{billing_identity_migrations\[@\]\}"/)
  assert.match(
    apply,
    /psql "\$\{DATABASE_URL\}" --no-psqlrc --set ON_ERROR_STOP=1 --file "\$\{migration\}"/,
  )
  assert.doesNotMatch(apply, /--single-transaction/)
  assert.match(apply, /keep API\/webhook and worker billing disabled/)
  assert.match(apply, /then 'complete'/)
  assert.match(apply, /then 'inconsistent-company-scoped'/)
  assert.match(apply, /validated the company-scoped postcondition and skipped reapplication/)
  assert.ok(
    apply.indexOf('node scripts/ci/verify-billing-maintenance-deployment.mjs') <
      apply.indexOf('psql "${DATABASE_URL}"'),
    'deployed versions must be verified before the first PostgreSQL connection',
  )
  assert.ok(
    apply.indexOf('initial_state="$(read_billing_identity_state)"') <
      apply.indexOf('for migration in "${billing_identity_migrations[@]}"'),
  )

  for (const path of BILLING_IDENTITY_MIGRATIONS) {
    const lines = read(path).split(/\r?\n/)
      .map(line => line.trim().toLowerCase())
      .filter(line => line && !line.startsWith('--'))
    assert.equal(lines[0], 'begin;', path)
    assert.equal(lines.at(-1), 'commit;', path)
  }

  const runner = read('scripts/ci/run-migration-tests.sh')
  assert.equal(
    (runner.match(/assert_atomic_migration_rollback "\$\{/g) || []).length,
    17,
  )
  assert.match(runner, /print "select 1\/0;"/)
  assert.match(runner, /Failure-injected migration left partial state/)
  assert.match(runner, /Applying the guarded 200004-200006 billing maintenance procedure immediately after 200003/)
  assert.equal((runner.match(/^run_billing_identity_maintenance$/gm) || []).length, 2)
  assert.ok(
    runner.indexOf('apply_migration "${billing_owner_migration}"') <
      runner.indexOf("echo 'Applying the guarded 200004-200006 billing maintenance procedure"),
  )
  assert.ok(
    runner.indexOf("echo 'Applying the guarded 200004-200006 billing maintenance procedure") <
      runner.indexOf('assert_atomic_migration_rollback "${billing_recovery_scope_migration}"'),
  )

  const initialKey = read('supabase/migrations/202607200005_initial_service_key.sql')
  assert.match(initialKey, /client_upgrade_required/)
  assert.match(initialKey, /required_contract','initial_service_key/)
  const initialKeyAssertions = read('scripts/ci/migration-initial-service-key-assertions.sql')
  assert.match(initialKeyAssertions, /legacy service-account RPC did not fail closed/)

  const ordering = read('supabase/migrations/202607200004_stripe_event_ordering.sql')
  for (const functionName of [
    'compare_and_set_stripe_subscription_snapshot',
    'compare_and_set_stripe_invoice_snapshot',
  ]) {
    assert.ok(
      ordering.indexOf(`drop function if exists public.${functionName}(`) <
        ordering.indexOf(`create function public.${functionName}(`),
      `${functionName} must be dropped transactionally before its input parameter is renamed`,
    )
  }

  const webhook = read('src/app/api/billing/webhook/route.ts')
  assert.ok(webhook.indexOf('if (!billingIsConfigured())') < webhook.indexOf('await request.text()'))
  assert.match(webhook, /'Retry-After': '30'/)
})

test('billing maintenance rejects versions before Postgres, skips complete state, resumes, and rejects drift', (t) => {
  const versionFailure = runBillingMaintenanceWithFakePostgres(t, 'version-failure')
  assert.equal(versionFailure.status, 2)
  assert.deepEqual(versionFailure.commands, ['version'])
  assert.deepEqual(versionFailure.applied, [])

  const complete = runBillingMaintenanceWithFakePostgres(t, 'complete')
  assert.equal(complete.status, 0, complete.stderr)
  assert.deepEqual(complete.applied, [])
  assert.equal(complete.commands[0], 'version')
  assert.ok(complete.commands.slice(1).every(command => command === 'psql'))
  assert.match(complete.stdout, /validated the company-scoped postcondition and skipped reapplication/)

  const pending = runBillingMaintenanceWithFakePostgres(t, 'pending')
  assert.equal(pending.status, 0, pending.stderr)
  assert.deepEqual(pending.applied, BILLING_IDENTITY_MIGRATIONS)
  assert.equal(pending.commands[0], 'version')
  assert.ok(pending.commands.slice(1).every(command => command === 'psql'))
  assert.match(pending.stdout, /Billing identity migrations 200004-200006 passed/)

  const inconsistent = runBillingMaintenanceWithFakePostgres(t, 'inconsistent')
  assert.equal(inconsistent.status, 1)
  assert.deepEqual(inconsistent.applied, [])
  assert.equal(inconsistent.commands[0], 'version')
  assert.match(inconsistent.stderr, /refusing to replay earlier identity migrations/)
})

test('organization key mutations append immutable audit evidence atomically', () => {
  const store = read('api/store.py')
  const classStart = store.indexOf('class SupabaseUsageStore:')
  assert.ok(classStart >= 0, 'SupabaseUsageStore is missing')
  const classBody = store.slice(classStart)
  const method = (name) => {
    const start = classBody.indexOf(`    def ${name}(`)
    assert.ok(start >= 0, `SupabaseUsageStore.${name} is missing`)
    const end = classBody.indexOf('\n    def ', start + 9)
    return classBody.slice(start, end >= 0 ? end : undefined)
  }
  for (const name of ['create_key', 'revoke_organization_key']) {
    const body = method(name)
    assert.match(
      body,
      /self\._request\(\s*"POST",\s*"rpc\/[^"]*key[^"]*"/,
      `${name} must call one key-mutation + audit PostgreSQL RPC`,
    )
    assert.doesNotMatch(body, /self\._request\(\s*"(?:POST|PATCH|DELETE)",\s*"api_keys"/)
    assert.doesNotMatch(body, /self\._request\(\s*"POST",\s*"audit_events"/)
  }
  const bulkRevoke = method('revoke_keys_by_type')
  assert.doesNotMatch(
    bulkRevoke,
    /self\._request\(\s*"(?:POST|PATCH|DELETE)",\s*"api_keys"/,
    'revoke_keys_by_type may not bypass the atomic audit boundary',
  )
  assert.doesNotMatch(bulkRevoke, /self\._request\(\s*"POST",\s*"audit_events"/)
})

test('staging target allowlist rejects local, production, and attacker-controlled URLs', () => {
  assert.equal(
    assertStagingTarget('https://staging-api.brevitassystems.com', 'api'),
    'https://staging-api.brevitassystems.com',
  )
  for (const value of [
    'http://staging-api.brevitassystems.com',
    'https://api.brevitassystems.com',
    'https://localhost',
    'https://127.0.0.1',
    'https://staging-api.brevitassystems.com.evil.example',
    'https://staging-api.brevitassystems.com/path',
    'https://user:password@staging-api.brevitassystems.com',
  ]) {
    assert.throws(() => assertStagingTarget(value, 'api'))
  }
})

test('staging smoke is manual, fork-safe, approval-gated, and non-mutating', async () => {
  const workflow = read('.github/workflows/staging-smoke.yml')
  assert.match(workflow, /^\s{4}environment: staging$/m)
  assert.match(workflow, /github\.event\.repository\.fork == false/)
  assert.match(workflow, /github\.repository == 'jeojdi\/Brevitas-Systems'/)
  assert.match(workflow, /github\.ref == 'refs\/heads\/main'/)
  for (const secret of [
    'STAGING_TENANT_A_API_KEY', 'STAGING_TENANT_B_API_KEY',
    'STAGING_TENANT_A_JOB_ID', 'STAGING_TENANT_B_JOB_ID',
    'STAGING_TENANT_A_CUSTOMER_ID', 'STAGING_TENANT_B_CUSTOMER_ID',
    'STAGING_BILLING_USER_TOKEN', 'STAGING_BILLING_RECOVERY_SECRET',
  ]) assert.match(workflow, new RegExp(`secrets\\.${secret}`))

  const environment = {
    STAGING_SMOKE_ALLOWED: 'true',
    STAGING_ENVIRONMENT_CONFIRMATION: 'staging',
    STAGING_REPOSITORY: 'jeojdi/Brevitas-Systems',
    STAGING_REPOSITORY_FORK: 'false',
    STAGING_GITHUB_EVENT: 'workflow_dispatch',
    STAGING_GITHUB_REF: 'refs/heads/main',
    STAGING_API_URL: 'https://staging-api.brevitassystems.com',
    STAGING_DASHBOARD_URL: 'https://staging.brevitassystems.com',
    STAGING_TENANT_A_API_KEY: 'tenant-a-secret',
    STAGING_TENANT_B_API_KEY: 'tenant-b-secret',
    STAGING_TENANT_A_JOB_ID: '10000000-0000-4000-8000-000000000001',
    STAGING_TENANT_B_JOB_ID: '20000000-0000-4000-8000-000000000002',
    STAGING_TENANT_A_CUSTOMER_ID: 'release-customer-a',
    STAGING_TENANT_B_CUSTOMER_ID: 'release-customer-b',
    STAGING_BILLING_USER_TOKEN: 'billing-user-token',
    STAGING_BILLING_RECOVERY_SECRET: 'billing-recovery-secret',
  }
  const calls = []
  const fakeFetch = async (url, options) => {
    calls.push({ url, options })
    const parsed = new URL(url)
    let status = 200
    let body = null
    if (parsed.pathname === '/v1/health/ready') {
      body = JSON.stringify({
        accepting_traffic: true,
        database_ready: true,
        redis_ready: true,
        kms_ready: true,
        dependencies: {
          compressor: { status: 'ready' },
          kms: { status: 'ready', configured: true, active_probe: true, fresh: true },
        },
      })
    }
    if (parsed.pathname === '/v1/stats') status = 401
    if (parsed.pathname.includes(environment.STAGING_TENANT_A_JOB_ID)) {
      status = options.headers['x-brevitas-key'] === environment.STAGING_TENANT_A_API_KEY &&
        options.headers['x-brevitas-customer-id'] === environment.STAGING_TENANT_A_CUSTOMER_ID ? 200 : 404
    }
    if (parsed.pathname.includes(environment.STAGING_TENANT_B_JOB_ID)) {
      status = options.headers['x-brevitas-key'] === environment.STAGING_TENANT_B_API_KEY &&
        options.headers['x-brevitas-customer-id'] === environment.STAGING_TENANT_B_CUSTOMER_ID ? 200 : 404
    }
    if (parsed.pathname === '/api/billing/status') {
      status = options.headers.authorization ? 200 : 401
    }
    if (parsed.pathname === '/api/billing/sync') {
      const userAuthorized = options.headers.authorization ===
        `Bearer ${environment.STAGING_BILLING_USER_TOKEN}`
      const recoveryAuthorized = options.headers['x-billing-recovery-secret'] ===
        environment.STAGING_BILLING_RECOVERY_SECRET
      status = userAuthorized && recoveryAuthorized ? 400 : 401
    }
    return new Response(body, {
      status,
      headers: body ? { 'content-type': 'application/json' } : undefined,
    })
  }
  const result = await runStagingSmoke(environment, fakeFetch)
  assert.deepEqual(result, {
    liveness: true, readiness: true, auth: true, tenantIsolation: true,
    billing: true, manualRecovery: true,
  })
  assert.equal(calls.length, 17)
  assert.ok(calls.some(({ url }) => new URL(url).pathname === '/v1/health/ready'))
  const deniedJobCalls = calls.filter(({ url, options }) => {
    const path = new URL(url).pathname
    if (!path.startsWith('/v1/jobs/')) return false
    const key = options.headers['x-brevitas-key']
    const customer = options.headers['x-brevitas-customer-id']
    return !(
      (path.endsWith(environment.STAGING_TENANT_A_JOB_ID) &&
        key === environment.STAGING_TENANT_A_API_KEY &&
        customer === environment.STAGING_TENANT_A_CUSTOMER_ID) ||
      (path.endsWith(environment.STAGING_TENANT_B_JOB_ID) &&
        key === environment.STAGING_TENANT_B_API_KEY &&
        customer === environment.STAGING_TENANT_B_CUSTOMER_ID)
    )
  })
  assert.equal(deniedJobCalls.length, 6)
  assert.ok(deniedJobCalls.some(({ options }) =>
    options.headers['x-brevitas-key'] === environment.STAGING_TENANT_A_API_KEY &&
    options.headers['x-brevitas-customer-id'] === environment.STAGING_TENANT_B_CUSTOMER_ID))
  assert.ok(deniedJobCalls.some(({ options }) =>
    options.headers['x-brevitas-key'] === environment.STAGING_TENANT_B_API_KEY &&
    options.headers['x-brevitas-customer-id'] === environment.STAGING_TENANT_A_CUSTOMER_ID))
  const manual = calls.filter(({ url }) => new URL(url).pathname === '/api/billing/sync')
  assert.equal(manual.length, 4)
  assert.ok(manual.every(({ options }) => options.body === '{}'))
  assert.equal(manual.filter(({ options }) =>
    options.headers.authorization === `Bearer ${environment.STAGING_BILLING_RECOVERY_SECRET}`
  ).length, 1)
  assert.equal(manual.filter(({ options }) =>
    options.headers.authorization === `Bearer ${environment.STAGING_BILLING_USER_TOKEN}` &&
    options.headers['x-billing-recovery-secret'] === environment.STAGING_BILLING_RECOVERY_SECRET
  ).length, 1)
  await assert.rejects(() => runStagingSmoke({ ...environment, STAGING_REPOSITORY_FORK: 'true' }, fakeFetch))
  const degradedFetch = async (url, options) => {
    const response = await fakeFetch(url, options)
    if (new URL(url).pathname !== '/v1/health/ready') return response
    return Response.json({
      accepting_traffic: true,
      database_ready: true,
      redis_ready: true,
      kms_ready: true,
      dependencies: {
        compressor: { status: 'unavailable' },
        kms: { status: 'ready', configured: true, active_probe: true, fresh: true },
      },
    })
  }
  await assert.rejects(() => runStagingSmoke(environment, degradedFetch), /compressor/)
  const staleKmsFetch = async (url, options) => {
    const response = await fakeFetch(url, options)
    if (new URL(url).pathname !== '/v1/health/ready') return response
    return Response.json({
      accepting_traffic: true,
      database_ready: true,
      redis_ready: true,
      kms_ready: false,
      dependencies: {
        compressor: { status: 'ready' },
        kms: { status: 'unavailable', configured: true, active_probe: true, fresh: false },
      },
    })
  }
  await assert.rejects(() => runStagingSmoke(environment, staleKmsFetch), /active KMS/)
  await assert.rejects(
    () => runStagingSmoke({
      ...environment,
      STAGING_TENANT_B_API_KEY: environment.STAGING_TENANT_A_API_KEY,
    }, fakeFetch),
    /distinct API keys/,
  )
  const leakyDenialFetch = async (url, options) => {
    const parsed = new URL(url)
    if (
      parsed.pathname === `/v1/jobs/${environment.STAGING_TENANT_A_JOB_ID}` &&
      options.headers?.['x-brevitas-key'] === environment.STAGING_TENANT_B_API_KEY
    ) {
      return Response.json({ job_id: environment.STAGING_TENANT_A_JOB_ID }, { status: 404 })
    }
    return fakeFetch(url, options)
  }
  await assert.rejects(() => runStagingSmoke(environment, leakyDenialFetch), /leaked fixture identity/)
})
