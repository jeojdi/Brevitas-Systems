-- Narrow, read-only warehouse surface for PostHog.
-- Never connect PostHog as postgres/service_role or grant it access to public/auth.
create schema if not exists analytics;
revoke all on schema analytics from public, anon, authenticated;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'posthog_reader') then
    create role posthog_reader nologin;
  end if;
end
$$;

create or replace view analytics.posthog_usage
with (security_barrier = true)
as
select
  id,
  ts,
  owner_id,
  project,
  environment,
  source,
  repo,
  client,
  provider,
  model,
  operation,
  baseline_tokens,
  optimized_tokens,
  tokens_saved,
  fresh_input_tokens,
  cached_input_tokens,
  cache_write_tokens,
  output_tokens,
  baseline_cost_usd,
  actual_cost_usd,
  measured_savings_usd,
  verified_savings_usd,
  brevitas_fee_usd,
  quality_proxy,
  quality_status,
  pricing_status,
  pricing_version,
  receipt_source,
  is_stream
from public.usage_log;

revoke all on analytics.posthog_usage from public, anon, authenticated;
grant connect on database postgres to posthog_reader;
grant usage on schema analytics to posthog_reader;
grant select on analytics.posthog_usage to posthog_reader;

comment on view analytics.posthog_usage is
  'Approved numeric/label usage surface for PostHog warehouse sync; excludes credentials and content.';
