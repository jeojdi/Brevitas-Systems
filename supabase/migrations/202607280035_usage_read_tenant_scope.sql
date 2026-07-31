-- REVERSE: DDL: re-apply 202607170006_database_scaling.sql's usage_page body and 202607280002_usage_stats_cache_metrics.sql's usage_stats, usage_breakdown and usage_grouped bodies verbatim
--
-- Close a cross-tenant read on the four usage RPCs the dashboard and the
-- /v1/stats family are served from.
--
-- ============================================================================
-- THE HOLE
-- ============================================================================
--
-- All four functions share one three-branch authorization predicate. Branch 2
-- is reached when the CALLING KEY carries no organization
-- (p_organization_id is null) and reads:
--
--     or (p_organization_id is null and p_owner_id <> ''
--         and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
--
-- Nothing in that branch constrains usage.organization_id. So an org-less key
-- authorizes on the owner_id STRING alone and is handed every row that string
-- ever wrote, INCLUDING rows that belong to an organization -- and with them
-- baseline_cost_usd, actual_cost_usd, verified_savings_usd and
-- brevitas_fee_usd. usage_page returns `setof public.usage_log`, i.e. every
-- column; usage_stats sums all four; usage_breakdown and usage_grouped project
-- them. api/server.py's _spend_readable returns true for exactly these key
-- types, so the money columns are not redacted on the way out either, and
-- _require_current_dashboard_membership returns '' without checking anything
-- unless key_type = 'dashboard_session'.
--
-- MEASURED ON PRODUCTION (wyfz, SELECT-only, 2026-07-30/31):
--    43 api_keys with organization_id IS NULL, every one key_type='legacy',
--       unrevoked, unexpired, with a non-empty owner_id and a scope set that
--       includes usage:read_own -- so the route-level scope gate stops none of
--       them.
--    18 of those 43 share an owner_id with a key that DOES carry an
--       organization.
--  7477 usage_log rows with organization_id IS NOT NULL are reachable through
--       branch 2 by those keys, across 3 organizations and 3 owner_ids,
--       carrying $423.93 baseline_cost_usd and $422.79 actual_cost_usd.
--     0 org-scoped rows match an org-less key by key_hash, so the entire
--       exposure today is the owner_id sub-branch, not the key_hash one.
-- All 3 crossing owners happen to be active members of the organizations whose
-- rows they can read, so nobody is reading rows they do not belong to right
-- now. That is luck, not a control: nothing on this path consults
-- organization_members, and deactivating a membership does not change what
-- these keys can read.
--
-- ============================================================================
-- THE EDIT
-- ============================================================================
--
-- Branch 2's owner_id sub-branch gains `usage.organization_id is null`:
--
--     or (p_organization_id is null and p_owner_id <> ''
--         and ((usage.organization_id is null and usage.owner_id = p_owner_id)
--              or usage.key_hash = p_key_hash))
--
-- An org-less key now sees org-less rows for its owner, and no others. The two
-- other branches are untouched, deliberately:
--   * Branch 1 (p_organization_id is not null) already pins
--     usage.organization_id = p_organization_id and is the tenant-scoped read.
--   * The key_hash sub-branch of branch 2, and branch 3, are SELF-scope: a
--     key_hash is a secret digest supplied by api/store.py's _usage_scope from
--     the authenticated api_keys row, never by the caller, and rows carrying it
--     were written BY that key. Reading what you yourself wrote is not a tenant
--     crossing, and branch 3 already grants it unconditionally, so restricting
--     it here would only make the two self-scope branches disagree. Production
--     has 0 rows where that distinction is even observable.
--
-- Nothing else changes: not the arguments, not the return types, not one
-- projected column, not the grants. The four bodies below were extracted
-- MECHANICALLY from their defining migrations (usage_page from
-- 202607170006_database_scaling.sql:51-71, the other three from
-- 202607280002_usage_stats_cache_metrics.sql:30-153, :156-207 and :210-273),
-- with only the predicate edit above and the search_path hardening applied.
-- usage_breakdown and usage_grouped keep 202607280002's return-type shape
-- exactly, which is what lets them be REPLACED here instead of dropped and
-- recreated -- and the self-check at the bottom fails closed if a signature
-- drifted and created an OVERLOAD instead of a replacement.
--
-- search_path moves to `pg_catalog, public, pg_temp` (202607170006 had bare
-- `public`, 202607280002 had `pg_catalog, public`), per the current contract
-- for SECURITY DEFINER functions. Every object reference in these bodies is
-- already schema-qualified, so resolution is unchanged.
--
-- 202607170006 is a GENERATED copy of api/migrations/004_database_scaling.sql
-- and is checksum-frozen; 202607280002 is frozen too. Neither is edited. This
-- migration supersedes their bodies the same way 202607280002 superseded
-- 202607170006's, so scripts/ci/sync-database-scaling-migration.mjs and the
-- frozen-checksum gate both stay green.
--
-- NOT A DATA CHANGE: no row is inserted, updated or deleted. Rollback is
-- re-applying the previous bodies, which is what the header above declares.

begin;

do $$
begin
    -- 202607280002 must already have run: this migration restates ITS return
    -- types for usage_breakdown / usage_grouped, so applying it against
    -- 202607170006's narrower shapes would fail on the return type instead of
    -- silently installing the wrong one.
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'usage_log'
          and column_name = 'provider_input_tokens_avoided'
    ) then
        raise exception
            '202607280035 requires 202607280002_usage_stats_cache_metrics first'
            using errcode = '55000';
    end if;
    if to_regprocedure('public.usage_page(text,uuid,text,timestamptz,bigint,integer)') is null
       or to_regprocedure('public.usage_stats(text,uuid,text)') is null
       or to_regprocedure('public.usage_breakdown(text,uuid,text,integer)') is null
       or to_regprocedure('public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)') is null
    then
        raise exception
            '202607280035 requires the database-scaling usage read path to exist'
            using errcode = '55000';
    end if;
