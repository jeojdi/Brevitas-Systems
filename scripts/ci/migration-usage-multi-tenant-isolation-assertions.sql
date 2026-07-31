-- END-TO-END tenant-isolation proof for 202607280035_usage_read_tenant_scope.
--
-- TWO organizations, each with usage and its own key, plus an ORG-LESS key
-- whose owner is an ACTIVE MEMBER of one of them and who ALSO has rows
-- attributed inside the OTHER one. Proves:
--   * the org-less key reads NEITHER org's rows through usage_page,
--     usage_stats, usage_breakdown or usage_grouped;
--   * each org's own key still reads exactly its own rows, money included;
--   * neither org key can see the other org's rows.
--
-- The scope triple passed to every RPC is DERIVED FROM public.api_keys the way
-- api/store.py does it (key_context, store.py:4093-4106 -> _usage_scope,
-- store.py:4603-4608): p_key_hash = the authenticated key's hash,
-- p_organization_id = its organization_id or NULL, p_owner_id = its owner_id
-- or ''. Nothing here hand-picks the arguments.
--
-- Section 0 first proves the fixture is NON-VACUOUS by evaluating the
-- PRE-0035 predicate inline: if the fixture did not actually reach the hole,
-- the isolation assertions below would pass for the wrong reason.
--
-- WHY THIS FILE EXISTS ALONGSIDE migration-usage-tenant-scope-assertions.sql.
-- That file has exactly ONE organization, so every row it withholds from the
-- org-less key is withheld by a predicate that only ever has one tenant to
-- choose between. It cannot distinguish "org-less keys read no org rows" from
-- "org-less keys read only THEIR OWN org's rows" -- and it never exercises an
-- org key at all against a second tenant's data. The row this file turns on is
-- `beta-cross-owner`: the org-less caller's OWNER STRING inside a tenant the
-- owner is NOT a member of. Under the pre-0035 predicate that row is handed
-- over on owner_id alone, and no single-organization fixture contains it.
--
-- Opens its own transaction and ends in ROLLBACK, so it is rerunnable,
-- order-independent, and leaves no user, organization, key or usage row behind.
\set ON_ERROR_STOP on

begin;

-- Section -1 -- registration guard, matching
-- migration-usage-tenant-scope-assertions.sql: if 202607280035 is missing from
-- the manifests this file must say so in those words rather than failing later
-- as if the fix had regressed.
do $$
begin
    if (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public'
           and p.proname in ('usage_page', 'usage_stats', 'usage_breakdown', 'usage_grouped')
           and p.prosrc like '%(usage.organization_id is null and usage.owner_id = p_owner_id)%'
       ) <> 4 then
        raise exception
            '202607280035_usage_read_tenant_scope is not applied: register it in both migration manifests';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Fixture
-- ---------------------------------------------------------------------------
insert into auth.users(id, email) values
    ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001', 'alpha-owner@example.invalid'),
    ('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb001', 'beta-owner@example.invalid');

insert into public.organizations(id, name) values
    ('4a000000-0000-4000-8000-00000000000a', 'Tenant Alpha'),
    ('4b000000-0000-4000-8000-00000000000b', 'Tenant Beta');

