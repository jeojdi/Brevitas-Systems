import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { parseBillingRecoveryAdmission } from '../src/lib/billing/recovery-admission.mjs'
import {
  recoverySecretAuthorized,
  recoverySecretIsStrong,
} from '../src/lib/billing/recovery-auth.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const migrationPath = 'supabase/migrations/202607200010_shared_endpoint_rate_limits.sql'
const strongSecret = 'Bv9_Qx2Lm7-Rp4Tz8Nc5Hs3Wk6Yf1Da0'

test('recovery secret configuration rejects weak and Unicode-confusable values', () => {
  assert.equal(recoverySecretIsStrong(strongSecret), true)
  assert.equal(recoverySecretAuthorized(strongSecret, strongSecret), true)
  for (const weak of [
    null,
    '',
    'too-short_A9',
    'a'.repeat(32),
    'Ab1_'.repeat(8),
    `${strongSecret.slice(0, -1)}𝟘`,
    `${strongSecret.slice(0, -1)}é`,
  ]) {
    assert.equal(recoverySecretIsStrong(weak), false)
    assert.equal(recoverySecretAuthorized(weak, strongSecret), false)
    assert.equal(recoverySecretAuthorized(strongSecret, weak), false)
  }
  assert.equal(
    recoverySecretAuthorized(`${strongSecret.slice(0, -1)}1`, strongSecret),
    false,
  )

  const helper = read('src/lib/billing/recovery-auth.mjs')
  const config = read('src/lib/billing/config.ts')
  assert.match(helper, /createHash\('sha256'\)/)
  assert.match(helper, /timingSafeEqual\(expectedDigest, suppliedDigest\)/)
  assert.match(helper, /MINIMUM_SECRET_BYTES = 32/)
  assert.match(helper, /byteLength !== candidate\.length/)
  assert.doesNotMatch(helper, /console\./)
  assert.match(config, /recoverySecretIsStrong\(config\.recoverySecret\)/)
  assert.doesNotMatch(config, /CRON_SECRET/)
})

test('billing recovery admission parsing is bounded and fails closed', () => {
  assert.deepEqual(
    parseBillingRecoveryAdmission({ ok: true, code: 'accepted' }),
    { status: 'accepted' },
  )
  assert.deepEqual(
    parseBillingRecoveryAdmission({
      ok: false,
      code: 'rate_limited',
      retry_after_seconds: 61,
    }),
    { status: 'rate_limited', retryAfterSeconds: 61 },
  )
  for (const invalid of [
    null,
    true,
    {},
    { ok: false, code: 'rate_limited', retry_after_seconds: '30' },
    { ok: false, code: 'rate_limited', retry_after_seconds: 0 },
    { ok: false, code: 'rate_limited', retry_after_seconds: 901 },
    { ok: true, code: 'unexpected' },
  ]) {
    assert.throws(
      () => parseBillingRecoveryAdmission(invalid),
      /Invalid billing recovery admission result/,
    )
  }
})

test('shared database counters bound billing recovery globally and per actor-company', () => {
  const migration = read(migrationPath)
  const helper = read('src/lib/billing/supabase.ts')

  assert.match(migration,
    /create or replace function public\.consume_billing_recovery_attempt\([\s\S]+p_actor_user_id uuid[\s\S]+p_organization_id uuid/)
  assert.match(migration, /v_global_window interval := interval '1 minute'/)
  assert.match(migration, /v_global_limit integer := 60/)
  assert.match(migration, /v_actor_company_window interval := interval '15 minutes'/)
  assert.match(migration, /v_actor_company_limit integer := 5/)
  assert.match(migration, /digest\([\s\S]+p_actor_user_id::text[\s\S]+p_organization_id::text[\s\S]+'sha256'/)
  assert.match(migration, /pg_advisory_xact_lock/g)
  assert.match(migration, /'billing_recovery\.global'/)
  assert.match(migration, /'billing_recovery\.actor_company'/)
  assert.match(migration,
    /revoke all on function public\.consume_billing_recovery_attempt\(uuid, uuid\)[\s\S]+from public, anon, authenticated, service_role/)
  assert.match(migration,
    /grant execute on function public\.consume_billing_recovery_attempt\(uuid, uuid\)[\s\S]+to service_role/)
  assert.doesNotMatch(migration,
    /grant execute on function public\.consume_billing_recovery_attempt[\s\S]+to (?:anon|authenticated)/i)
  assert.match(helper, /rpc\([\s\S]+['"]consume_billing_recovery_attempt['"]/)
  assert.match(helper, /parseBillingRecoveryAdmission\(data\)/)
  assert.match(helper, /throw new BillingRecoveryAdmissionError\(\)/)
})

test('route authenticates and authorizes before shared admission and secret comparison', () => {
  const route = read('src/app/api/billing/sync/route.ts')
  const authIndex = route.indexOf('authenticatedBillingUser(request)')
  const authorizationIndex = route.indexOf('authorizeActiveBillingCompany(user.id)')
  const admissionIndex = route.indexOf('consumeBillingRecoveryAttempt(')
  const secretIndex = route.indexOf("request.headers.get('x-billing-recovery-secret')")
  const mutationIndex = route.indexOf('manuallyResolveBillingLedgerEntry({')

  assert.ok(authIndex >= 0 && authIndex < authorizationIndex)
  assert.ok(authorizationIndex < admissionIndex)
  assert.ok(admissionIndex < secretIndex)
  assert.ok(secretIndex < mutationIndex)
  assert.match(route, /admission\.status === 'rate_limited'[\s\S]+status: 429/)
  assert.match(route, /'Retry-After': String\(admission\.retryAfterSeconds\)/)
  assert.match(route, /BillingRecoveryAdmissionError[\s\S]+status: 503/)
  assert.match(route, /recoverySecretIsStrong\(recoverySecret\)/)
  assert.doesNotMatch(route, /withRateLimit|RATE_LIMITS|x-forwarded-for|x-real-ip|x-client-ip/i)
  assert.doesNotMatch(route, /console\.|CRON_SECRET/)
})

test('separate-session fixture proves cross-instance and brute-force ceilings', () => {
  const runner = read('scripts/ci/run-billing-recovery-shared-limit-test.sh')
  const assertions = read('scripts/ci/billing-recovery-shared-rate-limit-assertions.sql')

  assert.match(runner, /worker_pids=\(\)/)
  assert.match(runner, /accepted_count[\s\S]+-ne 5/)
  assert.match(runner, /limited_count[\s\S]+-ne 7/)
  assert.match(assertions, /for v_index in 1\.\.5 loop/)
  assert.match(assertions, /for v_index in 1\.\.60 loop/)
  assert.match(assertions, /limiter crossed company identities/)
  assert.match(assertions, /identity_hash like '%-%'/)
})