end;
$$;

create or replace function public.usage_page(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text,
    p_cursor_ts timestamptz,
    p_cursor_id bigint,
    p_limit integer
) returns setof public.usage_log
language sql stable security definer set search_path = pg_catalog, public, pg_temp as $$
    select usage.*
    from public.usage_log usage
    where (
        (p_organization_id is not null and usage.organization_id = p_organization_id)
        or (p_organization_id is null and p_owner_id <> ''
            and ((usage.organization_id is null and usage.owner_id = p_owner_id)
                 or usage.key_hash = p_key_hash))
        or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
    )
      and (p_cursor_ts is null or (usage.ts, usage.id) < (p_cursor_ts, p_cursor_id))
    order by usage.ts desc, usage.id desc
    limit least(greatest(coalesce(p_limit, 100), 1), 200) + 1;
$$;

create or replace function public.usage_stats(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text
) returns jsonb
language sql stable security definer set search_path = pg_catalog, public, pg_temp as $$
    with scoped as materialized (
        select usage.*
        from public.usage_log usage
        where (
            (p_organization_id is not null and usage.organization_id = p_organization_id)
            or (p_organization_id is null and p_owner_id <> ''
                and ((usage.organization_id is null and usage.owner_id = p_owner_id)
                     or usage.key_hash = p_key_hash))
            or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
        )
    ), totals as (
        select count(*)::bigint as calls,
               coalesce(sum(baseline_tokens), 0)::bigint as baseline,
               coalesce(sum(optimized_tokens), 0)::bigint as optimized,
               coalesce(sum(fresh_input_tokens + cached_input_tokens + cache_write_tokens + output_tokens), 0)::bigint as actual_tokens,
               coalesce(sum(tokens_saved), 0)::bigint as saved,
               coalesce(avg(quality_proxy) filter (where quality_proxy is not null), 0) as quality,
               coalesce(sum(baseline_cost_usd), 0) as baseline_cost,
               coalesce(sum(actual_cost_usd), 0) as actual_cost,
               coalesce(sum(measured_savings_usd), 0) as measured,
               coalesce(sum(verified_savings_usd), 0) as verified,
               coalesce(sum(brevitas_fee_usd), 0) as fee,
               count(*) filter (where pricing_status <> 'priced')::bigint as unpriced,
               coalesce(sum(provider_input_tokens_avoided), 0)::bigint as provider_tokens_avoided,
               coalesce(sum(calls_avoided), 0)::bigint as avoided_calls,
               coalesce(sum(native_cache_discount_usd), 0)::numeric as native_discount,
               coalesce(sum(cached_input_tokens), 0)::bigint as cached_input,
               coalesce(sum(fresh_input_tokens), 0)::bigint as fresh_input,
               -- Billable cache slice, mirroring _record_usage_report: only
               -- authoritative rows bill; exact_cache replays bill their whole
               -- avoided-call cost; Brevitas-attributable reads bill the
               -- clamped native discount, never above the row's verified
               -- savings. SDK-reported claims and cache-write premiums stay
               -- analytics-only.
               coalesce(sum(
                   case
                       when not authoritative then 0
                       when strategy like 'exact_cache%'
                           then greatest(coalesce(verified_savings_usd, 0), 0)
                       when cache_attributable then least(
                           greatest(coalesce(native_cache_discount_usd, 0), 0),
                           greatest(coalesce(verified_savings_usd, 0), 0))
                       else 0
                   end), 0)::numeric as attributable_cache,
               coalesce(sum(actual_cost_usd)
                        filter (where strategy = 'cache_warm'), 0)::numeric as warm_spend,
               coalesce(sum(transport_bytes_avoided), 0)::bigint as transport_bytes,
               coalesce(sum(brevitas_incremental_savings_usd), 0)::numeric as incremental
        from scoped
    ), history as (
        select coalesce(jsonb_agg(jsonb_build_object(
            'timestamp', recent.ts,
            'baseline_tokens', recent.baseline_tokens,
            'optimized_tokens', recent.optimized_tokens,
            'savings_pct', recent.savings_pct,
            'quality_proxy', recent.quality_proxy,
            'project', coalesce(nullif(recent.project, ''), 'Unattributed'),
            'environment', coalesce(nullif(recent.environment, ''), 'Unattributed'),
            'source', coalesce(nullif(recent.source, ''), 'Unattributed'),
            'provider', recent.provider,
            'model', recent.model,
            'operation', recent.operation,
            'measured_savings_usd', recent.measured_savings_usd,
            'verified_savings_usd', recent.verified_savings_usd,
            'cost_saved_usd', recent.verified_savings_usd,
            'pricing_status', recent.pricing_status
        ) order by recent.ts desc, recent.id desc), '[]'::jsonb) as value
        from (select * from scoped order by ts desc, id desc limit 50) recent
    ), weekly as (
        select coalesce(jsonb_agg(to_jsonb(week_row) order by week_row.week_start desc), '[]'::jsonb) as value
        from (
            select to_char(date_trunc('week', ts at time zone 'UTC'), 'YYYY-MM-DD') as week_start,
                   count(*)::bigint as calls,
                   coalesce(sum(tokens_saved), 0)::bigint as tokens_saved,
                   round(coalesce(sum(actual_cost_usd), 0), 8) as actual_cost_usd,
                   round(coalesce(sum(measured_savings_usd), 0), 8) as measured_savings_usd,
                   round(coalesce(sum(verified_savings_usd), 0), 8) as verified_savings_usd,
                   round(coalesce(sum(verified_savings_usd), 0), 8) as cost_saved_usd,
                   round(coalesce(sum(brevitas_fee_usd), 0), 8) as brevitas_fee_usd,
                   coalesce(sum(provider_input_tokens_avoided), 0)::bigint as provider_input_tokens_avoided,
                   round(coalesce(sum(native_cache_discount_usd), 0)::numeric, 8) as native_cache_discount_usd,
                   coalesce(sum(cached_input_tokens), 0)::bigint as cached_input_tokens,
                   round(coalesce(sum(actual_cost_usd)
                                  filter (where strategy = 'cache_warm'), 0)::numeric, 8) as warm_spend_usd
            from scoped
            group by date_trunc('week', ts at time zone 'UTC')
            order by date_trunc('week', ts at time zone 'UTC') desc
            limit 12
        ) week_row
    )
    select jsonb_build_object(
        'total_calls', totals.calls,
        'total_baseline_tokens', totals.baseline,
        'total_optimized_tokens', totals.optimized,
        'total_actual_tokens', totals.actual_tokens,
        'total_tokens_saved', totals.saved,
        'avg_savings_pct', coalesce(round(100.0 * totals.saved / nullif(totals.baseline, 0), 2), 0),
        'avg_quality_proxy', round(totals.quality::numeric, 4),
        'total_baseline_cost_usd', round(totals.baseline_cost, 8),
        'total_actual_cost_usd', round(totals.actual_cost, 8),
        'total_measured_savings_usd', round(totals.measured, 8),
        'total_verified_savings_usd', round(totals.verified, 8),
        'total_cost_saved_usd', round(totals.verified, 8),
        'total_brevitas_fee_usd', round(totals.fee, 8),
        'unpriced_calls', totals.unpriced,
        'total_provider_input_tokens_avoided', totals.provider_tokens_avoided,
        'total_calls_avoided', totals.avoided_calls,
        'total_native_cache_discount_usd', round(totals.native_discount, 8),
        'total_cached_input_tokens', totals.cached_input,
        'total_fresh_input_tokens', totals.fresh_input,
        'total_attributable_cache_savings_usd', round(totals.attributable_cache, 8),
        'total_warm_spend_usd', round(totals.warm_spend, 8),
        'total_transport_bytes_avoided', totals.transport_bytes,
        'total_brevitas_incremental_savings_usd', round(totals.incremental, 8),
        'history', history.value,
        'billing_by_week', weekly.value
    )
    from totals cross join history cross join weekly;