-- The org-less key's owner is a REAL, ACTIVE member of Alpha. Membership is
-- what makes the pre-fix exposure look harmless on production ("everyone who
-- crosses is a member anyway"); it must not be what makes the read legal.
insert into public.organization_members(organization_id, user_id, role, status) values
    ('4a000000-0000-4000-8000-00000000000a',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001', 'company_owner', 'active'),
    ('4b000000-0000-4000-8000-00000000000b',
     'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb001', 'company_owner', 'active');

insert into public.api_keys(key_hash, name, owner_id, organization_id, key_type, scopes) values
    ('e2e-alpha-org-key', 'Alpha tenant key',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001',
     '4a000000-0000-4000-8000-00000000000a', 'legacy', array['usage:read_own']),
    ('e2e-beta-org-key', 'Beta tenant key',
     'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb001',
     '4b000000-0000-4000-8000-00000000000b', 'legacy', array['usage:read_own']),
    -- THE CALLER UNDER TEST. Same owner string as Alpha's key, no organization.
    ('e2e-orgless-key', 'Org-less legacy key',
     'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001', null, 'legacy', array['usage:read_own']);

insert into public.usage_log(
    key_hash, owner_id, organization_id, ts, request_id, project, pipeline,
    agent, run_id, baseline_tokens, optimized_tokens, tokens_saved,
    pricing_status, baseline_cost_usd, actual_cost_usd, measured_savings_usd,
    verified_savings_usd, brevitas_fee_usd, authoritative, receipt_source
) values
    -- ALPHA. Row 1 carries the SAME owner string as the org-less caller: this
    -- is the exact production shape the hole exposed.
    ('e2e-alpha-org-key', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001',
     '4a000000-0000-4000-8000-00000000000a', '2026-03-01 12:00:00+00',
     'alpha-same-owner', 'alpha-proj', 'alpha-pipe', 'alpha-agent', 'alpha-run',
     1000, 400, 600, 'priced', 100, 40, 60, 60, 15, true, 'proxy'),
    -- Row 2: a different owner inside Alpha. No branch may ever hand this over.
    ('e2e-alpha-org-key', 'cccccccc-cccc-4ccc-8ccc-ccccccccc001',
     '4a000000-0000-4000-8000-00000000000a', '2026-03-01 11:00:00+00',
     'alpha-stranger', 'alpha-proj', 'alpha-pipe', 'alpha-agent', 'alpha-run',
     2000, 800, 1200, 'priced', 200, 80, 120, 120, 30, true, 'proxy'),
    -- BETA. Row 3 is Beta's own owner.
    ('e2e-beta-org-key', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbb001',
     '4b000000-0000-4000-8000-00000000000b', '2026-03-01 10:00:00+00',
     'beta-own', 'beta-proj', 'beta-pipe', 'beta-agent', 'beta-run',
     4000, 1000, 3000, 'priced', 400, 100, 300, 300, 75, true, 'proxy'),
    -- Row 4: the org-less caller's OWNER STRING appearing inside the OTHER
    -- tenant, which it is NOT a member of. This is the "cannot read EITHER
    -- org" case, and the one membership would not have excused.
    ('e2e-beta-org-key', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001',
     '4b000000-0000-4000-8000-00000000000b', '2026-03-01 09:00:00+00',
     'beta-cross-owner', 'beta-proj', 'beta-pipe', 'beta-agent', 'beta-run',
     8000, 2000, 6000, 'priced', 800, 200, 600, 600, 150, true, 'proxy'),
    -- The org-less key's OWN untenanted row. It MUST stay readable.
    ('e2e-orgless-key', 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001',
     null, '2026-03-01 08:00:00+00',
     'orgless-free', 'free-proj', 'free-pipe', 'free-agent', 'free-run',
     10, 4, 6, 'priced', 1, 0.4, 0.6, 0.6, 0.15, false, 'proxy');

-- The scope triple api/store.py would compute for each key, from api_keys.
create temporary table e2e_scope on commit drop as
select key.key_hash                              as p_key_hash,
       key.organization_id                       as p_organization_id,
       coalesce(key.owner_id, '')                as p_owner_id,
       key.name                                  as label
  from public.api_keys key
 where key.key_hash in ('e2e-alpha-org-key', 'e2e-beta-org-key', 'e2e-orgless-key')
   and key.revoked_at is null;

do $$
declare
    v_org uuid;
    v_owner text;
begin
    if (select count(*) from e2e_scope) <> 3 then
        raise exception 'fixture did not produce three authenticable keys';
    end if;
    select p_organization_id, p_owner_id into v_org, v_owner
      from e2e_scope where p_key_hash = 'e2e-orgless-key';
    -- The caller under test must actually TAKE the org-less branch.
    if v_org is not null or v_owner <> 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaa001' then
        raise exception 'the org-less key does not take the org-less branch: %, %',
            v_org, v_owner;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 0 -- NON-VACUITY. Evaluate the PRE-0035 predicate inline (no
-- function is redefined) and confirm the fixture reaches the hole: the
-- org-less key WOULD have been handed rows from BOTH organizations.
-- ---------------------------------------------------------------------------
do $$
declare
    v_requests text[];
    v_orgs bigint;
begin
    select array_agg(usage.request_id order by usage.request_id),
           count(distinct usage.organization_id)
      into v_requests, v_orgs
      from public.usage_log usage,
           lateral (select p_key_hash, p_organization_id, p_owner_id
                      from e2e_scope where p_key_hash = 'e2e-orgless-key') scope
     where (scope.p_organization_id is not null
            and usage.organization_id = scope.p_organization_id)
        or (scope.p_organization_id is null and scope.p_owner_id <> ''
            and (usage.owner_id = scope.p_owner_id
                 or usage.key_hash = scope.p_key_hash))
        or (scope.p_organization_id is null and scope.p_owner_id = ''
            and usage.key_hash = scope.p_key_hash);
    if v_orgs < 2 then
        raise exception
            'FIXTURE IS VACUOUS: the pre-0035 predicate reached % organizations, not 2 (%)',
            v_orgs, v_requests;
    end if;
    raise notice 'non-vacuity: the PRE-0035 predicate handed the org-less key % (% orgs)',
        v_requests, v_orgs;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 1 -- usage_page. Returns setof public.usage_log, i.e. every column
-- including baseline/actual/verified/fee.
-- ---------------------------------------------------------------------------
do $$
declare
    s record;
    v_requests text[];
    v_leak text[];
begin
    select * into s from e2e_scope where p_key_hash = 'e2e-orgless-key';

    select array_agg(page.request_id order by page.request_id) into v_requests
      from public.usage_page(s.p_key_hash, s.p_organization_id, s.p_owner_id,
                            null, null, 200) page;
    if v_requests is distinct from array['orgless-free'] then
        raise exception 'usage_page gave the org-less key %', v_requests;
    end if;

    -- Restated as the property, so a fixture edit cannot make the above vacuous.
    select array_agg(page.request_id order by page.request_id) into v_leak
      from public.usage_page(s.p_key_hash, s.p_organization_id, s.p_owner_id,
                            null, null, 200) page
     where page.organization_id is not null;
    if v_leak is not null then
        raise exception 'usage_page handed org-owned rows to an org-less key: %', v_leak;
    end if;

    -- Alpha reads exactly Alpha, both owners, and nothing of Beta's.
    select * into s from e2e_scope where p_key_hash = 'e2e-alpha-org-key';
    select array_agg(page.request_id order by page.request_id) into v_requests
      from public.usage_page(s.p_key_hash, s.p_organization_id, s.p_owner_id,
                            null, null, 200) page;
    if v_requests is distinct from array['alpha-same-owner', 'alpha-stranger'] then
        raise exception 'the Alpha tenant read is wrong: %', v_requests;
    end if;

    -- Beta reads exactly Beta, including the row carrying Alpha's owner string.
    select * into s from e2e_scope where p_key_hash = 'e2e-beta-org-key';
    select array_agg(page.request_id order by page.request_id) into v_requests
      from public.usage_page(s.p_key_hash, s.p_organization_id, s.p_owner_id,
                            null, null, 200) page;
    if v_requests is distinct from array['beta-cross-owner', 'beta-own'] then
        raise exception 'the Beta tenant read is wrong: %', v_requests;
    end if;

    -- Neither org key sees the other org, whatever the owner strings say.
    if exists (
        select 1 from e2e_scope scope,
             lateral public.usage_page(scope.p_key_hash, scope.p_organization_id,
                                       scope.p_owner_id, null, null, 200) page
         where scope.p_organization_id is not null
           and page.organization_id is distinct from scope.p_organization_id
    ) then
        raise exception 'an org key crossed into another tenant through usage_page';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 2 -- usage_stats. The money surface: it SUMS baseline / actual /
-- verified / fee over whatever the predicate admits.
-- ---------------------------------------------------------------------------
do $$
declare
    s record;
    v jsonb;
begin
    select * into s from e2e_scope where p_key_hash = 'e2e-orgless-key';
    v := public.usage_stats(s.p_key_hash, s.p_organization_id, s.p_owner_id);
    -- Only the free row: 1 / 0.4 / 0.6 / 0.15.
    if (v->>'total_calls')::bigint <> 1
       or (v->>'total_baseline_cost_usd')::numeric <> 1
       or (v->>'total_actual_cost_usd')::numeric <> 0.4
       or (v->>'total_verified_savings_usd')::numeric <> 0.6
       or (v->>'total_brevitas_fee_usd')::numeric <> 0.15 then
        raise exception 'usage_stats leaked tenant money to an org-less key: %', v;
    end if;
    if exists (select 1 from jsonb_array_elements(v->'history') entry
                where entry->>'project' in ('alpha-proj', 'beta-proj')) then
        raise exception 'usage_stats history leaked tenant rows to an org-less key';
    end if;

    -- Alpha: 100 + 200 baseline, 40 + 80 actual, 60 + 120 verified, 15 + 30 fee.
    select * into s from e2e_scope where p_key_hash = 'e2e-alpha-org-key';
    v := public.usage_stats(s.p_key_hash, s.p_organization_id, s.p_owner_id);
    if (v->>'total_calls')::bigint <> 2
       or (v->>'total_baseline_cost_usd')::numeric <> 300
       or (v->>'total_actual_cost_usd')::numeric <> 120
       or (v->>'total_verified_savings_usd')::numeric <> 180
       or (v->>'total_brevitas_fee_usd')::numeric <> 45 then
        raise exception 'the Alpha usage_stats read is wrong: %', v;
    end if;
    if exists (select 1 from jsonb_array_elements(v->'history') entry
                where entry->>'project' <> 'alpha-proj') then
        raise exception 'Alpha usage_stats history carried a foreign project';
    end if;

    -- Beta: 400 + 800 baseline, 100 + 200 actual, 300 + 600 verified, 75 + 150 fee.
    select * into s from e2e_scope where p_key_hash = 'e2e-beta-org-key';
    v := public.usage_stats(s.p_key_hash, s.p_organization_id, s.p_owner_id);
    if (v->>'total_calls')::bigint <> 2
       or (v->>'total_baseline_cost_usd')::numeric <> 1200
       or (v->>'total_actual_cost_usd')::numeric <> 300
       or (v->>'total_verified_savings_usd')::numeric <> 900
       or (v->>'total_brevitas_fee_usd')::numeric <> 225 then
        raise exception 'the Beta usage_stats read is wrong: %', v;
    end if;
    if exists (select 1 from jsonb_array_elements(v->'history') entry
                where entry->>'project' <> 'beta-proj') then
        raise exception 'Beta usage_stats history carried a foreign project';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 3 -- usage_breakdown, which projects the same money columns through
-- a GROUP BY.
-- ---------------------------------------------------------------------------
do $$
declare
    s record;
    v_projects text[];
    v_calls bigint;
    v_fee numeric;
    v_baseline numeric;
begin
    select * into s from e2e_scope where p_key_hash = 'e2e-orgless-key';
    select array_agg(distinct b.project), sum(b.calls), sum(b.brevitas_fee_usd),
           sum(b.baseline_cost_usd)
      into v_projects, v_calls, v_fee, v_baseline
      from public.usage_breakdown(s.p_key_hash, s.p_organization_id,
                                  s.p_owner_id, 500) b;
    if v_projects is distinct from array['free-proj']
       or v_calls <> 1 or v_fee <> 0.15 or v_baseline <> 1 then
        raise exception 'usage_breakdown leaked to an org-less key: %, %, %, %',
            v_projects, v_calls, v_fee, v_baseline;
    end if;

    select * into s from e2e_scope where p_key_hash = 'e2e-alpha-org-key';
    select array_agg(distinct b.project), sum(b.calls), sum(b.brevitas_fee_usd),
           sum(b.baseline_cost_usd)
      into v_projects, v_calls, v_fee, v_baseline
      from public.usage_breakdown(s.p_key_hash, s.p_organization_id,
                                  s.p_owner_id, 500) b;
    if v_projects is distinct from array['alpha-proj']
       or v_calls <> 2 or v_fee <> 45 or v_baseline <> 300 then
        raise exception 'the Alpha usage_breakdown read is wrong: %, %, %, %',
            v_projects, v_calls, v_fee, v_baseline;
    end if;

    select * into s from e2e_scope where p_key_hash = 'e2e-beta-org-key';
    select array_agg(distinct b.project), sum(b.calls), sum(b.brevitas_fee_usd),
           sum(b.baseline_cost_usd)
      into v_projects, v_calls, v_fee, v_baseline
      from public.usage_breakdown(s.p_key_hash, s.p_organization_id,
                                  s.p_owner_id, 500) b;
    if v_projects is distinct from array['beta-proj']
       or v_calls <> 2 or v_fee <> 225 or v_baseline <> 1200 then
        raise exception 'the Beta usage_breakdown read is wrong: %, %, %, %',
            v_projects, v_calls, v_fee, v_baseline;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 4 -- usage_grouped, across all three groupings it accepts. Each
-- grouping is a separate projection of the same predicate, so each is checked.
-- ---------------------------------------------------------------------------
do $$
declare
    s record;
    v_field text;
    v_suffix text;
    v_labels text[];
    v_calls bigint;
    v_fee numeric;
begin
    foreach v_field in array array['pipeline', 'agent', 'run_id']
    loop
        -- The fixture's label for this grouping column.
        v_suffix := case v_field when 'pipeline' then 'pipe'
                                 when 'agent' then 'agent' else 'run' end;
        select * into s from e2e_scope where p_key_hash = 'e2e-orgless-key';
        select array_agg(distinct coalesce(g.pipeline, g.agent, g.run_id)),
               sum(g.calls), sum(g.brevitas_fee_usd)
          into v_labels, v_calls, v_fee
          from public.usage_grouped(s.p_key_hash, s.p_organization_id,
                                    s.p_owner_id, v_field, null, null, null, 500) g;
        if v_calls <> 1 or v_fee <> 0.15
           or v_labels is distinct from array['free-' || v_suffix] then
            raise exception 'usage_grouped(%) leaked to an org-less key: %, %, %',
                v_field, v_labels, v_calls, v_fee;
        end if;

        select * into s from e2e_scope where p_key_hash = 'e2e-alpha-org-key';
        select array_agg(distinct coalesce(g.pipeline, g.agent, g.run_id)),
               sum(g.calls), sum(g.brevitas_fee_usd)
          into v_labels, v_calls, v_fee
          from public.usage_grouped(s.p_key_hash, s.p_organization_id,
                                    s.p_owner_id, v_field, null, null, null, 500) g;
        if v_calls <> 2 or v_fee <> 45
           or v_labels is distinct from array['alpha-' || v_suffix] then
            raise exception 'the Alpha usage_grouped(%) read is wrong: %, %, %',
                v_field, v_labels, v_calls, v_fee;
        end if;

        select * into s from e2e_scope where p_key_hash = 'e2e-beta-org-key';
        select array_agg(distinct coalesce(g.pipeline, g.agent, g.run_id)),
               sum(g.calls), sum(g.brevitas_fee_usd)
          into v_labels, v_calls, v_fee
          from public.usage_grouped(s.p_key_hash, s.p_organization_id,
                                    s.p_owner_id, v_field, null, null, null, 500) g;
        if v_calls <> 2 or v_fee <> 225
           or v_labels is distinct from array['beta-' || v_suffix] then
            raise exception 'the Beta usage_grouped(%) read is wrong: %, %, %',
                v_field, v_labels, v_calls, v_fee;
        end if;
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- Section 5 -- conservation. Every fixture row is read by EXACTLY the key that
-- should read it, and the fee the three keys can see between them sums to the
-- fixture total with no row double-counted across tenants.
-- ---------------------------------------------------------------------------
do $$
declare
    v_total numeric;
    v_seen numeric;
begin
    select sum(brevitas_fee_usd) into v_total from public.usage_log
     where request_id like 'alpha-%' or request_id like 'beta-%'
        or request_id = 'orgless-free';
    select sum(page.brevitas_fee_usd) into v_seen
      from e2e_scope scope,
           lateral public.usage_page(scope.p_key_hash, scope.p_organization_id,
                                     scope.p_owner_id, null, null, 200) page;
    if v_total <> 270.15 or v_seen <> v_total then
        raise exception 'partition is not exact: fixture %, visible %', v_total, v_seen;
    end if;
    raise notice 'partition exact: $% of fees, each row visible to exactly one key', v_total;
end;
$$;

rollback;
