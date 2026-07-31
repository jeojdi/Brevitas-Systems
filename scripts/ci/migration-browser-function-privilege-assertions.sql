\set ON_ERROR_STOP on

-- 202607280032: no browser role may EXECUTE a SECURITY DEFINER function in
-- schema public, now or after the next CREATE FUNCTION.
--
-- Production (wyfz, 2026-07-30) had 99 SECURITY DEFINER functions in public
-- executable by BOTH anon and authenticated, 61 of the reachable ones writers,
-- none of them checking auth.uid(). A fresh database built from this repo had
-- ZERO, because every migration ends in `revoke ... from public, anon,
-- authenticated`. So the repo was right and production had drifted -- and the
-- reason no suite caught the drift is that every function-privilege assertion
-- in this harness is a HARDCODED ALLOWLIST: `proname = <literal>`, or `proname
-- like 'company_admin_%'`, or `proname in (...)`. Not one of them iterates
-- prosecdef over the whole schema, so a function nobody thought to enumerate --
-- or a project-level ALTER DEFAULT PRIVILEGES re-grant, which hits ALL of them
-- at once -- was invisible to the entire suite.
--
-- This file expresses the universal invariant instead of another list, and it
-- proves five things in order, because the later ones are worthless without the
-- earlier ones:
--
--   1. NON-VACUITY. anon and authenticated exist, can hold EXECUTE, and
--      has_function_privilege() reports it. Without this the whole file passes
--      on a database where the browser roles were never created.
--   2. THE INVENTORY IS EMPTY. Recomputed here from pg_proc directly, NOT by
--      calling the migration's own assertion function -- a suite that only
--      calls the guard cannot detect a guard that has stopped looking.
--   3. THE GUARD IS REAL. Introduce one violation and require
--      assert_browser_role_function_privileges() to raise on it, then require
--      it to pass again once the violation is removed.
--   4. THE ROOT CAUSE IS CLOSED. A SECURITY DEFINER function created AFTER the
--      whole chain must not come back anon-executable. This is the assertion
--      that would have caught the production drift, and the one that catches
--      the next migration that forgets its revoke.
--   5. SERVICE_ROLE STILL WORKS. The API and worker authenticate as
--      service_role; a revoke that took them down with it would be an outage,
--      not a fix. Asserted on the specific RPCs the Python store and the
--      Next.js billing client call, plus on newly created functions.
--
-- Rerunnable and side-effect free: one transaction, ends in ROLLBACK.

begin;

-- ---------------------------------------------------------------------------
-- 0. Registration gate. A missing migration must say so in those words rather
--    than failing as a regression somewhere below.
-- ---------------------------------------------------------------------------
do $$
begin
    if to_regprocedure('public.assert_browser_role_function_privileges()') is null then
        raise exception
            '202607280032 is not applied: public.assert_browser_role_function_privileges() is missing. Register 202607280032_browser_function_execute_contract.sql in scripts/ci/migration-fresh-manifest.txt and scripts/ci/migration-upgrade-manifest.txt.'
            using errcode = '55000';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 1. Non-vacuity: the browser roles exist and CAN hold function EXECUTE, so a
--    negative assertion below is a real observation rather than an artefact of
--    a role that is not there.
-- ---------------------------------------------------------------------------
create function public.brevitas_function_privilege_probe()
returns integer language sql immutable as $$ select 1 $$;
-- PostgreSQL grants EXECUTE to PUBLIC on every new function and anon is a
-- member of PUBLIC, so without this line the grant/revoke round trip below
-- could not observe anything: anon would test TRUE either way.
revoke execute on function public.brevitas_function_privilege_probe() from public;

do $$
declare
    v_grantee text;
