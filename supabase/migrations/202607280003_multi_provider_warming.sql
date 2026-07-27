-- Multi-provider warming: admit DeepSeek alongside Anthropic and OpenAI in the
-- warming schema. The admitted list is exactly the capability table's
-- "profitable" verdicts — warming exists only where the keep-alive math
-- provably works:
--   anthropic  reads 0.10x input and refresh the TTL at no cost;
--   openai     reads 0.10x on the gpt-5.6/5.5 families with explicit 30m
--              breakpoints (a minimum lifetime, not a cap);
--   deepseek   reads 0.02x with hours-long persistence while in use.
-- Deliberately NOT admitted, and why:
--   groq, fireworks   reads cost 0.50x: one keep-alive ping costs exactly what
--                     one warm return saves, so warming can never net positive
--                     — those providers get measurement only.
--   perplexity        no cached-input discount exists; there is nothing to warm.
--   mistral, together, xai, openrouter
--                     the discount may be deep enough, but cache TTL and
--                     refresh-on-read are undocumented, so a keep-alive ping is
--                     speculative spend against an unknown eviction policy —
--                     measurement only until the provider documents lifetime.
-- Runtime activation stays separately gated (the API's active-provider list
-- and the worker's per-provider endpoint allowlist); this migration only lets
-- the storage layer represent the providers where warming is honest.
--
-- 202607280001 is checksum-frozen, so this file supersedes it: the functions
-- below copy its bodies exactly and change only the provider checks, plus one
-- new trailing DEFAULT argument on warm_due_claim (see its comment). The
-- inline provider CHECKs carry PostgreSQL's auto-generated
-- <table>_provider_check names; they are dropped and re-added under the same
-- names so a re-run stays idempotent.

begin;

alter table public.warm_credentials
    drop constraint if exists warm_credentials_provider_check;
alter table public.warm_credentials
    add constraint warm_credentials_provider_check
    check (provider in ('anthropic', 'openai', 'deepseek'));

alter table public.warm_prefixes
    drop constraint if exists warm_prefixes_provider_check;
alter table public.warm_prefixes
    add constraint warm_prefixes_provider_check
    check (provider in ('anthropic', 'openai', 'deepseek'));

alter table public.warm_budget_ledger
    drop constraint if exists warm_budget_ledger_provider_check;
alter table public.warm_budget_ledger
    add constraint warm_budget_ledger_provider_check
    check (provider in ('anthropic', 'openai', 'deepseek'));

-- Proxy-side arrival observation: upsert the prefix, fold the interarrival gap
-- into the EWMA and hour-of-week histogram, and attribute warm hits/misses.
-- Counter semantics: warm_pings = keep-alives sent; warm_hits = observed cache
-- reads after a ping; warm_misses = observed returns that missed despite pings.
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
        next_due_at = excluded.next_due_at,
        expires_at = v_now + interval '7 days';

    return jsonb_build_object(
        'schema', 'brevitas.warm-observe.v1', 'status', 'observed',
        'cache_read', p_cache_read
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
) to service_role;

-- One bounded transaction is the advisory lease. Competing Railway replicas
-- receive a single lease_unavailable row and send no pings; the winner gets
-- ROI-, budget-, and stop-loss-filtered candidates with spend reserved so a
-- crashed worker can never overspend the daily ceiling.
--
-- Per-provider ROI: the break-even probability is read-cost-fraction-shaped
-- (~0.11 per ping at 0.10x reads for anthropic/openai, ~0.02 at DeepSeek's
-- 0.02x), so a single scalar over-filters cheap-read providers. It arrives as
-- a trailing DEFAULT jsonb map (provider -> break-even probability) rather
-- than a new column: it is deployment configuration, not per-row state, and a
-- null map preserves the 202607280001 behaviour exactly. TTL and reserve need
-- no per-provider arguments — provider_ttl_seconds and ping_reserve_usd are
-- already observer-supplied per row, priced at the worst-case ping (a full
-- cache write at the model+TTL premium for anthropic; a full-input cache
-- miss for automatic-cache providers), so the greatest() reservation
-- upper-bounds what warm_ping_settle can book by construction. The flat
-- p_reserve_usd_per_mtok floor is an anthropic-calibrated extra floor
-- (tunable 0..1000), not the guarantee. The 202607280001 nine-argument
-- signature is dropped first: leaving it behind would make the ten-argument
-- call ambiguous.
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer
);
create or replace function public.warm_due_claim(
    p_claim_limit integer,
    p_reserve_usd_per_mtok numeric,
    p_roi_min_arrivals integer,
    p_roi_min_p numeric,
    p_roi_break_even_p numeric,
    p_stop_loss integer,
    p_max_gap_seconds integer,
    p_safety_margin_seconds integer,
    p_claim_lease_seconds integer default 900,
    -- null means the caller predates per-provider ROI: every provider uses
    -- the flat p_roi_break_even_p, exactly as 202607280001 behaved.
    p_roi_break_even_by_provider jsonb default null
) returns setof jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_day date := (clock_timestamp() at time zone 'utc')::date;
    v_bucket_key text;
    v_row record;
    v_claimed integer := 0;
    v_claimed_counts jsonb := '{}'::jsonb;
    v_customer_key text;
    v_pings_today integer;
    v_p_return numeric;
    v_break_even numeric;
    v_reserve numeric;
    v_reserved numeric;
    v_spent numeric;
    v_token uuid;
