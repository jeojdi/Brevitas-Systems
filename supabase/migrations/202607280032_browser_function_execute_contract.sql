-- REVERSE: PITR-ONLY -- re-granting browser-role EXECUTE on public functions reopens unauthenticated RPC access to 61 writers, including the compliance erasure chain and the billing ledger claim path
--
-- Revoke EXECUTE on every public-schema routine the migration chain owns from
-- the browser roles, and stop Supabase's project default from re-granting it to
-- every routine created from now on.
--
-- WHAT WAS MEASURED ON PRODUCTION (wyfz, 2026-07-30, SELECT-only):
--   149 SECURITY DEFINER functions in schema public
--    99 of them EXECUTE-able by BOTH `anon` (the unauthenticated browser role,
--       whose key ships in the bundle by design) and `authenticated`
--    61 of the 85 RPC-reachable ones WRITE
--     0 of the 99 reference auth.uid(), auth.jwt() or auth.role()
-- Every one of those grants has grantor = `postgres` and comes from
-- pg_default_acl, never from an explicit GRANT in a migration: repo-wide,
-- `grant execute ... to anon|authenticated` appears in ZERO migration files.
-- The exposure is entirely the project default:
--   pg_default_acl -> f:postgres={postgres=X/postgres,anon=X/postgres,
--                                 authenticated=X/postgres,service_role=X/postgres}
--
-- This is the FUNCTION analogue of the hole 202607280015 / 202607280024 /
-- 202607280027 closed for TABLES, and it is worse than the table hole was: the
-- table grants are gated by row-level security, a SECURITY DEFINER function is
-- not gated by anything the database enforces. It runs as its owner, with RLS
-- bypassed, and executes whatever its body says. Among the 99:
--   claim_billing_ledger_entries / complete_billing_ledger_entry -- an
--     unauthenticated caller can lease and mark 'reported' fee rows that were
--     never sent to Stripe, tenant-agnostically. This repo's OWN suite
--     (scripts/ci/migration-billing-claim-assertions.sql) requires anon to be
--     DENIED here and passes on a fresh database, so production has drifted
--     from a guarantee the tests enforce.
--   compliance_delete_subject / compliance_anonymize_unshared_user and the
--     submit -> approve -> delete chain. These are NOT protected by an actor
--     check: compliance_actor_role() is a STRING-FORMAT REGEX
--     ('^(system|brevitas_admin):[A-Za-z0-9._:-]{3,96}$'), so the literal
--     'brevitas_admin:x' satisfies it. They fail closed on a MALFORMED actor,
--     not on an unauthenticated one.
--   admin_usage_report({}) -- cross-tenant owner_id, revenue and savings dump
--     with no arguments, which also hands out the UUIDs that the otherwise
--     fail-closed company_admin_* family gates on.
--   semantic_cache_store_bounded -- arbitrary ciphertext write, and with
--     p_max_entries = 1 it evicts the entire cache the fee basis is computed on.
--   approve_bvx_device -- no actor check in the body at all.
--   append_company_audit -- trusts p_actor_id/p_actor_role verbatim, so the
--     audit trail itself is forgeable by an unauthenticated caller.
--
-- WHY A BLANKET REVOKE IS SAFE. The browser calls NO public function at all.
-- Verified against the BUILT bundle, not from memory: in
-- public/dashboard/assets/index-*.js the only `.rpc(` occurrences are the
-- postgrest-js method definitions, there is not one `.rpc("name")` or
-- `.from("table")` string-literal call site (searched in the backtick form the
-- minifier emits), `functions.invoke` is absent, and none of the 99 exposed
-- function names appears anywhere in the artifact. The entire Supabase surface
-- of the shipped dashboard is supabase.auth.* -- getSession,
-- onAuthStateChange, signUp, signInWithPassword, signOut,
-- resetPasswordForEmail, updateUser -- which is GoTrue (/auth/v1), reached with
-- the anon key but NOT through PostgREST and NOT through any grant this
-- migration touches. All dashboard data flows over fetch('/v1/...') and
-- fetch('/api/admin/company/...') to the API, which authenticates as
-- service_role. So the keep-list is EMPTY: nothing is exempted below.
--
-- WHAT THIS DELIBERATELY DOES NOT TOUCH:
--   service_role -- the Next.js billing client (src/lib/billing/supabase.ts
--     billingDatabase()) and the FastAPI store (api/store.py) are service_role.
--     Every function that has service_role EXECUTE before this migration still
--     has it after; see the re-grant in the revoke loop.
--   the auth schema and supabase_auth_admin -- the anon key still has to reach
--     GoTrue or signup, login, password reset and session refresh all break.
--   extension-owned routines -- pgvector and pgcrypto install 154 functions
--     into public here, and their operators need the PUBLIC EXECUTE they ship
--     with. The loop below excludes anything with a pg_depend deptype='e'
--     edge, so no extension function is altered.
--   pg_catalog and every other schema -- the scope is exactly
--     pronamespace = 'public'.