begin
    foreach v_grantee in array array['anon', 'authenticated'] loop
        if not exists (select 1 from pg_roles where rolname = v_grantee) then
            raise exception
                'role % does not exist, so every browser-EXECUTE assertion in this file is vacuous',
                v_grantee using errcode = '55000';
        end if;

        execute format(
            'grant execute on function public.brevitas_function_privilege_probe() to %I',
            v_grantee);
        if not has_function_privilege(
            v_grantee, 'public.brevitas_function_privilege_probe()', 'EXECUTE') then
            raise exception
                'has_function_privilege() does not report an explicit EXECUTE grant to %, so this suite cannot observe the exposure it exists to prevent',
                v_grantee using errcode = '55000';
        end if;
        execute format(
            'revoke execute on function public.brevitas_function_privilege_probe() from %I',
            v_grantee);
        if has_function_privilege(
            v_grantee, 'public.brevitas_function_privilege_probe()', 'EXECUTE') then
            raise exception
                '% still holds EXECUTE after an explicit revoke', v_grantee
                using errcode = '55000';
        end if;
    end loop;

    if not has_schema_privilege('anon', 'public', 'USAGE')
       or not has_schema_privilege('authenticated', 'public', 'USAGE') then
        raise exception
            'migration-bootstrap.sql no longer grants USAGE on schema public to the browser roles, so no browser-role assertion can reach any routine'
            using errcode = '55000';
    end if;
end;
$$;

drop function public.brevitas_function_privilege_probe();

-- ---------------------------------------------------------------------------
-- 2. The invariant itself, recomputed independently of the migration's guard.
--    Extension routines are excluded on the same pg_depend edge the migration
--    uses: pgvector and pgcrypto put ~154 functions in public and their PUBLIC
--    EXECUTE is load-bearing for operator resolution. Section 5 proves that
--    exclusion is still a real exclusion and not an empty one.
-- ---------------------------------------------------------------------------
do $$
declare
    v_offenders text;
    v_secdef_total int;
begin
    select count(*) into v_secdef_total
      from pg_proc routine
     where routine.pronamespace = 'public'::regnamespace
       and routine.prosecdef
       and not exists (
           select 1 from pg_depend dependency
            where dependency.objid = routine.oid
              and dependency.classid = 'pg_proc'::regclass
              and dependency.deptype = 'e');

    -- If the chain ever stops installing SECURITY DEFINER functions, section 2
    -- would pass by having nothing to check.
    if v_secdef_total < 100 then
        raise exception
            'only % SECURITY DEFINER functions found in schema public; the chain installs well over a hundred, so this assertion is not seeing the schema it thinks it is',
            v_secdef_total using errcode = '55000';
    end if;

    select string_agg(format('%s: %s', entry.role_name, entry.signature), E'\n'
                      order by entry.signature, entry.role_name)
      into v_offenders
      from (
        select routine.oid::regprocedure::text as signature,
               grantee.rolname as role_name
          from pg_proc routine
         cross join (values ('anon'), ('authenticated')) as grantee(rolname)
         where routine.pronamespace = 'public'::regnamespace
           and routine.prosecdef
           and not exists (
               select 1 from pg_depend dependency
                where dependency.objid = routine.oid
                  and dependency.classid = 'pg_proc'::regclass
                  and dependency.deptype = 'e')
           and has_function_privilege(grantee.rolname, routine.oid, 'EXECUTE')
      ) entry;

    if v_offenders is not null then
        raise exception using
            errcode = '42501',
            message = format(
                'browser roles hold EXECUTE on SECURITY DEFINER functions in schema public (checked %s functions):%s%s',
                v_secdef_total, E'\n', v_offenders),
            hint = 'A SECURITY DEFINER function bypasses RLS and runs as its owner, so EXECUTE is the entire authorization check. Add `revoke execute on function ... from public, anon, authenticated` to the migration that creates it.';
    end if;
end;
$$;

-- Only after the independent recomputation: the shipped guard agrees.
select public.assert_browser_role_function_privileges();