begin
    if coalesce(p_claim_limit, 0) not between 1 and 500
       or coalesce(p_reserve_usd_per_mtok, -1) not between 0 and 1000
       or coalesce(p_roi_min_arrivals, 0) not between 1 and 1000
       or coalesce(p_roi_min_p, -1) not between 0 and 1
       or coalesce(p_roi_break_even_p, -1) not between 0 and 1
       or coalesce(p_stop_loss, 0) not between 1 and 100
       or coalesce(p_max_gap_seconds, 0) not between 1 and 604800
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600
       or coalesce(p_claim_lease_seconds, 0) not between 60 and 7200 then
        raise exception 'warm claim bounds are invalid';
    end if;
    if p_roi_break_even_by_provider is not null then
        if jsonb_typeof(p_roi_break_even_by_provider) <> 'object' or exists (
            select 1 from jsonb_each(p_roi_break_even_by_provider) entry
             where entry.key not in ('anthropic', 'openai', 'deepseek')
                or case when jsonb_typeof(entry.value) = 'number'
                       then (entry.value)::text::numeric not between 0 and 1
                       else true end
        ) then
            raise exception 'warm claim bounds are invalid';
        end if;
    end if;
    if not pg_try_advisory_xact_lock(
        hashtextextended('brevitas.warming.due_claim.v1', 0)
    ) then
        return next jsonb_build_object(
            'schema', 'brevitas.warm-claim.v1', 'status', 'lease_unavailable'
        );
        return;
    end if;

    v_bucket_key := ((extract(isodow from (v_now at time zone 'utc'))::integer - 1) * 24
                     + extract(hour from (v_now at time zone 'utc'))::integer)::text;

    for v_row in
        select prefix.*, cred.credential_ciphertext, cred.daily_budget_usd,
               cred.max_pings_per_customer_day
          from public.warm_prefixes prefix
          join public.warm_credentials cred
            on cred.organization_id = prefix.organization_id
           and cred.provider = prefix.provider
         where prefix.state = 'active'
           and prefix.next_due_at <= v_now
           and prefix.expires_at > v_now
           and prefix.consecutive_misses < p_stop_loss
           and coalesce(prefix.ewma_interarrival_s <= p_max_gap_seconds, true)
           and cred.enabled
           and cred.credential_state = 'active'
         order by prefix.next_due_at
         limit p_claim_limit * 4
    loop
        exit when v_claimed >= p_claim_limit;

        -- ROI gate: warm only when the observed hour-of-week return frequency
        -- clears the break-even probability, with a stricter cold-start floor
        -- until enough arrivals make the histogram trustworthy.
        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);
        -- The CASE must stay parenthesized: plpgsql otherwise terminates the
        -- IF expression at the CASE's own THEN and CREATE FUNCTION fails.
        if v_p_return < (case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end) then
            continue;
        end if;

        v_customer_key := v_row.organization_id || ':' || v_row.customer_id;
        select coalesce(sum(peer.pings_today), 0) into v_pings_today
          from public.warm_prefixes peer
         where peer.organization_id = v_row.organization_id
           and peer.customer_id = v_row.customer_id
           and peer.provider = v_row.provider
           and peer.pings_today_date = v_day;
        if v_pings_today + coalesce((v_claimed_counts ->> v_customer_key)::integer, 0)
           >= v_row.max_pings_per_customer_day then
            continue;
        end if;

        -- The reservation must upper-bound actual spend for the daily ceiling
        -- to hold: settle books real receipt cost, so reserve the larger of
        -- the observer-priced worst case and the flat caller floor.
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));
        insert into public.warm_budget_ledger (organization_id, provider, day)
        values (v_row.organization_id, v_row.provider, v_day)
        on conflict (organization_id, provider, day) do nothing;
        select ledger.reserved_usd, ledger.spent_usd into v_reserved, v_spent
          from public.warm_budget_ledger ledger
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;
        if v_reserved + v_spent + v_reserve > v_row.daily_budget_usd then
            continue;
        end if;
        update public.warm_budget_ledger ledger
           set reserved_usd = ledger.reserved_usd + v_reserve,
               updated_at = v_now
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;

        -- Claim lease: must outlive a full sequential worker batch — not one
        -- tick — or an unsynchronized replica re-claims the tail of a batch
        -- mid-flight. The rotated token fences warm_ping_settle so a lapsed
        -- claimant cannot double-apply counters; warm_ping_settle assigns the
        -- real next_due_at.
        v_token := gen_random_uuid();
        update public.warm_prefixes prefix
           set next_due_at = v_now + make_interval(secs => greatest(
                   p_claim_lease_seconds, p_safety_margin_seconds, 60)),
               claim_token = v_token
         where prefix.organization_id = v_row.organization_id
           and prefix.customer_id = v_row.customer_id
           and prefix.provider = v_row.provider
           and prefix.prefix_hash = v_row.prefix_hash;

        v_claimed := v_claimed + 1;
        v_claimed_counts := jsonb_set(
            v_claimed_counts, array[v_customer_key],
            to_jsonb(coalesce((v_claimed_counts ->> v_customer_key)::integer, 0) + 1));
        return next jsonb_build_object(
            'schema', 'brevitas.warm-claim.v1', 'status', 'claimed',
            'organization_id', v_row.organization_id,
            'customer_id', v_row.customer_id,
            'provider', v_row.provider,
            'prefix_hash', v_row.prefix_hash,
            'prefix_tokens', v_row.prefix_tokens,
            'provider_ttl_seconds', v_row.provider_ttl_seconds,
            'payload_ciphertext', v_row.payload_ciphertext,
            'credential_ciphertext', v_row.credential_ciphertext,
            'reserved_usd', v_reserve,
            'budget_day', v_day,
            'claim_token', v_token
        );
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb
) to service_role;

