-- REVERSE: PITR-ONLY -- re-granting browser-role TRUNCATE reopens the audit-and-evidence destruction hole this migration closes
--
-- Revoke browser-role TRUNCATE (and the other privileges no browser role ever
-- needs) from every table in the public schema, and stop Supabase's project
-- default from re-granting them to every table created from now on.
--
-- Why this exists after 202607280015 and 202607280024 enumerated tables by name:
-- both were table lists, so every table added afterwards silently inherited the
-- project default again. On the production database this left NINETEEN tables
-- with `anon=arwdDxtm` -- among them semantic_cache (customer prompt content),
-- user_keys (provider credential storage), stripe_webhook_events,
-- data_subject_requests, legal_holds and waitlist (PII).
--
-- RLS is enabled on all of them, which is why this is not a read breach: SELECT,
-- INSERT, UPDATE and DELETE are all policy-gated, and a table with zero policies
-- denies those outright. TRUNCATE is the exception -- it is NOT subject to
-- row-level security at any point -- so the holder of the public anon key, which
-- ships in the browser bundle by design, could empty any of those tables. That
-- destroys the audit trail (the thing an enterprise buyer asks to see), the
-- compliance evidence, the Stripe webhook inbox and the customer key store.
--
-- The fix is deliberately narrow. It revokes ONLY the privileges that RLS does
-- not gate and that no browser role has a legitimate use for:
--   TRUNCATE   -- bypasses RLS entirely; this is the actual hole
--   TRIGGER    -- lets a role attach a trigger function to a table it can read
--   REFERENCES -- lets a role create an FK revealing the existence of rows
--   MAINTAIN   -- the PG17 table-maintenance privilege; no browser use
-- SELECT/INSERT/UPDATE/DELETE are left EXACTLY as they are, so no dashboard or
-- signup flow changes behaviour. Narrowing those is a per-table policy question
-- and is deliberately out of scope here: this migration must be safe to apply to
-- a live database with no coordinated frontend deploy.

begin;

do $migration_precondition$
begin
    if to_regprocedure('public.assert_browser_role_table_privileges()') is null then
        raise exception using
            errcode = '55000',
            message = '202607280027 requires 202607280015';
    end if;
end;
$migration_precondition$;

-- Existing tables. Written as a loop rather than a name list precisely because a
-- name list is what let this recur twice.
--
-- MAINTAIN is a PostgreSQL 17 privilege. Naming it unconditionally makes the
-- whole statement fail on PG16 with 'unrecognized privilege type "maintain"' --
-- which is where CI runs (pgvector/pgvector:pg16-bookworm, .github/workflows/
-- migrations.yml:86), so this migration could never pass the ephemeral
-- integration job even though it applies fine on wyfz (17.6). Build the
-- privilege list from the server version instead: on PG17+ the behaviour is
-- unchanged, and on PG16 MAINTAIN does not exist, so there is nothing to revoke
-- and no exposure to leave behind.
do $revoke_existing$
declare
    target record;
    privileges constant text := case
        when current_setting('server_version_num')::int >= 170000
            then 'truncate, trigger, references, maintain'
        else 'truncate, trigger, references'
    end;
begin
    for target in
        select relation.oid::regclass::text as qualified_name
          from pg_class relation
         where relation.relnamespace = 'public'::regnamespace
           and relation.relkind in ('r', 'p')
         order by 1
    loop
        execute format(
            'revoke %s on table %s from anon, authenticated',
            privileges, target.qualified_name);
    end loop;
end;
$revoke_existing$;

-- The root cause: the project default re-grants everything on each new table, so
-- both previous table-list migrations were overtaken by the next table added.
--
-- Default privileges are recorded PER GRANTING ROLE (pg_default_acl.defaclrole),
-- and a managed Supabase project has more than one: `postgres` (which owns the
-- migration chain, so this is the one that governs every table we create) and
-- `supabase_admin`. Fix every role we are actually permitted to alter, and do not
-- fail the migration on the ones we are not -- ALTER DEFAULT PRIVILEGES FOR ROLE
-- requires membership in that role, and `postgres` is not a superuser on a
-- managed project. A role left unfixed is latent only: it matters solely if some
-- future table is created BY that role rather than by the migration chain, and
-- assert_browser_role_truncate_contract() below fails loudly if that ever
-- produces a real exposure.
do $revoke_defaults$
declare
    granting_role text;
    -- Same PG16/PG17 split as the loop above; see the note there.
    privileges constant text := case
        when current_setting('server_version_num')::int >= 170000
            then 'truncate, trigger, references, maintain'
        else 'truncate, trigger, references'
    end;
begin
    for granting_role in
        select distinct pg_get_userbyid(defaults.defaclrole)
          from pg_default_acl defaults
          join pg_namespace namespace on namespace.oid = defaults.defaclnamespace
         where namespace.nspname = 'public'
           and defaults.defaclobjtype = 'r'
        union
        select current_user
    loop
        begin
            execute format(
                'alter default privileges for role %I in schema public '
                'revoke %s on tables from anon, authenticated',
                granting_role, privileges);
        exception
            when insufficient_privilege then
                raise notice
                    'could not alter default privileges for role % (not a member); '
                    'tables created by that role would still grant browser TRUNCATE',
                    granting_role;
        end;
    end loop;
end;
$revoke_defaults$;

-- Fail closed on regression, in the same style as
-- assert_browser_role_table_privileges. Kept as a separate assertion so the
-- table-list contract and the blanket-TRUNCATE contract can fail independently
-- and name different causes.
create or replace function public.assert_browser_role_truncate_contract()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    offender record;
begin
    for offender in
        select relation.oid::regclass::text as qualified_name,
               grantee.rolname             as role_name,
               privilege
          from pg_class relation
         cross join (select unnest(array['anon', 'authenticated']) as rolname) grantee
         -- MAINTAIN only exists on PG17+; has_table_privilege() raises
         -- 'unrecognized privilege type' on PG16 rather than returning false,
         -- so the list is built from the server version for the same reason the
         -- revoke loops above are. On PG16 the privilege cannot be held, so
         -- omitting it checks strictly everything that can exist there.
         cross join (select unnest(
                       case when current_setting('server_version_num')::int >= 170000
                            then array['TRUNCATE', 'TRIGGER', 'REFERENCES', 'MAINTAIN']
                            else array['TRUNCATE', 'TRIGGER', 'REFERENCES']
                       end) as privilege) privileges
         where relation.relnamespace = 'public'::regnamespace
           and relation.relkind in ('r', 'p')
           and has_table_privilege(grantee.rolname, relation.oid, privileges.privilege)
    loop
        raise exception using
            errcode = '42501',
            message = format('browser role %s must not hold %s on %s',
                             offender.role_name, offender.privilege, offender.qualified_name);
    end loop;
end;
$$;

revoke all on function public.assert_browser_role_truncate_contract() from public, anon, authenticated;
grant execute on function public.assert_browser_role_truncate_contract() to service_role;

select public.assert_browser_role_truncate_contract();

commit;
