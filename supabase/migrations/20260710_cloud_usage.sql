-- Canonical cloud store for Brevitas. Idempotent: safe to run repeatedly.

create table if not exists public.api_keys (
    key_hash text primary key,
    name text not null,
    created timestamptz not null default now(),
    owner_id text not null default ''
);
alter table public.api_keys add column if not exists owner_id text not null default '';
create index if not exists api_keys_owner_idx on public.api_keys(owner_id);

create table if not exists public.provider_config (
    key_hash text primary key,
    provider text not null default 'ollama',
    provider_api_key text not null default '',
    model text not null default 'llama3.2'
);

create table if not exists public.usage_log (
    id bigint generated always as identity primary key,
    key_hash text not null,
    ts timestamptz not null default now(),
    owner_id text not null default '',
    project text not null default 'Unattributed',
    environment text not null default 'Unattributed',
    source text not null default 'Unattributed',
    repo text not null default '',
    client text not null default '',
    agent text not null default '',
    call_site_id text not null default '',
    framework text not null default '',
    gateway text not null default '',
    operation text not null default 'chat',
    provider text not null default '',
    model text not null default '',
    baseline_tokens bigint not null default 0,
    optimized_tokens bigint not null default 0,
    tokens_saved bigint not null default 0,
    savings_pct double precision not null default 0,
    fresh_input_tokens bigint not null default 0,
    cached_input_tokens bigint not null default 0,
    cache_write_tokens bigint not null default 0,
    output_tokens bigint not null default 0,
    baseline_cost_usd numeric(18,10),
    actual_cost_usd numeric(18,10),
    measured_savings_usd numeric(18,10),
    verified_savings_usd numeric(18,10) not null default 0,
    cost_saved_usd numeric(18,10) not null default 0,
    brevitas_fee_usd numeric(18,10) not null default 0,
    quality_proxy double precision,
    quality_status text not null default '',
    pricing_status text not null default 'unpriced',
    pricing_version text not null default '',
    strategy text not null default '',
    receipt_source text not null default 'sdk',
    is_stream boolean not null default false,
    session_id text not null default '',
    pipeline text not null default '',
    run_id text not null default '',
    request_id text not null default '',
    usage_raw text not null default ''
);

-- Upgrade any earlier usage_log in place.
alter table public.usage_log add column if not exists owner_id text not null default '';
alter table public.usage_log add column if not exists project text not null default 'Unattributed';
alter table public.usage_log add column if not exists environment text not null default 'Unattributed';
alter table public.usage_log add column if not exists source text not null default 'Unattributed';
alter table public.usage_log add column if not exists repo text not null default '';
alter table public.usage_log add column if not exists client text not null default '';
alter table public.usage_log add column if not exists agent text not null default '';
alter table public.usage_log add column if not exists call_site_id text not null default '';
alter table public.usage_log add column if not exists framework text not null default '';
alter table public.usage_log add column if not exists gateway text not null default '';
alter table public.usage_log add column if not exists operation text not null default 'chat';
alter table public.usage_log add column if not exists provider text not null default '';
alter table public.usage_log add column if not exists model text not null default '';
alter table public.usage_log add column if not exists baseline_tokens bigint not null default 0;
alter table public.usage_log add column if not exists optimized_tokens bigint not null default 0;
alter table public.usage_log add column if not exists tokens_saved bigint not null default 0;
alter table public.usage_log add column if not exists savings_pct double precision not null default 0;
alter table public.usage_log add column if not exists fresh_input_tokens bigint not null default 0;
alter table public.usage_log add column if not exists cached_input_tokens bigint not null default 0;
alter table public.usage_log add column if not exists cache_write_tokens bigint not null default 0;
alter table public.usage_log add column if not exists output_tokens bigint not null default 0;
alter table public.usage_log add column if not exists baseline_cost_usd numeric(18,10);
alter table public.usage_log add column if not exists actual_cost_usd numeric(18,10);
alter table public.usage_log add column if not exists measured_savings_usd numeric(18,10);
alter table public.usage_log add column if not exists verified_savings_usd numeric(18,10) not null default 0;
alter table public.usage_log add column if not exists cost_saved_usd numeric(18,10) not null default 0;
alter table public.usage_log add column if not exists brevitas_fee_usd numeric(18,10) not null default 0;
alter table public.usage_log add column if not exists quality_proxy double precision;
alter table public.usage_log add column if not exists quality_status text not null default '';
alter table public.usage_log add column if not exists pricing_status text not null default 'unpriced';
alter table public.usage_log add column if not exists pricing_version text not null default '';
alter table public.usage_log add column if not exists strategy text not null default '';
alter table public.usage_log add column if not exists receipt_source text not null default 'sdk';
alter table public.usage_log add column if not exists is_stream boolean not null default false;
alter table public.usage_log add column if not exists session_id text not null default '';
alter table public.usage_log add column if not exists pipeline text not null default '';
alter table public.usage_log add column if not exists run_id text not null default '';
alter table public.usage_log add column if not exists request_id text not null default '';
alter table public.usage_log add column if not exists usage_raw text not null default '';
alter table public.usage_log alter column quality_proxy drop not null;

update public.usage_log
set tokens_saved = baseline_tokens - optimized_tokens
where tokens_saved = 0 and baseline_tokens <> optimized_tokens;
update public.usage_log
set measured_savings_usd = cost_saved_usd,
    verified_savings_usd = cost_saved_usd
where measured_savings_usd is null and cost_saved_usd <> 0;

create unique index if not exists usage_log_request_unique
    on public.usage_log(key_hash, request_id) where request_id <> '';
create index if not exists usage_log_key_ts_idx on public.usage_log(key_hash, ts desc);
create index if not exists usage_log_owner_ts_idx on public.usage_log(owner_id, ts desc);
create index if not exists usage_log_project_idx on public.usage_log(key_hash, project, ts desc);
create index if not exists usage_log_source_idx on public.usage_log(key_hash, source, ts desc);
create index if not exists usage_log_repo_idx on public.usage_log(key_hash, repo, ts desc);
create index if not exists usage_log_client_idx on public.usage_log(key_hash, client, ts desc);
create index if not exists usage_log_provider_idx on public.usage_log(key_hash, provider, ts desc);
create index if not exists usage_log_model_idx on public.usage_log(key_hash, model, ts desc);
create index if not exists usage_log_call_site_idx on public.usage_log(key_hash, call_site_id, ts desc);

-- Dashboard key cache. The raw key is visible only to its owning Supabase user.
create table if not exists public.user_keys (
    user_id uuid primary key references auth.users(id) on delete cascade,
    api_key text not null,
    created_at timestamptz not null default now()
);

alter table public.api_keys enable row level security;
alter table public.provider_config enable row level security;
alter table public.usage_log enable row level security;
alter table public.user_keys enable row level security;

drop policy if exists user_keys_owner on public.user_keys;
drop policy if exists "users can access only their own key" on public.user_keys;
create policy user_keys_owner on public.user_keys
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- No policies are intentionally created for service-owned tables. Only service_role
-- can read API keys, provider configuration, or usage receipts.
