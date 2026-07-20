-- GENERATED DEPLOYMENT COPY. DO NOT EDIT BY HAND.
-- Source: api/migrations/004_database_scaling.sql
-- Source-SHA256: 2b8bdfdcbd3755da76e58b35ca94d384f12e7c094e0f73c8c7bf97d22bd08a32
-- Release order: after 202607170005_company_administration.sql.
-- Enterprise database scaling contracts. Forward-only and safe to re-run.
-- Production application code calls these functions with the service role only.
-- Ordering prerequisite: Supabase timestamped migrations through
-- 202607170003_durable_jobs.sql. In particular, 202607170001_enterprise_tenancy.sql
-- adds organization_id, customer_id, and authoritative to usage_log.
--
-- The CREATE INDEX statements below are appropriate for an empty/small table or a
-- controlled maintenance window. For a large live table, first run the companion
-- 004_database_scaling.concurrent_indexes.sql outside a transaction; these IF NOT
-- EXISTS statements then become idempotent no-ops during the recorded migration.

do $$
begin
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'usage_log'
          and column_name = 'organization_id'
    ) then
        raise exception '004_database_scaling requires Supabase migrations through 202607170003 first';
    end if;
end;
$$;

create index if not exists usage_log_org_page_idx
    on public.usage_log (organization_id, ts desc, id desc);
create index if not exists usage_log_owner_page_idx
    on public.usage_log (owner_id, ts desc, id desc);
create index if not exists usage_log_key_page_idx
    on public.usage_log (key_hash, ts desc, id desc);
create index if not exists usage_log_org_customer_page_idx
    on public.usage_log (organization_id, customer_id, ts desc, id desc);
create index if not exists usage_log_org_pipeline_idx
    on public.usage_log (organization_id, pipeline, ts desc, id desc);
create index if not exists usage_log_org_agent_idx
    on public.usage_log (organization_id, agent, ts desc, id desc);
create index if not exists usage_log_org_run_idx
    on public.usage_log (organization_id, run_id, ts desc, id desc);
create index if not exists usage_log_admin_project_idx
    on public.usage_log (project, ts desc, id desc);
create index if not exists usage_log_admin_client_idx
    on public.usage_log (client, ts desc, id desc);
create index if not exists usage_log_admin_provider_idx
    on public.usage_log (provider, ts desc, id desc);
create index if not exists usage_log_admin_model_idx
    on public.usage_log (model, ts desc, id desc);

create or replace function public.usage_page(
    p_key_hash text,
    p_organization_id uuid,
    p_owner_id text,
    p_cursor_ts timestamptz,
    p_cursor_id bigint,
    p_limit integer
) returns setof public.usage_log
language sql stable security definer set search_path = public as $$
    select usage.*
    from public.usage_log usage
    where (
        (p_organization_id is not null and usage.organization_id = p_organization_id)
        or (p_organization_id is null and p_owner_id <> ''
            and (usage.owner_id = p_owner_id or usage.key_hash = p_key_hash))
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
language sql stable security definer set search_path = public as $$
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
               count(*) filter (where pricing_status <> 'priced')::bigint as unpriced
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
                   round(coalesce(sum(brevitas_fee_usd), 0), 8) as brevitas_fee_usd
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
    brevitas_fee_usd numeric, unpriced_calls bigint
)
language sql stable security definer set search_path = public as $$
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
           count(*) filter (where usage.pricing_status <> 'priced')::bigint
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
    brevitas_fee_usd numeric
)
language plpgsql stable security definer set search_path = public as $$
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
               coalesce(sum(usage.brevitas_fee_usd), 0) as fee
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
           round(grouped.fee, 8)
    from grouped
    order by grouped.saved desc, grouped.label
    limit least(greatest(coalesce(p_limit, 100), 1), 500);
end;
$$;

