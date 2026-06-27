-- Billing events table — one row per Brevitas compression call
-- recorded by the SDK or proxy via POST /v1/usage

create table if not exists public.billing_events (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references auth.users(id) on delete cascade,
  session_id       text not null default '',
  ts               timestamptz not null default now(),
  provider         text not null default '',
  model            text not null default '',
  baseline_tokens  integer not null default 0,
  compressed_tokens integer not null default 0,
  tokens_saved     integer not null generated always as (baseline_tokens - compressed_tokens) stored,
  cost_saved_usd   numeric(12,8) not null default 0,
  brevitas_fee_usd numeric(12,8) not null default 0
);

alter table public.billing_events enable row level security;

create policy "Users can view own billing events"
  on public.billing_events for select
  using (auth.uid() = user_id);

-- Indexes for dashboard queries
create index if not exists billing_events_user_ts
  on public.billing_events (user_id, ts desc);

create index if not exists billing_events_user_month
  on public.billing_events (user_id, date_trunc('month', ts) desc);

-- Monthly billing summary view
create or replace view public.billing_monthly as
select
  user_id,
  date_trunc('month', ts)::date as month,
  count(*)                       as calls,
  sum(tokens_saved)              as tokens_saved,
  sum(cost_saved_usd)            as cost_saved_usd,
  sum(brevitas_fee_usd)          as brevitas_fee_usd
from public.billing_events
group by user_id, date_trunc('month', ts);
