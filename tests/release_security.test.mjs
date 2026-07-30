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

import {
  ALLOW_MISSING_CREDENTIALS_FLAG,
  runSchemaDriftCheck,
} from '../scripts/ci/check-schema-drift.mjs'
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
    for (const action of uses) {
      // Same-repo reusable workflows (uses: ./…) are referenced by path and are
      // immutable by construction (pinned to the caller's commit); they cannot be
      // SHA-pinned. Only third-party actions must carry an immutable @<sha> ref.
      if (action.startsWith('./') || action.startsWith('../')) continue
      assert.match(action, shaRef, `${path}: ${action}`)
    }
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
  const trivyDigest = /aquasec\/trivy@sha256:[0-9a-f]{64}/g
  assert.equal((workflow.match(trivyDigest) || []).length, 3)
  assert.equal((workflow.match(/image --input \/tmp\/image\.tar/g) || []).length, 2)
  assert.equal((workflow.match(/--exit-code 1/g) || []).length, 2)
  assert.equal((workflow.match(/--severity HIGH,CRITICAL/g) || []).length, 2)
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

  // The suite CI runs must be the graph the image ships. python-test.in is
  // `-r python-runtime.in` + pytest, so the test lock is a strict superset of the
  // runtime lock at identical versions; it had drifted by 15 packages (the whole
  // supabase/postgrest/realtime/pyjwt stack) plus a websockets major version.
  const pins = (path) => new Map(
    read(path)
      .split(/\r?\n/)
      .map((line) => line.match(/^([a-z0-9][a-z0-9._-]*)==([^\s\\]+)/i))
      .filter(Boolean)
      .map(([, name, version]) => [name.toLowerCase().replace(/_/g, '-'), version]),
  )
  const runtimePins = pins('scripts/ci/python-runtime.lock')
  const testPins = pins('scripts/ci/python-test.lock')
  assert.deepEqual(
    [...runtimePins].filter(([name, version]) => testPins.get(name) !== version),
    [],
    'scripts/ci/python-test.lock does not contain every python-runtime.lock pin at the same version',
  )
  // Only that direction: the test lock legitimately adds pytest and its deps.
  assert.deepEqual(
    [...testPins.keys()].filter((name) => !runtimePins.has(name)).sort(),
    ['iniconfig', 'pluggy', 'pytest'],
    'the test lock gained or lost a test-only dependency',
  )
})

test('release schema-drift gate fails closed without credentials', () => {
  // A gate that exits 0 when unconfigured gates nothing. release.yml supplies
  // DATABASE_URL from `secrets.DATABASE_URL` under `environment: inputs.target`,
  // and a secret absent from that Environment interpolates to the empty string,
  // so the whole release chain used to go green with the deployed schema never
  // compared to migration-fresh-manifest.txt or to the money-column types.
  assert.throws(
    () => runSchemaDriftCheck({
      env: {},
      manifestText: 'supabase/migrations/20260611_create_user_keys.sql\n',
      query: () => assert.fail('the drift check must not connect without a DATABASE_URL'),
      logger: { log() {}, warn() {} },
    }),
    /DATABASE_URL is missing or empty/,
  )
  assert.throws(
    () => runSchemaDriftCheck({
      env: { DATABASE_URL: '   ' },
      manifestText: 'supabase/migrations/20260611_create_user_keys.sql\n',
      query: () => assert.fail('a whitespace-only DSN must not be treated as configured'),
      logger: { log() {}, warn() {} },
    }),
    /DATABASE_URL is missing or empty/,
  )
  // The credential-free local run still exists, but only when asked for.
  const messages = []
  assert.deepEqual(
    runSchemaDriftCheck({
      env: {},
      manifestText: 'supabase/migrations/20260611_create_user_keys.sql\n',
      query: () => assert.fail('the skip path must not connect'),
      logger: { log: (message) => messages.push(message), warn() {} },
      allowMissingCredentials: true,
    }),
    { skipped: true },
  )
  assert.match(messages.join('\n'), /must never be used on a release path/)

  const source = read('scripts/ci/check-schema-drift.mjs')
  assert.equal(ALLOW_MISSING_CREDENTIALS_FLAG, '--allow-missing-credentials')
  // The escape hatch is argv-only on purpose: no environment variable can enable
  // it, so no CI environment can turn the gate off by accident.
  assert.doesNotMatch(source, /env\.[A-Z_]*ALLOW_MISSING/)
  assert.doesNotMatch(source, /ALLOW_MISSING[A-Z_]*\]?\s*(?:=|:)\s*(?:env|process\.env)/)

  const workflow = read('.github/workflows/release.yml')
  const invocation = workflow.match(/node scripts\/ci\/check-schema-drift\.mjs.*/)
  assert.ok(invocation, 'release.yml no longer runs the schema-drift gate')
  assert.equal(
    invocation[0].trim(),
    'node scripts/ci/check-schema-drift.mjs',
    'the release path must invoke the drift gate bare, with no credential-free opt-out',
  )
})

