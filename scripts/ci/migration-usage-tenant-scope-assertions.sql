-- 202607280035: an ORG-LESS key may not read ORG-OWNED usage rows.
--
-- The four usage read RPCs (usage_page, usage_stats, usage_breakdown,
-- usage_grouped) share one authorization predicate whose ORG-LESS branch --
-- taken when the calling key has organization_id IS NULL -- used to authorize
-- on the owner_id STRING alone:
--
--     or (p_organization_id is null and p_owner_id <> ''
--         and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
--
-- Nothing there constrained usage.organization_id, so an org-less key was
-- handed every row its owner string ever wrote, tenant-owned rows included,
-- with baseline_cost_usd / actual_cost_usd / verified_savings_usd /
-- brevitas_fee_usd on them. On production that reached 7,477 org-scoped rows
-- across 3 organizations.
--
-- WHY THIS FILE EXISTS AT ALL. scripts/ci/migration-assertions.sql:186-192
-- already asserts "usage_page crossed the tenant boundary" -- but it passes a
-- NON-NULL p_organization_id and an EMPTY p_owner_id, so it only ever exercises
-- branch 1, which was never the broken one. The org-less branch was exercised
-- nowhere in scripts/ci. Every assertion below is on that branch.
--
-- FIXTURE SHAPE, taken from what production actually looks like: one owner
-- string holds BOTH an org-attached key and an org-less key (18 of the 43
-- org-less production keys are in exactly that state). The org-less key is the
-- caller; the org row it must not see was written by the OTHER key.
--
-- Opens its own transaction and ends in ROLLBACK, so it is rerunnable,
-- order-independent, and leaves no organization, key or usage row behind.
\set ON_ERROR_STOP on

begin;

-- Section 0 -- registration guard. If 202607280035 is missing from
-- migration-fresh-manifest.txt / migration-upgrade-manifest.txt this file must
-- say so in those words rather than failing later as if the fix had regressed.
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

-- billing_owner_id stays NULL: it is FK-bound to auth.users and this file needs
-- no billing identity, only a tenant boundary.
insert into public.organizations(id, name, legacy_owner_id) values
    ('35000000-0000-4000-8000-000000000035', 'Usage scope tenant', 'usage-scope-owner');

insert into public.api_keys(key_hash, name, owner_id, organization_id, key_type) values
    -- The tenant's own key. It WROTE the org-owned row.
    ('usage-scope-org-key', 'Usage scope org key',
     'cccccccc-cccc-4ccc-8ccc-ccccccccc035', '35000000-0000-4000-8000-000000000035', 'legacy'),
    -- The caller under test: same owner string, NO organization. This is the
    -- production shape -- 43 such keys, all key_type='legacy', all unrevoked,
    -- all carrying usage:read_own so no route-level scope gate stops them.
    ('usage-scope-orgless-key', 'Usage scope org-less key',
     'cccccccc-cccc-4ccc-8ccc-ccccccccc035', null, 'legacy'),
    -- A key with neither organization nor owner, for the third predicate branch.
    ('usage-scope-anon-key', 'Usage scope key-hash-only key', '', null, 'legacy');

insert into public.usage_log(
    key_hash, owner_id, organization_id, ts, request_id, project, pipeline, agent,
    baseline_tokens, optimized_tokens, tokens_saved, pricing_status,
    baseline_cost_usd, actual_cost_usd, measured_savings_usd,
    verified_savings_usd, brevitas_fee_usd, authoritative, receipt_source
) values
    -- THE ROW THE ORG-LESS KEY MUST NOT SEE. Written by the org key, owned by
    -- the organization, same owner string as the caller.
    ('usage-scope-org-key', 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
     '35000000-0000-4000-8000-000000000035', '2026-02-01 12:00:00+00',
     'usage-scope-org-row', 'scope-org', 'scope-pipeline', 'scope-agent',
     1000, 400, 600, 'priced', 70, 30, 40, 40, 10, true, 'proxy'),
    -- The org-less key's own untenanted row. It MUST still be visible.
    ('usage-scope-orgless-key', 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
     null, '2026-02-01 11:00:00+00',
     'usage-scope-free-row', 'scope-free', 'scope-pipeline', 'scope-agent',
     100, 40, 60, 'priced', 7, 3, 4, 4, 1, false, 'proxy'),
    -- SELF-SCOPE, pinned deliberately: an org-stamped row the CALLER'S OWN key
    -- wrote. api/store.py:679-694 lets a caller supply organization_id on its
    -- own receipt, and the key_hash is a secret digest that api/store.py's
    -- _usage_scope takes from the authenticated api_keys row -- never from the
    -- caller -- so a row carrying it was written BY this key. 202607280035
    -- leaves that sub-branch alone (branch 3 below grants the same thing
    -- unconditionally, so restricting only one of them would make the two
    -- self-scope branches disagree). Production has 0 rows in this state.
    ('usage-scope-orgless-key', 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
     '35000000-0000-4000-8000-000000000035', '2026-02-01 10:00:00+00',
     'usage-scope-self-row', 'scope-self', 'scope-pipeline', 'scope-agent',
     10, 4, 6, 'priced', 0.7, 0.3, 0.4, 0.4, 0.1, false, 'proxy'),
    -- A row belonging to a DIFFERENT owner inside the same organization. No
    -- branch may ever hand this to the org-less caller.
    ('usage-scope-org-key', 'dddddddd-dddd-4ddd-8ddd-ddddddddd035',
     '35000000-0000-4000-8000-000000000035', '2026-02-01 09:00:00+00',
     'usage-scope-stranger-row', 'scope-stranger', 'scope-pipeline', 'scope-agent',
     5000, 2000, 3000, 'priced', 350, 150, 200, 200, 50, true, 'proxy');

-- Section 1 -- usage_page. Returns `setof public.usage_log`, i.e. EVERY column
-- including the money ones, so this is the widest of the four.
do $$
declare
    v_requests text[];
begin
    select array_agg(page.request_id order by page.request_id)
      into v_requests
      from public.usage_page(
        'usage-scope-orgless-key', null, 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
        null, null, 200
      ) page;
    if v_requests is distinct from array['usage-scope-free-row', 'usage-scope-self-row'] then
        raise exception
            'usage_page crossed the tenant boundary for an org-less key: %', v_requests;
    end if;

    -- Restated as the property rather than as a row list, so a future fixture
    -- edit cannot make the assertion above pass vacuously.
    if exists (
        select 1 from public.usage_page(
          'usage-scope-orgless-key', null, 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
          null, null, 200
        ) page
        where page.organization_id is not null
          and page.key_hash <> 'usage-scope-orgless-key'
    ) then
        raise exception 'usage_page handed an org-owned row to a key with no organization';
    end if;

    -- Branch 1 is untouched: the org-attached key still reads the whole tenant,
    -- including the other owner's row. Without this the fix could "pass" by
    -- breaking the tenant read outright.
    select array_agg(page.request_id order by page.request_id)
      into v_requests
      from public.usage_page(
        'usage-scope-org-key', '35000000-0000-4000-8000-000000000035', '',
        null, null, 200
      ) page;
    if v_requests is distinct from array[
        'usage-scope-org-row', 'usage-scope-self-row', 'usage-scope-stranger-row'] then
        raise exception 'the tenant-scoped read regressed: %', v_requests;
    end if;

    -- Branch 3 (no organization, no owner) still resolves by key_hash alone.
    select array_agg(page.request_id order by page.request_id)
      into v_requests
      from public.usage_page('usage-scope-orgless-key', null, '', null, null, 200) page;
    if v_requests is distinct from array['usage-scope-free-row', 'usage-scope-self-row'] then
        raise exception 'the key-hash-only branch regressed: %', v_requests;
    end if;
    if exists (
        select 1 from public.usage_page('usage-scope-anon-key', null, '', null, null, 200)
    ) then
        raise exception 'a key that wrote nothing was handed rows';
    end if;
end;
$$;

-- Section 2 -- usage_stats. This is the money surface: it SUMS
-- baseline/actual/verified/fee over whatever the predicate admits, and
-- api/server.py's _spend_readable returns true for legacy keys, so nothing
-- downstream redacts the result.
do $$
declare
    v_stats jsonb;
begin
    select public.usage_stats(
        'usage-scope-orgless-key', null, 'cccccccc-cccc-4ccc-8ccc-ccccccccc035')
      into v_stats;
    -- free row (7 / 3 / 4 / 1) + self row (0.7 / 0.3 / 0.4 / 0.1). The org row
    -- (70 / 30 / 40 / 10) and the stranger row (350 / 150 / 200 / 50) are out.
    if (v_stats->>'total_calls')::bigint <> 2 then
        raise exception 'usage_stats counted % calls for an org-less key',
            v_stats->>'total_calls';
    end if;
    if (v_stats->>'total_baseline_cost_usd')::numeric <> 7.7
       or (v_stats->>'total_actual_cost_usd')::numeric <> 3.3
       or (v_stats->>'total_verified_savings_usd')::numeric <> 4.4
       or (v_stats->>'total_brevitas_fee_usd')::numeric <> 1.1 then
        raise exception 'usage_stats leaked tenant money to an org-less key: %', v_stats;
    end if;
    -- The history array carries per-row money too, so it needs its own check.
    if exists (
        select 1 from jsonb_array_elements(v_stats->'history') entry
         where entry->>'project' in ('scope-org', 'scope-stranger')
    ) then
        raise exception 'usage_stats history leaked tenant rows to an org-less key';
    end if;

    -- Branch 1 still sums the whole tenant: 70 + 0.7 + 350.
    select public.usage_stats(
        'usage-scope-org-key', '35000000-0000-4000-8000-000000000035', '')
      into v_stats;
    if (v_stats->>'total_calls')::bigint <> 3
       or (v_stats->>'total_baseline_cost_usd')::numeric <> 420.7 then
        raise exception 'the tenant-scoped usage_stats regressed: %', v_stats;
    end if;
end;
$$;

-- Section 3 -- usage_breakdown and usage_grouped, which project the same money
-- columns through a GROUP BY.
do $$
declare
    v_projects text[];
    v_fee numeric;
    v_calls bigint;
begin
    select array_agg(breakdown.project order by breakdown.project), sum(breakdown.brevitas_fee_usd)
      into v_projects, v_fee
      from public.usage_breakdown(
        'usage-scope-orgless-key', null, 'cccccccc-cccc-4ccc-8ccc-ccccccccc035', 500) breakdown;
    if v_projects is distinct from array['scope-free', 'scope-self'] or v_fee <> 1.1 then
        raise exception 'usage_breakdown leaked tenant rows to an org-less key: % / %',
            v_projects, v_fee;
    end if;

    select sum(grouped.calls), sum(grouped.brevitas_fee_usd)
      into v_calls, v_fee
      from public.usage_grouped(
        'usage-scope-orgless-key', null, 'cccccccc-cccc-4ccc-8ccc-ccccccccc035',
        'pipeline', null, null, null, 500) grouped;
    if v_calls <> 2 or v_fee <> 1.1 then
        raise exception 'usage_grouped leaked tenant rows to an org-less key: % calls, % fee',
            v_calls, v_fee;
    end if;

    -- Branch 1 unchanged for both: 3 rows, 10 + 0.1 + 50 in fees.
    select sum(breakdown.calls), sum(breakdown.brevitas_fee_usd)
      into v_calls, v_fee
      from public.usage_breakdown(
        'usage-scope-org-key', '35000000-0000-4000-8000-000000000035', '', 500) breakdown;
    if v_calls <> 3 or v_fee <> 60.1 then
        raise exception 'the tenant-scoped usage_breakdown regressed: % calls, % fee',
            v_calls, v_fee;
    end if;
    select sum(grouped.calls), sum(grouped.brevitas_fee_usd)
      into v_calls, v_fee
      from public.usage_grouped(
        'usage-scope-org-key', '35000000-0000-4000-8000-000000000035', '',
        'agent', null, null, null, 500) grouped;
    if v_calls <> 3 or v_fee <> 60.1 then
        raise exception 'the tenant-scoped usage_grouped regressed: % calls, % fee',
            v_calls, v_fee;
    end if;
end;
$$;

-- Section 4 -- the browser roles still cannot call any of the four. The whole
-- predicate argument is moot if anon can invoke these directly with an
-- attacker-chosen p_organization_id.
do $$
begin
    if has_function_privilege('anon',
        'public.usage_page(text,uuid,text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('anon', 'public.usage_stats(text,uuid,text)', 'execute')
       or has_function_privilege('anon', 'public.usage_breakdown(text,uuid,text,integer)', 'execute')
       or has_function_privilege('anon',
        'public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.usage_page(text,uuid,text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('authenticated', 'public.usage_stats(text,uuid,text)', 'execute')
       or has_function_privilege('authenticated',
        'public.usage_breakdown(text,uuid,text,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)', 'execute')
    then
        raise exception 'the usage read path is browser-executable';
    end if;
end;
$$;

rollback;
