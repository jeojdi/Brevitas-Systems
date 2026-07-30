-- Complete the browser-role privilege contract for the four surfaces
-- 202607280015 did not cover.
--
-- 202607280015 enumerated only the fifteen tables named by the 202607220001
-- service_role contract, so four browser-reachable surfaces kept Supabase's
-- project-default GRANT ALL for anon/authenticated. By that migration's own
-- rationale ("RLS bypass does not bypass PostgreSQL table privileges", and
-- TRUNCATE is not subject to RLS at all) each one is a hole:
--
--   * public.audit_events -- RLS enabled with ZERO policies, i.e. reads are
--     blocked but TRUNCATE is not, and the append-only audit history is the one
--     table whose destruction is unrecoverable evidence loss. Its identity
--     sequence (audit_events_id_seq) carried the same defaults.
--   * public.organization_invitations -- RLS enabled, zero policies, never
--     revoked. Holds token digests; browser roles have no legitimate access
--     ("Delivery is an application concern", 202607170005:66-67).
--   * public.legal_acceptances -- carries a real "view own" SELECT policy, so it
--     follows the profiles pattern: SELECT stays for the browser roles (the RLS
--     policy is what narrows it), every write/TRUNCATE privilege goes.
--   * schema audit_evidence_archive -- 202607170005:900 revokes the SCHEMA, but
--     any relation inside it (the archive function creates
--     audit_evidence_archive.company_admin_audit lazily with LIKE ... INCLUDING
--     ALL) was never covered by a table-level revoke. Schema USAGE is the outer
--     wall; this adds the inner one so a future schema grant alone cannot expose
--     archived audit evidence.
--
-- The executable contract from 202607280015,
-- public.assert_browser_role_table_privileges(), is widened to cover all four,
-- so a re-run of Supabase's project bootstrap (or a stray GRANT) is detected by
-- the same assertion suite that already proves the function non-vacuous.
--
-- No caller breaks: browser-side code performs no direct table access (zero
-- PostgREST .from() table calls in dashboard/src/), and every server read of
-- these tables uses the service key or a definer RPC.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- re-granting the browser-role project defaults reopens the audit/invitation/legal-evidence exposure this migration closes

begin;

do $migration_precondition$
begin
    if to_regclass('public.audit_events') is null
       or to_regclass('public.organization_invitations') is null
       or to_regclass('public.legal_acceptances') is null
       or to_regclass('public.audit_events_id_seq') is null
       or to_regprocedure('public.assert_browser_role_table_privileges()') is null then
        raise exception using
            errcode = '55000',
            message = '202607280024 requires 202607170005, 20260714 and 202607280015 first';
    end if;
    if not exists (
        select 1 from pg_namespace namespace
         where namespace.nspname = 'audit_evidence_archive'
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280024 requires the audit_evidence_archive schema from 202607170005';
    end if;
end;
$migration_precondition$;

-- Service-owned, RLS with zero policies: zero browser privileges, like the
-- fifteen tables 202607280015 already covers.
revoke all on table public.audit_events from public, anon, authenticated;
revoke all on table public.organization_invitations from public, anon, authenticated;
revoke all on sequence public.audit_events_id_seq from public, anon, authenticated;

-- The profiles pattern: the "Users can view own legal acceptance" SELECT policy
-- narrows reads, so SELECT stays; every write path is server-side (the
-- record_legal_acceptance definer trigger), so nothing else does.
revoke insert, update, delete, truncate, references, trigger
    on table public.legal_acceptances from public, anon, authenticated;

-- Belt and braces around the archive: re-assert the schema revoke from
-- 202607170005 and strip table-level privileges from any relation that exists
-- inside it now. Relations the archive function creates LATER are covered by
-- the widened assertion below, which the CI suite runs on every chain.
revoke all on schema audit_evidence_archive from public, anon, authenticated;
revoke all on all tables in schema audit_evidence_archive from public, anon, authenticated;

-- The widened executable contract. Everything from 202607280015 is retained
-- verbatim; the four surfaces above are added.
create or replace function public.assert_browser_role_table_privileges()
returns void
language plpgsql
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    contract record;
    grantee text;
    privilege text;
    archived record;
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
              ('billing_ledger', array[]::text[]),
              -- 202607280024: the four surfaces 202607280015 missed.
              ('audit_events', array[]::text[]),
              ('organization_invitations', array[]::text[]),
              -- Read-only behind "Users can view own legal acceptance".
              ('legal_acceptances', array['SELECT']::text[])
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
            if has_sequence_privilege(
                grantee, 'public.audit_events_id_seq', privilege
            ) then
                raise exception
                    'unsafe % privilege for public.audit_events_id_seq: %',
                    grantee, privilege
                    using errcode = '55000';
            end if;
        end loop;
        -- The archive schema is browser-invisible in BOTH layers: no schema
        -- USAGE/CREATE, and no table privilege on any relation inside it --
        -- including relations created after this migration ran.
        foreach privilege in array array['USAGE', 'CREATE'] loop
            if has_schema_privilege(
                grantee, 'audit_evidence_archive', privilege
            ) then
                raise exception
                    'unsafe % privilege for schema audit_evidence_archive: %',
                    grantee, privilege
                    using errcode = '55000';
            end if;
        end loop;
        for archived in
            select relation.oid, relation.relname
              from pg_class relation
              join pg_namespace namespace
                on namespace.oid = relation.relnamespace
             where namespace.nspname = 'audit_evidence_archive'
               and relation.relkind in ('r', 'p', 'v', 'm', 'f')
        loop
            foreach privilege in array array[
                'SELECT','INSERT','UPDATE','DELETE',
                'TRUNCATE','REFERENCES','TRIGGER'
            ] loop
                if has_table_privilege(grantee, archived.oid, privilege) then
                    raise exception
                        'unsafe % privilege for audit_evidence_archive.%: %',
                        grantee, archived.relname, privilege
                        using errcode = '55000';
                end if;
            end loop;
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
    -- Same rule for legal_acceptances: its tolerated SELECT privilege is only
    -- safe while the sole policy is the read-own SELECT policy.
    if exists (
        select 1
          from pg_policy policy
         where policy.polrelid = 'public.legal_acceptances'::regclass
           and policy.polcmd <> 'r'
    ) then
        raise exception 'public.legal_acceptances gained a non-SELECT policy'
            using errcode = '55000';
    end if;
end;
$function$;

revoke all on function public.assert_browser_role_table_privileges()
    from public, anon, authenticated, service_role;

select public.assert_browser_role_table_privileges();

commit;