test('CI reproduces Supabase default grants so revoke assertions can fail', () => {
  const bootstrap = read('scripts/ci/migration-bootstrap.sql')
  // Without these, a relation created in public starts with ZERO privileges for
  // anon/authenticated under test and ALL privileges for them in a real project,
  // which makes every `revoke all on table ... from public, anon, authenticated`
  // in the chain a no-op and every revoke assertion in the suite vacuous.
  assert.match(bootstrap, /grant usage on schema public to anon, authenticated, service_role;/)
  for (const objects of ['tables', 'sequences', 'functions']) {
    assert.match(
      bootstrap,
      new RegExp(
        `alter default privileges in schema public\\s*\\n\\s*grant all on ${objects} to anon, authenticated, service_role;`,
      ),
      `migration-bootstrap.sql does not grant Supabase's default privileges on ${objects}`,
    )
  }
  // ALTER DEFAULT PRIVILEGES attaches to the executing role; the harness applies
  // the bootstrap and every migration over one DATABASE_URL, so FOR ROLE would
  // silently pin the grants to the wrong creator.
  assert.doesNotMatch(bootstrap, /alter default privileges\s+for role/i)

  const baseline = read('scripts/ci/migration-browser-privilege-baseline-assertions.sql')
  assert.match(baseline, /create table public\.brevitas_privilege_baseline_probe/)
  assert.match(baseline, /every revoke assertion in this suite is vacuous/)
  assert.match(baseline, /browser-reachable inventory of schema public changed/)
  assert.match(baseline, /^rollback;$/m)

  const runner = read('scripts/ci/run-migration-tests.sh')
  const forwardStart = runner.indexOf('run_forward_assertions() {')
  const forwardEnd = runner.indexOf('\n}', forwardStart)
  assert.ok(forwardStart > 0 && forwardEnd > forwardStart, 'run_forward_assertions is unparseable')
  const forward = runner.slice(forwardStart, forwardEnd)
  assert.ok(
    forward.includes('migration-browser-privilege-baseline-assertions.sql'),
    'the privilege baseline is not asserted on either migration path',
  )
  assert.ok(
    forward.indexOf('migration-browser-privilege-baseline-assertions.sql') <
      forward.indexOf('migration-browser-role-isolation-assertions.sql'),
    'the baseline must be proven before the assertions that depend on it',
  )
})