begin;

do $migration_precondition$
begin
    -- Pins this migration behind the table-privilege contract it is the
    -- function-side counterpart of. If 202607280015/27 have not been applied,
    -- the browser-role story is incomplete in a different way and the operator
    -- should know which half is missing.
    if to_regprocedure('public.assert_browser_role_truncate_contract()') is null then
        raise exception using
            errcode = '55000',
            message = '202607280032 requires 202607280027';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- 1. THE ROOT CAUSE, FIRST. Fix the default before creating anything new, so
--    the assertion function created in section 3 is itself born locked down.
--
--    Two things would have to be revoked for a newly created function to be
--    completely unreachable by the browser, and ONLY THE FIRST IS ACHIEVABLE:
--
--    (a) anon / authenticated. This is the Supabase project default, and it is
--        what actually produced the 99 grants measured on wyfz
--        (pg_default_acl -> f:postgres={...,anon=X/postgres,
--        authenticated=X/postgres,...}, every grant carrying grantor=postgres
--        and no migration anywhere issuing an explicit GRANT). Revoking it here
--        removes those entries from the stored row, so the next CREATE FUNCTION
--        no longer arrives with anon and authenticated already named on it.
--        This is the part that fixes the measured drift, and it works:
--          before: {anon=X/owner,authenticated=X/owner,service_role=X/owner}
--          after:  {service_role=X/owner}
--
--    (b) PUBLIC -- PostgreSQL's OWN built-in EXECUTE-to-PUBLIC default, which
--        anon inherits like every other role. THIS CANNOT BE REMOVED WITH
--        ALTER DEFAULT PRIVILEGES, despite the recipe appearing in the
--        PostgreSQL documentation. Measured on PostgreSQL 17.10, twice:
--          * In a virgin database, `alter default privileges in schema public
--            revoke execute on functions from public` stores NO pg_default_acl
--            row at all (select count(*) from pg_default_acl -> 0) and the next
--            function still comes out with proacl = NULL, i.e. the built-in
--            default, i.e. PUBLIC EXECUTE.
--          * With a row already present here, after the revoke the stored row
--            is {service_role=X/owner} -- no owner entry, no PUBLIC entry --
--            and yet a function created immediately afterwards has
--            proacl = {=X/owner, owner=X/owner, service_role=X/owner}.
--        The owner and PUBLIC entries in that result are not in the stored row,
--        which is the proof: a pg_default_acl row is a DIFF that is merged
--        ADDITIVELY onto acldefault() at CREATE time. A revoke can only remove
--        entries the diff already contains; it can never subtract a built-in
--        default. So every function created in schema public, on this project
--        and on any PostgreSQL, is born PUBLIC-executable no matter what this
--        migration does.
--
--        `public` is still named in the revoke below because it is a no-op when
--        the stored row has no PUBLIC entry and a real fix when it does (a
--        project where someone ran ALTER DEFAULT PRIVILEGES ... GRANT ... TO
--        PUBLIC would carry one).
--
--        What actually closes (b) is what the chain has always done: every
--        migration ends its function definitions with `revoke execute on
--        function ... from public, anon, authenticated`. All 78 migrations do,
--        which is why a fresh database has ZERO anon-executable SECURITY
--        DEFINER functions while production has 99. That discipline had no
--        enforcement -- every function-privilege assertion in the harness was a
--        hardcoded name list -- so the durable half of this fix is section 3:
--        assert_browser_role_function_privileges() is what makes a forgotten
--        revoke fail loudly instead of shipping.
--
--    Default privileges are recorded PER GRANTING ROLE (pg_default_acl
--    .defaclrole) and a managed Supabase project has more than one --
--    `postgres` (which owns the migration chain) and `supabase_admin`. Fix
--    every role we are permitted to alter and do not fail on the ones we are
--    not: ALTER DEFAULT PRIVILEGES FOR ROLE requires membership in that role,
--    and `postgres` is not a superuser on a managed project. A role left
--    unfixed is latent only -- it matters solely if some future routine is
--    created BY that role rather than by the migration chain -- and
--    assert_browser_role_function_privileges() below fails loudly if that ever
--    turns into a real exposure. Skipped roles are named in a NOTICE.
-- ---------------------------------------------------------------------------
do $revoke_defaults$
declare
    granting_role text;
    skipped text[] := '{}';
    fixed text[] := '{}';
