-- Surface the split savings metrics added by 20260720_split_savings_metrics.sql
-- in the usage aggregates. 202607170006_database_scaling.sql is checksum-frozen,
-- so this migration supersedes its usage_stats / usage_breakdown / usage_grouped
-- bodies instead of editing them. Every pre-existing output field, argument
-- signature, and grant is preserved; new fields are additive only.
--
-- usage_breakdown and usage_grouped return table(...), and adding output columns
-- changes the declared return type, which create or replace refuses — so both are
-- dropped and recreated inside this transaction, with their revoke/grant contract
-- restated below. usage_stats returns jsonb and is replaced in place.
--
-- Money sums are cast ::numeric before round(): prod has held money columns as
-- double precision (see docs/PROD_DB_RECONCILIATION.md), and round(x, n) has only
-- a numeric overload. The cast is a no-op on a conforming schema.

begin;

do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'usage_log'
          and column_name = 'provider_input_tokens_avoided'
    ) then
        raise exception '202607280002_usage_stats_cache_metrics requires 20260720_split_savings_metrics first';
    end if;
end;
$$;

create or replace function public.usage_stats(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text
) returns jsonb
language sql stable security definer set search_path = pg_catalog, public as $$
    with scoped as materialized (
        select usage.*
        from public.usage_log usage
        where (
            (p_organization_id is not null and usage.organization_id = p_organization_id)
            or (p_organization_id is null and p_owner_id <> ''
                and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
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

drop function if exists public.usage_breakdown(text,uuid,text,integer);
create function public.usage_breakdown(
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
language sql stable security definer set search_path = pg_catalog, public as $$
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
            and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
        or (p_organization_id is null and p_owner_id = '' and usage.key_hash = p_key_hash)
    )
    group by 1,2,3,4,5,6,7,8,9,10,11,12
    order by coalesce(sum(usage.tokens_saved), 0) desc, 1, 3, 9
    limit least(greatest(coalesce(p_limit, 100), 1), 500);
$$;

drop function if exists public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer);
create function public.usage_grouped(
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
language plpgsql stable security definer set search_path = pg_catalog, public as $$
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
                and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
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

revoke all on function public.usage_stats(text,uuid,text) from public, anon, authenticated;
revoke all on function public.usage_breakdown(text,uuid,text,integer) from public, anon, authenticated;
revoke all on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) from public, anon, authenticated;

grant execute on function public.usage_stats(text,uuid,text) to service_role;
grant execute on function public.usage_breakdown(text,uuid,text,integer) to service_role;
grant execute on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) to service_role;

commit;
