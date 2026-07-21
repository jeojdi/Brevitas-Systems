import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')
const migrationPath = 'supabase/migrations/202607200017_billing_customer_owner_fencing.sql'

test('Checkout does not carry a billing-owner snapshot into customer persistence', () => {
  const route = read('src/app/api/billing/checkout/route.ts')
  const helper = read('src/lib/billing/supabase.ts')
  const persistence = helper.slice(
    helper.indexOf('export async function saveBillingCustomerIdentity'),
    helper.indexOf('function checkoutRpcRecord'),
  )

  assert.match(route, /saveBillingCustomerIdentity\(organizationId, customerId\)/)
  assert.doesNotMatch(route, /const billingOwnerId = authorization\.billingOwnerId/)
  assert.match(persistence, /billingDatabase\(\)\.rpc\(\s*'save_billing_customer_identity'/)
  assert.match(persistence, /p_organization_id: organizationId/)
  assert.match(persistence, /p_stripe_customer_id: stripeCustomerId/)
  assert.doesNotMatch(persistence, /billingOwnerId|\.from\('billing_accounts'\)|\.upsert\(/)
})

test('database persistence locks and derives the active current billing owner', () => {
  const migration = read(migrationPath)

  assert.match(migration, /select organization\.billing_owner_id[\s\S]+for update of organization, member/)
  assert.match(migration, /member\.user_id = organization\.billing_owner_id/)
  assert.match(migration, /member\.status = 'active'/)
  assert.match(migration, /values \([\s\S]+p_organization_id,[\s\S]+v_billing_owner_id,[\s\S]+p_stripe_customer_id/)
  assert.doesNotMatch(migration, /p_(?:billing_)?owner_id|p_user_id/)
  assert.match(migration, /on conflict \(organization_id\) do update/)
  assert.match(migration, /account\.stripe_customer_id is null[\s\S]+account\.stripe_customer_id = excluded\.stripe_customer_id/)
  assert.match(migration, /revoke all on function public\.save_billing_customer_identity\(uuid, text\)/)
  assert.match(migration, /from public, anon, authenticated, service_role/)
  assert.match(migration, /grant execute on function public\.save_billing_customer_identity\(uuid, text\)[\s\S]+to service_role/)
})

test('PostgreSQL fixture covers transfer, conflict, inactivity, ledger, and grants', () => {
  const fixture = read('scripts/ci/migration-billing-customer-owner-fencing-assertions.sql')

  assert.match(fixture, /stale Checkout attribution overwrote the current billing owner/)
  assert.match(fixture, /customer identity persistence changed organization ledger state/)
  assert.match(fixture, /different Stripe customer identity was overwritten/)
  assert.match(fixture, /inactive billing owner was accepted for persistence/)
  assert.match(fixture, /billing customer persistence privilege boundary is invalid/)
})

test('independent PostgreSQL sessions prove owner-transfer lock serialization', () => {
  const runner = read('scripts/ci/run-billing-owner-transfer-race-test.sh')
  const assertions = read('scripts/ci/billing-owner-transfer-race-assertions.sql')

  assert.match(runner, /pg_advisory_lock\(170017\)/)
  assert.match(runner, /set lock_timeout='750ms'/)
  assert.match(runner, /Owner transfer bypassed the customer-persistence organization lock/)
  assert.match(runner, /canceling statement due to lock timeout/)
  assert.match(assertions, /serialized owner transfer did not win final attribution/)
  assert.match(assertions, /owner-transfer persistence race changed company ledger state/)
})