create or replace function public.admin_usage_report(
    p_filters jsonb,
    p_limit integer
) returns jsonb
language sql stable security definer set search_path = public as $$
    with filtered as materialized (
        select usage.*
        from public.usage_log usage
        where (not (coalesce(p_filters, '{}'::jsonb) ? 'start') or usage.ts >= (p_filters->>'start')::timestamptz)
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'organization_id') or usage.organization_id = (p_filters->>'organization_id')::uuid)
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'owner_id') or usage.owner_id = p_filters->>'owner_id')
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'project') or usage.project = p_filters->>'project')
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'client') or usage.client = p_filters->>'client')
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'provider') or usage.provider = p_filters->>'provider')
          and (not (coalesce(p_filters, '{}'::jsonb) ? 'model') or usage.model = p_filters->>'model')
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
               count(*) filter (where pricing_status <> 'priced')::bigint as unpriced
        from filtered
    ), grouped as (
        select coalesce(nullif(owner_id, ''), 'Unattributed') as account_id,
               coalesce(nullif(repo, ''), nullif(project, ''), 'Unattributed') as repo,
               coalesce(nullif(environment, ''), 'Unattributed') as environment,
               coalesce(nullif(client, ''), nullif(source, ''), 'Unattributed') as client,
               agent, call_site_id, framework, gateway, provider, model, operation,
               coalesce(nullif(project, ''), nullif(repo, ''), 'Unattributed') as project,
               coalesce(nullif(source, ''), nullif(client, ''), 'Unattributed') as source,
               count(*)::bigint as calls,
               coalesce(sum(baseline_tokens), 0)::bigint as baseline_tokens,
               coalesce(sum(optimized_tokens), 0)::bigint as optimized_tokens,
               coalesce(sum(fresh_input_tokens + cached_input_tokens + cache_write_tokens + output_tokens), 0)::bigint as actual_tokens,
               coalesce(sum(tokens_saved), 0)::bigint as tokens_saved,
               round(coalesce(sum(baseline_cost_usd), 0), 8) as baseline_cost_usd,
               round(coalesce(sum(actual_cost_usd), 0), 8) as actual_cost_usd,
               round(coalesce(sum(measured_savings_usd), 0), 8) as measured_savings_usd,
               round(coalesce(sum(verified_savings_usd), 0), 8) as verified_savings_usd,
               round(coalesce(sum(brevitas_fee_usd), 0), 8) as brevitas_fee_usd,
               count(*) filter (where pricing_status <> 'priced')::bigint as unpriced_calls
        from filtered
        group by 1,2,3,4,5,6,7,8,9,10,11,12,13
    ), rows as (
        select coalesce(jsonb_agg(to_jsonb(page) order by page.tokens_saved desc, page.account_id, page.repo), '[]'::jsonb) as value
        from (
            select * from grouped
            order by tokens_saved desc, account_id, repo
            limit least(greatest(coalesce(p_limit, 100), 1), 500)
        ) page
    )
    select jsonb_build_object(
        'totals', jsonb_build_object(
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
            'history', '[]'::jsonb,
            'billing_by_week', '[]'::jsonb
        ),
        'rows', rows.value,
        'truncated', (select count(*) from grouped) > least(greatest(coalesce(p_limit, 100), 1), 500)
    )
    from totals cross join rows;
$$;

create or replace function public.admin_key_repository_usage(p_limit integer)
returns table (key_hash text, owner_id text, repo text, project text, ts timestamptz)
language sql stable security definer set search_path = public as $$
    select usage.key_hash,
           max(usage.owner_id),
           coalesce(nullif(usage.repo, ''), nullif(usage.project, ''), 'Unattributed') as repo,
           coalesce(nullif(usage.repo, ''), nullif(usage.project, ''), 'Unattributed') as project,
           max(usage.ts) as ts
    from public.usage_log usage
    group by usage.key_hash, coalesce(nullif(usage.repo, ''), nullif(usage.project, ''), 'Unattributed')
    order by max(usage.ts) desc, usage.key_hash, 3
    limit least(greatest(coalesce(p_limit, 100), 1), 2000);
$$;

