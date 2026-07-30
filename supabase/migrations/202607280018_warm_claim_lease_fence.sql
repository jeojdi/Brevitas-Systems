-- The warming claim lease must survive live traffic on the prefix it claimed.
--
-- warm_due_claim leases a prefix by pushing next_due_at out past a full
-- sequential worker batch and rotating claim_token
-- (202607280003_multi_provider_warming.sql:352-358). The lease exists because
-- "it must outlive a full sequential batch of pings, not one tick, or an
-- unsynchronized replica re-claims the batch's tail". But
-- warm_prefix_observe's `on conflict ... do update` recomputed the same column
-- from the arrival -- `next_due_at = excluded.next_due_at`, i.e.
-- now + (ttl - safety_margin), 240s for anthropic against a 1500s default lease
-- (api/worker.py:522-525) -- with no reference to claim_token. Warming
-- deliberately targets hot prefixes (`ewma_interarrival_s <= p_max_gap_seconds`),
-- so an arrival inside the lease window is the normal case, not the edge case:
-- the lease was erased almost immediately for exactly the rows it protects.
--
-- This migration fences that one assignment on the lease, and clears claim_token
-- when a claim settles as 'release' so a released row is handed straight back to
-- live traffic rather than sitting at the lease horizon until its next 'warmed'
-- settle.
--
-- What this deliberately does NOT do, per the same analysis: it does not add
-- `and prefix.claim_token is null` to warm_due_claim's candidate select. There is
-- no lease-expiry column, so a worker that dies between claim and settle leaves
-- the token set forever, and that predicate would then exclude the row from
-- warming permanently. The pushed-out next_due_at is the recovery bound, and it
-- still expires. It also does not touch the counter fence in warm_ping_settle
-- (202607280003:396-400), which exists so a lapsed claimant cannot double-apply
-- counters after a re-claim.
--
-- api/store.py:2972-2990 is the declared SQLite mirror of warm_prefix_observe and
-- must follow; it is not edited here.
--
-- Signatures, privileges and result schemas are unchanged. Forward-only and
-- idempotent.

-- REVERSE: PITR-ONLY -- removing the lease fence reintroduces the double-claim race; no evidence-preserving inverse exists

begin;

