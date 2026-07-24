-- Restore the explicit PostgREST data-plane contract for the hosted backend.
-- RLS bypass does not bypass PostgreSQL table privileges: every direct
-- service-role read/write below must be granted separately from RPC EXECUTE.

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
                '202607220001 requires public.%', required_table;
        end if;
        if not (
            select relation.relrowsecurity
              from pg_class relation
             where relation.oid = to_regclass(format('public.%I', required_table))
        ) then
            raise exception
                '202607220001 refuses to expose service data without RLS: public.%',
                required_table;
        end if;
    end loop;
end;
$migration_precondition$;

-- Revoke Supabase/project defaults first so service_role cannot retain
-- REFERENCES, TRIGGER, or TRUNCATE beyond its PostgREST DML contract.
revoke all on table public.organizations from service_role;
revoke all on table public.organization_members from service_role;
revoke all on table public.customers from service_role;
revoke all on table public.service_accounts from service_role;
revoke all on table public.bvx_device_auth from service_role;
revoke all on table public.api_keys from service_role;
revoke all on table public.installations from service_role;
revoke all on table public.devices from service_role;
revoke all on table public.key_repositories from service_role;
revoke all on table public.profiles from service_role;
revoke all on table public.usage_log from service_role;
revoke all on table public.provider_config from service_role;
revoke all on table public.ai_jobs from service_role;
revoke all on table public.billing_accounts from service_role;
revoke all on table public.billing_ledger from service_role;

grant select, update
    on table public.organizations to service_role;
grant select
    on table public.organization_members to service_role;
grant select, update
    on table public.customers to service_role;
grant select, insert
    on table public.service_accounts to service_role;
grant select, insert, delete
    on table public.bvx_device_auth to service_role;
grant select, update, delete
    on table public.api_keys to service_role;
grant select, update
    on table public.installations to service_role;
grant select
    on table public.devices to service_role;
grant select, insert, update
    on table public.key_repositories to service_role;
grant select
    on table public.profiles to service_role;
grant select, insert
    on table public.usage_log to service_role;
grant select, insert, update
    on table public.provider_config to service_role;
grant select, insert, update, delete
    on table public.ai_jobs to service_role;
grant select
    on table public.billing_accounts to service_role;
grant select
    on table public.billing_ledger to service_role;

-- usage_log is the only directly inserted table in this contract backed by an
-- identity sequence.
revoke all on sequence public.usage_log_id_seq from service_role;
grant usage, select on sequence public.usage_log_id_seq to service_role;

do $privilege_contract$
declare
    contract record;
    privilege text;
begin
    for contract in
        select *
          from (values
              ('organizations', array['SELECT','UPDATE']::text[]),
              ('organization_members', array['SELECT']::text[]),
              ('customers', array['SELECT','UPDATE']::text[]),
              ('service_accounts', array['SELECT','INSERT']::text[]),
              ('bvx_device_auth', array['SELECT','INSERT','DELETE']::text[]),
              ('api_keys', array['SELECT','UPDATE','DELETE']::text[]),
              ('installations', array['SELECT','UPDATE']::text[]),
              ('devices', array['SELECT']::text[]),
              ('key_repositories', array['SELECT','INSERT','UPDATE']::text[]),
              ('profiles', array['SELECT']::text[]),
              ('usage_log', array['SELECT','INSERT']::text[]),
              ('provider_config', array['SELECT','INSERT','UPDATE']::text[]),
              ('ai_jobs', array['SELECT','INSERT','UPDATE','DELETE']::text[]),
              ('billing_accounts', array['SELECT']::text[]),
              ('billing_ledger', array['SELECT']::text[])
          ) expected(table_name, allowed_privileges)
    loop
        foreach privilege in array array[
            'SELECT','INSERT','UPDATE','DELETE',
            'TRUNCATE','REFERENCES','TRIGGER'
        ] loop
            if has_table_privilege(
                'service_role',
                format('public.%I', contract.table_name),
                privilege
            ) <> (privilege = any(contract.allowed_privileges)) then
                raise exception
                    'unsafe service_role privilege contract for public.%: %',
                    contract.table_name, privilege;
            end if;
        end loop;
    end loop;
    if not has_sequence_privilege(
        'service_role', 'public.usage_log_id_seq', 'USAGE'
    ) or not has_sequence_privilege(
        'service_role', 'public.usage_log_id_seq', 'SELECT'
    ) or has_sequence_privilege(
        'service_role', 'public.usage_log_id_seq', 'UPDATE'
    ) then
        raise exception 'unsafe service_role usage_log sequence contract';
    end if;
end;
$privilege_contract$;

commit;