$$;

create or replace function public.usage_breakdown(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text,
    p_limit integer
) returns table (
    repo text, environment text, client text, agent text, call_site_id text,
    framework text, gateway text, provider text, model text, operation text,
    project text, source text, calls bigint, baseline_tokens bigint,
    optimized_tokens bigint, actual_tokens bigint, tokens_saved bigint,
    baseline_cost_usd numeric, actual_cost_usd numeric,
    measured_savings_usd numeric, verified_savings_usd numeric,
    brevitas_fee_usd numeric, unpriced_calls bigint,
    provider_input_tokens_avoided bigint, calls_avoided bigint,
    native_cache_discount_usd numeric, transport_bytes_avoided bigint,
    brevitas_incremental_savings_usd numeric
)
language sql stable security definer set search_path = pg_catalog, public, pg_temp as $$
    select coalesce(nullif(usage.repo, ''), nullif(usage.project, ''), 'Unattributed'),
           coalesce(nullif(usage.environment, ''), 'Unattributed'),
           coalesce(nullif(usage.client, ''), nullif(usage.source, ''), 'Unattributed'),
           usage.agent, usage.call_site_id, usage.framework, usage.gateway,
           usage.provider, usage.model, usage.operation,
           coalesce(nullif(usage.project, ''), nullif(usage.repo, ''), 'Unattributed'),
           coalesce(nullif(usage.source, ''), nullif(usage.client, ''), 'Unattributed'),
           count(*)::bigint,
           coalesce(sum(usage.baseline_tokens), 0)::bigint,
           coalesce(sum(usage.optimized_tokens), 0)::bigint,
           coalesce(sum(usage.fresh_input_tokens + usage.cached_input_tokens + usage.cache_write_tokens + usage.output_tokens), 0)::bigint,
           coalesce(sum(usage.tokens_saved), 0)::bigint,
           round(coalesce(sum(usage.baseline_cost_usd), 0), 8),
           round(coalesce(sum(usage.actual_cost_usd), 0), 8),
           round(coalesce(sum(usage.measured_savings_usd), 0), 8),
           round(coalesce(sum(usage.verified_savings_usd), 0), 8),
           round(coalesce(sum(usage.brevitas_fee_usd), 0), 8),
           count(*) filter (where usage.pricing_status <> 'priced')::bigint,
           coalesce(sum(usage.provider_input_tokens_avoided), 0)::bigint,
           coalesce(sum(usage.calls_avoided), 0)::bigint,
           round(coalesce(sum(usage.native_cache_discount_usd), 0)::numeric, 8),
           coalesce(sum(usage.transport_bytes_avoided), 0)::bigint,
           round(coalesce(sum(usage.brevitas_incremental_savings_usd), 0)::numeric, 8)
    from public.usage_log usage
    where (
        (p_organization_id is not null and usage.organization_id = p_organization_id)
        or (p_organization_id is null and p_owner_id <> ''
            and ((usage.organization_id is null and usage.owner_id = p_owner_id)
                 or usage.key_hash = p_key_hash))
        or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
    )
    group by 1,2,3,4,5,6,7,8,9,10,11,12
    order by coalesce(sum(usage.tokens_saved), 0) desc, 1, 3, 9
    limit least(greatest(coalesce(p_limit, 100), 1), 500);
