import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import test from 'node:test'

import { runStagingSmoke, assertStagingTarget } from '../scripts/ci/staging-smoke.mjs'
import { validateMigrationDsn } from '../scripts/ci/validate-migration-dsn.mjs'
import { verifyMigrations } from '../scripts/ci/verify-migrations.mjs'

const root = resolve(import.meta.dirname, '..')
const read = (path) => readFileSync(resolve(root, path), 'utf8')

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
  const workflow = read('.github/workflows/migrations.yml')
  assert.match(workflow, /pgvector\/pgvector:pg16-bookworm@sha256:[0-9a-f]{64}/)
  assert.match(workflow, /docker run --detach --rm --network host/)
  assert.match(workflow, /postgres -c listen_addresses=127\.0\.0\.1/)
  assert.match(workflow, /brevitas-restore-postgres/)
  assert.match(workflow, /postgres -p 5433 -c listen_addresses=127\.0\.0\.1/)
  assert.match(workflow, /bash scripts\/ci\/run-migration-tests\.sh/)
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
  assert.match(runner, /select inet_server_addr\(\)::text/)
  assert.match(runner, /127\.0\.0\.1\|::1/)
  assert.match(runner, /migration-fresh-manifest\.txt/)
  assert.match(runner, /migration-upgrade-manifest\.txt/)
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
  assert.match(runner, /migration-receipt-accounting-rollback\.sql/)
  assert.match(runner, /migration-receipt-accounting-rollback-assertions\.sql/)
  assert.match(runner, /scripts\/dr\/compliance-workflow-assertions\.sql/)
  assert.match(runner, /cache_pids/)
  assert.match(runner, /127\.0\.0\.1/)
  assert.match(runner, /#fresh_migrations\[@\]\}" -ne 25/)
  assert.match(runner, /#upgrade_migrations\[@\]\}" -ne 13/)
  assert.equal((runner.match(/apply_migration "\$\{device_migration\}"/g) || []).length, 3)
  assert.equal((runner.match(/apply_migration "\$\{membership_migration\}"/g) || []).length, 3)
  assert.equal((runner.match(/apply_migration "\$\{receipt_migration\}"/g) || []).length, 4)
  assert.equal((runner.match(/apply_migration "\$\{selection_migration\}"/g) || []).length, 3)
  const frozenChecksums = read('scripts/ci/migration-frozen-checksums.txt')
  assert.match(
    frozenChecksums,
    /a1ec546eed185128b545093a0ea3de6567ca0629bf157995d2012b4a620b3f62  supabase\/migrations\/202607170007_compliance_workflows\.sql/,
  )
  assert.match(
    frozenChecksums,
    /ceac523bba3ba41bd7197393c9a15236f1a6cc16c76ecbdc9bf21d8bc50cbae9  scripts\/dr\/compliance-workflow-assertions\.sql/,
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
    'STAGING_BILLING_USER_TOKEN', 'STAGING_BILLING_RECOVERY_TOKEN',
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
    STAGING_BILLING_RECOVERY_TOKEN: 'billing-recovery-token',
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
        dependencies: { compressor: { status: 'ready' } },
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
      status = options.headers.authorization ? 400 : 401
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
  assert.equal(calls.length, 15)
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
  assert.equal(manual.length, 2)
  assert.ok(manual.every(({ options }) => options.body === '{}'))
  await assert.rejects(() => runStagingSmoke({ ...environment, STAGING_REPOSITORY_FORK: 'true' }, fakeFetch))
  const degradedFetch = async (url, options) => {
    const response = await fakeFetch(url, options)
    if (new URL(url).pathname !== '/v1/health/ready') return response
    return Response.json({
      accepting_traffic: true,
      database_ready: true,
      redis_ready: true,
      dependencies: { compressor: { status: 'unavailable' } },
    })
  }
  await assert.rejects(() => runStagingSmoke(environment, degradedFetch), /compressor/)
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
