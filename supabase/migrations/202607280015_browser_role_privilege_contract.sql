-- Close the browser-role half of the data-plane privilege contract.
--
-- 202607220001_service_role_data_plane.sql states the threat model exactly:
-- "RLS bypass does not bypass PostgreSQL table privileges" (line 2-3), and
-- "Revoke Supabase/project defaults first so service_role cannot retain
-- REFERENCES, TRIGGER, or TRUNCATE beyond its PostgREST DML contract" (line
-- 45-46). It then enumerates a byte-exact contract for service_role on fifteen
-- tables -- and never names anon or authenticated, which therefore keep
-- Supabase's project-default `grant all on tables`, TRUNCATE included. TRUNCATE
-- is not subject to row-level security at all, so `enable row level security`
-- with zero policies does not contain it. Tables created after that migration do
-- it correctly (202607280001_cache_warming.sql:102-104,
-- 202607280007_period_settlement_ledger.sql:232, which revoke from
-- "public, anon, authenticated, service_role"); these fifteen were simply missed.
--
-- public.profiles additionally hands every authenticated user unrestricted
-- UPDATE of their own row: "Users can update own profile" (20260624:18-20) has no
-- column list and no narrowing WITH CHECK, so a user can rewrite profiles.email,
-- which is the only account name the operator console has -- get_admin_key_inventory
-- (api/store.py:3957), get_admin_report_page (:4190), get_admin_account_detail
-- (:4228) and GET /v1/admin/billing (api/server.py:4693) all read it. The column
-- is written once by the handle_new_user() trigger from auth.users.email and is
-- never re-verified, so an operator-visible identity is user-controlled. Nothing
-- writes profiles except that definer trigger (service_role holds only SELECT,
-- 202607220001:81), so the policy is dropped rather than narrowed. The SELECT
-- policy stays, so `authenticated` keeps SELECT on profiles and nothing else.
--
-- No caller breaks: browser-side code performs no direct table access (zero
-- PostgREST .from() table calls in dashboard/src/, whose Supabase client is
-- auth-only), and the Next.js routes that read tables use the service key.
--
-- Forward-only and idempotent. REVOKE of a privilege that was never granted is a
-- no-op, so this is safe whether or not the roles ever held the project-default
-- grants -- and on the CI harness they do: migration-bootstrap.sql reproduces
-- Supabase's default ALTER DEFAULT PRIVILEGES baseline precisely so these
-- revokes have something real to remove under test.

-- REVERSE: PITR-ONLY -- re-granting the browser-role project defaults reopens the exposure this migration closes

begin;

do $migration_precondition$
declare
    required_table text;
begin
    foreach required_table in array array[
        'organizations',
        'organization_members',
        'customers',
        'service_accounts',
        'bvx_device_auth',
        'api_keys',
        'installations',
        'devices',
        'key_repositories',
        'profiles',
        'usage_log',
        'provider_config',
        'ai_jobs',
        'billing_accounts',
        'billing_ledger'
    ] loop
        if to_regclass(format('public.%I', required_table)) is null then
            raise exception
                '202607280015 requires public.%', required_table;
        end if;
    end loop;
end;
$migration_precondition$;

-- profiles.email is trigger-authored evidence, not a self-service field.
drop policy if exists "Users can update own profile" on public.profiles;

revoke insert, update, delete, truncate, references, trigger
    on table public.profiles from public, anon, authenticated;

-- Every other table in the 202607220001 contract is service-owned: it has RLS
-- enabled and deliberately zero policies ("No policies are intentionally created
-- for service-owned tables", 20260710_cloud_usage.sql:164-165), so no browser
-- role has any legitimate direct access to remove.
revoke all on table public.organizations from public, anon, authenticated;
revoke all on table public.organization_members from public, anon, authenticated;
revoke all on table public.customers from public, anon, authenticated;
revoke all on table public.service_accounts from public, anon, authenticated;
revoke all on table public.bvx_device_auth from public, anon, authenticated;
revoke all on table public.api_keys from public, anon, authenticated;
revoke all on table public.installations from public, anon, authenticated;
revoke all on table public.devices from public, anon, authenticated;
revoke all on table public.key_repositories from public, anon, authenticated;
revoke all on table public.usage_log from public, anon, authenticated;
revoke all on table public.provider_config from public, anon, authenticated;
revoke all on table public.ai_jobs from public, anon, authenticated;
revoke all on table public.billing_accounts from public, anon, authenticated;
revoke all on table public.billing_ledger from public, anon, authenticated;

-- 202607220001:92-94 already treats this sequence as part of the contract, but
-- again only for service_role.
revoke all on sequence public.usage_log_id_seq from public, anon, authenticated;

-- The contract as an executable assertion rather than a one-shot check, so a
-- future `grant all on all tables in schema public` (or a re-run of Supabase's
-- project bootstrap) is detectable instead of silent. migration-bootstrap.sql
-- reproduces Supabase's default-privilege baseline, so the has_table_privilege
-- reads below are live under CI: they fail if a revoke above is removed. It
-- still ships as a callable function so the assertion suite can additionally
-- grant a privilege inside a rolled-back transaction and require this to raise,
-- proving the check itself cannot pass by absence.
create or replace function public.assert_browser_role_table_privileges()
returns void
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    contract record;
    grantee text;
    privilege text;
begin
    for contract in
        select *
          from (values
              ('organizations', array[]::text[]),
              ('organization_members', array[]::text[]),
              ('customers', array[]::text[]),
              ('service_accounts', array[]::text[]),
              ('bvx_device_auth', array[]::text[]),
              ('api_keys', array[]::text[]),
              ('installations', array[]::text[]),
              ('devices', array[]::text[]),
              ('key_repositories', array[]::text[]),
              -- Read-only, and only what the "Users can view own profile"
              -- policy can narrow: no INSERT/UPDATE/DELETE for any browser role.
              ('profiles', array['SELECT']::text[]),
              ('usage_log', array[]::text[]),
              ('provider_config', array[]::text[]),
              ('ai_jobs', array[]::text[]),
              ('billing_accounts', array[]::text[]),
              ('billing_ledger', array[]::text[])
          ) expected(table_name, allowed_privileges)
    loop
        foreach grantee in array array['anon', 'authenticated'] loop
            foreach privilege in array array[
                'SELECT','INSERT','UPDATE','DELETE',
                'TRUNCATE','REFERENCES','TRIGGER'
            ] loop
                if has_table_privilege(
                    grantee,
                    format('public.%I', contract.table_name),
                    privilege
                ) and not privilege = any(contract.allowed_privileges) then
                    raise exception
                        'unsafe % privilege for public.%: %',
                        grantee, contract.table_name, privilege
                        using errcode = '55000';
                end if;
            end loop;
        end loop;
    end loop;
    foreach grantee in array array['anon', 'authenticated'] loop
        foreach privilege in array array['USAGE', 'SELECT', 'UPDATE'] loop
            if has_sequence_privilege(
                grantee, 'public.usage_log_id_seq', privilege
            ) then
                raise exception
                    'unsafe % privilege for public.usage_log_id_seq: %',
                    grantee, privilege
                    using errcode = '55000';
            end if;
        end loop;
    end loop;
    if exists (
        select 1
          from pg_policy policy
         where policy.polrelid = 'public.profiles'::regclass
           and policy.polcmd in ('w', '*')
    ) then
        raise exception 'public.profiles retains a self-service UPDATE policy'
            using errcode = '55000';
    end if;
end;
$function$;

revoke all on function public.assert_browser_role_table_privileges()
    from public, anon, authenticated, service_role;

select public.assert_browser_role_table_privileges();

commit;
