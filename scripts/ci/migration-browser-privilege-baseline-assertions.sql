\set ON_ERROR_STOP on

-- The privilege baseline itself, and the exact browser-reachable inventory it
-- exposes. Two jobs, in this order, because the second is worthless without the
-- first:
--
--   1. PROVE THE BOOTSTRAP IS FAITHFUL. migration-bootstrap.sql used to create
--      anon/authenticated with no privileges at all, so a relation created in
--      public started with ZERO privileges for them in CI and ALL privileges for
--      them in a real Supabase project. Every `revoke all on table ... from
--      public, anon, authenticated` in the migration chain was therefore a no-op
--      under test: ~40 assertion files were asserting the absence of a privilege
--      that had never been granted, i.e. assertions that could not fail. That is
--      how public.billing_monthly shipped anon-SELECTable past all of them.
--      Section 1 creates a throwaway relation and requires the browser roles to
--      hold the DML defaults on it. If anyone removes the ALTER DEFAULT
--      PRIVILEGES from the bootstrap, this raises here instead of silently
--      re-vacuuming the suite. It no longer demands ALL, because 202607280027
--      revokes TRUNCATE/REFERENCES/TRIGGER from the browser roles' defaults --
--      those are asserted ABSENT in the same block, so coverage moved rather
--      than shrank.
--
--   2. PIN THE INVENTORY. Section 2 enumerates every relation and sequence in
--      public that a browser role can reach at the end of the chain and compares
--      it to the expected set, failing in BOTH directions: an extra object is a
--      new exposure, a missing one means a revoke landed and this allowlist has
--      gone stale and must be tightened. 202607280015 installs the durable
--      per-table contract for the fifteen tenant tables it names, and
--      202607280024 widens it to audit_events, organization_invitations,
--      legal_acceptances and the audit_evidence_archive schema; this file is
--      what covers everything they do not name, including any relation a future
--      migration adds without a revoke.
--
-- The tolerated entries in section 2 are NOT endorsements. They are Supabase
-- project defaults that no migration has revoked, and the only thing standing
-- between a browser JWT and them is row-level security -- so section 3 freezes
-- the policy inventory that makes them unreachable through PostgREST. If a
-- policy is ever added to one of those tables, the write privileges stop being
-- inert and section 3 fails.
--
-- Rerunnable and side-effect free: one transaction, ends in rollback.
begin;

-- ---------------------------------------------------------------------------
-- 1. The bootstrap really does reproduce Supabase's default grants.
-- ---------------------------------------------------------------------------
create table public.brevitas_privilege_baseline_probe (id bigint primary key);

do $$
declare
    v_grantee text;
    v_privilege text;
begin
    -- Non-vacuity, the original purpose of this block: the bootstrap must still
    -- reproduce Supabase's default DML grants, or every revoke assertion in this
    -- suite passes without proving anything. 202607280027 deliberately removes
    -- TRUNCATE/REFERENCES/TRIGGER from the browser roles' defaults, so those are
    -- asserted separately below rather than dropped from coverage.
    foreach v_grantee in array array['anon', 'authenticated', 'service_role'] loop
        foreach v_privilege in array array['SELECT','INSERT','UPDATE','DELETE'] loop
            if not has_table_privilege(
                v_grantee, 'public.brevitas_privilege_baseline_probe', v_privilege
            ) then
                raise exception
                    'migration-bootstrap.sql no longer reproduces Supabase default privileges: % lacks % on a newly created public relation, so every revoke assertion in this suite is vacuous',
                    v_grantee, v_privilege
                    using errcode = '55000';
            end if;
        end loop;
    end loop;
    -- service_role is the data plane and keeps the full default set.
    foreach v_privilege in array array['TRUNCATE','REFERENCES','TRIGGER'] loop
        if not has_table_privilege(
            'service_role', 'public.brevitas_privilege_baseline_probe', v_privilege
        ) then
            raise exception
                'migration-bootstrap.sql no longer reproduces Supabase default privileges: service_role lacks % on a newly created public relation',
                v_privilege
                using errcode = '55000';
        end if;
    end loop;
    -- 202607280027's contract, asserted on a table created AFTER the whole chain:
    -- a new public relation must never hand a browser role a privilege RLS does
    -- not gate. TRUNCATE is the load-bearing one -- it is not subject to
    -- row-level security at all, so the public anon key could otherwise empty
    -- the audit trail, the compliance evidence or the webhook inbox.
    foreach v_grantee in array array['anon', 'authenticated'] loop
        foreach v_privilege in array array['TRUNCATE','REFERENCES','TRIGGER'] loop
            if has_table_privilege(
                v_grantee, 'public.brevitas_privilege_baseline_probe', v_privilege
            ) then
                raise exception
                    '202607280027 default-privilege contract broken: % gained % on a newly created public relation',
                    v_grantee, v_privilege
                    using errcode = '42501';
            end if;
        end loop;
    end loop;
    if not has_schema_privilege('anon', 'public', 'USAGE')
       or not has_schema_privilege('authenticated', 'public', 'USAGE') then
        raise exception
            'migration-bootstrap.sql no longer grants USAGE on schema public to the browser roles, so no browser-role assertion can reach any object'
            using errcode = '55000';
    end if;
end;
$$;

drop table public.brevitas_privilege_baseline_probe;

-- ---------------------------------------------------------------------------
-- 2. The exact browser-reachable inventory of schema public.
-- ---------------------------------------------------------------------------
do $$
declare
    v_actual text;
    v_expected text;
