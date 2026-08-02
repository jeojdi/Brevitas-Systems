-- OpenRouter (and other reseller) authoritative cost on the semantic cache.
--
-- WHY. MODEL_PRICES is deliberately silent for resellers (brevitas/receipts.py
-- _RESELLER_PROVIDERS): OpenRouter bills its own rates, so pricing their traffic
-- off a first-party table would bill confidently-wrong dollars. The honest
-- alternative is the reseller's OWN reported figure -- OpenRouter returns
-- usage.cost, the dollars a call actually cost -- which brevitas/receipts.py
-- provider_reported_cost() reads and reseller_authoritative_costs() turns into a
-- priced row. A LIVE reseller call records that as real actual spend; a zero-spend
-- CACHE REPLAY bills the whole of it as measured savings.
--
-- For the replay to be priced at record time (the settlement evidence function
-- 202607280008 values a zero-spend row from its OWN verified_savings_usd, using
-- the anchor only as an eligibility gate), the original call's cost must travel on
-- the cache entry. That is what this column stores, and what the store overload
-- below carries in.
--
-- The exact-hit read path is a direct table select (brevitas/semantic_cache.py
-- _exact_select), so it needs only the column -- no lookup RPC change. Semantic
-- hits are quality-affecting and non-billable by default, so their RPC is left
-- untouched and they replay at reported_cost_usd = 0 (unpriced/$0), which is the
-- safe direction.

alter table public.semantic_cache
    add column if not exists reported_cost_usd numeric(18,10) not null default 0;

comment on column public.semantic_cache.reported_cost_usd is
    'Dollars the ORIGINAL paid call cost, as reported by a reseller provider '
    '(OpenRouter usage.cost). Carried onto a zero-spend replay receipt as its '
    'measured savings. 0 means not reported (first-party providers, or an entry '
    'written before this column) and the replay prices unpriced/$0 -- never guessed.';

-- A NEW 12-argument overload. The 11-argument anchor version from 202607280031
-- and the 10-argument base from 202607170002 STAY, so a process that predates
-- this column still resolves its call and degrades to un-costed (replay bills $0)
-- rather than failing the write. Body is 202607280031's plus the one column.
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
    p_max_entries integer,
    p_origin_request_id text,
    p_reported_cost_usd numeric
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

    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.semantic_cache.write_bound.v1', 0)
    );
    v_now := clock_timestamp();
    v_ttl_seconds := least(86400, greatest(1, coalesce(p_ttl_seconds, 3600)));
    delete from public.semantic_cache where expires_at <= v_now;

    insert into public.semantic_cache (
        exact_hash, context_hash, model_id, embedding, response_json,
        response_ciphertext, tenant_namespace, prompt_tokens, completion_tokens,
        created_at, expires_at, origin_request_id, reported_cost_usd
    ) values (
        p_exact_hash, p_context_hash, p_model_id, p_embedding, null,
        p_response_ciphertext, p_tenant_namespace,
        least(2000000000, greatest(0, coalesce(p_prompt_tokens, 0))),
        least(2000000000, greatest(0, coalesce(p_completion_tokens, 0))),
        v_now, v_now + make_interval(secs => v_ttl_seconds),
        left(coalesce(p_origin_request_id, ''), 128),
        greatest(0, coalesce(p_reported_cost_usd, 0))
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
        origin_request_id = excluded.origin_request_id,
        reported_cost_usd = excluded.reported_cost_usd,
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
    integer, integer, text, numeric
) from public, anon, authenticated;
grant execute on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    integer, integer, text, numeric
) to service_role;

comment on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer, integer, integer, text, numeric
) is
    'Cost-carrying overload of the bounded cache write (202608010001). Identical '
    'to the 202607280031 eleven-argument anchor version plus reported_cost_usd, '
    'the reseller-reported dollars the stored (paid) call cost. The eleven- and '
    'ten-argument versions are deliberately retained so an un-restarted process '
    'still resolves its call and degrades to un-costed (replay bills $0) rather '
    'than losing the write.';
