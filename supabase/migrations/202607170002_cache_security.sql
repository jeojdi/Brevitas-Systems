-- Hosted response cache: encrypted, tenant-scoped, purgeable, and service-only.
-- Existing plaintext cache rows are deliberately discarded rather than migrated.

create extension if not exists vector;

create table if not exists public.semantic_cache (
    exact_hash text primary key,
    context_hash text not null,
    model_id text not null,
    embedding vector(384),
    response_json jsonb,
    response_ciphertext text not null default '',
    tenant_namespace text not null default '',
    prompt_tokens integer not null default 0,
    completion_tokens integer not null default 0,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    hit_count integer not null default 0
);

alter table public.semantic_cache add column if not exists response_ciphertext text not null default '';
alter table public.semantic_cache add column if not exists tenant_namespace text not null default '';
alter table public.semantic_cache alter column response_json drop not null;
alter table public.semantic_cache alter column response_ciphertext drop default;

-- Application encryption keys are intentionally unavailable to SQL migrations.
-- Purging old plaintext is safer than retaining content that violates the new policy.
delete from public.semantic_cache where response_ciphertext = '';
update public.semantic_cache set response_json = null where response_json is not null;
delete from public.semantic_cache
 where expires_at <= created_at
    or expires_at > created_at + interval '24 hours'
    or octet_length(response_ciphertext) > 16777216;

do $$ begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'semantic_cache_positive_bounded_ttl'
           and conrelid = 'public.semantic_cache'::regclass
    ) then
        alter table public.semantic_cache add constraint semantic_cache_positive_bounded_ttl
            check (expires_at > created_at
                   and expires_at <= created_at + interval '24 hours');
    end if;
    if not exists (
        select 1 from pg_constraint
         where conname = 'semantic_cache_ciphertext_size'
           and conrelid = 'public.semantic_cache'::regclass
    ) then
        alter table public.semantic_cache add constraint semantic_cache_ciphertext_size
            check (octet_length(response_ciphertext) between 1 and 16777216);
    end if;
    if not exists (
        select 1 from pg_constraint
         where conname = 'semantic_cache_metadata_size'
           and conrelid = 'public.semantic_cache'::regclass
    ) then
        alter table public.semantic_cache add constraint semantic_cache_metadata_size check (
            exact_hash ~ '^[0-9a-f]{64}$'
            and context_hash ~ '^[0-9a-f]{64}$'
            and octet_length(model_id) between 1 and 512
            and tenant_namespace ~ '^[0-9a-f]{64}$'
            and prompt_tokens between 0 and 2000000000
            and completion_tokens between 0 and 2000000000
        );
    end if;
    if not exists (
        select 1 from pg_constraint
         where conname = 'semantic_cache_no_plaintext'
           and conrelid = 'public.semantic_cache'::regclass
    ) then
        alter table public.semantic_cache add constraint semantic_cache_no_plaintext
            check (response_json is null);
    end if;
end $$;

create index if not exists semantic_cache_ctx
    on public.semantic_cache (context_hash, expires_at);
create index if not exists semantic_cache_tenant
    on public.semantic_cache (tenant_namespace, expires_at);
create index if not exists semantic_cache_evict
    on public.semantic_cache (created_at desc, exact_hash desc);
create index if not exists semantic_cache_emb
    on public.semantic_cache using ivfflat (embedding vector_cosine_ops) with (lists = 100);

alter table public.semantic_cache enable row level security;
revoke all on table public.semantic_cache from public, anon, authenticated;
-- Writes go only through the security-definer RPC so every replica participates
-- in the same serialized purge/upsert/eviction critical section.
revoke insert, update on table public.semantic_cache from service_role;
grant select, delete on table public.semantic_cache to service_role;

-- PostgreSQL, not an application replica, owns the retention timestamps. This
-- also repairs future/backdated direct owner writes and strips plaintext before
-- constraints are evaluated.
create or replace function public.normalize_semantic_cache_write()
returns trigger as $$
declare
    v_now timestamptz := clock_timestamp();
    v_ttl_seconds integer;
begin
    v_ttl_seconds := least(86400, greatest(1, coalesce(
        extract(epoch from (new.expires_at - new.created_at))::integer, 3600
    )));
    if tg_op = 'INSERT' or new.created_at > v_now then
        new.created_at := v_now;
        new.expires_at := v_now + make_interval(secs => v_ttl_seconds);
    else
        new.expires_at := new.created_at + make_interval(secs => v_ttl_seconds);
    end if;
    new.response_json := null;
    return new;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

drop trigger if exists semantic_cache_normalize_write on public.semantic_cache;
create trigger semantic_cache_normalize_write
before insert or update on public.semantic_cache
for each row execute function public.normalize_semantic_cache_write();

revoke all on function public.normalize_semantic_cache_write()
    from public, anon, authenticated;

