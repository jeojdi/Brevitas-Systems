-- Migration 002 — caching support for Brevitas.
-- Run once on the canonical Supabase project (same one as 001_persistent_stores.sql).
--
-- Two independent pieces:
--   A) usage_log.cached_tokens  — REQUIRED. The backend now reports provider-native
--      prompt-cache read tokens per call; without this column the Supabase usage insert
--      fails. Safe to run on an existing table (idempotent).
--   B) semantic_cache table      — OPTIONAL, for the HOSTED semantic response cache
--      (cross-machine sharing). The local proxy uses a SQLite cache and does NOT need
--      this. Only needed once the pgvector-backed SemanticCache backend is enabled.

-- ── A) provider-cache accounting column (required by the new billing path) ──────
alter table public.usage_log
    add column if not exists cached_tokens bigint not null default 0;

-- ── B) hosted semantic response cache (optional; needs the `vector` extension) ──
create extension if not exists vector;

-- Embedding dimension is 384 — Brevitas embeds locally with bge-small-en-v1.5.
-- If you swap the embedding model, this dimension and the app must change together.
create table if not exists public.semantic_cache (
    exact_hash        text primary key,        -- SHA-256 of the whole request (Layer 1)
    context_hash      text not null,           -- request minus the last user msg (Layer 2 bucket)
    model_id          text not null,           -- "provider:model" — isolates cache per model
    embedding         vector(384),             -- last-user-message embedding (nullable)
    response_json     jsonb not null,          -- provider response, replayed verbatim on a hit
    prompt_tokens     integer not null default 0,
    completion_tokens integer not null default 0,
    created_at        timestamptz not null default now(),
    expires_at        timestamptz not null,
    hit_count         integer not null default 0
);

-- Layer-2 search is scoped to one context bucket (identical prefix), so it stays small.
create index if not exists semantic_cache_ctx
    on public.semantic_cache (context_hash, expires_at);
-- Approximate-NN index for cosine similarity within a bucket.
create index if not exists semantic_cache_emb
    on public.semantic_cache using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- Backend-only table (written with the service-role key); keep RLS off + service-role only,
-- matching the api_keys/usage_log convention in migration 001.

-- Semantic lookup: nearest non-expired neighbour in the same context bucket, above a
-- similarity floor. Returns at most one row. (<=> is cosine distance; 1 - distance = cosine.)
create or replace function public.semantic_cache_lookup(
    p_embedding    vector(384),
    p_context_hash text,
    p_threshold    float default 0.97
) returns table (
    exact_hash        text,
    response_json     jsonb,
    prompt_tokens     integer,
    completion_tokens integer,
    similarity        float
) as $$
    select exact_hash, response_json, prompt_tokens, completion_tokens,
           (1 - (embedding <=> p_embedding))::float as similarity
    from public.semantic_cache
    where context_hash = p_context_hash
      and expires_at > now()
      and embedding is not null
      and (1 - (embedding <=> p_embedding)) >= p_threshold
    order by embedding <=> p_embedding
    limit 1;
$$ language sql;