do $migration_precondition$
begin
    if to_regprocedure(
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)'
    ) is null
       or to_regprocedure(
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202607280018 requires the multi-provider warming RPCs';
    end if;
end;
$migration_precondition$;

create or replace function public.warm_prefix_observe(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_payload_ciphertext text,
    p_prefix_tokens integer,
    p_provider_ttl_seconds integer,
    p_safety_margin_seconds integer,
    p_cache_read boolean,
    -- null means the caller predates observer pricing: keep the stored value.
    p_ping_reserve_usd numeric default null
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_bucket_key text;
    v_customer_count integer;
begin
    if p_organization_id is null or p_customer_id is null then
        raise exception 'warm observation requires an organization and customer';
    end if;
    if p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm observation provider is invalid';
    end if;
    if p_prefix_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'warm observation prefix hash is invalid';
    end if;
    if octet_length(p_payload_ciphertext) < 1
       or octet_length(p_payload_ciphertext) > 16777216 then
        raise exception 'warm observation payload exceeds its absolute bound';
    end if;
    if p_prefix_tokens not between 0 and 2000000000
       or coalesce(p_provider_ttl_seconds, 0) not between 60 and 86400
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600
       or coalesce(p_ping_reserve_usd, 0) not between 0 and 99999999 then
        raise exception 'warm observation bounds are invalid';
    end if;

    -- Serialized per organization+provider so concurrent replicas cannot each
    -- observe a below-cap snapshot of this org's customer cap. The key is
    -- deliberately not fleet-global: one busy org's observations must never
    -- queue every other org's behind a single mutex.
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.warm_prefixes.write_bound.v1:'
                         || p_organization_id::text || ':' || p_provider, 0)
    );

    if not exists (
        select 1 from public.warm_credentials cred
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider
           and cred.enabled
           and cred.credential_state = 'active'
    ) then
        return jsonb_build_object(
            'schema', 'brevitas.warm-observe.v1', 'status', 'not_enabled'
        );
    end if;

    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id
       and prefix.expires_at <= v_now;

    if not exists (
        select 1 from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
    ) then
        select count(distinct prefix.customer_id) into v_customer_count
          from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
           and prefix.provider = p_provider;
        if v_customer_count >= (
            select cred.max_warm_customers from public.warm_credentials cred
             where cred.organization_id = p_organization_id
               and cred.provider = p_provider
        ) then
            return jsonb_build_object(
                'schema', 'brevitas.warm-observe.v1', 'status', 'customer_cap'
            );
        end if;
    end if;

    v_bucket_key := ((extract(isodow from (v_now at time zone 'utc'))::integer - 1) * 24
                     + extract(hour from (v_now at time zone 'utc'))::integer)::text;

    insert into public.warm_prefixes as prefix (
        organization_id, customer_id, provider, prefix_hash,
        payload_ciphertext, prefix_tokens, provider_ttl_seconds,
        ping_reserve_usd, arrival_count, ewma_interarrival_s, hour_histogram,
        created_at, last_seen_at, next_due_at, expires_at
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash,
        p_payload_ciphertext, p_prefix_tokens, p_provider_ttl_seconds,
        coalesce(p_ping_reserve_usd, 0),
        1, null, jsonb_build_object(v_bucket_key, 1),
        v_now, v_now,
        v_now + make_interval(secs => greatest(
            1, p_provider_ttl_seconds - p_safety_margin_seconds)),
        v_now + interval '7 days'
    )
    on conflict (organization_id, customer_id, provider, prefix_hash) do update set
        payload_ciphertext = excluded.payload_ciphertext,
        prefix_tokens = excluded.prefix_tokens,
        provider_ttl_seconds = excluded.provider_ttl_seconds,
        ping_reserve_usd = coalesce(p_ping_reserve_usd, prefix.ping_reserve_usd),
        arrival_count = prefix.arrival_count + 1,
        ewma_interarrival_s = round(coalesce(
            0.3 * extract(epoch from (v_now - prefix.last_seen_at))
            + 0.7 * prefix.ewma_interarrival_s,
            extract(epoch from (v_now - prefix.last_seen_at))), 3),
        hour_histogram = jsonb_set(
            prefix.hour_histogram, array[v_bucket_key],
            to_jsonb(coalesce((prefix.hour_histogram ->> v_bucket_key)::integer, 0) + 1)),
        warm_hits = prefix.warm_hits
            + case when p_cache_read and prefix.warm_pings > 0 then 1 else 0 end,
        warm_misses = prefix.warm_misses
            + case when not p_cache_read and prefix.warm_pings > 0 then 1 else 0 end,
        consecutive_misses = case when p_cache_read then 0
            else prefix.consecutive_misses end,
        state = 'active',
        last_seen_at = v_now,
        -- Fence the schedule on the claim lease. warm_due_claim pushes
        -- next_due_at out past a full sequential worker batch and rotates
        -- claim_token (202607280003:352-358) precisely so an unsynchronized
        -- replica cannot re-claim the tail of a batch mid-flight. Warming
        -- targets HOT prefixes, so an arrival inside that lease window is the
        -- normal case -- and recomputing next_due_at here erased the lease
        -- almost immediately for exactly the rows it protects (anthropic:
        -- 300 - 60 = 240s against a 1500s default lease). While a claim token
        -- is held, the claimant owns the schedule; warm_ping_settle assigns the
        -- real next_due_at and clears the token.
        next_due_at = case
            when prefix.claim_token is null then excluded.next_due_at
            else prefix.next_due_at
        end,
        expires_at = v_now + interval '7 days';

    return jsonb_build_object(
        'schema', 'brevitas.warm-observe.v1', 'status', 'observed',
        'cache_read', p_cache_read
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

create or replace function public.warm_ping_settle(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_budget_day date,
    p_reserved_usd numeric,
    p_spent_usd numeric,
    p_outcome text,
    p_provider_ttl_seconds integer,
    p_safety_margin_seconds integer,
    p_claim_token uuid default null
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_day date := (clock_timestamp() at time zone 'utc')::date;
begin
    if p_provider not in ('anthropic', 'openai', 'deepseek')
       or p_prefix_hash !~ '^[0-9a-f]{64}$'
       or p_budget_day is null
       or coalesce(p_reserved_usd, -1) not between 0 and 99999999
       or coalesce(p_spent_usd, -1) not between 0 and 99999999
       or p_outcome not in ('warmed', 'release', 'prefix_invalid', 'auth_failed')
       or coalesce(p_provider_ttl_seconds, 0) not between 60 and 86400
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600 then
        raise exception 'warm settle arguments are invalid';
    end if;

    update public.warm_budget_ledger ledger
       set reserved_usd = greatest(0, ledger.reserved_usd - p_reserved_usd),
           spent_usd = ledger.spent_usd
               + case when p_outcome = 'warmed' then p_spent_usd else 0 end,
           updated_at = v_now
     where ledger.organization_id = p_organization_id
       and ledger.provider = p_provider
       and ledger.day = p_budget_day;

    if p_outcome = 'warmed' then
        update public.warm_prefixes prefix
           set warm_pings = prefix.warm_pings + 1,
               consecutive_misses = prefix.consecutive_misses + 1,
               pings_today = case when prefix.pings_today_date = v_day
                   then prefix.pings_today + 1 else 1 end,
               pings_today_date = v_day,
               next_due_at = v_now + make_interval(secs => greatest(
                   1, p_provider_ttl_seconds - p_safety_margin_seconds)),
               claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'release' then
        -- A released claim is over, so drop the token: the schedule fence in
        -- warm_prefix_observe hands the row back to live traffic instead of
        -- pinning it at the lease horizon until the next claim settles as
        -- 'warmed'. Same token fence as the other arms, so a lapsed claimant
        -- cannot release someone else's claim.
        update public.warm_prefixes prefix
           set claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'prefix_invalid' then
        update public.warm_prefixes prefix
           set state = 'stopped',
               claim_token = null
         where prefix.organization_id = p_organization_id
           and prefix.customer_id = p_customer_id
           and prefix.provider = p_provider
           and prefix.prefix_hash = p_prefix_hash
           and (p_claim_token is null or prefix.claim_token = p_claim_token);
    elsif p_outcome = 'auth_failed' then
        update public.warm_credentials cred
           set credential_state = 'auth_failed',
               updated_at = v_now
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider;
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-settle.v1', 'status', 'settled',
        'outcome', p_outcome
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) to service_role;
revoke all on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) from public, anon, authenticated;
grant execute on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) to service_role;

comment on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) is 'Proxy-side warm prefix observation; the warming schedule is fenced on the claim lease so a live arrival cannot cancel an in-flight ping batch.';

commit;