begin
    select coalesce(string_agg(entry, E'\n' order by entry), '(none)')
      into v_actual
      from (
        select format(
                   '%s %s %s: %s',
                   case relation.relkind when 'S' then 'sequence' else 'relation' end,
                   relation.relname,
                   grantee.rolname,
                   string_agg(privilege.name, ',' order by privilege.name)
               ) as entry
          from pg_class relation
          join pg_namespace namespace
            on namespace.oid = relation.relnamespace
           and namespace.nspname = 'public'
          cross join (values ('anon'), ('authenticated')) as grantee(rolname)
          cross join unnest(array[
              'SELECT','INSERT','UPDATE','DELETE',
              'TRUNCATE','REFERENCES','TRIGGER','USAGE'
          ]) as privilege(name)
         where relation.relkind in ('r', 'p', 'v', 'm', 'f', 'S')
           and case
                 when relation.relkind = 'S' then
                   privilege.name in ('USAGE', 'SELECT', 'UPDATE')
                   and has_sequence_privilege(grantee.rolname, relation.oid, privilege.name)
                 else
                   privilege.name <> 'USAGE'
                   and has_table_privilege(grantee.rolname, relation.oid, privilege.name)
               end
         group by relation.relkind, relation.relname, grantee.rolname
      ) reachable;

    -- Tolerated Supabase project defaults, one line per (object, browser role).
    -- ALL-privilege entries are tables no migration revokes: billing_events
    -- relies on RLS alone (section 3), and three of the sequences belong to
    -- revoked tables. profiles and legal_acceptances keep SELECT for their
    -- respective "view own" policies (202607280015 / 202607280024);
    -- stripe_webhook_events is partially revoked. audit_events,
    -- organization_invitations and audit_events_id_seq were revoked to zero by
    -- 202607280024.
    v_expected := concat_ws(E'\n',
        'relation billing_events anon: DELETE,INSERT,SELECT,UPDATE',
        'relation billing_events authenticated: DELETE,INSERT,SELECT,UPDATE',
        'relation legal_acceptances anon: SELECT',
        'relation legal_acceptances authenticated: SELECT',
        'relation profiles anon: SELECT',
        'relation profiles authenticated: SELECT',
        'relation stripe_webhook_events anon: SELECT',
        'relation stripe_webhook_events authenticated: SELECT',
        'sequence billing_ledger_id_seq anon: SELECT,UPDATE,USAGE',
        'sequence billing_ledger_id_seq authenticated: SELECT,UPDATE,USAGE',
        'sequence billing_recovery_audit_id_seq anon: SELECT,UPDATE,USAGE',
        'sequence billing_recovery_audit_id_seq authenticated: SELECT,UPDATE,USAGE',
        'sequence period_settlement_ledger_id_seq anon: SELECT,UPDATE,USAGE',
        'sequence period_settlement_ledger_id_seq authenticated: SELECT,UPDATE,USAGE'
    );

    if v_actual is distinct from v_expected then
        raise exception '%', concat_ws(E'\n',
            'browser-reachable inventory of schema public changed.',
            'expected:', v_expected,
            'actual:', v_actual)
            using errcode = '55000',
                  hint = 'An EXTRA line is a new anon/authenticated exposure: add the revoke to a forward migration. A MISSING line means a revoke landed and this allowlist must be tightened.';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. What makes the remaining tolerated privileges inert: RLS with no browser
--    policy. PostgREST cannot emit TRUNCATE, so the reachable surface on those
--    tables is exactly the DML that RLS admits -- which is none while they carry
--    no policy. Freeze that, and freeze billing_events/legal_acceptances at
--    read-only policies, so the tolerance cannot quietly become exploitable.
--    audit_events and organization_invitations no longer carry any browser
--    privilege (202607280024), but their policy-free state is frozen anyway:
--    a policy appearing there would mean someone intends browser access.
-- ---------------------------------------------------------------------------
do $$
declare
    v_table text;
    v_policy record;
begin
    foreach v_table in array array[
        'audit_events', 'organization_invitations', 'stripe_webhook_events'
    ] loop
        if not (
            select relation.relrowsecurity
              from pg_class relation
             where relation.oid = format('public.%I', v_table)::regclass
        ) then
            raise exception
                'public.% retains browser privileges with row-level security disabled',
                v_table
                using errcode = '55000';
        end if;
        if exists (
            select 1 from pg_policy policy
             where policy.polrelid = format('public.%I', v_table)::regclass
        ) then
            raise exception
                'public.% gained a policy while browser roles still hold ALL privileges on it; revoke the privileges in a forward migration first',
                v_table
                using errcode = '55000';
        end if;
    end loop;

    for v_policy in
        select policy.polrelid::regclass::text as relation_name,
               policy.polname,
               policy.polcmd
          from pg_policy policy
         where policy.polrelid in (
                   'public.billing_events'::regclass,
                   'public.legal_acceptances'::regclass
               )
    loop
        -- 'r' is SELECT. Anything else is a write policy, which would turn the
        -- unrevoked INSERT/UPDATE/DELETE privileges into a live write path.
        if v_policy.polcmd <> 'r' then
            raise exception
                '% has a non-SELECT policy (%: %) while browser roles still hold write privileges on it',
                v_policy.relation_name, v_policy.polname, v_policy.polcmd
                using errcode = '55000';
        end if;
    end loop;
end;
$$;

rollback;