create or replace function public.admin_usage_report_page(
    p_filters jsonb,
    p_sort text,
    p_direction text,
    p_cursor_value numeric,
    p_cursor_key text,
    p_limit integer
) returns jsonb
language plpgsql stable security definer set search_path = public as $$
begin
    if p_sort not in ('actual_cost_usd', 'baseline_cost_usd',
                      'verified_savings_usd', 'brevitas_fee_usd',
                      'calls', 'tokens_saved')
       or p_direction not in ('asc', 'desc') then
        raise exception 'unsupported admin report ordering';
    end if;

    return (
        with filtered as materialized (
            select usage.*
            from public.usage_log usage
            where (not (coalesce(p_filters, '{}'::jsonb) ? 'start') or usage.ts >= (p_filters->>'start')::timestamptz)
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'organization_id') or usage.organization_id = (p_filters->>'organization_id')::uuid)
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'owner_id') or usage.owner_id = p_filters->>'owner_id')
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'project') or usage.project = p_filters->>'project')
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'client') or usage.client = p_filters->>'client')
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'provider') or usage.provider = p_filters->>'provider')
              and (not (coalesce(p_filters, '{}'::jsonb) ? 'model') or usage.model = p_filters->>'model')
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
                   count(*) filter (where pricing_status <> 'priced')::bigint as unpriced
            from filtered
        ), grouped as (
            select coalesce(nullif(owner_id, ''), 'Unattributed') as account_id,
                   coalesce(nullif(repo, ''), nullif(project, ''), 'Unattributed') as repo,
                   coalesce(nullif(environment, ''), 'Unattributed') as environment,
                   coalesce(nullif(client, ''), nullif(source, ''), 'Unattributed') as client,
                   agent, call_site_id, framework, gateway, provider, model, operation,
                   coalesce(nullif(project, ''), nullif(repo, ''), 'Unattributed') as project,
                   coalesce(nullif(source, ''), nullif(client, ''), 'Unattributed') as source,
                   count(*)::bigint as calls,
                   coalesce(sum(baseline_tokens), 0)::bigint as baseline_tokens,
                   coalesce(sum(optimized_tokens), 0)::bigint as optimized_tokens,
                   coalesce(sum(fresh_input_tokens + cached_input_tokens + cache_write_tokens + output_tokens), 0)::bigint as actual_tokens,
                   coalesce(sum(tokens_saved), 0)::bigint as tokens_saved,
                   round(coalesce(sum(baseline_cost_usd), 0), 8) as baseline_cost_usd,
                   round(coalesce(sum(actual_cost_usd), 0), 8) as actual_cost_usd,
                   round(coalesce(sum(measured_savings_usd), 0), 8) as measured_savings_usd,
                   round(coalesce(sum(verified_savings_usd), 0), 8) as verified_savings_usd,
                   round(coalesce(sum(brevitas_fee_usd), 0), 8) as brevitas_fee_usd,
                   count(*) filter (where pricing_status <> 'priced')::bigint as unpriced_calls,
                   md5(concat_ws(chr(31),
                       coalesce(nullif(owner_id, ''), 'Unattributed'),
                       coalesce(nullif(repo, ''), nullif(project, ''), 'Unattributed'),
                       coalesce(nullif(environment, ''), 'Unattributed'),
                       coalesce(nullif(client, ''), nullif(source, ''), 'Unattributed'),
                       agent, call_site_id, framework, gateway, provider, model, operation,
                       coalesce(nullif(project, ''), nullif(repo, ''), 'Unattributed'),
                       coalesce(nullif(source, ''), nullif(client, ''), 'Unattributed')
                   )) as row_key
            from filtered
            group by 1,2,3,4,5,6,7,8,9,10,11,12,13
        ), ranked as (
            select grouped.*,
                   case p_sort
                       when 'actual_cost_usd' then actual_cost_usd
                       when 'baseline_cost_usd' then baseline_cost_usd
                       when 'verified_savings_usd' then verified_savings_usd
                       when 'brevitas_fee_usd' then brevitas_fee_usd
                       when 'calls' then calls::numeric
                       when 'tokens_saved' then tokens_saved::numeric
                   end as sort_value
            from grouped
        ), page as (
            select ranked.*, ranked.sort_value as "_sort_value",
                   ranked.row_key as "_row_key"
            from ranked
            where p_cursor_value is null
               or (p_direction = 'asc' and (ranked.sort_value, ranked.row_key) > (p_cursor_value, p_cursor_key))
               or (p_direction = 'desc' and (ranked.sort_value, ranked.row_key) < (p_cursor_value, p_cursor_key))
            order by
                case when p_direction = 'asc' then ranked.sort_value end asc,
                case when p_direction = 'desc' then ranked.sort_value end desc,
                case when p_direction = 'asc' then ranked.row_key end asc,
                case when p_direction = 'desc' then ranked.row_key end desc
            limit least(greatest(coalesce(p_limit, 100), 1), 500) + 1
        ), rows as (
            select coalesce(jsonb_agg((to_jsonb(page) - 'sort_value' - 'row_key') order by
                       case when p_direction = 'asc' then page.sort_value end asc,
                       case when p_direction = 'desc' then page.sort_value end desc,
                       case when p_direction = 'asc' then page.row_key end asc,
                       case when p_direction = 'desc' then page.row_key end desc), '[]'::jsonb) as value
            from page
        )
        select jsonb_build_object(
            'totals', jsonb_build_object(
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
                'history', '[]'::jsonb,
                'billing_by_week', '[]'::jsonb
            ),
            'rows', rows.value,
            'total', (select count(*) from grouped)
        )
        from totals cross join rows
    );
end;
$$;

revoke all on function public.usage_page(text,uuid,text,timestamptz,bigint,integer) from public, anon, authenticated;
revoke all on function public.usage_stats(text,uuid,text) from public, anon, authenticated;
revoke all on function public.usage_breakdown(text,uuid,text,integer) from public, anon, authenticated;
revoke all on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) from public, anon, authenticated;
revoke all on function public.admin_usage_report(jsonb,integer) from public, anon, authenticated;
revoke all on function public.admin_key_repository_usage(integer) from public, anon, authenticated;
revoke all on function public.admin_usage_report_page(jsonb,text,text,numeric,text,integer) from public, anon, authenticated;

grant execute on function public.usage_page(text,uuid,text,timestamptz,bigint,integer) to service_role;
grant execute on function public.usage_stats(text,uuid,text) to service_role;
grant execute on function public.usage_breakdown(text,uuid,text,integer) to service_role;
grant execute on function public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer) to service_role;
grant execute on function public.admin_usage_report(jsonb,integer) to service_role;
grant execute on function public.admin_key_repository_usage(integer) to service_role;
grant execute on function public.admin_usage_report_page(jsonb,text,text,numeric,text,integer) to service_role;
