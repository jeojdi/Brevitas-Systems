\set ON_ERROR_STOP on

-- Simulate the schema deployed by the retired cache migration. The canonical
-- timestamped migration must purge this plaintext and replace its RPC signature.
create table if not exists public.semantic_cache (
    exact_hash text primary key,
    context_hash text not null,
    model_id text not null,
    embedding vector(384),
    response_json jsonb not null,
    prompt_tokens integer not null default 0,
    completion_tokens integer not null default 0,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    hit_count integer not null default 0
);

alter table public.semantic_cache disable row level security;
grant select, insert, update, delete on table public.semantic_cache to service_role;

create or replace function public.semantic_cache_lookup(
    p_embedding vector(384),
    p_context_hash text,
    p_threshold float default 0.97
) returns table (
    exact_hash text,
    response_json jsonb,
    prompt_tokens integer,
    completion_tokens integer,
    similarity float
) language sql security invoker set search_path = public, extensions, pg_catalog as $$
    select cache.exact_hash, cache.response_json, cache.prompt_tokens,
           cache.completion_tokens, 1.0::float
      from public.semantic_cache cache
     where cache.context_hash = p_context_hash
     limit 1;
$$;
grant execute on function public.semantic_cache_lookup(vector,text,float)
    to service_role;

insert into public.semantic_cache(
    exact_hash, context_hash, model_id, response_json, created_at, expires_at
) values (
    repeat('1', 64), repeat('2', 64), 'legacy:model',
    '{"plaintext":"must be purged"}'::jsonb,
    clock_timestamp(), clock_timestamp() + interval '1 hour'
) on conflict (exact_hash) do update set response_json = excluded.response_json;