begin
    for granting_role in
        select distinct pg_get_userbyid(defaults.defaclrole)
          from pg_default_acl defaults
          join pg_namespace namespace on namespace.oid = defaults.defaclnamespace
         where namespace.nspname = 'public'
           and defaults.defaclobjtype = 'f'
        union
        select current_user
    loop
        begin
            -- ALTER DEFAULT PRIVILEGES spells this ON FUNCTIONS, and there it
            -- genuinely does cover functions, procedures and aggregates. The
            -- REVOKE in section 2 is the opposite: there, ON FUNCTION rejects a
            -- procedure outright, so it says ON ROUTINE instead.
            execute format(
                'alter default privileges for role %I in schema public '
                'revoke execute on functions from public, anon, authenticated',
                granting_role);
            fixed := fixed || granting_role;
        exception
            when insufficient_privilege then
                skipped := skipped || granting_role;
        end;
    end loop;

    raise notice
        '202607280032: browser-role default EXECUTE revoked for granting '
        'role(s): %. NOTE: PostgreSQL''s built-in EXECUTE-to-PUBLIC default is '
        'NOT removable this way (see the comment above), so a new function is '
        'still born PUBLIC-executable until its own migration revokes it; '
        'assert_browser_role_function_privileges() is what catches one that '
        'does not.', fixed;
    if array_length(skipped, 1) is not null then
        raise notice
            '202607280032: could NOT alter default privileges for role(s) % '
            '(not a member); routines created BY those roles would still grant '
            'browser EXECUTE. assert_browser_role_function_privileges() fails '
            'if that ever produces a real exposure.',
            skipped;
    end if;
end;
$revoke_defaults$;

-- ---------------------------------------------------------------------------
-- 2. Today's inventory. Written as a loop over the catalog rather than a name
--    list precisely because a name list is what let the table version of this
--    recur twice (202607280015, then 202607280024, then 202607280027).
--
--    Scope, deliberately narrow in two directions:
--      * pronamespace = 'public' only. pg_catalog, auth, extensions, graphql
--        and storage are untouched.
--      * NOT extension-owned. pgvector/pgcrypto ship 154 routines into public
--        on this schema and their PUBLIC EXECUTE is load-bearing for operator
--        resolution; a pg_depend deptype='e' edge excludes every one of them.
--
--    service_role is preserved EXACTLY: its privilege is sampled per routine
--    before the revoke and restored after, and only where it already held it.
--    That matters because `revoke ... from public` removes the PUBLIC grant
--    that a routine may be relying on to reach service_role, and because a
--    blanket `grant to service_role` would be an escalation --
--    202607280030 deliberately withholds EXECUTE on the attestation writer
--    from service_role (and from brevitas_attestor on everything else), and
--    this migration must not quietly hand it back.
-- ---------------------------------------------------------------------------
do $revoke_existing$
declare
    target record;
    revoked_secdef int := 0;
    revoked_invoker int := 0;
