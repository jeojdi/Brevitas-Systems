import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('..', import.meta.url))
const read = path => readFileSync(resolve(root, path), 'utf8')

test('billing authorization resolves only the actor server-owned active company', () => {
  const helper = read('src/lib/billing/supabase.ts')
  const migration = read('supabase/migrations/202607200006_company_billing_authorization.sql')
  const authorizationHelper = helper.slice(
    helper.indexOf('export async function authorizeActiveBillingCompany'),
    helper.indexOf('export async function getBillingAccount'),
  )

  assert.match(authorizationHelper, /company_billing_authorize_actor/)
  assert.match(authorizationHelper, /p_actor_user_id: actorUserId/)
  assert.doesNotMatch(authorizationHelper, /p_(?:requested_)?organization_id/)
  assert.match(migration, /company_admin_resolve_active_membership\(p_actor_user_id\)/)
  assert.match(migration, /member\.organization_id=v_organization_id/)
  assert.match(migration, /member\.status='active'/)
  assert.match(migration, /'billing:manage'=any\(public\.company_role_permissions\(v_role\)\)/)
})

test('owners and billing admins are allowed while company admins and members are denied', () => {
  const migration = read('supabase/migrations/202607200006_company_billing_authorization.sql')
  assert.match(migration, /company_role_permissions\('company_owner'\)/)
  assert.match(migration, /company_role_permissions\('billing_admin'\)/)
  assert.match(migration, /'billing:manage'=any\(public\.company_role_permissions\('company_admin'\)\)/)
  assert.match(migration, /'billing:manage'=any\(public\.company_role_permissions\('member'\)\)/)
})

test('billing accounts and ledger remain isolated for multiple companies with one owner', () => {
  const helper = read('src/lib/billing/supabase.ts')
  const migration = read('supabase/migrations/202607200006_company_billing_authorization.sql')
  const customerPersistence = read(
    'supabase/migrations/202607200017_billing_customer_owner_fencing.sql',
  )

  assert.match(migration, /primary key \(organization_id\)/)
  assert.doesNotMatch(migration, /unique\s*\(user_id\)/)
  assert.match(customerPersistence, /on conflict \(organization_id\) do update/)
  assert.match(helper, /save_billing_customer_identity/)
  assert.match(helper, /\.eq\('organization_id', organizationId\)/)
  assert.match(migration, /billing_account\.organization_id=candidate\.organization_id/)
  assert.match(migration, /ledger\.organization_id=candidate\.organization_id/)
  assert.match(migration, /hashtextextended\(candidate\.organization_id::text,0\)/)
  assert.match(migration, /new\.organization_id is distinct from old\.organization_id/)
})

test('checkout portal and status authorize and query the active company identity', () => {
  const checkout = read('src/app/api/billing/checkout/route.ts')
  const portal = read('src/app/api/billing/portal/route.ts')
  const status = read('src/app/api/billing/status/route.ts')

  for (const route of [checkout, portal, status]) {
    assert.match(route, /authorizeActiveBillingCompany\(user\.id\)/)
    assert.match(route, /Billing permission is required for the active company/)
  }
  assert.match(checkout, /getBillingAccount\(organizationId\)/)
  assert.match(checkout, /saveBillingCustomerIdentity\(organizationId, customerId\)/)
  assert.match(checkout, /persistBillingCheckoutSession\(\{[\s\S]+organizationId,[\s\S]+generation,[\s\S]+reservationToken,[\s\S]+checkoutSessionId: session\.id/)
  assert.doesNotMatch(checkout, /saveBillingCheckoutSessionIdentity/)
  assert.match(checkout, /brevitas_organization_id: organizationId/)
  assert.match(checkout, /client_reference_id: organizationId/)
  assert.doesNotMatch(checkout, /brevitas_user_id|client_reference_id: user\.id/)
  assert.match(portal, /getBillingAccount\(authorization\.organizationId\)/)
  assert.match(status, /\.eq\('organization_id', authorization\.organizationId\)/)
  assert.doesNotMatch(status, /\.eq\('user_id', user\.id\)/)
})

test('Stripe webhooks validate organization metadata and mutate by company', () => {
  const route = read('src/app/api/billing/webhook/route.ts')
  const helper = read('src/lib/billing/canonical-persistence.ts')
  const migration = read('supabase/migrations/202607200006_company_billing_authorization.sql')

  assert.match(route, /session\.metadata\?\.brevitas_organization_id/)
  assert.match(route, /subscription\.metadata\?\.brevitas_organization_id/)
  assert.match(route, /metadataOrganizationId !== account\.organization_id/)
  assert.match(route, /getBillingAccount\(metadataOrganizationId\)/)
  assert.match(route, /accountForCustomer\(customerId\)/)
  assert.match(route, /compareAndSetSubscriptionSnapshot\(/)
  assert.match(route, /compareAndSetInvoiceSnapshot\(/)
  assert.match(helper, /p_organization_id: organizationId/)
  assert.match(migration, /where account\.organization_id=p_organization_id/g)
})

test('legacy billing rows are never guessed across multiple owned companies', () => {
  const migration = read('supabase/migrations/202607200006_company_billing_authorization.sql')
  assert.match(migration, /organization\.legacy_owner_id=account\.user_id::text/)
  assert.match(migration, /and 1=\([\s\S]+count\(\*\)[\s\S]+organization\.billing_owner_id=account\.user_id/)
  assert.match(migration, /legacy billing account has no unambiguous company identity/)
  assert.match(migration, /retained billing ledger row has no authoritative company identity/)
})

test('dedicated database fixture exercises roles, switching, webhooks, and ledger isolation', () => {
  const fixture = read('scripts/ci/migration-company-billing-assertions.sql')
  assert.match(fixture, /count\(\*\) from public\.billing_accounts[\s\S]+<>2/)
  assert.match(fixture, /company_billing_authorize_actor/g)
  assert.match(fixture, /company admin gained billing permission/)
  assert.match(fixture, /ordinary member gained billing permission/)
  assert.match(fixture, /compare_and_set_stripe_subscription_snapshot/g)
  assert.match(fixture, /stale Stripe reconciliation snapshot won a lost CAS/)
  assert.match(fixture, /Stripe subscription events crossed company accounts/)
  assert.match(fixture, /billing ledger did not preserve company isolation/)
  const billingInsert = fixture.slice(
    fixture.indexOf('insert into public.billing_accounts'),
    fixture.indexOf('do $$'),
  )
  assert.match(billingInsert, /on conflict \(organization_id\)/)
  assert.doesNotMatch(billingInsert, /on conflict \(user_id\)/)
})
