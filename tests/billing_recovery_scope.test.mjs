import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

test('manual recovery requires human identity before a separate recovery secret', () => {
  const route = read('src/app/api/billing/sync/route.ts')
  const config = read('src/lib/billing/config.ts')

  assert.ok(route.indexOf('authenticatedBillingUser(request)') <
    route.indexOf("request.headers.get('x-billing-recovery-secret')"))
  assert.ok(route.indexOf('authorizeActiveBillingCompany(user.id)') <
    route.indexOf("request.headers.get('x-billing-recovery-secret')"))
  assert.ok(route.indexOf('consumeBillingRecoveryAttempt(') <
    route.indexOf("request.headers.get('x-billing-recovery-secret')"))
  assert.match(route, /request\.headers\.get\('x-billing-recovery-secret'\)/)
  assert.match(route, /authorizeActiveBillingCompany\(user\.id\)/)
  assert.match(route, /Billing permission is required for the active company/)
  assert.doesNotMatch(route, /recoveryBearerAuthorized/)
  assert.match(config, /process\.env\.BILLING_RECOVERY_SECRET \|\| ''/)
  assert.match(config, /recoverySecretIsStrong\(config\.recoverySecret\)/)
  assert.doesNotMatch(config, /CRON_SECRET/)
})

test('database recovery reauthorizes canonical active company and tenant row', () => {
  const migration = read('supabase/migrations/202607200007_billing_recovery_scope.sql')
  const helper = read('src/lib/billing/supabase.ts')

  assert.match(migration, /company_admin_resolve_active_membership\([\s\S]+p_actor_user_id/)
  assert.match(migration, /member\.status='active'/)
  assert.match(migration, /'billing:manage'=any\(public\.company_role_permissions\(v_role\)\)/)
  assert.match(migration, /ledger\.organization_id=v_organization_id/)
  assert.match(migration, /v_organization_id<>p_expected_organization_id/)
  assert.doesNotMatch(migration, /p_(?:actor_)?role/)
  assert.match(helper, /p_actor_user_id: values\.actorUserId/)
  assert.match(helper, /p_expected_organization_id: values\.expectedOrganizationId/)
})

test('global recovery RPC is removed and scoped RPC is service-only', () => {
  const migration = read('supabase/migrations/202607200007_billing_recovery_scope.sql')

  assert.match(migration, /drop function if exists public\.manually_resolve_billing_ledger_entry\(\s*bigint,text,text/)
  assert.match(migration, /manually_resolve_billing_ledger_entry\(\s*uuid,uuid,bigint,text,text,text/)
  assert.match(migration, /from public, anon, authenticated/)
  assert.match(migration, /to service_role/)
})

test('manual recovery appends immutable actor company request note and outcome evidence', () => {
  const migration = read('supabase/migrations/202607200007_billing_recovery_scope.sql')

  for (const column of [
    'organization_id', 'actor_id', 'actor_role', 'request_id', 'ledger_entry_id',
    'requested_resolution', 'prior_status', 'outcome', 'result_code', 'note',
  ]) assert.match(migration, new RegExp(`\\b${column}\\b`))
  assert.match(migration, /billing_recovery_audit_reject_update_delete/)
  assert.match(migration, /billing_recovery_audit_reject_truncate/)
  assert.match(migration, /outcome text not null check \(outcome in \('committed','denied'\)\)/)
  assert.match(migration, /insert into public\.billing_recovery_audit/g)
})

test('safe retry semantics and SQL fixture cover cross-tenant and role denials', () => {
  const migration = read('supabase/migrations/202607200007_billing_recovery_scope.sql')
  const fixture = read('scripts/ci/migration-billing-recovery-scope-assertions.sql')

  assert.match(migration, /p_resolution='pending' then null/)
  assert.match(migration, /least\(10,greatest\(max_attempts,attempts\+1\)\)/)
  assert.match(migration, /v_prior_status not in \('review','dead'\)/)
  assert.match(fixture, /manual recovery crossed the active company boundary/)
  assert.match(fixture, /company admin gained manual billing recovery permission/)
  assert.match(fixture, /billing recovery audit update was allowed/)
  assert.match(fixture, /legacy unscoped manual recovery RPC still exists/)
})