-- Settle one claimed ping: release the reservation against the day it was
-- reserved, record actual spend, and apply the outcome. auth_failed halts the
-- whole organization+provider without a deploy; prefix_invalid stops one row.
-- Ledger money always books (the reservation and the ping were both real),
-- but the prefix row only mutates while the caller's claim token is current:
-- a claimant whose lease lapsed cannot double-count pings/misses or clobber
-- the schedule the row's new owner set.
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

revoke all on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) from public, anon, authenticated;
grant execute on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) to service_role;

-- Opt-in write path for PUT /v1/warming. An empty ciphertext keeps the stored
-- key; supplying a new ciphertext clears a prior auth_failed halt. Enabling
-- always restamps consent_at: spend terms are re-accepted per enable request.
create or replace function public.warm_credentials_upsert(
    p_organization_id uuid,
    p_provider text,
    p_credential_ciphertext text,
    p_enabled boolean,
    p_consent_actor_id uuid,
    p_daily_budget_usd numeric,
    p_max_warm_customers integer,
    p_max_pings_per_customer_day integer
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_saved record;
begin
    if p_organization_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm credential target is invalid';
    end if;
    if p_credential_ciphertext <> ''
       and octet_length(p_credential_ciphertext) > 65536 then
        raise exception 'warm credential ciphertext exceeds its absolute bound';
    end if;
    if coalesce(p_daily_budget_usd, -1) not between 0 and 99999999
       or coalesce(p_max_warm_customers, 0) not between 1 and 1000000
       or coalesce(p_max_pings_per_customer_day, 0) not between 1 and 10000 then
        raise exception 'warm credential bounds are invalid';
    end if;
    if p_enabled and p_consent_actor_id is null then
        raise exception 'enabling warming requires a consenting actor';
    end if;
    if p_enabled and p_daily_budget_usd <= 0 then
        raise exception 'enabling warming requires a nonzero daily budget';
    end if;
    if p_credential_ciphertext = '' and not exists (
        select 1 from public.warm_credentials cred
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider
    ) then
        raise exception 'warming requires a stored provider credential';
    end if;

    if p_credential_ciphertext = '' then
        -- Keep-existing-key must be a plain UPDATE: Postgres evaluates table
        -- CHECK constraints on the proposed insert row before ON CONFLICT
        -- resolution, so routing '' through the upsert raises the ciphertext
        -- length check and the DO UPDATE arm is unreachable. The guard above
        -- proved a row exists; a concurrent purge still leaves this findable.
        update public.warm_credentials cred
           set enabled = p_enabled,
               consent_actor_id = coalesce(p_consent_actor_id, cred.consent_actor_id),
               consent_at = case when p_enabled then v_now else cred.consent_at end,
               daily_budget_usd = p_daily_budget_usd,
               max_warm_customers = p_max_warm_customers,
               max_pings_per_customer_day = p_max_pings_per_customer_day,
               updated_at = v_now
         where cred.organization_id = p_organization_id
           and cred.provider = p_provider
        returning cred.provider, cred.enabled, cred.credential_state into v_saved;
        if not found then
            raise exception 'warming requires a stored provider credential';
        end if;
    else
        insert into public.warm_credentials as cred (
            organization_id, provider, credential_ciphertext, enabled,
            consent_actor_id, consent_at, daily_budget_usd,
            max_warm_customers, max_pings_per_customer_day,
            created_at, updated_at
        ) values (
            p_organization_id, p_provider, p_credential_ciphertext, p_enabled,
            p_consent_actor_id, case when p_enabled then v_now end,
            p_daily_budget_usd, p_max_warm_customers, p_max_pings_per_customer_day,
            v_now, v_now
        )
        on conflict (organization_id, provider) do update set
            credential_ciphertext = excluded.credential_ciphertext,
            credential_state = 'active',
            enabled = p_enabled,
            consent_actor_id = coalesce(p_consent_actor_id, cred.consent_actor_id),
            consent_at = case when p_enabled then v_now else cred.consent_at end,
            daily_budget_usd = p_daily_budget_usd,
            max_warm_customers = p_max_warm_customers,
            max_pings_per_customer_day = p_max_pings_per_customer_day,
            updated_at = v_now
        returning cred.provider, cred.enabled, cred.credential_state into v_saved;
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-credentials.v1', 'status', 'saved',
        'provider', v_saved.provider, 'enabled', v_saved.enabled,
        'credential_state', v_saved.credential_state
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_credentials_upsert(
    uuid, text, text, boolean, uuid, numeric, integer, integer
) from public, anon, authenticated;
grant execute on function public.warm_credentials_upsert(
    uuid, text, text, boolean, uuid, numeric, integer, integer
) to service_role;

-- DELETE /v1/warming/{provider}: drop the credential and every tracked prefix.
-- The budget ledger is financial evidence and is retained until purge.
create or replace function public.warm_credentials_purge(
    p_organization_id uuid,
    p_provider text
) returns jsonb as $$
declare
    v_prefixes_deleted integer;
    v_credentials_deleted integer;
begin
    if p_organization_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm credential target is invalid';
    end if;
    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id
       and prefix.provider = p_provider;
    get diagnostics v_prefixes_deleted = row_count;
    delete from public.warm_credentials cred
     where cred.organization_id = p_organization_id
       and cred.provider = p_provider;
    get diagnostics v_credentials_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-credentials.v1', 'status', 'purged',
        'provider', p_provider,
        'prefixes_deleted', v_prefixes_deleted,
        'credentials_deleted', v_credentials_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_credentials_purge(uuid, text)
    from public, anon, authenticated;
grant execute on function public.warm_credentials_purge(uuid, text)
    to service_role;

-- Self-contained privilege contract, re-asserted after superseding the RPCs:
-- warming state stays RPC-mediated only. No PostgREST role may retain any
-- direct table privilege, and RLS must be on.
do $privilege_contract$
declare
    contract_table text;
    grantee text;
    privilege text;
begin
    foreach contract_table in array array[
        'warm_credentials',
        'warm_prefixes',
        'warm_budget_ledger'
    ] loop
        if not (
            select relation.relrowsecurity
              from pg_class relation
             where relation.oid = to_regclass(format('public.%I', contract_table))
        ) then
            raise exception
                '202607280003 refuses to expose warming data without RLS: public.%',
                contract_table;
        end if;
        foreach grantee in array array['service_role', 'anon', 'authenticated'] loop
            foreach privilege in array array[
                'SELECT','INSERT','UPDATE','DELETE',
                'TRUNCATE','REFERENCES','TRIGGER'
            ] loop
                if has_table_privilege(
                    grantee, format('public.%I', contract_table), privilege
                ) then
                    raise exception
                        'unsafe % privilege contract for public.%: %',
                        grantee, contract_table, privilege;
                end if;
            end loop;
        end loop;
    end loop;
end;
$privilege_contract$;

commit;
