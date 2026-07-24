import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { parseBillingControlAdmission } from '../src/lib/billing/control-admission.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const migrationPath = 'supabase/migrations/202607200013_billing_control_rate_limits.sql'

test('billing control admission parsing is narrow and bounded', () => {
  assert.deepEqual(
    parseBillingControlAdmission({ ok: true, code: 'accepted' }),
    { status: 'accepted' },
  )
  assert.deepEqual(
    parseBillingControlAdmission({
      ok: false,
      code: 'rate_limited',
      retry_after_seconds: 300,
    }),
    { status: 'rate_limited', retryAfterSeconds: 300 },
  )
  for (const invalid of [
    null,
    true,
    {},
    { ok: false, code: 'rate_limited', retry_after_seconds: '30' },
    { ok: false, code: 'rate_limited', retry_after_seconds: 0 },
    { ok: false, code: 'rate_limited', retry_after_seconds: 301 },
    { ok: true, code: 'unexpected' },
  ]) {
    assert.throws(
      () => parseBillingControlAdmission(invalid),
      /Invalid billing control admission result/,
    )
  }
})

test('shared database limiter uses verified actor company and operation identities', () => {
  const migration = read(migrationPath)
  const helper = read('src/lib/billing/supabase.ts')

  assert.match(migration,
    /consume_billing_control_attempt\([\s\S]+p_actor_user_id uuid[\s\S]+p_organization_id uuid[\s\S]+p_operation text/)
  assert.match(migration, /p_operation is null[\s\S]+v_operation not in \('checkout', 'portal'\)/)
  assert.match(migration, /v_global_window interval := interval '1 minute'/)
  assert.match(migration, /v_global_limit integer := 120/)
  assert.match(migration, /v_identity_window := interval '5 minutes'[\s\S]+v_identity_limit := 5/)
  assert.match(migration, /v_identity_window := interval '1 minute'[\s\S]+v_identity_limit := 30/)
  assert.match(migration,
    /digest\([\s\S]+p_actor_user_id::text[\s\S]+p_organization_id::text[\s\S]+v_operation[\s\S]+'sha256'/)
  assert.match(migration, /pg_advisory_xact_lock/g)
  assert.match(migration, /'billing_control\.global'/)
  assert.match(migration, /'billing_control\.' \|\| v_operation \|\| '\.actor_company'/)
  assert.match(migration,
    /revoke all on function public\.consume_billing_control_attempt\(uuid, uuid, text\)[\s\S]+from public, anon, authenticated, service_role/)
  assert.match(migration,
    /grant execute on function public\.consume_billing_control_attempt\(uuid, uuid, text\)[\s\S]+to service_role/)
  assert.doesNotMatch(migration,
    /grant execute on function public\.consume_billing_control_attempt[\s\S]+to (?:anon|authenticated)/i)
  assert.match(helper, /rpc\([\s\S]+['"]consume_billing_control_attempt['"]/)
  assert.match(helper, /p_actor_user_id: actorUserId/)
  assert.match(helper, /p_organization_id: organizationId/)
  assert.match(helper, /p_operation: operation/)
  assert.match(helper, /parseBillingControlAdmission\(data\)/)
  assert.match(helper, /throw new BillingControlAdmissionError\(\)/)
})

test('checkout and portal authorize before shared admission and external billing work', () => {
  const controls = [
    {
      operation: 'checkout',
      route: read('src/app/api/billing/checkout/route.ts'),
      firstExternalWork: ['billingIsConfigured()', 'getStripe()', 'getBillingAccount(organizationId)'],
    },
    {
      operation: 'portal',
      route: read('src/app/api/billing/portal/route.ts'),
      firstExternalWork: ['getBillingAccount(authorization.organizationId)', 'getStripe()'],
    },
  ]

  for (const control of controls) {
    const maintenanceIndex = control.route.indexOf('billingMaintenanceResponse()')
    const authIndex = control.route.indexOf('authenticatedBillingUser(request)')
    const authorizationIndex = control.route.indexOf('authorizeActiveBillingCompany(user.id)')
    const admissionIndex = control.route.indexOf('consumeBillingControlAttempt(')
    const operationIndex = control.route.indexOf(`'${control.operation}'`, admissionIndex)

    assert.ok(maintenanceIndex >= 0 && maintenanceIndex < authIndex)
    assert.ok(authIndex < authorizationIndex)
    assert.ok(authorizationIndex < admissionIndex)
    assert.ok(admissionIndex < operationIndex)
    for (const externalWork of control.firstExternalWork) {
      assert.ok(
        admissionIndex < control.route.indexOf(externalWork),
        `${control.operation} admission must precede ${externalWork}`,
      )
    }
    assert.match(control.route, /admission\.status === 'rate_limited'[\s\S]+status: 429/)
    assert.match(control.route, /'Retry-After': String\(admission\.retryAfterSeconds\)/)
    assert.match(control.route, /BillingControlAdmissionError[\s\S]+status: 503/)
    assert.match(control.route, /status: 503[\s\S]+Cache-Control': 'no-store'[\s\S]+Retry-After': '5'/)
    assert.doesNotMatch(
      control.route,
      /withRateLimit|RATE_LIMITS|rate-limiter|x-forwarded-for|x-real-ip|x-client-ip|\bforwarded\b/i,
    )
  }
})

test('PostgreSQL fixtures prove successful counting and shared partitions', () => {
  const assertions = read('scripts/ci/billing-control-shared-rate-limit-assertions.sql')
  const runner = read('scripts/ci/run-billing-control-shared-limit-test.sh')

  assert.match(assertions, /for v_index in 1\.\.5 loop/)
  assert.match(assertions, /request_count[\s\S]+<> 5/)
  assert.match(assertions, /crossed company identities/)
  assert.match(assertions, /crossed operation identities/)
  assert.match(assertions, /for v_index in 1\.\.120 loop/)
  assert.match(assertions, /identity_hash !~ '\^\[0-9a-f\]\{64\}\$'/)
  assert.match(assertions, /has_function_privilege/)
  assert.match(runner, /worker_pids=\(\)/)
  assert.match(runner, /accepted_count[\s\S]+-ne 5/)
  assert.match(runner, /limited_count[\s\S]+-ne 7/)
  assert.match(runner, /inet_server_addr\(\)/)
  assert.match(runner, /127\.0\.0\.1\|::1/)
})