-- ---------------------------------------------------------------------------
-- 3. The guard is not a no-op. Plant exactly the exposure the migration exists
--    to prevent and require the guard to raise on it -- both for a direct grant
--    to anon and for the PUBLIC grant that PostgreSQL's own CREATE FUNCTION
--    default produces, because the second is the shape production drifted into
--    and a guard that only looks for explicit anon grants would miss it.
-- ---------------------------------------------------------------------------
create function public.brevitas_function_privilege_violation()
returns integer language sql security definer set search_path = public, pg_temp
as $$ select 1 $$;
revoke execute on function public.brevitas_function_privilege_violation()
    from public, anon, authenticated;

do $$
declare
    v_raised boolean;
begin
    -- Baseline: locked down, the guard passes.
    perform public.assert_browser_role_function_privileges();

    -- (a) explicit grant to anon.
    grant execute on function public.brevitas_function_privilege_violation() to anon;
    v_raised := false;
    begin
        perform public.assert_browser_role_function_privileges();
    exception when insufficient_privilege then
        v_raised := true;
    end;
    if not v_raised then
        raise exception
            'assert_browser_role_function_privileges() did not raise on a SECURITY DEFINER function explicitly granted to anon, so it cannot detect the exposure it exists to prevent'
            using errcode = '55000';
    end if;
    revoke execute on function public.brevitas_function_privilege_violation() from anon;

    -- (b) the PUBLIC grant. anon is a member of PUBLIC, so this is executable
    --     by the browser even though no grant names anon at all.
    grant execute on function public.brevitas_function_privilege_violation() to public;
    if not has_function_privilege(
        'anon', 'public.brevitas_function_privilege_violation()', 'EXECUTE') then
        raise exception
            'a PUBLIC grant no longer reaches anon; this suite can no longer model the drift it exists to catch'
            using errcode = '55000';
    end if;
    v_raised := false;
    begin
        perform public.assert_browser_role_function_privileges();
    exception when insufficient_privilege then
        v_raised := true;
    end;
    if not v_raised then
        raise exception
            'assert_browser_role_function_privileges() did not raise on a SECURITY DEFINER function granted to PUBLIC, so a migration that simply forgets `revoke ... from public` would ship anon-executable'
            using errcode = '55000';
    end if;
    revoke execute on function public.brevitas_function_privilege_violation() from public;

    -- Cleaned up, the guard passes again: it raised on the violation, not on
    -- some unrelated pre-existing state.
    perform public.assert_browser_role_function_privileges();
end;
$$;

drop function public.brevitas_function_privilege_violation();

-- ---------------------------------------------------------------------------
-- 4. THE ROOT CAUSE. Two independent defaults decide what a routine created
--    AFTER the whole chain is born with, and they are NOT equally fixable.
--    This section asserts each one for exactly what it is, because collapsing
--    them into a single "new functions are safe" check would be a false claim.
--
--    (a) Supabase's project ALTER DEFAULT PRIVILEGES GRANT to anon and
--        authenticated -- reproduced by migration-bootstrap.sql, which is the
--        only reason this test is non-vacuous in CI. This is what put the 99
--        grants on production, it IS removable, and 202607280032 removes it.
--        Asserted hard below: a new routine must carry no explicit aclitem for
--        either browser role.
--
--    (b) PostgreSQL's own built-in EXECUTE-to-PUBLIC default, which anon
--        inherits as a member of PUBLIC. This is NOT removable with ALTER
--        DEFAULT PRIVILEGES. Measured on PostgreSQL 17.10: in a virgin
--        database the documented `revoke execute on functions from public`
--        recipe stores no pg_default_acl row at all and the next function still
--        comes out proacl = NULL; and here, where the stored row is
--        {service_role=X/owner} with no owner and no PUBLIC entry, a function
--        created right after it comes out {=X/owner, owner=X/owner,
--        service_role=X/owner}. Entries that are not in the stored row appear
--        on the object, so the row is a diff merged additively onto
--        acldefault(), and a revoke can never subtract a built-in default.
--        Therefore a new function IS born PUBLIC-executable, and the control
--        that closes it is the per-function `revoke ... from public, anon,
--        authenticated` every migration in this chain already writes -- with
--        section 3 above as the enforcement that a forgotten one fails CI.
--        Asserted below in the only honest direction: if the new routine is
--        browser-executable at all, it must be through the PUBLIC entry and
--        nothing else. If a future PostgreSQL or a project setting removes even
--        that, this section passes and simply reports it.
-- ---------------------------------------------------------------------------
create function public.brevitas_function_default_probe()
returns integer language sql security definer set search_path = public, pg_temp
as $$ select 1 $$;

