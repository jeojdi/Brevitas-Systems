import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const sql = readFileSync(new URL('../supabase/migrations/202607170001_enterprise_tenancy.sql', import.meta.url), 'utf8')
const server = readFileSync(new URL('../api/server.py', import.meta.url), 'utf8')
const dashboard = readFileSync(new URL('../dashboard/src/lib/supabase.js', import.meta.url), 'utf8')
const administration = readFileSync(new URL('../supabase/migrations/202607170005_company_administration.sql', import.meta.url), 'utf8')
const companyApi = readFileSync(new URL('../api/company_admin.py', import.meta.url), 'utf8')
const companyRoute = readFileSync(new URL('../src/app/api/admin/[...path]/route.ts', import.meta.url), 'utf8')
const operationsAdmin = readFileSync(new URL('../dashboard/src/components/Admin.jsx', import.meta.url), 'utf8')
const companyAdminUi = readFileSync(new URL('../dashboard/src/components/CompanyAdministration.jsx', import.meta.url), 'utf8')
const dashboardApp = readFileSync(new URL('../dashboard/src/App.jsx', import.meta.url), 'utf8')
const adminProxy = readFileSync(new URL('../src/lib/admin/proxy.ts', import.meta.url), 'utf8')

test('enterprise customers are subordinate identities and never own API keys', () => {
  assert.match(sql, /unique \(organization_id, external_id\)/)
  assert.match(sql, /organization_service/)
  assert.doesNotMatch(sql, /customer_id uuid[^;]*api_keys/s)
  assert.match(server, /customer:route/)
  assert.match(server, /customer:auto_provision/)
})

test('billing accepts only server-authoritative usage', () => {
  assert.match(sql, /authoritative boolean not null default false/)
  assert.match(sql, /if not new\.authoritative/)
  assert.match(server, /authoritative=False/)
  assert.match(server, /authoritative=True/)
})

test('raw keys are one-time secrets and are not stored in the browser database', () => {
  assert.match(sql, /drop table if exists public\.user_keys/)
  assert.doesNotMatch(dashboard, /\.from\(['"]user_keys['"]\)/)
  assert.match(server, /secret_available_once/)
})

test('tenant cache state defaults off and is credential-derived', () => {
  assert.match(sql, /cache_enabled boolean not null default false/)
  assert.match(server, /request\.state\.brevitas_cache_enabled/)
  assert.match(server, /auth_context\.organization_id, auth_context\.customer_id/)
})

test('company roles are explicit and privileged mutations are database-authorized', () => {
  for (const role of ['company_owner', 'company_admin', 'member', 'billing_admin']) {
    assert.match(administration, new RegExp(`'${role}'`))
    assert.match(companyApi, new RegExp(`"${role}"`))
  }
  assert.match(administration, /lock_company_actor_role/)
  assert.match(administration, /for update/)
  assert.match(administration, /last_owner/)
  assert.match(administration, /p_invitee_lookup_hash/)
  assert.match(administration, /existing_member/)
  assert.match(administration, /lock_company_admin_namespace/)
  assert.match(administration, /for update/)
})

test('Postgres company caps lock the tenant before stale cleanup and counting', () => {
  const invite = administration.slice(
    administration.indexOf('create or replace function public.company_admin_invite_member'),
    administration.indexOf('create or replace function public.company_admin_cancel_invitation'),
  )
  assert.ok(invite.indexOf('lock_company_admin_namespace') < invite.indexOf("set status='expired'"))
  assert.ok(invite.indexOf("set status='expired'") < invite.indexOf('select count(*)'))
  const service = administration.slice(
    administration.indexOf('create or replace function public.company_admin_create_service_account'),
    administration.indexOf('create or replace function public.company_admin_rotate_service_key'),
  )
  assert.ok(service.indexOf('lock_company_admin_namespace') < service.indexOf('select count(*)'))
})

test('audit evidence is append-only beyond RLS and carries only content-free fields', () => {
  assert.match(administration, /before update or delete on public\.audit_events/)
  assert.match(administration, /before truncate on public\.audit_events/)
  assert.match(administration, /including all/)
  assert.match(administration, /audit_evidence_archive/)
  assert.match(administration, /actor_key_hash is not null/)
  assert.match(administration, /new\.actor_role not in/)
  assert.match(administration, /new\.target_id ~\* '\^\[0-9a-f\]\{64\}\$'/)
  assert.match(companyApi, /admin_audit_committed/)
  const appendFunction = administration.slice(
    administration.indexOf('create or replace function public.append_company_audit'),
    administration.indexOf('create or replace function public.company_role_permissions'),
  )
  assert.doesNotMatch(appendFunction, /(?:email|name|body|secret)/i)
})

test('Next admin BFF awaits route params and does not trust browser company or role fields', () => {
  assert.match(companyRoute, /params: Promise/)
  assert.match(companyRoute, /await context\.params/)
  assert.doesNotMatch(companyRoute, /company[_-]?id|actor[_-]?role/i)
})

test('company lists authorize and keyset-page in one RPC transaction', () => {
  for (const name of ['members', 'invitations', 'service_accounts', 'audit']) {
    assert.match(administration, new RegExp(`company_admin_${name}_page`))
  }
  assert.match(companyApi, /rpc="company_admin_members_page"/)
  assert.doesNotMatch(companyApi, /_store\._request\("GET", table/)
})

test('service key authorization joins live key and account expiry', () => {
  assert.match(administration, /service_key_authorization/)
  assert.match(administration, /credential\.expires_at<=account\.expires_at/)
  assert.match(administration, /account\.status='active'/)
  assert.match(companyApi, /def service_account_key_context/)
})

test('admin BFF bounds upstream bodies and secures every local error response', () => {
  assert.match(adminProxy, /ADMIN_RESPONSE_MAX_BYTES/)
  assert.match(adminProxy, /response\.headers\.get\('content-length'\)/)
  assert.match(adminProxy, /reader\.cancel\(\)/)
  assert.match(adminProxy, /'Cache-Control': 'private, no-store'/)
  assert.match(adminProxy, /'X-Content-Type-Options': 'nosniff'/)
  assert.match(companyAdminUi, /redactBrowserError/)
})

test('dashboard pagination treats cursors as opaque and resets cursor stacks', () => {
  assert.doesNotMatch(operationsAdmin, /\boffset\b/)
  assert.match(operationsAdmin, /params\.set\('cursor', cursor\)/)
  assert.match(operationsAdmin, /setCursorStack\(\[\]\)/)
  assert.match(companyAdminUi, /next_cursor/)
  assert.doesNotMatch(companyAdminUi, /(?:atob|fromBase64|JSON\.parse\(.*cursor)/s)
})

test('dashboard clears in-memory raw credentials on auth loss and sign out', () => {
  assert.match(dashboardApp, /if \(!session\) \{\s*clearSessionKeyCache\(\)/)
  const signOut = dashboardApp.slice(dashboardApp.indexOf('const signOut'), dashboardApp.indexOf('const activateApiKey'))
  assert.match(signOut, /clearSessionKeyCache\(\)/)
  assert.match(signOut, /setApiKey\(''\)/)
  assert.match(dashboardApp, /credentialUserId\.current !== nextUserId/)
  assert.match(dashboardApp, /CompanyAdministration key=\{`\$\{session\.user\.id\}:\$\{companyContext\.activeCompanyId\}`\}/)
  assert.match(companyAdminUi, /setInvitationSecret\(''\)/)
  assert.match(companyAdminUi, /setServiceSecret\(''\)/)
})
