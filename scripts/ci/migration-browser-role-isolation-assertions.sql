\set ON_ERROR_STOP on

-- What a BROWSER role can actually reach. Two things had never been tested:
--
--   1. Table privileges for anon/authenticated. 202607220001 wrote a byte-exact
--      contract for service_role and never named the browser roles, so they kept
--      Supabase's project-default GRANT ALL on fifteen tenant tables.
--      202607280015 revokes that and installs
--      public.assert_browser_role_table_privileges() as the durable contract;
--      202607280024 widens both the revokes and the contract to audit_events,
--      organization_invitations, legal_acceptances and audit_evidence_archive.
--      migration-bootstrap.sql reproduces Supabase's default-privilege baseline,
--      so the contract's has_table_privilege() reads are live under CI -- and
--      this file additionally grants a privilege inside a savepoint and requires
--      the contract to raise, proving the check cannot pass by absence.
--
--   2. RLS behaviour. Across ~80 test files and 40+ SQL assertion files there was
--      not one SET ROLE or request.jwt.* fixture: every policy body that calls
--      auth.uid() was dead code under test, and the only assertions were
--      structural ("relrowsecurity is true"). Every table in the final schema
--      that still has a policy -- profiles, billing_events, legal_acceptances --
--      is exercised here as two real tenants. (public.user_keys also carried an
--      auth.uid() policy, but 202607170001_enterprise_tenancy.sql:220 drops the
--      table, so that policy is not part of any deployed schema.)
--
-- The grants below reproduce, for those three tables only, what Supabase's project
-- default grants give a live project; they are transactional and disappear with
-- the rollback, so the privilege baseline of every other assertion file is
-- untouched. Every role switch is followed by an explicit RESET ROLE: a leaked
-- SET LOCAL ROLE would silently run the rest of the file as a browser role.
begin;

-- ---------------------------------------------------------------------------
-- 1. The privilege contract, proven non-vacuous.
-- ---------------------------------------------------------------------------
select public.assert_browser_role_table_privileges();

savepoint before_browser_grant;
grant insert on table public.api_keys to anon;
do $$
declare
    v_raised boolean := false;
begin
    begin
        perform public.assert_browser_role_table_privileges();
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception
            'the browser-role privilege contract does not detect a new grant';
    end if;
end;
$$;
rollback to savepoint before_browser_grant;

-- The 202607280024 widening, proven non-vacuous the same way: TRUNCATE on the
-- audit history is the exact privilege RLS cannot contain, and a table inside
-- the archive schema must be unreachable even when the schema wall has a grant.
savepoint before_audit_grant;
grant truncate on table public.audit_events to authenticated;
do $$
declare
    v_raised boolean := false;
begin
    begin
        perform public.assert_browser_role_table_privileges();
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception
            'the browser-role privilege contract does not cover public.audit_events';
    end if;
end;
$$;
rollback to savepoint before_audit_grant;

savepoint before_archive_grant;
create table if not exists audit_evidence_archive.company_admin_audit
    (like public.audit_events including all);
grant select on table audit_evidence_archive.company_admin_audit to anon;
do $$
declare
    v_raised boolean := false;
begin
    begin
        perform public.assert_browser_role_table_privileges();
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception
            'the browser-role privilege contract does not cover audit_evidence_archive relations';
    end if;
end;
$$;
rollback to savepoint before_archive_grant;

savepoint before_profile_policy;
create policy "assertion self service profile" on public.profiles
    for update using (auth.uid() = id);
do $$
declare
    v_raised boolean := false;
begin
    begin
        perform public.assert_browser_role_table_privileges();
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception
            'a self-service profiles UPDATE policy is no longer detected';
    end if;
end;
$$;
rollback to savepoint before_profile_policy;

-- ---------------------------------------------------------------------------
-- 2. Structural view guard. A view over an RLS-protected base table that is not
--    security_invoker evaluates policies as its OWNER, which is how
--    public.billing_monthly leaked every tenant's billing aggregate to the anon
--    key. This is the check that generalises, and it is non-vacuous on plain
--    PostgreSQL: it reads reloptions, not grants.
-- ---------------------------------------------------------------------------
do $$
declare
    v_relation text;
begin
    if to_regclass('public.billing_monthly') is not null then
        raise exception 'the legacy definer view public.billing_monthly is back';
    end if;
    for v_relation in
        select relation.relname
          from pg_class relation
          join pg_namespace namespace on namespace.oid = relation.relnamespace
         where namespace.nspname = 'public'
           and relation.relkind in ('v', 'm')
           and not coalesce(
                array_to_string(relation.reloptions, ',') like '%security_invoker=true%',
                false)
    loop
        raise exception
            'public.% is a view without security_invoker: it evaluates base-table RLS as its owner',
            v_relation;
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. Real tenant isolation under RLS.
-- ---------------------------------------------------------------------------
insert into auth.users (id, email, raw_user_meta_data) values
    ('7c000000-0000-4000-8000-000000000001', 'rls-tenant-a@example.invalid',
     jsonb_build_object('accepted_terms_at', now()::text)),
    ('7c000000-0000-4000-8000-000000000002', 'rls-tenant-b@example.invalid',
     jsonb_build_object('accepted_terms_at', now()::text));

