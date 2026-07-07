-- Persistent stores for Brevitas — run this once on the canonical Supabase project
-- (ctlhawahnwcfzdikrcxr — the dashboard's Supabase project). Fixes the "Failed to load stats"
-- bug: the backend was keeping
-- API keys + usage in an ephemeral SQLite file that Railway wipes on every redeploy, while
-- the dashboard cached each user's key in Supabase. After a redeploy the cached key no longer
-- existed on the backend -> 401. Moving the backend stores here makes keys/usage survive deploys.

-- ---------------------------------------------------------------------------
-- Dashboard cache: which Brevitas key belongs to which authenticated user.
-- Read/written by the dashboard using the user's Supabase session (anon key + RLS).
-- ---------------------------------------------------------------------------
create table if not exists public.user_keys (
    user_id    uuid primary key references auth.users(id) on delete cascade,
    api_key    text not null,
    created_at timestamptz not null default now()
);

alter table public.user_keys enable row level security;

drop policy if exists "user_keys self read"   on public.user_keys;
drop policy if exists "user_keys self write"  on public.user_keys;
drop policy if exists "user_keys self update" on public.user_keys;

create policy "user_keys self read"   on public.user_keys
    for select using (auth.uid() = user_id);
create policy "user_keys self write"  on public.user_keys
    for insert with check (auth.uid() = user_id);
create policy "user_keys self update" on public.user_keys
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Backend stores (written by the API using the SERVICE ROLE key; not exposed to
-- the browser, so RLS is left disabled and access is service-role only).
-- ---------------------------------------------------------------------------
create table if not exists public.api_keys (
    key_hash text primary key,
    name     text not null,
    created  timestamptz not null default now()
);

create table if not exists public.provider_config (
    key_hash         text primary key,
    provider         text not null default 'ollama',
    provider_api_key text not null default '',
    model            text not null default 'llama3.2'
);

create table if not exists public.usage_log (
    id               bigserial primary key,
    key_hash         text not null,
    ts               timestamptz not null default now(),
    baseline_tokens  bigint  not null,
    optimized_tokens bigint  not null,
    savings_pct      double precision not null,
    quality_proxy    double precision not null default 0,
    provider         text not null default '',
    model            text not null default '',
    cost_saved_usd   double precision not null default 0,
    brevitas_fee_usd double precision not null default 0,
    session_id       text not null default '',
    pipeline         text not null default '',
    agent            text not null default '',
    run_id           text not null default ''
);

create index if not exists usage_log_key_hash_ts on public.usage_log (key_hash, ts desc);
