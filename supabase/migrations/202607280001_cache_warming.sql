-- Predictive cache warming: org-scoped opt-in credentials, per-customer prefix
-- observations, and a reservation-then-settle budget ledger. Every table is
-- RLS-enabled with zero policies and zero direct DML for any PostgREST role:
-- all reads and writes flow through the security-definer RPCs below so budget
-- accounting, caps, and eviction share one serialized critical section.

begin;

create table if not exists public.warm_credentials (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    provider text not null check (provider in ('anthropic', 'openai')),
    -- KMS-encrypted provider key; plaintext never reaches SQL.
    credential_ciphertext text not null
        check (octet_length(credential_ciphertext) between 1 and 65536),
    enabled boolean not null default false,
    consent_actor_id uuid references auth.users(id) on delete set null,
    consent_at timestamptz,
    daily_budget_usd numeric(18,10) not null default 0
        check (daily_budget_usd >= 0),
    max_warm_customers integer not null default 100
        check (max_warm_customers between 1 and 1000000),
    max_pings_per_customer_day integer not null default 288
        check (max_pings_per_customer_day between 1 and 10000),
    credential_state text not null default 'active'
        check (credential_state in ('active', 'auth_failed')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (organization_id, provider),
    -- Spending someone else's provider budget requires recorded consent and a
    -- nonzero spend ceiling; enabling without either is unrepresentable.
    constraint warm_credentials_enabled_requires_consent
        check (not enabled or (consent_at is not null and daily_budget_usd > 0))
);

create table if not exists public.warm_prefixes (
    organization_id uuid not null,
    customer_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai')),
    prefix_hash text not null check (prefix_hash ~ '^[0-9a-f]{64}$'),
    -- Encrypted replay payload (system/tools/messages-prefix/markers/ttl/vary
    -- headers); plaintext never reaches SQL.
    payload_ciphertext text not null
        check (octet_length(payload_ciphertext) between 1 and 16777216),
    prefix_tokens integer not null check (prefix_tokens between 0 and 2000000000),
    provider_ttl_seconds integer not null
        check (provider_ttl_seconds between 60 and 86400),
    -- Observer-priced worst case for one keep-alive ping (a full cache write
    -- at the stored model+TTL rate). warm_due_claim reserves at least this,
    -- so warm_ping_settle's actual spend can never exceed its reservation.
    ping_reserve_usd numeric(18,10) not null default 0
        check (ping_reserve_usd >= 0 and ping_reserve_usd <= 99999999),
    -- Rotated by warm_due_claim; warm_ping_settle only mutates the row when
    -- the caller still holds the token, so a replica that re-claims after a
    -- lapsed lease cannot have its counters double-applied.
    claim_token uuid,
    arrival_count integer not null default 0 check (arrival_count >= 0),
    ewma_interarrival_s numeric check (ewma_interarrival_s is null or ewma_interarrival_s >= 0),
    -- 168-bucket hour-of-week arrival histogram keyed '0'..'167' (UTC).
    hour_histogram jsonb not null default '{}'::jsonb,
    warm_pings integer not null default 0 check (warm_pings >= 0),
    warm_hits integer not null default 0 check (warm_hits >= 0),
    warm_misses integer not null default 0 check (warm_misses >= 0),
    consecutive_misses integer not null default 0 check (consecutive_misses >= 0),
    pings_today integer not null default 0 check (pings_today >= 0),
    pings_today_date date,
    state text not null default 'active' check (state in ('active', 'stopped')),
    created_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    next_due_at timestamptz not null,
    expires_at timestamptz not null,
    primary key (organization_id, customer_id, provider, prefix_hash),
    constraint warm_prefixes_customer_tenant_fk
        foreign key (organization_id, customer_id)
        references public.customers(organization_id, id) on delete cascade,
    constraint warm_prefixes_positive_bounded_ttl
        check (expires_at > created_at
               and expires_at <= last_seen_at + interval '7 days')
);

create table if not exists public.warm_budget_ledger (
    organization_id uuid not null references public.organizations(id) on delete cascade,
    provider text not null check (provider in ('anthropic', 'openai')),
    day date not null,
    reserved_usd numeric(18,10) not null default 0 check (reserved_usd >= 0),
    spent_usd numeric(18,10) not null default 0 check (spent_usd >= 0),
    updated_at timestamptz not null default now(),
    primary key (organization_id, provider, day)
);

create index if not exists warm_prefixes_due_idx
    on public.warm_prefixes (next_due_at) where state = 'active';
create index if not exists warm_prefixes_expiry_idx
    on public.warm_prefixes (expires_at);
create index if not exists warm_prefixes_org_idx
    on public.warm_prefixes (organization_id, provider, last_seen_at desc);
create index if not exists warm_prefixes_bound_idx
    on public.warm_prefixes (last_seen_at desc, prefix_hash desc);

alter table public.warm_credentials enable row level security;
alter table public.warm_prefixes enable row level security;
alter table public.warm_budget_ledger enable row level security;
revoke all on table public.warm_credentials from public, anon, authenticated, service_role;
revoke all on table public.warm_prefixes from public, anon, authenticated, service_role;
revoke all on table public.warm_budget_ledger from public, anon, authenticated, service_role;

-- Absolute backstop: even a buggy or older caller cannot grow this table
-- without limit. Normal callers stay far below the cap through the
-- per-organization limits enforced by warm_prefix_observe. The planner's row
-- estimate gates the pass so the common far-below-cap insert pays one catalog
-- probe instead of a fleet-global lock plus a full-table sort; reltuples lags
-- autoanalyze, so the cap binds approximately — acceptable for a backstop.
create or replace function public.enforce_warm_prefixes_absolute_bound()
returns trigger as $$
begin
    if coalesce((
        select greatest(relation.reltuples, 0)
          from pg_class relation
         where relation.oid = 'public.warm_prefixes'::regclass
    ), 0) < 1000000 then
        return null;
    end if;
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.warm_prefixes.write_bound.v1', 0)
    );
    delete from public.warm_prefixes where expires_at <= now();
    delete from public.warm_prefixes prefix
     where (prefix.organization_id, prefix.customer_id,
            prefix.provider, prefix.prefix_hash) in (
        select victim.organization_id, victim.customer_id,
               victim.provider, victim.prefix_hash
          from public.warm_prefixes victim
         order by victim.last_seen_at desc, victim.prefix_hash desc
         offset 1000000
     );
    return null;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

