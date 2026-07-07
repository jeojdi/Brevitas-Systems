-- Migration 001 — persistent stores for Brevitas (keys, usage, provider config, waitlist).
-- Run ONCE on the canonical Supabase project — the one Railway's NEXT_PUBLIC_SUPABASE_URL
-- points at (currently wyfzmfnswtzyhwbltbpy). Idempotent: safe to re-run.
-- profiles/user_keys use IF NOT EXISTS so an already-populated project is left untouched.
--
-- Without these tables the backend's SupabaseUsageStore insert fails and make_store()
-- silently falls back to Railway's ephemeral SQLite, which is wiped on every redeploy
-- (that is the "Failed to load stats" / re-login bug). After running this, run
-- 002_semantic_cache.sql (optional — hosted semantic cache only).

-- ── API keys (backend key store; keyed by SHA-256 hash of the raw key) ─────────
create table if not exists public.api_keys (
    key_hash text primary key,
    name     text not null,
    created  timestamptz not null default now()
);

-- ── Usage log (one row per proxied call; drives stats + %-of-savings billing) ──
create table if not exists public.usage_log (
    id               bigint generated always as identity primary key,
    key_hash         text        not null,
    ts               timestamptz not null default now(),
    baseline_tokens  integer     not null,
    optimized_tokens integer     not null,
    savings_pct      double precision not null,
    quality_proxy    double precision not null,
    provider         text   not null default '',
    model            text   not null default '',
    cost_saved_usd   double precision not null default 0,
    brevitas_fee_usd double precision not null default 0,
    session_id       text   not null default '',
    pipeline         text   not null default '',
    agent            text   not null default '',
    run_id           text   not null default '',
    cached_tokens    bigint not null default 0
);
create index if not exists usage_log_key_hash_idx on public.usage_log (key_hash);

-- ── Per-key provider routing (which upstream + key each Brevitas key uses) ─────
create table if not exists public.provider_config (
    key_hash         text primary key,
    provider         text not null default 'ollama',
    provider_api_key text not null default '',
    model            text not null default 'llama3.2'
);

-- ── Waitlist (marketing signups; mirrors the legacy project's columns) ─────────
create table if not exists public.waitlist (
    id             bigint generated always as identity primary key,
    email          text not null,
    name           text,
    company        text,
    role           text,
    use_case       text,
    source         text,
    pipeline_shape text,
    monthly_spend  text,
    orchestrator   text,
    notes          text,
    design_partner boolean default false,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- ── Dashboard key cache (user_id -> api_key). Already present on the live
--    project; created here only so a fresh project bootstraps cleanly. ──────────
create table if not exists public.user_keys (
    user_id uuid primary key references auth.users(id) on delete cascade,
    api_key text not null
);

-- ── RLS ────────────────────────────────────────────────────────────────────────
-- Backend tables are service_role-only: the backend bypasses RLS, and the dashboard
-- reaches them through the /v1 API, never directly. Enable RLS with no policies so
-- anon/authenticated clients get nothing.
alter table public.api_keys        enable row level security;
alter table public.usage_log       enable row level security;
alter table public.provider_config enable row level security;

-- Waitlist: the public signup form (anon) may insert; only service_role reads.
alter table public.waitlist enable row level security;
drop policy if exists waitlist_public_insert on public.waitlist;
create policy waitlist_public_insert on public.waitlist
    for insert to anon, authenticated with check (true);

-- user_keys: each signed-in user manages only their own row.
alter table public.user_keys enable row level security;
drop policy if exists user_keys_owner on public.user_keys;
create policy user_keys_owner on public.user_keys
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