test('the schema is forward-only and the harness says what it actually proves', () => {
  const runner = read('scripts/ci/run-migration-tests.sh')
  // The 24 failure-injection probes were named assert_atomic_migration_rollback,
  // which reads as a reverse-path test. It is not one: it injects `select 1/0;`
  // before COMMIT and proves a FAILED apply leaves no partial state. No migration
  // in supabase/migrations/ has a down file and none is required to.
  assert.doesNotMatch(runner, /assert_atomic_migration_rollback/)
  assert.match(runner, /^assert_failed_apply_is_atomic\(\) \{$/m)
  assert.match(runner, /FORWARD-ONLY SCHEMA CHANGE CONTRACT/)
  assert.match(runner, /PITR to the pre-cutover\n# +checkpoint/)
  // The three genuine down-then-up legs, each pinned on both sides.
  for (const reverse of [
    'api/migrations/004_database_scaling.rollback.sql',
    'scripts/ci/migration-cache-rollback.sql',
    'scripts/ci/migration-receipt-accounting-rollback.sql',
  ]) assert.ok(runner.includes(reverse), `the harness lost its ${reverse} leg`)

  // Every migration from the cutoff onward must DECLARE a reverse posture. The
  // cutoff is the next unused number: back-dating it would change the frozen
  // checksum of an already-applied migration, and per MEMORY
  // "production-schema-drift" production has no ledger to re-baseline against.
  const verifier = read('scripts/ci/verify-migrations.mjs')
  const [, cutoff] = verifier.match(/REVERSE_POSTURE_CUTOFF = '(\d+)'/) ?? []
  assert.ok(cutoff, 'verify-migrations.mjs lost its reverse-posture cutoff')
  const governed = expectedFreshMigrationOrder.filter(
    (path) => path.slice('supabase/migrations/'.length, 'supabase/migrations/'.length + cutoff.length) >= cutoff,
  )
  for (const path of governed) {
    const declarations = read(path)
      .split(/\r?\n/)
      .filter((line) => /^\s*--\s*REVERSE:/.test(line))
    assert.equal(declarations.length, 1, `${path} must declare exactly one reverse posture`)
  }
  const lastExisting = expectedFreshMigrationOrder
    .at(-1)
    .slice('supabase/migrations/'.length, 'supabase/migrations/'.length + cutoff.length)
  assert.ok(
    cutoff > lastExisting,
    `the reverse-posture cutoff ${cutoff} must stay ahead of the applied chain (head ${lastExisting})`,
  )
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
  // What follows is an independent, human-reviewed statement of the release migration
  // chain. verify-migrations.mjs checks its own expected order against the filesystem, so
  // a migration deleted or renamed in BOTH places would still pass there; re-stating the
  // contract here means such a silent drop fails the release gate. Every assertion is
  // either derived from the filesystem or anchored FORWARD from a fixed migration, never
  // counted back from the end, so appending a migration does not shift any of them.
  const migrationInventory = readdirSync(resolve(root, 'supabase/migrations'))
    .filter((name) => name.endsWith('.sql'))
    .map((name) => `supabase/migrations/${name}`)
    .sort()
  assert.deepEqual(
    expectedFreshMigrationOrder,
    migrationInventory,
    'the fresh order must be exactly the on-disk forward migrations, in lexicographic (deployment) order',
  )
  assert.equal(
    new Set(expectedFreshMigrationOrder).size,
    expectedFreshMigrationOrder.length,
    'a forward migration is listed more than once',
  )
  assert.deepEqual(
    expectedFreshMigrationOrder.slice(-expectedUpgradeMigrationOrder.length),
    expectedUpgradeMigrationOrder,
    'the upgrade order must remain the exact tail of the fresh order',
  )
  // The chain is append-only, so its length may grow but never shrink. This floor catches
  // a dropped or renamed migration without needing an edit for every legitimate addition:
  // ADDING A MIGRATION KEEPS THIS GREEN. Raise it only when deliberately re-baselining the
  // release chain, and extend releaseChain below in the same commit.
  assert.ok(
    expectedFreshMigrationOrder.length >= 53,
    `the release contract lists ${expectedFreshMigrationOrder.length} forward migrations, ` +
      'but the chain is append-only and has had at least 53: one was dropped or renamed. ' +
      'Restore it rather than lowering this floor.',
  )
  // The pre-upgrade production baseline is frozen history: nothing is ever inserted
  // before it, and nothing in it carries a frozen checksum, so pin it by identity.
  assert.deepEqual(
    expectedFreshMigrationOrder.slice(
      0,
      expectedFreshMigrationOrder.length - expectedUpgradeMigrationOrder.length,
    ),
    [
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
    ],
    'the production baseline preceding the upgrade chain was altered',
  )
  // Exact identity and order of every hardened migration from the security baseline
  // through the current head. Migrations added later append after this chain and are
  // still covered by the inventory equality above.
  const releaseChain = [
    ...securitySuffix,
    'supabase/migrations/20260720_split_savings_metrics.sql',
    'supabase/migrations/202607220001_service_role_data_plane.sql',
    'supabase/migrations/202607220002_supabase_advisor_hardening.sql',
    'supabase/migrations/202607270001_bvx_device_auth_expiry_index.sql',
    'supabase/migrations/202607270002_widen_billing_events_money.sql',
    'supabase/migrations/202607280001_cache_warming.sql',
    'supabase/migrations/202607280002_usage_stats_cache_metrics.sql',
    'supabase/migrations/202607280003_multi_provider_warming.sql',
    'supabase/migrations/202607280004_onboarding_local_proxy_evidence.sql',
    'supabase/migrations/202607280005_installation_on_device_activation.sql',
  ]
  for (const [label, order] of [
    ['fresh', expectedFreshMigrationOrder],
    ['upgrade', expectedUpgradeMigrationOrder],
  ]) {
    const start = order.indexOf(releaseChain[0])
    assert.ok(start > 0, `${label} order lost the 202607200001 security baseline`)
    assert.equal(
      order[start - 1],
      'supabase/migrations/202607170013_active_company_selection.sql',
      `${label} order must enter the security baseline directly after 202607170013`,
    )
    assert.deepEqual(
      order.slice(start, start + releaseChain.length),
      releaseChain,
      `${label} order dropped, renamed, or reordered a hardened release migration`,
    )
  }
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
  // The harness guards the manifests by relationship (non-empty; fresh >= upgrade),
  // not an absolute count that goes stale on every legitimate migration addition.
  // The exact set/order/checksums are enforced by verify-migrations.mjs.
  assert.match(runner, /Migration manifests must be non-empty/)
  assert.match(runner, /fresh = baseline \+ upgrade/)
  assert.match(runner, /migration-supabase-advisor-hardening-assertions\.sql/)
  assert.match(runner, /migration-onboarding-local-proxy-assertions\.sql/)
  // Idempotence used to be pinned as "exactly three literal `apply_migration
  // "${handle}"` call sites per migration". That counted the hand-written index
  // list, and it stopped being a coverage statement the moment the harness began
  // driving the whole manifest: a migration is now applied because it is IN the
  // manifest, not because someone remembered to write its name three times.
  // (That is precisely how 202607280005-202607280009 came to be registered and
  // never applied.) Pin the mechanism the loop uses instead, and let the U5
  // coverage test below own "every entry is applied".
  //
  // Upgrade path: every entry applied once, and twice at or after the idempotence
  // boundary, which is DERIVED from the manifest so a new migration is in the
  // double-apply set by default and a non-idempotent addition fails the harness.
  assert.match(
    runner,
    /idempotence_boundary_index="\$\(manifest_index "\$\{membership_migration\}"\)"/,
  )
  assert.match(
    runner,
    /if \(\(index >= idempotence_boundary_index\)\); then\n\s*apply_migration "\$\{migration\}"/,
  )
  // Fresh path: the whole chain, with the frozen 010-013 + 200001-220002 window
  // replayed in the middle and the remaining tail applied after it, so the replay
  // cannot land in a post-202607280006 schema and cannot clobber a later body.
  assert.match(
    runner,
    /fresh_replay_start="\$\(fresh_manifest_index 'supabase\/migrations\/202607170010_device_delivery_idempotency\.sql'\)"/,
  )
  assert.match(
    runner,
    /fresh_replay_end="\$\(fresh_manifest_index 'supabase\/migrations\/202607220002_supabase_advisor_hardening\.sql'\)"/,
  )
  assert.match(runner, /Fresh manifest does not order the frozen replay window before a remaining tail/)
  for (const bounds of [
    /for \(\(index = 0; index <= fresh_replay_end; index\+\+\)\)/,
    /for \(\(index = fresh_replay_start; index <= fresh_replay_end; index\+\+\)\)/,
    /for \(\(index = fresh_replay_end \+ 1; index < \$\{#fresh_migrations\[@\]\}; index\+\+\)\)/,
  ]) assert.match(runner, bounds)
  // The three billing-identity migrations must never be applied directly: they
  // are reachable only through the guarded maintenance procedure that owns their
  // quiesce and deployed-version preconditions.
  for (const variable of [
    'stripe_ordering_migration',
    'initial_service_key_migration',
    'company_billing_migration',
  ]) {
    assert.equal(
      (runner.match(new RegExp(`apply_migration "\\$\\{${variable}\\}"`, 'g')) || []).length,
      0,
      `${variable} must be applied only by the guarded maintenance runner`,
    )
  }
  const frozenChecksums = read('scripts/ci/migration-frozen-checksums.txt')
  assert.match(
    frozenChecksums,
    /a1ec546eed185128b545093a0ea3de6567ca0629bf157995d2012b4a620b3f62  supabase\/migrations\/202607170007_compliance_workflows\.sql/,
  )
  // Refrozen twice. First when the DR compliance assertions gained direct
  // billing_ledger fixtures: 202607280006 retired queue_brevitas_fee_after_usage,
  // so the four ledger rows those subject/customer/tenant erasure suites require
  // are now seeded explicitly instead of by the per-row trigger. Second for
  // 202607280026, which replaces the 20260710 usage dedupe index with an
  // authority-scoped one: an ON CONFLICT arbiter names an index by its exact
  // column list, so the four usage fixtures in this file had to name
  // (key_hash, request_id, authoritative) or fail to plan at all. The change is
  // mechanical and the file's evidence-preservation assertions are untouched.
  assert.match(
    frozenChecksums,
    /90f285f0174c811eab69b35a7fc609987bdd787e377f45491e6b0c8661098b8b  scripts\/dr\/compliance-workflow-assertions\.sql/,
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

// U5. 202607280005-202607280009 sat in BOTH manifests for a whole release while
// the upgrade path never applied a single one of them: the harness bound a fixed
// list of `${upgrade_migrations[N]}` indexes that stopped at index 39, so every
// later entry was skipped by omission and the per-row fee trigger it retires was
// still attached at the end of the upgrade run. Registering a migration must not
// be able to leave it unexercised again, so derive the covered index set from the
// harness source and require it to be the WHOLE manifest. This is deliberately a
// static check: it fails in CI's cheap tier, before any database exists, and it
// fails CLOSED — an entry no code path can be shown to apply is a failure, not a
// migration to skip. The harness carries the runtime twin of this assertion
// (`Upgrade path never applied ...`), which is required below.
test('every upgrade manifest entry is applied by the migration harness', () => {
  const runner = read('scripts/ci/run-migration-tests.sh')
  const manifest = read('scripts/ci/migration-upgrade-manifest.txt')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
  assert.deepEqual(
    manifest,
    expectedUpgradeMigrationOrder,
    'the upgrade manifest and the release contract disagree',
  )
  const indexOfPath = new Map(manifest.map((path, index) => [path, index]))

  // Named handles: `foo_migration="$(manifest_entry 'supabase/migrations/....sql')"`.
  // These resolve BY FILENAME inside the harness, so a handle is a manifest entry.
  const pathForHandle = new Map(
    [...runner.matchAll(/^(\w+)="\$\(manifest_entry '([^']+)'\)"$/gm)]
      .map(([, handle, path]) => [handle, path]),
  )
  for (const [handle, path] of pathForHandle) {
    assert.ok(
      indexOfPath.has(path),
      `harness handle ${handle} resolves ${path}, which is not an upgrade manifest entry`,
    )
  }

  const covered = new Set()
  const cover = (path, why) => {
    assert.ok(indexOfPath.has(path), `${why} applies ${path}, which is not in the manifest`)
    covered.add(indexOfPath.get(path))
  }
  // (a) literal `apply_migration supabase/migrations/....sql` paths.
  for (const [, path] of runner.matchAll(
    /apply_migration\s+"?(supabase\/migrations\/[^"\s]+\.sql)"?/g,
  )) cover(path, 'a literal apply_migration')
  // (b) `apply_migration "${handle}"` for a manifest-resolved handle.
  for (const [, handle] of runner.matchAll(/apply_migration\s+"\$\{(\w+)\}"/g)) {
    if (pathForHandle.has(handle)) cover(pathForHandle.get(handle), `apply_migration ${handle}`)
  }
  // (c) explicit index bindings, the shape that caused the original defect.
  for (const [, index] of runner.matchAll(/\$\{upgrade_migrations\[(\d+)\]\}/g)) {
    const position = Number(index)
    assert.ok(
      position < manifest.length,
      `the harness binds upgrade_migrations[${position}], past the end of the manifest`,
    )
    covered.add(position)
  }

  // (d) The whole-array driver loop. Recognised only in its exact shape, so this
  // cannot become a rubber stamp: it must iterate the entire array, bind the
  // element to `migration`, and apply that element. Entries diverted by a
  // non-default arm of its apply switch are NOT credited to the loop.
  assert.match(
    runner,
    /for \(\(index = 0; index < \$\{#upgrade_migrations\[@\]\}; index\+\+\)\); do\n\s*migration="\$\{upgrade_migrations\[\$\{index\}\]\}"/,
    'the harness must drive the entire upgrade manifest, not a hand-bound index list',
  )
  const applyCall = runner.indexOf('apply_migration "${migration}"')
  assert.ok(applyCall > 0, 'the driver loop never applies the manifest entry it selected')
  const switchStart = runner.lastIndexOf('case "${migration}" in', applyCall)
  const switchEnd = runner.indexOf('\n  esac', applyCall)
  assert.ok(switchStart > 0 && switchEnd > switchStart, 'the apply switch is unparseable')
  const applySwitch = runner.slice(switchStart, switchEnd)
  assert.match(
    applySwitch,
    /\*\)\n\s*apply_migration "\$\{migration\}"\n\s*record_upgrade_applied "\$\{migration\}"/,
    'the default arm must apply and record the selected manifest entry',
  )
  const divertedHandles = [...applySwitch.matchAll(/^\s*("\$\{\w+\}"(?:\|"\$\{\w+\}")*)\)$/gm)]
    .flatMap(([, patterns]) => [...patterns.matchAll(/\$\{(\w+)\}/g)].map(([, handle]) => handle))
  const diverted = new Set()
  for (const handle of divertedHandles) {
    assert.ok(pathForHandle.has(handle), `the apply switch diverts unresolved handle ${handle}`)
    diverted.add(pathForHandle.get(handle))
  }
  for (const [path, index] of indexOfPath) {
    if (!diverted.has(path)) covered.add(index)
  }
  // A diverted entry is only covered if something else demonstrably applies it.
  // Here that is the guarded billing-identity maintenance procedure, which owns
  // 200004-200006's quiesce and deployed-version preconditions; each one must be
  // recorded as applied so the harness's runtime coverage guard sees it too.
  assert.deepEqual(
    [...diverted].sort(),
    [...BILLING_IDENTITY_MIGRATIONS].sort(),
    'only the guarded billing-identity migrations may bypass the driver loop',
  )
  assert.match(applySwitch, /run_billing_identity_maintenance/)
  for (const path of BILLING_IDENTITY_MIGRATIONS) {
    const handle = [...pathForHandle].find(([, candidate]) => candidate === path)?.[0]
    assert.ok(handle, `${path} has no manifest-resolved harness handle`)
    assert.ok(
      applySwitch.includes(`record_upgrade_applied "\${${handle}}"`),
      `${path} bypasses the driver loop without being recorded as applied`,
    )
    covered.add(indexOfPath.get(path))
  }

  assert.deepEqual(
    manifest.filter((_, index) => !covered.has(index)),
    [],
    'these upgrade manifest entries are registered but never applied by the harness',
  )
  assert.deepEqual(
    [...covered].sort((left, right) => left - right),
    manifest.map((_, index) => index),
    'the covered index set must be exactly {0..len-1}',
  )

  // The runtime twin: even if the static analysis above is ever fooled, the
  // harness itself must abort naming any entry the run did not apply.
  assert.match(runner, /Upgrade path never applied \$\{upgrade_migrations\[\$\{index\}\]\}\./)
  assert.match(runner, /^\s*record_upgrade_applied\(\) \{$/m)
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
  // Atomicity probes are no longer a literal list of call sites. The driver loop
  // asks upgrade_rollback_invariant() for a pre-COMMIT invariant per migration
  // and probes every migration that declares one, so a raw call-site count now
  // measures the refactor rather than the coverage. Pin the INVENTORY of probed
  // migrations by name instead: that is the property the old count of 24 stood
  // for, it does not go stale when the loop is rewritten, and it fails closed if
  // a money migration's invariant is deleted.
  const invariantStart = runner.indexOf('upgrade_rollback_invariant() {')
  const invariantEnd = runner.indexOf('applied_upgrade_migrations=()')
  assert.ok(
    invariantStart > 0 && invariantEnd > invariantStart,
    'the harness lost upgrade_rollback_invariant()',
  )
  const invariantBody = runner.slice(invariantStart, invariantEnd)
  assert.deepEqual(
    [...invariantBody.matchAll(/^\s*"\$\{(\w+)\}"\)$/gm)].map(([, name]) => name),
    [
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
      'workspace_experiences_migration',
      'split_savings_migration',
      'service_role_data_plane_migration',
      'supabase_advisor_hardening_migration',
      'cache_warming_migration',
      'multi_provider_warming_migration',
      'onboarding_evidence_migration',
      // The money path added by the billing-correctness phases. Each of these
      // must keep a pre-COMMIT invariant: a rolled-back retirement must leave
      // the per-row fee trigger attached, and a rolled-back settlement schema
      // must leave no ledger, guard, or writer half-created.
      'retire_fee_trigger_migration',
      'period_settlement_migration',
      'halting_conditions_migration',
      'billing_attestation_migration',
      'period_settlement_latch_migration',
      'unsent_release_migration',
      'evidence_warm_days_migration',
      'settlement_writer_migration',
      // 202607280024 is a privilege migration: a half-applied one would widen
      // the executable browser-role contract without performing the revokes it
      // claims, i.e. report a safety it never established. 202607280025-026 are
      // an admission-control function and a uniqueness swap on the billable
      // usage table; the dedupe swap in particular must never leave an instant
      // with no unique index at all.
      'browser_privilege_completion_migration',
      'waitlist_network_budget_migration',
      'usage_dedupe_authority_migration',
    ],
    'a migration lost (or silently gained) its pre-COMMIT rollback invariant',
  )
  // Every declared invariant must actually be probed, and only through the
  // generic driver call plus the three guarded billing-identity reruns.
  assert.match(
    runner,
    /upgrade_probe="\$\(upgrade_rollback_invariant "\$\{migration\}"\)"/,
  )
  assert.match(
    runner,
    /assert_failed_apply_is_atomic "\$\{migration\}" "\$\{upgrade_probe\}"/,
  )
  assert.deepEqual(
    [...new Set(
      [...runner.matchAll(/assert_failed_apply_is_atomic "\$\{(\w+)\}"/g)]
        .map(([, name]) => name),
    )],
    [
      'migration',
      'stripe_ordering_migration',
      'initial_service_key_migration',
      'company_billing_migration',
    ],
    'atomicity probes must run through the driver loop plus the guarded billing-identity reruns',
  )
  assert.match(runner, /print "select 1\/0;"/)
  assert.match(runner, /Failure-injected migration left partial state/)
  assert.match(runner, /Applying the guarded 200004-200006 billing maintenance procedure immediately after 200003/)
  // Two invocations (the second proves completed company-scoped state is
  // validated and skipped), plus the definition. The call sites moved inside the
  // driver loop's case arm, so they are indented now.
  assert.equal((runner.match(/^[ \t]+run_billing_identity_maintenance$/gm) || []).length, 2)
  assert.equal((runner.match(/^run_billing_identity_maintenance\(\) \{$/gm) || []).length, 1)
  // "Immediately after 200003 and before 200007" used to be checked as the source
  // order of three literal call sites. Those call sites are gone: the harness
  // dispatches the guarded procedure from a case arm inside the manifest-driven
  // loop, so the ordering is now a property of the MANIFEST plus that dispatch.
  // Restate it that way — the old form silently passed on indexOf(...) === -1
  // once the call sites disappeared, which is exactly how a stale ordering
  // assertion becomes vacuous.
  assert.deepEqual(
    expectedUpgradeMigrationOrder.slice(
      expectedUpgradeMigrationOrder.indexOf(
        'supabase/migrations/202607200003_billing_owner_attribution.sql',
      ),
      expectedUpgradeMigrationOrder.indexOf(
        'supabase/migrations/202607200003_billing_owner_attribution.sql',
      ) + 5,
    ),
    [
      'supabase/migrations/202607200003_billing_owner_attribution.sql',
      'supabase/migrations/202607200004_stripe_event_ordering.sql',
      'supabase/migrations/202607200005_initial_service_key.sql',
      'supabase/migrations/202607200006_company_billing_authorization.sql',
      'supabase/migrations/202607200007_billing_recovery_scope.sql',
    ],
    'the guarded billing-identity window must sit between 200003 and 200007',
  )
  const guardedArm = runner.slice(
    runner.indexOf('"${stripe_ordering_migration}")'),
    runner.indexOf('"${initial_service_key_migration}"|"${company_billing_migration}")'),
  )
  assert.ok(guardedArm.length > 0, 'the harness lost the guarded billing-identity case arm')
  assert.match(
    guardedArm,
    /Applying the guarded 200004-200006 billing maintenance procedure immediately after 200003/,
  )
  assert.ok(
    guardedArm.indexOf("echo 'Applying the guarded 200004-200006") <
      guardedArm.indexOf('run_billing_identity_maintenance'),
    'the guarded procedure must be announced before it runs',
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
    assertStagingTarget(
      'https://brevitas-api-staging-975273324573.us-west1.run.app',
      'api',
    ),
    'https://brevitas-api-staging-975273324573.us-west1.run.app',
  )
  for (const value of [
    'http://brevitas-api-staging-975273324573.us-west1.run.app',
    'https://api.brevitassystems.com',
    'https://localhost',
    'https://127.0.0.1',
    'https://brevitas-api-staging-975273324573.us-west1.run.app.evil.example',
    'https://brevitas-api-staging-975273324573.us-west1.run.app/path',
    'https://user:password@brevitas-api-staging-975273324573.us-west1.run.app',
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
    STAGING_API_URL: 'https://brevitas-api-staging-975273324573.us-west1.run.app',
    STAGING_DASHBOARD_URL: 'https://brevitas-systems-staging.vercel.app',
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