-- public.profiles and public.legal_acceptances rows are written by the
-- handle_new_user / record_legal_acceptance triggers on the inserts above.
insert into public.billing_events (user_id, session_id, provider, model) values
    ('7c000000-0000-4000-8000-000000000001', 'rls-a', 'test', 'test'),
    ('7c000000-0000-4000-8000-000000000002', 'rls-b', 'test', 'test');

do $$
begin
    if (select count(*) from public.legal_acceptances acceptance
         where acceptance.user_id in (
            '7c000000-0000-4000-8000-000000000001',
            '7c000000-0000-4000-8000-000000000002')
           and acceptance.terms_version
               = public.current_legal_versions()->>'terms_version'
           and acceptance.privacy_version
               = public.current_legal_versions()->>'privacy_version'
           and acceptance.accepted) <> 2 then
        raise exception
            'signup did not record server-pinned legal acceptance evidence';
    end if;
end;
$$;

grant select on table public.profiles to anon, authenticated;
grant select on table public.billing_events to authenticated;
grant select on table public.legal_acceptances to authenticated;

do $$
declare
    v_tenant uuid;
    v_other uuid;
    v_count bigint;
    v_updated bigint;
    v_denied boolean;
begin
    foreach v_tenant in array array[
        '7c000000-0000-4000-8000-000000000001'::uuid,
        '7c000000-0000-4000-8000-000000000002'::uuid
    ] loop
        v_other := case when v_tenant = '7c000000-0000-4000-8000-000000000001'
            then '7c000000-0000-4000-8000-000000000002'::uuid
            else '7c000000-0000-4000-8000-000000000001'::uuid end;
        set local role authenticated;
        perform set_config('request.jwt.claim.sub', v_tenant::text, true);
        perform set_config(
            'request.jwt.claims',
            json_build_object('sub', v_tenant::text)::text, true);

        select count(*) into v_count from public.profiles profile
         where profile.id in (v_tenant, v_other);
        if v_count <> 1 or not exists (
            select 1 from public.profiles profile where profile.id = v_tenant
        ) then
            raise exception 'profiles RLS exposed % rows to tenant %',
                v_count, v_tenant;
        end if;

        select count(*) into v_count from public.billing_events event
         where event.user_id in (v_tenant, v_other);
        if v_count <> 1 then
            raise exception 'billing_events RLS exposed % rows to tenant %',
                v_count, v_tenant;
        end if;

        select count(*) into v_count from public.legal_acceptances acceptance
         where acceptance.user_id in (v_tenant, v_other);
        if v_count <> 1 then
            raise exception 'legal_acceptances RLS exposed % rows to tenant %',
                v_count, v_tenant;
        end if;

        -- profiles.email is the only account name the operator console has, and
        -- before 202607280015 every user could rewrite their own row. The write
        -- side is now closed by privilege, so a self-UPDATE must be refused
        -- outright -- and a cross-tenant UPDATE must reach nothing either way.
        v_denied := false;
        begin
            update public.profiles profile set email = 'spoofed@example.invalid'
             where profile.id = v_tenant;
            get diagnostics v_updated = row_count;
            if v_updated <> 0 then
                raise exception
                    'authenticated rewrote its own profiles.email';
            end if;
        exception
            when insufficient_privilege then v_denied := true;
            when others then raise;
        end;
        if not v_denied then
            raise exception
                'public.profiles still grants UPDATE to authenticated';
        end if;

        -- Service-owned tables have RLS with zero policies and, after
        -- 202607280015, no browser privilege at all: both layers must refuse.
        v_denied := false;
        begin
            select count(*) into v_count from public.api_keys;
        exception when others then v_denied := true;
        end;
        if not v_denied and v_count <> 0 then
            raise exception 'public.api_keys is readable by authenticated';
        end if;

        reset role;
    end loop;

    -- Anonymous callers hold SELECT on profiles in a live project (the "view own
    -- profile" policy is what narrows it), so this must be a policy result, not
    -- a privilege error: zero rows, no exception.
    set local role anon;
    perform set_config('request.jwt.claim.sub', '', true);
    perform set_config('request.jwt.claims', '', true);
    select count(*) into v_count from public.profiles;
    reset role;
    if v_count <> 0 then
        raise exception 'profiles RLS exposed % rows to anon', v_count;
    end if;
end;
$$;

do $$
begin
    if current_user is distinct from session_user then
        raise exception 'a role switch leaked out of the isolation fixture';
    end if;
end;
$$;

rollback;