$$;

create or replace function public.usage_grouped(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text,
    p_field text,
    p_pipeline text,
    p_start timestamptz,
    p_end timestamptz,
    p_limit integer
) returns table (
    pipeline text, agent text, run_id text, calls bigint, tokens_saved bigint,
    avg_savings_pct numeric, avg_quality numeric, cost_saved_usd numeric,
    brevitas_fee_usd numeric,
    provider_input_tokens_avoided bigint, calls_avoided bigint,
    native_cache_discount_usd numeric, transport_bytes_avoided bigint,
    brevitas_incremental_savings_usd numeric
)
language plpgsql stable security definer set search_path = pg_catalog, public, pg_temp as $$
begin
    if p_field not in ('pipeline', 'agent', 'run_id') then
        raise exception 'unsupported usage grouping';
    end if;
    return query
    with grouped as (
        select case p_field when 'pipeline' then usage.pipeline
                            when 'agent' then usage.agent else usage.run_id end as label,
               count(*)::bigint as call_count,
               coalesce(sum(usage.tokens_saved), 0)::bigint as saved,
               coalesce(sum(usage.baseline_tokens), 0)::bigint as baseline,
               coalesce(avg(usage.quality_proxy) filter (where usage.quality_proxy is not null), 0) as quality,
               coalesce(sum(usage.verified_savings_usd), 0) as verified,
               coalesce(sum(usage.brevitas_fee_usd), 0) as fee,
               coalesce(sum(usage.provider_input_tokens_avoided), 0)::bigint as provider_tokens_avoided,
               coalesce(sum(usage.calls_avoided), 0)::bigint as avoided_calls,
               coalesce(sum(usage.native_cache_discount_usd), 0)::numeric as native_discount,
               coalesce(sum(usage.transport_bytes_avoided), 0)::bigint as transport_bytes,
               coalesce(sum(usage.brevitas_incremental_savings_usd), 0)::numeric as incremental
        from public.usage_log usage
        where (
            (p_organization_id is not null and usage.organization_id = p_organization_id)
            or (p_organization_id is null and p_owner_id <> ''
                and ((usage.organization_id is null and usage.owner_id = p_owner_id)
                     or usage.key_hash = p_key_hash))
            or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
        )
          and (p_pipeline is null or usage.pipeline = p_pipeline)
          and (p_start is null or usage.ts >= p_start)
          and (p_end is null or usage.ts < p_end)
        group by 1
    )
    select case when p_field = 'pipeline' then grouped.label end,
           case when p_field = 'agent' then grouped.label end,
           case when p_field = 'run_id' then grouped.label end,
           grouped.call_count, grouped.saved,
           coalesce(round(100.0 * grouped.saved / nullif(grouped.baseline, 0), 2), 0),
           round(grouped.quality::numeric, 4), round(grouped.verified, 8),
           round(grouped.fee, 8),
           grouped.provider_tokens_avoided, grouped.avoided_calls,
           round(grouped.native_discount, 8), grouped.transport_bytes,
           round(grouped.incremental, 8)
    from grouped
    order by grouped.saved desc, grouped.label
    limit least(greatest(coalesce(p_limit, 100), 1), 500);