drop trigger if exists warm_prefixes_absolute_bound on public.warm_prefixes;
create trigger warm_prefixes_absolute_bound
after insert on public.warm_prefixes
for each statement execute function public.enforce_warm_prefixes_absolute_bound();

revoke all on function public.enforce_warm_prefixes_absolute_bound()
    from public, anon, authenticated;

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
    if p_provider not in ('anthropic', 'openai') then
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
create or replace function public.warm_due_claim(
    p_claim_limit integer,
    p_reserve_usd_per_mtok numeric,
    p_roi_min_arrivals integer,
    p_roi_min_p numeric,
    p_roi_break_even_p numeric,
    p_stop_loss integer,
    p_max_gap_seconds integer,
    p_safety_margin_seconds integer,
    p_claim_lease_seconds integer default 900
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
        -- The CASE must stay parenthesized: plpgsql otherwise terminates the
        -- IF expression at the CASE's own THEN and CREATE FUNCTION fails.
        if v_p_return < (case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else p_roi_break_even_p end) then
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
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer
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
    if p_provider not in ('anthropic', 'openai')
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
    if p_organization_id is null or p_provider not in ('anthropic', 'openai') then
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
    if p_organization_id is null or p_provider not in ('anthropic', 'openai') then
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

-- Org status for GET /v1/warming. Ciphertexts never leave the database here.
create or replace function public.warm_read_status(
    p_organization_id uuid
) returns jsonb
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select jsonb_build_object(
        'schema', 'brevitas.warm-status.v1',
        'organization_id', p_organization_id,
        'providers', coalesce(jsonb_agg(jsonb_build_object(
            'provider', cred.provider,
            'enabled', cred.enabled,
            'credential_state', cred.credential_state,
            'consent_at', cred.consent_at,
            'daily_budget_usd', cred.daily_budget_usd,
            'max_warm_customers', cred.max_warm_customers,
            'max_pings_per_customer_day', cred.max_pings_per_customer_day,
            'reserved_today_usd', coalesce(ledger.reserved_usd, 0),
            'spent_today_usd', coalesce(ledger.spent_usd, 0),
            'active_prefixes', coalesce(stats.active_prefixes, 0),
            'warm_customers', coalesce(stats.warm_customers, 0),
            'warm_pings', coalesce(stats.warm_pings, 0),
            'warm_hits', coalesce(stats.warm_hits, 0),
            'warm_misses', coalesce(stats.warm_misses, 0),
            'next_due_at', stats.next_due_at
        ) order by cred.provider), '[]'::jsonb)
    )
      from public.warm_credentials cred
      left join public.warm_budget_ledger ledger
        on ledger.organization_id = cred.organization_id
       and ledger.provider = cred.provider
       and ledger.day = (now() at time zone 'utc')::date
      left join lateral (
        select count(*) filter (where prefix.state = 'active') as active_prefixes,
               count(distinct prefix.customer_id) as warm_customers,
               sum(prefix.warm_pings) as warm_pings,
               sum(prefix.warm_hits) as warm_hits,
               sum(prefix.warm_misses) as warm_misses,
               min(prefix.next_due_at) filter (where prefix.state = 'active')
                   as next_due_at
          from public.warm_prefixes prefix
         where prefix.organization_id = cred.organization_id
           and prefix.provider = cred.provider
      ) stats on true
     where cred.organization_id = p_organization_id;
$$;

revoke all on function public.warm_read_status(uuid)
    from public, anon, authenticated;
grant execute on function public.warm_read_status(uuid)
    to service_role;

-- Maintenance-loop retention: expired prefixes go immediately; settled ledger
-- days age out after the retention window.
create or replace function public.purge_warm_state(
    p_retention_days integer
) returns jsonb as $$
declare
    v_prefixes_deleted integer;
    v_ledger_deleted integer;
begin
    if coalesce(p_retention_days, 0) not between 1 and 365 then
        raise exception 'warm retention bounds are invalid';
    end if;
    delete from public.warm_prefixes prefix where prefix.expires_at <= now();
    get diagnostics v_prefixes_deleted = row_count;
    delete from public.warm_budget_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date - p_retention_days;
    get diagnostics v_ledger_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-purge.v1', 'status', 'purged',
        'prefixes_deleted', v_prefixes_deleted,
        'ledger_deleted', v_ledger_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.purge_warm_state(integer)
    from public, anon, authenticated;
grant execute on function public.purge_warm_state(integer)
    to service_role;

-- Self-contained privilege contract: warming state is RPC-mediated only. No
-- PostgREST role may retain any direct table privilege, and RLS must be on.
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
                '202607280001 refuses to expose warming data without RLS: public.%',
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