do $$
declare
    v_grantee text;
    v_public_holds boolean;
    v_explicit text;
begin
    -- (a) the removable half: no explicit browser grant may be inherited from
    --     pg_default_acl.
    select string_agg(entry.grantee::regrole::text, ', ')
      into v_explicit
      from pg_proc routine,
           aclexplode(routine.proacl) entry
     where routine.oid = 'public.brevitas_function_default_probe()'::regprocedure
       and entry.privilege_type = 'EXECUTE'
       and entry.grantee in ('anon'::regrole::oid, 'authenticated'::regrole::oid);

    if v_explicit is not null then
        raise exception
            '202607280032 default-privilege contract broken: a newly created SECURITY DEFINER function in schema public was granted EXECUTE to % straight out of pg_default_acl. This is exactly how production drifted to 99 anon-executable SECURITY DEFINER functions -- the ALTER DEFAULT PRIVILEGES revoke in 202607280032 has been reverted or was applied for a different granting role than the one creating functions here.',
            v_explicit using errcode = '42501';
    end if;

    -- (b) the unremovable half: PUBLIC. Recorded, not failed -- but if the new
    --     routine is reachable by a browser role it must be via PUBLIC only,
    --     never via a browser-named grant, which (a) has already established.
    v_public_holds := exists (
        select 1
          from pg_proc routine,
               aclexplode(routine.proacl) entry
         where routine.oid = 'public.brevitas_function_default_probe()'::regprocedure
           and entry.grantee = 0
           and entry.privilege_type = 'EXECUTE')
        or (select proacl is null
              from pg_proc
             where oid = 'public.brevitas_function_default_probe()'::regprocedure);

    foreach v_grantee in array array['anon', 'authenticated'] loop
        if has_function_privilege(
               v_grantee, 'public.brevitas_function_default_probe()', 'EXECUTE')
           and not v_public_holds then
            raise exception
                '% can execute a newly created SECURITY DEFINER function in schema public and it is not via the PUBLIC default, so a browser grant is arriving from somewhere this suite does not model',
                v_grantee using errcode = '42501';
        end if;
    end loop;

    if v_public_holds then
        raise notice
            '202607280032: as designed, a newly created function in schema public is still EXECUTE-able by PUBLIC (and therefore by anon) -- PostgreSQL''s built-in default cannot be revoked via ALTER DEFAULT PRIVILEGES. Every migration must keep ending its function definitions with `revoke execute on function ... from public, anon, authenticated`; section 3 above is what fails CI when one does not.';
    else
        raise notice
            '202607280032: a newly created function in schema public is not EXECUTE-able by PUBLIC on this server -- stronger than the contract requires.';
    end if;
end;
$$;

-- The guard must agree that the newly created function, once its migration
-- would have revoked PUBLIC, is compliant -- i.e. the invariant and the
-- default-privilege fix agree on the same object.
revoke execute on function public.brevitas_function_default_probe()
    from public, anon, authenticated;
select public.assert_browser_role_function_privileges();