end;
$$;

revoke all on function public.usage_page(text,uuid,text,timestamptz,bigint,integer) from public, anon, authenticated;
revoke all on function public.usage_stats(text,uuid,text) from public, anon, authenticated;
revoke all on function public.usage_breakdown(text,uuid,text,integer) from public, anon, authenticated;
revoke all on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) from public, anon, authenticated;

grant execute on function public.usage_page(text,uuid,text,timestamptz,bigint,integer) to service_role;
grant execute on function public.usage_stats(text,uuid,text) to service_role;
grant execute on function public.usage_breakdown(text,uuid,text,integer) to service_role;
grant execute on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) to service_role;

-- Apply-time self-check. Everything below reads the catalog: it proves the
-- edit landed on all four functions, that each name still resolves to exactly
-- one function (a retyped signature would have created an overload beside the
-- original and left the hole open on whichever one PostgREST picked), and that
-- the browser roles still cannot reach any of them.
do $contract$
declare
    v_name text;
    v_src text;
begin
    foreach v_name in array array['usage_page', 'usage_stats', 'usage_breakdown', 'usage_grouped']
    loop
        if (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and p.proname = v_name) <> 1 then
            raise exception '202607280035 left more than one public.% -- an overload, not a replacement', v_name
                using errcode = '55000';
        end if;
        select p.prosrc into v_src from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = v_name;
        if v_src not like '%(usage.organization_id is null and usage.owner_id = p_owner_id)%' then
            raise exception 'public.% still authorizes an org-less key on owner_id alone', v_name
                using errcode = '55000';
        end if;
        if v_src like '%and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))%' then
            raise exception 'public.% still carries the unrestricted org-less branch', v_name
                using errcode = '55000';
        end if;
        if not exists (
            select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'public' and p.proname = v_name and p.prosecdef
        ) then
            raise exception 'public.% lost SECURITY DEFINER', v_name
                using errcode = '55000';
        end if;
    end loop;

    if has_function_privilege('anon',
        'public.usage_page(text,uuid,text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('anon', 'public.usage_stats(text,uuid,text)', 'execute')
       or has_function_privilege('anon', 'public.usage_breakdown(text,uuid,text,integer)', 'execute')
       or has_function_privilege('anon',
        'public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.usage_page(text,uuid,text,timestamptz,bigint,integer)', 'execute')
       or has_function_privilege('authenticated', 'public.usage_stats(text,uuid,text)', 'execute')
       or has_function_privilege('authenticated', 'public.usage_breakdown(text,uuid,text,integer)', 'execute')
       or has_function_privilege('authenticated',
        'public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)', 'execute')
    then
        raise exception 'the usage read path is browser-executable'
            using errcode = '42501';
    end if;

    if not has_function_privilege('service_role',
        'public.usage_page(text,uuid,text,timestamptz,bigint,integer)', 'execute')
       or not has_function_privilege('service_role', 'public.usage_stats(text,uuid,text)', 'execute')
       or not has_function_privilege('service_role', 'public.usage_breakdown(text,uuid,text,integer)', 'execute')
       or not has_function_privilege('service_role',
        'public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer)', 'execute')
    then
        raise exception 'the API service role lost the usage read path'
            using errcode = '42501';
    end if;
end
$contract$;

commit;