begin
    for target in
        select routine.oid::regprocedure::text as signature,
               routine.prosecdef,
               has_function_privilege('service_role', routine.oid, 'EXECUTE')
                   as service_role_had_execute,
               (has_function_privilege('anon', routine.oid, 'EXECUTE')
                or has_function_privilege('authenticated', routine.oid, 'EXECUTE'))
                   as browser_had_execute
          from pg_proc routine
         where routine.pronamespace = 'public'::regnamespace
           and not exists (
               select 1
                 from pg_depend dependency
                where dependency.objid = routine.oid
                  and dependency.classid = 'pg_proc'::regclass
                  and dependency.deptype = 'e')
         order by 1
    loop
        -- ON ROUTINE, not ON FUNCTION. The cursor above selects every
        -- non-extension pg_proc row in public with no prokind filter -- which is
        -- correct, because a SECURITY DEFINER PROCEDURE is exactly as
        -- anon-reachable as a function -- but `REVOKE EXECUTE ON FUNCTION`
        -- raises `<name> is not a function` for prokind='p' and takes the whole
        -- migration down with it. ON ROUTINE accepts functions, procedures and
        -- aggregates alike, so the revoke matches the rows the cursor yields.
        execute format(
            'revoke execute on routine %s from public, anon, authenticated',
            target.signature);

        if target.service_role_had_execute then
            execute format(
                'grant execute on routine %s to service_role',
                target.signature);
        end if;

        if target.browser_had_execute then
            if target.prosecdef then
                revoked_secdef := revoked_secdef + 1;
            else
                revoked_invoker := revoked_invoker + 1;
            end if;
        end if;
    end loop;

    raise notice
        '202607280032: browser EXECUTE removed from % SECURITY DEFINER and % '
        'SECURITY INVOKER routine(s) in schema public', revoked_secdef, revoked_invoker;
end;
$revoke_existing$;

-- ---------------------------------------------------------------------------
-- 3. Fail closed on regression, in the same style as
--    assert_browser_role_table_privileges (202607280015) and
--    assert_browser_role_truncate_contract (202607280027). Kept separate from
--    both so the three contracts fail independently and name different causes.
--
--    Scoped to SECURITY DEFINER because that is the class where a grant is
--    unconditionally exploitable: the body runs as the owner with RLS off, so
--    EXECUTE is the ONLY thing standing between an anonymous caller and the
--    write. A SECURITY INVOKER function still runs under the caller's own
--    privileges and RLS, so it is covered by the table contracts instead.
--    Section 2 revokes both classes anyway; this asserts the one that cannot
--    be allowed to come back.
-- ---------------------------------------------------------------------------
create or replace function public.assert_browser_role_function_privileges()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    offender record;
begin
    for offender in
        select routine.oid::regprocedure::text as signature,
               grantee.rolname as role_name
          from pg_proc routine
         cross join (select unnest(array['anon', 'authenticated']) as rolname) grantee
         where routine.pronamespace = 'public'::regnamespace
           and routine.prosecdef
           and not exists (
               select 1
                 from pg_depend dependency
                where dependency.objid = routine.oid
                  and dependency.classid = 'pg_proc'::regclass
                  and dependency.deptype = 'e')
           and has_function_privilege(grantee.rolname, routine.oid, 'EXECUTE')
         order by 1, 2
    loop
        raise exception using
            errcode = '42501',
            message = format(
                'browser role %s must not hold EXECUTE on SECURITY DEFINER function %s',
                offender.role_name, offender.signature),
            hint = 'A SECURITY DEFINER function runs as its owner with RLS bypassed, so EXECUTE is the only authorization check there is. Either add `revoke execute on function ... from public, anon, authenticated` to the migration that creates it, or check whether ALTER DEFAULT PRIVILEGES has been re-granted at the project level (202607280032).';
    end loop;
end;
$$;

revoke execute on function public.assert_browser_role_function_privileges()
    from public, anon, authenticated;
grant execute on function public.assert_browser_role_function_privileges() to service_role;

-- The migration fails here if it did not fully close the hole.
select public.assert_browser_role_function_privileges();

commit;