-- Absolute backstop: even a buggy or older caller cannot grow this table without
-- limit. Normal callers request a much smaller tenant-independent cap through the
-- transactional store function below.
create or replace function public.enforce_semantic_cache_absolute_bound()
returns trigger as $$
begin
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.semantic_cache.write_bound.v1', 0)
    );
    delete from public.semantic_cache where expires_at <= now();
    delete from public.semantic_cache
     where exact_hash in (
        select exact_hash from public.semantic_cache
         order by created_at desc, exact_hash desc
         offset 1000000
     );
    return null;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

drop trigger if exists semantic_cache_absolute_bound on public.semantic_cache;
create trigger semantic_cache_absolute_bound
after insert on public.semantic_cache
for each statement execute function public.enforce_semantic_cache_absolute_bound();

revoke all on function public.enforce_semantic_cache_absolute_bound() from public, anon, authenticated;

drop function if exists public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    timestamptz, timestamptz, integer
);

create or replace function public.semantic_cache_store_bounded(
    p_exact_hash text,
    p_context_hash text,
    p_model_id text,
    p_embedding vector(384),
    p_response_ciphertext text,
    p_tenant_namespace text,
    p_prompt_tokens integer,
    p_completion_tokens integer,
    p_ttl_seconds integer,
    p_max_entries integer
) returns void as $$
declare
    v_now timestamptz;
    v_ttl_seconds integer;
begin
    if p_max_entries < 1 or p_max_entries > 1000000 then
        raise exception 'semantic cache entry bound is invalid';
    end if;
    if octet_length(p_response_ciphertext) < 1
       or octet_length(p_response_ciphertext) > 16777216 then
        raise exception 'semantic cache ciphertext exceeds its absolute bound';
    end if;

    -- Transaction-scoped and shared by the trigger. The lock is acquired before
    -- every state read/write so concurrent PostgREST replicas cannot each observe
    -- a below-cap snapshot and overshoot after committing.
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.semantic_cache.write_bound.v1', 0)
    );
    v_now := clock_timestamp();
    v_ttl_seconds := least(86400, greatest(1, coalesce(p_ttl_seconds, 3600)));
    delete from public.semantic_cache where expires_at <= v_now;

    insert into public.semantic_cache (
        exact_hash, context_hash, model_id, embedding, response_json,
        response_ciphertext, tenant_namespace, prompt_tokens, completion_tokens,
        created_at, expires_at
    ) values (
        p_exact_hash, p_context_hash, p_model_id, p_embedding, null,
        p_response_ciphertext, p_tenant_namespace,
        least(2000000000, greatest(0, coalesce(p_prompt_tokens, 0))),
        least(2000000000, greatest(0, coalesce(p_completion_tokens, 0))),
        v_now, v_now + make_interval(secs => v_ttl_seconds)
    )
    on conflict (exact_hash) do update set
        context_hash = excluded.context_hash,
        model_id = excluded.model_id,
        embedding = excluded.embedding,
        response_json = null,
        response_ciphertext = excluded.response_ciphertext,
        tenant_namespace = excluded.tenant_namespace,
        prompt_tokens = excluded.prompt_tokens,
        completion_tokens = excluded.completion_tokens,
        created_at = excluded.created_at,
        expires_at = excluded.expires_at,
        hit_count = 0;

    delete from public.semantic_cache
     where exact_hash in (
        select exact_hash from public.semantic_cache
         order by created_at desc, exact_hash desc
         offset p_max_entries
     );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public, extensions;

revoke all on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    integer, integer
) from public, anon, authenticated;
grant execute on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    integer, integer
) to service_role;

-- The legacy function returned response_json. PostgreSQL cannot change OUT
-- columns with CREATE OR REPLACE, so upgrades must remove that signature first.
drop function if exists public.semantic_cache_lookup(vector, text, float);
drop function if exists public.semantic_cache_lookup(vector, text, float, text, text);

create or replace function public.semantic_cache_lookup(
    p_embedding vector(384),
    p_context_hash text,
    p_threshold float,
    p_tenant_namespace text,
    p_model_id text
) returns table (
    exact_hash text,
    response_ciphertext text,
    prompt_tokens integer,
    completion_tokens integer,
    similarity float
) as $$
    select cache.exact_hash, cache.response_ciphertext, cache.prompt_tokens,
           cache.completion_tokens,
           (1 - (cache.embedding <=> p_embedding))::float as similarity
      from public.semantic_cache cache
     where cache.context_hash = p_context_hash
       and cache.tenant_namespace = p_tenant_namespace
       and cache.model_id = p_model_id
       and cache.expires_at > now()
       and cache.embedding is not null
       and cache.response_ciphertext <> ''
       and (1 - (cache.embedding <=> p_embedding)) >= p_threshold
     order by cache.embedding <=> p_embedding
     limit 1;
$$ language sql security invoker set search_path = pg_catalog, public, extensions;

revoke all on function public.semantic_cache_lookup(vector, text, float, text, text)
    from public, anon, authenticated;
grant execute on function public.semantic_cache_lookup(vector, text, float, text, text)
    to service_role;