-- ---------------------------------------------------------------------------
-- 5. The blast radius stayed where it belongs.
--
--    (a) service_role keeps EXECUTE on the RPCs the deployed callers actually
--        invoke. api/store.py, api/company_admin.py and api/billing_recovery.py
--        POST /rest/v1/rpc/<name> with SUPABASE_SERVICE_ROLE_KEY, and
--        src/lib/billing/supabase.ts billingDatabase() is a service-role
--        client. If the revoke had caught service_role, billing, device
--        approval, dashboard session-key minting and the Stripe webhook would
--        all 403 at runtime -- an outage, not a fix -- and no other suite here
--        asserts these in the positive direction.
--    (b) newly created routines still reach service_role, so the default-
--        privilege change did not turn every future migration into a manual
--        grant.
--    (c) extension routines are untouched, which is what proves the exclusion
--        in sections 2 and 4 is a real carve-out rather than a clause that
--        matches nothing.
-- ---------------------------------------------------------------------------
do $$
declare
    v_signature text;
    v_oid oid;
    v_checked int := 0;
begin
    foreach v_signature in array array[
        'public.claim_billing_ledger_entries(text,integer,integer,bigint)',
        'public.complete_billing_ledger_entry(bigint,text,text,text)',
        'public.approve_bvx_device(text,text,text,text,uuid)',
        'public.append_company_audit(uuid,text,text,text,text,text,text,text)',
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer,text)',
        'public.company_admin_create_dashboard_session_key(uuid,uuid,text,text,timestamptz,text)'
    ] loop
        v_oid := to_regprocedure(v_signature);
        -- Signatures move as migrations widen them; a missing one is skipped
        -- rather than failed, and the count below keeps the block honest.
        if v_oid is null then
            continue;
        end if;
        if not has_function_privilege('service_role', v_oid, 'EXECUTE') then
            raise exception
                '202607280032 revoked too far: service_role lost EXECUTE on %, which api/store.py and src/lib/billing/supabase.ts call with the service-role key',
                v_signature using errcode = '42501';
        end if;
        v_checked := v_checked + 1;
    end loop;

    if v_checked < 3 then
        raise exception
            'only % of the named service_role RPCs resolved; this section is no longer proving service_role survived the revoke -- refresh the signatures',
            v_checked using errcode = '55000';
    end if;

    if not has_function_privilege(
        'service_role', 'public.brevitas_function_default_probe()', 'EXECUTE') then
        raise exception
            '202607280032 revoked too far: service_role does not get EXECUTE on newly created routines in schema public, so every future migration would need a manual grant and the data plane would break the moment one forgets'
            using errcode = '42501';
    end if;

    if not has_function_privilege('service_role', 'public.assert_browser_role_function_privileges()', 'EXECUTE') then
        raise exception
            'service_role cannot execute assert_browser_role_function_privileges(), so the contract cannot be checked from the data plane'
            using errcode = '42501';
    end if;
    if has_function_privilege('anon', 'public.assert_browser_role_function_privileges()', 'EXECUTE')
       or has_function_privilege('authenticated', 'public.assert_browser_role_function_privileges()', 'EXECUTE') then
        raise exception
            'the browser roles can execute assert_browser_role_function_privileges() itself'
            using errcode = '42501';
    end if;
end;
$$;

drop function public.brevitas_function_default_probe();

do $$
declare
    v_extension_routines int;
    v_extension_public int;
begin
    select count(*),
           count(*) filter (where has_function_privilege('anon', routine.oid, 'EXECUTE'))
      into v_extension_routines, v_extension_public
      from pg_proc routine
     where routine.pronamespace = 'public'::regnamespace
       and exists (
           select 1 from pg_depend dependency
            where dependency.objid = routine.oid
              and dependency.classid = 'pg_proc'::regclass
              and dependency.deptype = 'e');

    if v_extension_routines = 0 then
        raise exception
            'no extension-owned routines found in schema public, so the pg_depend carve-out in 202607280032 and in section 2 above is matching nothing and has never been exercised'
            using errcode = '55000';
    end if;
    if v_extension_public = 0 then
        raise exception
            '202607280032 revoked extension routines in schema public: all % of them lost PUBLIC EXECUTE. pgvector/pgcrypto operator resolution depends on it, and the migration is supposed to exclude anything with a pg_depend deptype=''e'' edge.',
            v_extension_routines using errcode = '42501';
    end if;
end;
$$;

rollback;
