-- Phase 0 warming instrumentation: a decision log and a TTL-physics sensor.
--
-- Two things the warming loop knows and then throws away.
--
-- 1. THE COUNTERFACTUAL. warm_due_claim scores every due prefix and then
--    `continue`s past the ones it will not warm (202607280003:305, :321, :338):
--    below the ROI floor, over the per-customer daily ping cap, over the daily
--    budget. Only the claimed rows are ever visible again, so every denial --
--    the exact rows a learned policy has to reason about -- is unrecoverable.
--    public.warm_decision_log records one row per candidate the claim loop
--    actually evaluates, claimed or denied, with the belief snapshot that
--    produced the verdict. Rows the candidate query filters out (stopped,
--    stop-loss, cold credential) and rows past `exit when v_claimed >=
--    p_claim_limit` are NOT evaluated and so are NOT logged: the log means
--    "what the scorer decided", not "what existed".
--
-- 2. THE TTL. Two free sensors already exist in the data and are discarded:
--    a warm ping's own receipt says whether the entry was still alive
--    (cache-read tokens) or had to be rewritten (cache-creation tokens), and
--    so does every real arrival (warm_prefix_observe's p_cache_read). Paired
--    with the gap since the prefix was last touched, each is a censored
--    observation of the provider's real TTL -- the quantity every warming
--    schedule is currently guessing with a hard-coded floor.
--    public.warm_ttl_observations persists both.
--
-- THE TWO-PLANE RULE. warm_ttl_observations carries NO tenant keys at all:
-- provider, model class, TTL tier, gap, outcome, source, timestamp. It is
-- provider physics, not customer behavior, and it is the one warming table
-- that may be aggregated across organizations. That is also why tenant erasure
-- deliberately does not touch it (documented at the delete site below) and why
-- it retains on the aggregate horizon in purge_warm_state rather than as a
-- compliance retention class -- there is no data subject to hold, export or
-- erase. warm_decision_log is the opposite: it is per-customer behavioral
-- evidence, so it is wired into tenant erasure, tenant export, customer-scoped
-- subject export and compliance_run_retention in this same migration, per the
-- 202607280016 lesson (a new warming table that no compliance function
-- enumerates is silently exempt from every data right).
--
-- WHAT DOES NOT CHANGE. No admission decision, no ordering, no reservation, no
-- settle arithmetic. The claim loop's gates are byte-identical; the only
-- reordering is that v_reserve (pure arithmetic on the candidate row) is
-- computed before the ROI gate instead of after it, so a denied row can record
-- what it would have cost. v_pings_today is deliberately NOT hoisted: it costs
-- a query per candidate, and paying it for rows the ROI gate rejects would be a
-- real production cost for a nullable column. Denials that never reached the
-- cap gate log a null there, which is honest.
--
-- NEW COLUMN. warm_prefixes.last_touch_at is the timestamp of the last event
-- that provably wrote or refreshed the provider-side cache entry: an arrival
-- (warm_prefix_observe) or a ping the provider answered 2xx (warm_ping_settle,
-- outcome 'warmed'). It is what makes gap_seconds meaningful. 'spent_unknown'
-- does NOT stamp it -- that outcome exists precisely because we do not know
-- whether the provider ever processed the ping, and a guessed touch would
-- poison the physics table. Deriving the touch from next_due_at instead was
-- rejected: 202607280018's schedule fence and the 'release' arm both leave
-- next_due_at somewhere other than last-touch + horizon.
--
-- SIGNATURE CHANGE. warm_prefix_observe gains a trailing `p_model_class text
-- default ''` -- the model class is inside the encrypted payload, so SQL
-- cannot recover it, and an arrival-sourced TTL observation without it is not
-- comparable to a ping-sourced one. The 10-argument signature is dropped and
-- replaced, exactly as 202607280003:214-216 did for warm_due_claim; PostgREST
-- callers that still send ten named arguments keep resolving, because the new
-- argument has a default. api/store.py's SQLite mirror and api/server.py's
-- _hosted_warm_observe follow in the same change.
--
-- INSTRUMENTATION MUST NOT ABORT MONEY. Every write added here is an INSERT of
-- metadata into an append-only table inside a transaction that was already
-- open. The new tables carry no foreign keys on purpose: an FK to
-- public.customers would make a concurrent tenant deletion able to block or
-- abort a warming claim -- a money path -- to protect an analytics row.
-- Tenant erasure deletes warm_decision_log explicitly instead. warm_ttl_observe
-- validates strictly and raises for its direct RPC callers, and every call site
-- on a live request path guards its arguments first so it cannot raise there.
--
-- NO CONTENT. Neither table stores prompt or response material: hashes, token
-- counts, probabilities, dollars, and provider catalog identifiers only.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_decision_log and public.warm_ttl_observations, drop public.warm_ttl_observe / public.warm_decision_settle_outcome / public.warm_ttl_tier, drop warm_prefixes.last_touch_at, drop the two compliance_retention_runs warm_decision_* columns, and re-apply 202607280018_warm_claim_lease_fence.sql's warm_prefix_observe (after dropping the 11-argument signature), 202607280003_multi_provider_warming.sql's warm_due_claim, 202608080001_warm_spent_unknown_settle.sql's warm_ping_settle, 202607280017_warm_evidence_retention_floor.sql's purge_warm_state, 202607280036_settlement_evidence_erasure_fence.sql's compliance_delete_tenant and compliance_run_retention, and 202607280016_compliance_warm_state_erasure.sql's compliance_export_tenant and compliance_export_subject verbatim

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    -- Either arity satisfies this: the 10-argument form on a first apply, the
    -- 11-argument form on a re-apply after this migration already replaced it.
    -- Naming only the 10-argument form here would make the migration fail on
    -- its second run, which is not what "forward-only and idempotent" means.
    if to_regprocedure(
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)'
    ) is null
       and to_regprocedure(
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202608090001 requires public.warm_prefix_observe';
    end if;
    foreach required_routine in array array[
        'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb)',
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)',
        'public.purge_warm_state(integer)',
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_run_retention(uuid,text,integer,boolean)',
        'public.compliance_preservation_hold(uuid)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608090001 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.compliance_retention_runs') is null then
        raise exception using
            errcode = '55000',
            message = '202608090001 requires public.warm_prefixes and public.compliance_retention_runs';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

-- One row per candidate warm_due_claim scored. Behavioral tenant evidence:
-- RLS on, zero policies, zero direct DML for every PostgREST role, reachable
-- only through the security-definer routines below.
create table if not exists public.warm_decision_log (
    id bigint generated always as identity primary key,
    organization_id uuid not null,
    customer_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    prefix_hash text not null check (prefix_hash ~ '^[0-9a-f]{64}$'),
    ts timestamptz not null default now(),
    -- 'pinged' is the claim: the worker is handed the row and will ping it.
    -- 'skipped_roi' / 'cap_denied' / 'budget_denied' are the three gates the
    -- claim loop actually applies today. 'stopped' and 'holdout' are declared
    -- now and produced by nobody in Phase 0: the stop-loss is a candidate-query
    -- filter (so a stopped row is never scored) and the (org, prefix) control
    -- arm does not exist yet. Declaring them here means the arm that adds them
    -- does not have to rewrite this constraint under load.
    decision text not null check (decision in (
        'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
        'stopped', 'holdout')),
    -- Belief snapshot as the scorer saw it.
    p_return numeric not null check (p_return between 0 and 1),
    roi_floor numeric not null check (roi_floor between 0 and 1),
    reserve_usd numeric(18,10) not null
        check (reserve_usd >= 0 and reserve_usd <= 99999999),
    prefix_tokens integer not null check (prefix_tokens between 0 and 2000000000),
    ewma_interarrival_s numeric
        check (ewma_interarrival_s is null or ewma_interarrival_s >= 0),
    arrival_count integer not null check (arrival_count >= 0),
    -- Null when the decision was made before the per-customer cap was read;
    -- see the hoisting note in this migration's header.
    pings_today integer check (pings_today is null or pings_today >= 0),
    -- Forward slots for the stochastic policy. A knapsack-coupled sampler has
    -- no arm-local propensity, so the seed is logged to permit Monte Carlo
    -- re-estimation; both stay null until such a policy exists.
    rng_seed bigint,
    propensity numeric check (propensity is null or propensity between 0 and 1),
    -- Stamped after the fact by warm_decision_settle_outcome, keyed on the
    -- claim token this row carries. Only 'pinged' rows can ever have one.
    settle_outcome text check (settle_outcome is null or settle_outcome in (
        'warmed', 'spent_unknown', 'release', 'prefix_invalid', 'auth_failed')),
    -- Reserved for the reward join (Phase 0 item 2); no producer yet.
    realized_net_usd numeric(18,10),
    -- The claim token warm_due_claim rotated onto the prefix, so settle can
    -- find this exact row. Null for denials, which never got one.
    claim_token uuid,
    constraint warm_decision_log_outcome_requires_ping
        check (settle_outcome is null or decision = 'pinged'),
    constraint warm_decision_log_token_requires_ping
        check (claim_token is null or decision = 'pinged')
);

-- Plane G. No organization, no customer, no prefix hash -- nothing that ties an
-- observation to a tenant. This is the only warming table whose rows may be
-- pooled across organizations, and the absence of tenant keys is the reason.
create table if not exists public.warm_ttl_observations (
    id bigint generated always as identity primary key,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    -- Provider catalog model family (snapshot suffix stripped by the caller),
    -- e.g. 'claude-sonnet-4-5'. Provider metadata, never customer data.
    model_class text not null default ''
        check (octet_length(model_class) <= 128),
    ttl_tier text not null check (ttl_tier in ('5m', '1h', 'auto')),
    -- Seconds since the entry was last provably touched. Bounded at 30 days so
    -- a clock jump cannot write nonsense into the physics table.
    gap_seconds numeric(18,3) not null
        check (gap_seconds >= 0 and gap_seconds <= 2592000),
    -- 'warm': the provider served this from cache. 'expired': the provider had
    -- to write the entry again, so it was gone.
    outcome text not null check (outcome in ('warm', 'expired')),
    -- 'ping': measured by a keep-alive the worker sent. 'arrival': measured by
    -- real customer traffic, at zero cost.
    source text not null check (source in ('ping', 'arrival')),
    observed_at timestamptz not null default now()
);

create index if not exists warm_decision_log_tenant_idx
    on public.warm_decision_log (organization_id, ts);
create index if not exists warm_decision_log_subject_idx
    on public.warm_decision_log (organization_id, customer_id, ts);
create index if not exists warm_decision_log_retention_idx
    on public.warm_decision_log (ts, id);
-- The settle-time update key. Partial and unique: exactly one 'pinged' row
-- carries a given claim token, so the stamp can never fan out.
create unique index if not exists warm_decision_log_claim_token_idx
    on public.warm_decision_log (claim_token) where claim_token is not null;
create index if not exists warm_ttl_observations_retention_idx
    on public.warm_ttl_observations (observed_at, id);
create index if not exists warm_ttl_observations_physics_idx
    on public.warm_ttl_observations (provider, model_class, ttl_tier, observed_at);

alter table public.warm_decision_log enable row level security;
alter table public.warm_ttl_observations enable row level security;
revoke all on table public.warm_decision_log
    from public, anon, authenticated, service_role;
revoke all on table public.warm_ttl_observations
    from public, anon, authenticated, service_role;
-- Identity columns hand out sequence privileges separately from the table.
revoke all on sequence public.warm_decision_log_id_seq
    from public, anon, authenticated, service_role;
revoke all on sequence public.warm_ttl_observations_id_seq
    from public, anon, authenticated, service_role;

-- The provable-touch clock. Null on rows that predate this migration; every
-- reader coalesces to last_seen_at, which is the best available lower bound.
alter table public.warm_prefixes
    add column if not exists last_touch_at timestamptz;

-- compliance_retention_runs is immutable per-cycle evidence (UPDATE/DELETE are
-- rejected by trigger), so the new class is added as columns, exactly as
-- 202607280023:64-92 added its own. Existing rows default to 0, the truthful
-- value for runs that predate the class.
alter table public.compliance_retention_runs
    add column if not exists warm_decision_candidates integer not null default 0,
    add column if not exists warm_decision_deleted integer not null default 0;

do $retention_run_bounds$
declare
    bounded_column text;
begin
    foreach bounded_column in array array[
        'warm_decision_candidates', 'warm_decision_deleted'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_constraint
             where conrelid = 'public.compliance_retention_runs'::regclass
               and conname = 'compliance_retention_runs_' || bounded_column || '_check'
        ) then
            execute format(
                'alter table public.compliance_retention_runs'
                || ' add constraint %I check (%I between 0 and batch_limit)',
                'compliance_retention_runs_' || bounded_column || '_check',
                bounded_column
            );
        end if;
    end loop;
end;
$retention_run_bounds$;

-- ---------------------------------------------------------------------------
-- Writers
-- ---------------------------------------------------------------------------

-- One definition of the tier so the ping path, the arrival path and the SQLite
-- mirror cannot disagree about what '5m' means. Anthropic's two cache tiers are
-- exactly 300 and 3600; a provider-managed automatic cache (deepseek, 14400) is
-- not a tier the caller chose, so it is 'auto'.
create or replace function public.warm_ttl_tier(
    p_provider_ttl_seconds integer
) returns text as $$
    select case
        when coalesce($1, 0) <= 300 then '5m'
        when $1 <= 3600 then '1h'
        else 'auto'
    end;
-- Pinned like every other routine here even though the body touches no table:
-- a SET clause blocks inlining, and this is called at most once per
-- observation, so the hardening is free.
$$ language sql immutable set search_path = pg_catalog;

-- Record one censored TTL observation. Tenant-free by construction: this
-- function has no organization or customer argument, so no caller can put one
-- in the table by mistake.
create or replace function public.warm_ttl_observe(
    p_provider text,
    p_model_class text,
    p_ttl_tier text,
    p_gap_seconds numeric,
    p_outcome text,
    p_source text
) returns jsonb as $$
begin
    -- Strict, because the worker calls this directly and a silently dropped
    -- observation is worse than a logged failure. Call sites that live on a
    -- synchronous request path guard these bounds themselves before calling,
    -- so this can never abort live traffic.
    if p_provider not in ('anthropic', 'openai', 'deepseek')
       or octet_length(coalesce(p_model_class, '')) > 128
       or coalesce(p_ttl_tier, '') not in ('5m', '1h', 'auto')
       or coalesce(p_gap_seconds, -1) not between 0 and 2592000
       or coalesce(p_outcome, '') not in ('warm', 'expired')
       or coalesce(p_source, '') not in ('ping', 'arrival') then
        raise exception 'warm ttl observation arguments are invalid';
    end if;
    insert into public.warm_ttl_observations (
        provider, model_class, ttl_tier, gap_seconds, outcome, source
    ) values (
        p_provider, coalesce(p_model_class, ''), p_ttl_tier,
        round(p_gap_seconds, 3), p_outcome, p_source
    );
    return jsonb_build_object(
        'schema', 'brevitas.warm-ttl-observation.v1', 'status', 'recorded'
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- The single writer for public.warm_decision_log. warm_due_claim calls it once
-- per verdict; keeping the insert here rather than inline four times means the
-- claim loop cannot grow four subtly different column lists, and the SQLite
-- mirror has one shape to match. Called from inside the claim's advisory-locked
-- transaction, so a failure here aborts the claim -- which is why the argument
-- checks are exactly the table's own constraints and nothing more.
create or replace function public.warm_decision_record(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_prefix_hash text,
    p_decision text,
    p_p_return numeric,
    p_roi_floor numeric,
    p_reserve_usd numeric,
    p_prefix_tokens integer,
    p_ewma_interarrival_s numeric,
    p_arrival_count integer,
    p_pings_today integer default null,
    p_claim_token uuid default null,
    p_rng_seed bigint default null,
    p_propensity numeric default null
) returns jsonb as $$
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_prefix_hash, '') !~ '^[0-9a-f]{64}$'
       or coalesce(p_decision, '') not in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout')
       or coalesce(p_p_return, -1) not between 0 and 1
       or coalesce(p_roi_floor, -1) not between 0 and 1
       or coalesce(p_reserve_usd, -1) not between 0 and 99999999
       or coalesce(p_prefix_tokens, -1) not between 0 and 2000000000
       or coalesce(p_arrival_count, -1) < 0
       or coalesce(p_ewma_interarrival_s, 0) < 0
       or coalesce(p_pings_today, 0) < 0
       or coalesce(p_propensity, 0) not between 0 and 1
       or (p_claim_token is not null and p_decision <> 'pinged') then
        raise exception 'warm decision arguments are invalid';
    end if;
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, ewma_interarrival_s,
        arrival_count, pings_today, claim_token, rng_seed, propensity
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash, p_decision,
        p_p_return, p_roi_floor, p_reserve_usd, p_prefix_tokens,
        p_ewma_interarrival_s, p_arrival_count, p_pings_today, p_claim_token,
        p_rng_seed, p_propensity
    );
    return jsonb_build_object(
        'schema', 'brevitas.warm-decision.v1', 'status', 'recorded',
        'decision', p_decision
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- Close the loop on one logged decision. The claim token is the key: it is
-- unique, it is already carried through the worker to warm_ping_settle, and it
-- cannot address a row belonging to a different claim. Settling a token with no
-- logged decision is not an error -- decisions predating this migration, or
-- pruned by retention, have none.
create or replace function public.warm_decision_settle_outcome(
    p_claim_token uuid,
    p_settle_outcome text
) returns jsonb as $$
declare
    v_updated integer := 0;
begin
    if p_claim_token is null
       or coalesce(p_settle_outcome, '') not in (
            'warmed', 'spent_unknown', 'release', 'prefix_invalid', 'auth_failed') then
        raise exception 'warm decision outcome arguments are invalid';
    end if;
    update public.warm_decision_log entry
       set settle_outcome = p_settle_outcome
     where entry.claim_token = p_claim_token;
    get diagnostics v_updated = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-decision-outcome.v1', 'status', 'recorded',
        'updated', v_updated
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_prefix_observe (202607280018:55-199) + the arrival TTL sensor and the
-- provable-touch clock. The 10-argument signature is replaced, not overloaded.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric
);

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
    p_ping_reserve_usd numeric default null,
    -- Provider catalog model family for the TTL sensor. The model itself lives
    -- inside payload_ciphertext, which SQL cannot read, so the caller supplies
    -- the coarsened class. '' means "unknown", and the observation is still
    -- recorded: the provider and TTL tier alone are useful physics.
    p_model_class text default ''
) returns jsonb as $$
declare
    v_now timestamptz := clock_timestamp();
    v_bucket_key text;
    v_customer_count integer;
    -- Pre-upsert snapshot of the provable-touch clock, read before the row is
    -- rewritten. Null on a first observation and on rows predating 202608090001.
    v_prior_touch timestamptz;
    v_gap_seconds numeric;
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

    -- Read the touch clock before the upsert overwrites it. The advisory lock
    -- taken above already serializes this organization+provider, so no other
    -- observation can move it between this read and the write.
    select prefix.last_touch_at into v_prior_touch
      from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id
       and prefix.customer_id = p_customer_id
       and prefix.provider = p_provider
       and prefix.prefix_hash = p_prefix_hash;

    insert into public.warm_prefixes as prefix (
        organization_id, customer_id, provider, prefix_hash,
        payload_ciphertext, prefix_tokens, provider_ttl_seconds,
        ping_reserve_usd, arrival_count, ewma_interarrival_s, hour_histogram,
        created_at, last_seen_at, last_touch_at, next_due_at, expires_at
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash,
        p_payload_ciphertext, p_prefix_tokens, p_provider_ttl_seconds,
        coalesce(p_ping_reserve_usd, 0),
        1, null, jsonb_build_object(v_bucket_key, 1),
        v_now, v_now, v_now,
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
        -- An arrival provably wrote or refreshed the provider-side entry --
        -- that is what "cache read" and "cache write" both mean -- so it is a
        -- touch regardless of p_cache_read.
        last_touch_at = v_now,
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

    -- The free sensor. A real arrival against a prefix we have already seen is
    -- a censored TTL observation at zero cost: p_cache_read is the provider's
    -- own verdict on whether the entry survived the gap. Guarded rather than
    -- validated-and-raised, because this runs inside a live request: a clock
    -- jump or a prefix idle past the 30-day bound skips the observation instead
    -- of failing the observation call that the response path depends on.
    if v_prior_touch is not null then
        v_gap_seconds := extract(epoch from (v_now - v_prior_touch));
        if v_gap_seconds between 0 and 2592000 then
            perform public.warm_ttl_observe(
                p_provider,
                left(coalesce(p_model_class, ''), 128),
                public.warm_ttl_tier(p_provider_ttl_seconds),
                v_gap_seconds,
                case when p_cache_read then 'warm' else 'expired' end,
                'arrival'
            );
        end if;
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-observe.v1', 'status', 'observed',
        'cache_read', p_cache_read
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_due_claim (202607280003:217-388) + the decision log. Gates unchanged.
-- ---------------------------------------------------------------------------
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
    -- The floor actually applied, materialized so the decision log can record
    -- the bar a denied candidate failed instead of leaving it to be re-derived.
    v_floor numeric;
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
        -- plpgsql locals survive the loop iteration, so a candidate denied
        -- before the cap query would otherwise log the previous candidate's
        -- count. Null means "not read yet", which is what happened.
        v_pings_today := null;
        v_token := null;

        -- ROI gate: warm only when the observed hour-of-week return frequency
        -- clears the break-even probability, with a stricter cold-start floor
        -- until enough arrivals make the histogram trustworthy.
        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);
        v_floor := case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end;
        -- Pure arithmetic on the candidate row, hoisted above the gates so a
        -- denied candidate can record what warming it would have cost. The
        -- value and its use below are unchanged: the reservation must
        -- upper-bound actual spend for the daily ceiling to hold, so reserve the
        -- larger of the observer-priced worst case and the flat caller floor.
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));
        if v_p_return < v_floor then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'skipped_roi', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
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
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'cap_denied', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
            continue;
        end if;

        insert into public.warm_budget_ledger (organization_id, provider, day)
        values (v_row.organization_id, v_row.provider, v_day)
        on conflict (organization_id, provider, day) do nothing;
        select ledger.reserved_usd, ledger.spent_usd into v_reserved, v_spent
          from public.warm_budget_ledger ledger
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;
        if v_reserved + v_spent + v_reserve > v_row.daily_budget_usd then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'budget_denied', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token);
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
        perform public.warm_decision_record(
            v_row.organization_id, v_row.customer_id, v_row.provider,
            v_row.prefix_hash, 'pinged', v_p_return, v_floor, v_reserve,
            v_row.prefix_tokens, v_row.ewma_interarrival_s,
            v_row.arrival_count, v_pings_today, v_token);
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
            'claim_token', v_token,
            -- The worker needs the pre-claim touch to turn its ping receipt
            -- into a TTL observation. Rows written before 202608090001 have no
            -- touch clock; last_seen_at is the best available lower bound and
            -- is exactly what the clock was seeded from.
            'last_touch_at', coalesce(v_row.last_touch_at, v_row.last_seen_at)
        );
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_ping_settle (202608080001:65-161) + the provable-touch clock.
-- ---------------------------------------------------------------------------
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
    v_booked numeric := 0;
begin
    if p_provider not in ('anthropic', 'openai', 'deepseek')
       or p_prefix_hash !~ '^[0-9a-f]{64}$'
       or p_budget_day is null
       or coalesce(p_reserved_usd, -1) not between 0 and 99999999
       or coalesce(p_spent_usd, -1) not between 0 and 99999999
       or p_outcome not in ('warmed', 'spent_unknown', 'release',
                            'prefix_invalid', 'auth_failed')
       or coalesce(p_provider_ttl_seconds, 0) not between 60 and 86400
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600 then
        raise exception 'warm settle arguments are invalid';
    end if;

    -- 'spent_unknown' books the reservation, not the caller's spend: the ping
    -- may have been charged and nothing priced it, so the admitted upper bound
    -- is the conservative booking.
    v_booked := case
        when p_outcome = 'warmed' then p_spent_usd
        when p_outcome = 'spent_unknown' then p_reserved_usd
        else 0 end;

    update public.warm_budget_ledger ledger
       set reserved_usd = greatest(0, ledger.reserved_usd - p_reserved_usd),
           spent_usd = ledger.spent_usd + v_booked,
           updated_at = v_now
     where ledger.organization_id = p_organization_id
       and ledger.provider = p_provider
       and ledger.day = p_budget_day;

    if p_outcome in ('warmed', 'spent_unknown') then
        update public.warm_prefixes prefix
           set warm_pings = prefix.warm_pings + 1,
               consecutive_misses = prefix.consecutive_misses + 1,
               pings_today = case when prefix.pings_today_date = v_day
                   then prefix.pings_today + 1 else 1 end,
               pings_today_date = v_day,
               next_due_at = v_now + make_interval(secs => greatest(
                   1, p_provider_ttl_seconds - p_safety_margin_seconds)),
               -- Only 'warmed' advances the provable-touch clock. That outcome
               -- means the provider answered 2xx, so the entry was written or
               -- refreshed for certain. 'spent_unknown' exists precisely
               -- because we do not know whether the provider ever processed
               -- the ping; stamping a touch there would feed a fabricated gap
               -- into warm_ttl_observations. Leaving the clock alone makes the
               -- next observation's gap conservatively long, which is the
               -- error direction that cannot invent cache lifetime.
               last_touch_at = case when p_outcome = 'warmed'
                   then v_now else prefix.last_touch_at end,
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

-- ---------------------------------------------------------------------------
-- purge_warm_state (202607280017:63-98) + the TTL observation horizon.
-- ---------------------------------------------------------------------------
create or replace function public.purge_warm_state(
    p_retention_days integer
) returns jsonb as $$
declare
    -- The financial-evidence floor. warm_budget_ledger is the only record of
    -- what a warming ping cost, and every settlement and correction revision
    -- recomputes the deduction from it, so it outlives the maintenance window by
    -- construction rather than by configuration.
    v_ledger_retention_days integer := greatest(coalesce(p_retention_days, 0), 365);
    -- Absolute ceiling on a warm prefix payload, independent of how often the
    -- prefix is re-observed. Must stay above the rolling
    -- `last_seen_at + interval '7 days'` TTL so the two do not fight.
    v_prefix_absolute_days integer := 30;
    -- Aggregate physics horizon. warm_ttl_observations has no tenant keys, so
    -- no data subject can hold, export or erase it and it has no business in
    -- compliance_run_retention's class list; it ages here, on the sweeper that
    -- already owns warming maintenance. A year is chosen to span provider TTL
    -- and pricing regime changes, which is the timescale the table exists to
    -- detect. It is deliberately NOT tied to p_retention_days: that argument is
    -- the operator's prefix-payload window, and letting a 7-day default erase
    -- the physics evidence is the same mistake 202607280017 fixed for the
    -- budget ledger.
    v_observation_retention_days integer := 365;
    v_prefixes_deleted integer;
    v_prefixes_absolute_deleted integer;
    v_ledger_deleted integer;
    v_observations_deleted integer;
begin
    if coalesce(p_retention_days, 0) not between 1 and 365 then
        raise exception 'warm retention bounds are invalid';
    end if;
    delete from public.warm_prefixes prefix where prefix.expires_at <= now();
    get diagnostics v_prefixes_deleted = row_count;
    delete from public.warm_prefixes prefix
     where prefix.created_at < now() - make_interval(days => v_prefix_absolute_days);
    get diagnostics v_prefixes_absolute_deleted = row_count;
    delete from public.warm_budget_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date - v_ledger_retention_days;
    get diagnostics v_ledger_deleted = row_count;
    delete from public.warm_ttl_observations observation
     where observation.observed_at
           < now() - make_interval(days => v_observation_retention_days);
    get diagnostics v_observations_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-purge.v1', 'status', 'purged',
        'prefixes_deleted', v_prefixes_deleted,
        'prefixes_absolute_deleted', v_prefixes_absolute_deleted,
        'ledger_retention_days', v_ledger_retention_days,
        'ledger_deleted', v_ledger_deleted,
        'observation_retention_days', v_observation_retention_days,
        'observations_deleted', v_observations_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- compliance_delete_tenant (202607280036:569-668) + warm_decision_log erasure.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns text
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_result text;
    v_identity_ids uuid[];
    v_identity_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select coalesce(
        pg_catalog.array_agg(distinct identity_id), array[]::uuid[]
    ) into v_identity_ids
      from (
        select member.user_id as identity_id
          from public.organization_members member
         where member.organization_id = p_organization_id
        union all
        select organization.billing_owner_id
          from public.organizations organization
         where organization.id = p_organization_id
           and organization.billing_owner_id is not null
      ) identities;
    select count(*) into v_billing_before
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    select count(*) into v_settlement_before
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);

    v_result := public.compliance_delete_tenant_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    -- Warming state was introduced by 202607280001, after the frozen inner
    -- body was written, so nothing in the chain deletes it. warm_credentials
    -- holds a KMS-encrypted third-party provider key plus the named consent
    -- actor and consent timestamp; it cascades only from public.organizations,
    -- and tenant deletion deliberately RENAMES that row instead of deleting it,
    -- so the cascade never fires. warm_prefixes cascades from public.customers
    -- (deleted by the inner body) and is repeated here as belt and braces.
    -- Both deletes are inside the caller's transaction, so the whole deletion
    -- still rolls back as one unit, and they re-run for an already-'completed'
    -- request because the inner body short-circuits before them.
    delete from public.warm_credentials credential
     where credential.organization_id = p_organization_id;
    delete from public.warm_prefixes prefix
     where prefix.organization_id = p_organization_id;
    -- public.warm_decision_log is per-customer behavioral evidence -- which
    -- prefixes were scored, how often they were expected to return, what was
    -- warmed and what was denied. It carries no foreign key (an FK to
    -- public.customers would let a tenant deletion block or abort a warming
    -- claim, a money path, to protect an analytics row), so nothing cascades it
    -- away and this explicit delete is the only thing that erases it.
    delete from public.warm_decision_log entry
     where entry.organization_id = p_organization_id;
    -- public.warm_ttl_observations is intentionally NOT deleted, and this is a
    -- deliberate two-plane decision rather than an oversight. Its rows are
    -- (provider, model class, TTL tier, gap, warm/expired, ping/arrival,
    -- timestamp): provider physics with no organization, no customer and no
    -- prefix hash, so there is nothing in it belonging to this data subject to
    -- erase. It ages out on the 365-day aggregate horizon in
    -- public.purge_warm_state.
    -- public.warm_budget_ledger is intentionally NOT deleted. It is content-free
    -- (organization, provider, day, reserved/spent money) and it is the operand
    -- billing_period_settlement_evidence recomputes the warm deduction from
    -- (202607280008:344-350), so erasing it would silently raise the fee ceiling
    -- for a retained period. It ages out on its own retention horizon in
    -- public.purge_warm_state.

    update public.billing_accounts account
       set checkout_session_id = null,
           updated_at = pg_catalog.clock_timestamp()
     where account.organization_id = p_organization_id;
    update public.billing_events event
       set session_id = ''
     where event.organization_id = p_organization_id;

    foreach v_identity_id in array v_identity_ids loop
        perform public.compliance_anonymize_unshared_user(v_identity_id);
    end loop;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'company financial preservation invariant failed'
            using errcode = '55000';
    end if;
    select count(*) into v_settlement_after
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and exists (select 1 from public.period_settlement_ledger settlement
                    where settlement.organization_id = usage.organization_id
                      and settlement.status <> 'void'
                      and settlement.usage_log_watermark_id >= usage.id);
    if v_settlement_after <> v_settlement_before then
        raise exception 'company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_tenant (202607280016:147-315) + warming decisions.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_export_tenant(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
begin
    return query
        select exported.record
          from public.compliance_export_tenant_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    -- A completed request is intentionally replay-empty, matching the original
    -- RPC. The private implementation changes an approved request to processing
    -- before returning its records.
    if not exists (
        select 1
          from public.data_subject_requests request
         where request.id = p_request_id
           and request.organization_id = p_organization_id
           and request.request_type = 'export'
           and request.request_scope = 'tenant'
           and request.status = 'processing'
    ) then
        return;
    end if;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
         order by event.ts, event.id;

    -- Warming state, absent from the original record set because
    -- 202607280001 landed later. Ciphertext columns
    -- (warm_credentials.credential_ciphertext, warm_prefixes.payload_ciphertext)
    -- are reported as present but NOT emitted: the portable-export envelope
    -- decoder allowlists exactly three kind/purpose pairs
    -- (scripts/dr/portable-export.py:45-54) and hard-fails on any other, so
    -- emitting them as 'encrypted_content' today would break every export
    -- artifact. Adding the two purposes there is a prerequisite for exporting
    -- the ciphertext itself; until then the subject learns the record exists,
    -- which is what the omission previously hid.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_credential',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', credential.organization_id,
                'provider', credential.provider,
                'credential_configured', true,
                'credential_ciphertext_exported', false,
                'enabled', credential.enabled,
                'consent_actor_id', credential.consent_actor_id,
                'consent_at', credential.consent_at,
                'daily_budget_usd', credential.daily_budget_usd,
                'max_warm_customers', credential.max_warm_customers,
                'max_pings_per_customer_day', credential.max_pings_per_customer_day,
                'credential_state', credential.credential_state,
                'created_at', credential.created_at,
                'updated_at', credential.updated_at
            )
        )
          from public.warm_credentials credential
         where credential.organization_id = p_organization_id
         order by credential.provider;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_prefix',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', prefix.organization_id,
                'customer_id', prefix.customer_id,
                'provider', prefix.provider,
                'prefix_hash', prefix.prefix_hash,
                'payload_ciphertext_exported', false,
                'prefix_tokens', prefix.prefix_tokens,
                'provider_ttl_seconds', prefix.provider_ttl_seconds,
                'ping_reserve_usd', prefix.ping_reserve_usd,
                'arrival_count', prefix.arrival_count,
                'ewma_interarrival_s', prefix.ewma_interarrival_s,
                'hour_histogram', prefix.hour_histogram,
                'warm_pings', prefix.warm_pings,
                'warm_hits', prefix.warm_hits,
                'warm_misses', prefix.warm_misses,
                'consecutive_misses', prefix.consecutive_misses,
                'pings_today', prefix.pings_today,
                'pings_today_date', prefix.pings_today_date,
                'state', prefix.state,
                'created_at', prefix.created_at,
                'last_seen_at', prefix.last_seen_at,
                'next_due_at', prefix.next_due_at,
                'expires_at', prefix.expires_at
            )
        )
          from public.warm_prefixes prefix
         where prefix.organization_id = p_organization_id
         order by prefix.customer_id, prefix.provider, prefix.prefix_hash;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_budget_ledger',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', ledger.organization_id,
                'provider', ledger.provider,
                'day', ledger.day,
                'reserved_usd', ledger.reserved_usd,
                'spent_usd', ledger.spent_usd,
                'updated_at', ledger.updated_at
            )
        )
          from public.warm_budget_ledger ledger
         where ledger.organization_id = p_organization_id
         order by ledger.day, ledger.provider;

    -- Warming decisions. Portable in full: every column is a hash, a count, a
    -- probability, a dollar amount or an enum -- there is no prompt or response
    -- material to withhold, so unlike the ciphertext columns above nothing here
    -- is reported-but-omitted. The volume is bounded by the 90-day retention
    -- class this migration registers in compliance_run_retention.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_decision',
            'data', pg_catalog.jsonb_build_object(
                'id', entry.id,
                'organization_id', entry.organization_id,
                'customer_id', entry.customer_id,
                'provider', entry.provider,
                'prefix_hash', entry.prefix_hash,
                'ts', entry.ts,
                'decision', entry.decision,
                'p_return', entry.p_return,
                'roi_floor', entry.roi_floor,
                'reserve_usd', entry.reserve_usd,
                'prefix_tokens', entry.prefix_tokens,
                'ewma_interarrival_s', entry.ewma_interarrival_s,
                'arrival_count', entry.arrival_count,
                'pings_today', entry.pings_today,
                'rng_seed', entry.rng_seed,
                'propensity', entry.propensity,
                'settle_outcome', entry.settle_outcome,
                'realized_net_usd', entry.realized_net_usd
            )
        )
          from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
         order by entry.ts, entry.id;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_subject (202607280016:317-447) + warming decisions.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_export_subject(
    p_organization_id uuid,
    p_request_id uuid,
    p_actor_id text
) returns setof jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, auth, pg_temp
as $function$
declare
    v_request public.data_subject_requests%rowtype;
begin
    return query
        select exported.record
          from public.compliance_export_subject_pre_company_identity(
                p_organization_id, p_request_id, p_actor_id
          ) as exported(record)
         where coalesce(exported.record->>'record_type', '')
               not in ('billing_account', 'billing_ledger', 'legacy_billing_event');

    select * into v_request
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
    if not found
       or v_request.request_type <> 'export'
       or v_request.request_scope not in ('member', 'customer')
       or v_request.status <> 'processing' then
        return;
    end if;

    -- Preserve the original member-subject semantics: billing evidence is a
    -- subject relationship only when that member is the compatibility owner,
    -- while organization_id prevents evidence from another owned company.
    -- Billing evidence stays a member-only relationship, exactly as
    -- 202607200011 defined it; the guard above now also admits a
    -- customer-scoped request, which must not reach it.
    if v_request.request_scope = 'member' then
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_account',
            'data', pg_catalog.jsonb_build_object(
                'user_id', account.user_id,
                'stripe_customer_id', account.stripe_customer_id,
                'stripe_subscription_id', account.stripe_subscription_id,
                'subscription_status', account.subscription_status,
                'checkout_session_id', account.checkout_session_id,
                'billing_started_at', account.billing_started_at,
                'current_period_start', account.current_period_start,
                'current_period_end', account.current_period_end,
                'last_invoice_id', account.last_invoice_id,
                'last_invoice_status', account.last_invoice_status,
                'stripe_subscription_event_created',
                    account.stripe_subscription_event_created,
                'stripe_invoice_event_created',
                    account.stripe_invoice_event_created,
                'created_at', account.created_at,
                'updated_at', account.updated_at
            )
        )
          from public.billing_accounts account
         where account.organization_id = p_organization_id
           and account.user_id = v_request.subject_id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'billing_ledger',
            'data', pg_catalog.jsonb_build_object(
                'id', ledger.id,
                'usage_log_id', ledger.usage_log_id,
                'user_id', ledger.user_id,
                'occurred_at', ledger.occurred_at,
                'fee_microusd', ledger.fee_microusd,
                'status', ledger.status,
                'attempts', ledger.attempts,
                'reported_at', ledger.reported_at,
                'last_error', ledger.last_error,
                'created_at', ledger.created_at
            )
        )
          from public.billing_ledger ledger
         where ledger.organization_id = p_organization_id
           and ledger.user_id = v_request.subject_id
         order by ledger.id;

    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'legacy_billing_event',
            'data', pg_catalog.to_jsonb(event)
        )
          from public.billing_events event
         where event.organization_id = p_organization_id
           and event.user_id = v_request.subject_id
         order by event.ts, event.id;
    end if;

    -- A customer-scoped subject request now also carries that customer's
    -- warming observations. warm_prefixes is keyed by (organization, customer),
    -- so the customer IS the subject here.
    if v_request.request_scope = 'customer' then
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_prefix',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', prefix.organization_id,
                    'customer_id', prefix.customer_id,
                    'provider', prefix.provider,
                    'prefix_hash', prefix.prefix_hash,
                    'payload_ciphertext_exported', false,
                    'prefix_tokens', prefix.prefix_tokens,
                    'provider_ttl_seconds', prefix.provider_ttl_seconds,
                    'arrival_count', prefix.arrival_count,
                    'ewma_interarrival_s', prefix.ewma_interarrival_s,
                    'hour_histogram', prefix.hour_histogram,
                    'warm_pings', prefix.warm_pings,
                    'warm_hits', prefix.warm_hits,
                    'warm_misses', prefix.warm_misses,
                    'state', prefix.state,
                    'created_at', prefix.created_at,
                    'last_seen_at', prefix.last_seen_at,
                    'next_due_at', prefix.next_due_at,
                    'expires_at', prefix.expires_at
                )
            )
              from public.warm_prefixes prefix
             where prefix.organization_id = p_organization_id
               and prefix.customer_id = v_request.subject_id
             order by prefix.provider, prefix.prefix_hash;

        -- warm_decision_log is keyed by (organization, customer) exactly as
        -- warm_prefixes is, so the customer is the subject here too. A prefix
        -- that has since expired leaves no warm_prefixes row but does leave
        -- decisions, which is the whole point of logging them; omitting these
        -- would repeat the 202607280016 gap on a newer table.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_decision',
                'data', pg_catalog.jsonb_build_object(
                    'id', entry.id,
                    'organization_id', entry.organization_id,
                    'customer_id', entry.customer_id,
                    'provider', entry.provider,
                    'prefix_hash', entry.prefix_hash,
                    'ts', entry.ts,
                    'decision', entry.decision,
                    'p_return', entry.p_return,
                    'roi_floor', entry.roi_floor,
                    'reserve_usd', entry.reserve_usd,
                    'prefix_tokens', entry.prefix_tokens,
                    'ewma_interarrival_s', entry.ewma_interarrival_s,
                    'arrival_count', entry.arrival_count,
                    'pings_today', entry.pings_today,
                    'rng_seed', entry.rng_seed,
                    'propensity', entry.propensity,
                    'settle_outcome', entry.settle_outcome,
                    'realized_net_usd', entry.realized_net_usd
                )
            )
              from public.warm_decision_log entry
             where entry.organization_id = p_organization_id
               and entry.customer_id = v_request.subject_id
             order by entry.ts, entry.id;
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_run_retention (202607280036:754-1057) + the warm_decision class.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_run_retention(
    p_run_id uuid,
    p_actor_id text,
    p_batch_limit integer,
    p_apply boolean
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_usage_cutoff timestamptz := clock_timestamp()-interval '13 months';
    v_support_cutoff timestamptz := clock_timestamp()-interval '24 months';
    -- Prospect contact data has never had a retention rule. 24 months from
    -- submission, matching the support-record period.
    v_waitlist_cutoff timestamptz := clock_timestamp()-interval '24 months';
    v_evidence_cutoff timestamptz := clock_timestamp()-interval '400 days';
    -- Warming decisions are behavioral telemetry, not financial evidence:
    -- nothing recomputes money from them and no settlement reads them, so they
    -- get the shortest horizon that still spans a full seasonal cycle for the
    -- replay simulator. 90 days.
    v_warm_decision_cutoff timestamptz := clock_timestamp()-interval '90 days';
    v_existing public.compliance_retention_runs%rowtype;
    v_usage_candidates integer := 0;
    v_audit_candidates integer := 0;
    v_support_candidates integer := 0;
    v_request_candidates integer := 0;
    v_hold_candidates integer := 0;
    v_prior_run_candidates integer := 0;
    v_usage_minimize_candidates integer := 0;
    v_waitlist_candidates integer := 0;
    v_warm_decision_candidates integer := 0;
    v_warm_decision_deleted integer := 0;
    v_usage_deleted integer := 0;
    v_audit_deleted integer := 0;
    v_support_deleted integer := 0;
    v_requests_deleted integer := 0;
    v_holds_deleted integer := 0;
    v_prior_run_deleted integer := 0;
    v_usage_minimized integer := 0;
    v_waitlist_deleted integer := 0;
    v_hold_ids uuid[] := array[]::uuid[];
begin
    perform public.compliance_actor_role(p_actor_id);
    if p_run_id is null or p_apply is null or p_batch_limit is null
       or p_batch_limit not between 1 and 10000 then
        raise exception 'retention batch limit must be between 1 and 10000' using errcode='22023';
    end if;
    select * into v_existing from public.compliance_retention_runs where id=p_run_id;
    if found then
        if not p_apply or v_existing.actor_id<>p_actor_id
           or v_existing.batch_limit<>p_batch_limit then
            raise exception 'retention run idempotency conflict' using errcode='23505';
        end if;
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','apply',
            'run_id',v_existing.id,'batch_limit',v_existing.batch_limit,
            'usage_candidates',v_existing.usage_candidates,
            'audit_candidates',v_existing.audit_candidates,
            'support_candidates',v_existing.support_candidates,
            'requests_candidates',v_existing.requests_candidates,
            'holds_candidates',v_existing.holds_candidates,
            'prior_run_evidence_candidates',v_existing.prior_run_evidence_candidates,
            'usage_deleted',v_existing.usage_deleted,
            'audit_deleted',v_existing.audit_deleted,
            'support_deleted',v_existing.support_deleted,
            'requests_deleted',v_existing.requests_deleted,
            'holds_deleted',v_existing.holds_deleted,
            'prior_run_evidence_deleted',v_existing.prior_run_evidence_deleted,
            'usage_minimize_candidates',v_existing.usage_minimize_candidates,
            'usage_minimized',v_existing.usage_minimized,
            'waitlist_candidates',v_existing.waitlist_candidates,
            'waitlist_deleted',v_existing.waitlist_deleted,
            'warm_decision_candidates',v_existing.warm_decision_candidates,
            'warm_decision_deleted',v_existing.warm_decision_deleted,
            'idempotent_replay',true,'evidence_contains_customer_content',false
        );
    end if;

    select count(*)::integer into v_usage_candidates from (
        select 1 from public.usage_log usage
         where usage.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=usage.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=usage.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=usage.id)
           and not public.compliance_preservation_hold(usage.organization_id)
         order by usage.ts,usage.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_audit_candidates from (
        select 1 from public.audit_events event
         where event.occurred_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(event.organization_id)
         order by event.occurred_at,event.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_request_candidates from (
        select 1 from public.data_subject_requests request
         where request.status='completed' and request.completed_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(request.organization_id)
         order by request.completed_at,request.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_hold_candidates from (
        select 1 from public.legal_holds hold
         where not hold.active and hold.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(hold.organization_id)
         order by hold.released_at,hold.id limit p_batch_limit
    ) candidate;
    -- Ledger-referenced usage rows cannot be deleted, and nothing ever
    -- minimized them: compliance_run_retention is delete-only. The deletion path
    -- already classifies these exact columns as needing removal
    -- (202607170007:2417-2423), so past the same 13-month cutoff the rows the
    -- ledger forces us to keep get the same treatment.
    select count(*)::integer into v_usage_minimize_candidates from (
        select 1 from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted')
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_waitlist_candidates from (
        select 1 from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id limit p_batch_limit
    ) candidate;
    -- Same preservation-hold fence as every other tenant class: an organization
    -- under legal hold keeps its decision log until the hold lifts.
    select count(*)::integer into v_warm_decision_candidates from (
        select 1 from public.warm_decision_log candidate
         where candidate.ts<v_warm_decision_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id limit p_batch_limit
    ) candidate;
    select count(*)::integer into v_prior_run_candidates from (
        select 1 from public.compliance_retention_runs run
         where run.completed_at<v_evidence_cutoff
           and not public.compliance_global_preservation_hold()
         order by run.completed_at,run.id limit p_batch_limit
    ) candidate;

    if to_regclass('public.support_records') is not null then
        if not exists (select 1 from information_schema.columns
                        where table_schema='public' and table_name='support_records'
                          and column_name='organization_id')
           or not exists (select 1 from information_schema.columns
                           where table_schema='public' and table_name='support_records'
                             and column_name='created_at') then
            raise exception 'support_records retention contract is unsupported' using errcode='55000';
        end if;
        execute 'select count(*)::integer from (select 1 from public.support_records support where support.created_at<$1 and not public.compliance_preservation_hold(support.organization_id) order by support.created_at,support.ctid limit $2) candidate'
          into v_support_candidates using v_support_cutoff,p_batch_limit;
    end if;

    if not p_apply then
        return jsonb_build_object(
            'schema','brevitas.compliance-retention-result.v1','mode','dry_run',
            'run_id',p_run_id,'batch_limit',p_batch_limit,
            'usage_candidates',v_usage_candidates,
            'audit_candidates',v_audit_candidates,
            'support_candidates',v_support_candidates,
            'requests_candidates',v_request_candidates,
            'holds_candidates',v_hold_candidates,
            'prior_run_evidence_candidates',v_prior_run_candidates,
            'usage_minimize_candidates',v_usage_minimize_candidates,
            'waitlist_candidates',v_waitlist_candidates,
            'warm_decision_candidates',v_warm_decision_candidates,
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
            'usage_minimized',0,'waitlist_deleted',0,'warm_decision_deleted',0,
            'idempotent_replay',false,'evidence_contains_customer_content',false
        );
    end if;

    delete from public.usage_log usage
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and not exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not exists (select 1 from public.period_settlement_ledger settlement
                            where settlement.organization_id=candidate.organization_id
                              and settlement.status<>'void'
                              and settlement.usage_log_watermark_id>=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_deleted = row_count;

    -- Minimize what the ledger forces us to retain. The preserved columns are
    -- exactly the ones the financial evidence needs -- id, organization_id, ts,
    -- authoritative, pricing_status and the token/price/savings columns, verified
    -- against billing_period_settlement_evidence (202607280008:325-363) -- plus
    -- key_hash and request_id, which are NOT cleared here even though the
    -- deletion path clears them: usage_log carries a unique index on
    -- (key_hash, request_id) where request_id<>'', so rewriting key_hash while
    -- leaving request_id in place could collide two retained rows and abort the
    -- retention run, and the seven-year financial evidence is correlated by
    -- request_id in the DR assertions. Clearing both (as tenant erasure does) is
    -- correct only when the whole tenant is going away.
    update public.usage_log usage
       set customer_id = null,
           owner_id = '', project = 'Deleted', environment = 'Deleted',
           source = 'Deleted', repo = '', client = '', agent = '',
           call_site_id = '', framework = '', gateway = '', provider = '',
           model = '', session_id = '', pipeline = '', run_id = '',
           usage_raw = ''
     where usage.id in (
        select candidate.id from public.usage_log candidate
         where candidate.ts<v_usage_cutoff
           and exists (select 1 from public.billing_ledger ledger where ledger.usage_log_id=candidate.id)
           and not public.compliance_preservation_hold(candidate.organization_id)
           -- Already-minimized rows must stop being candidates or the batch
           -- would rewrite the same rows forever and never converge.
           and (candidate.owner_id<>'' or candidate.customer_id is not null
                or candidate.usage_raw<>'' or candidate.session_id<>''
                or candidate.pipeline<>'' or candidate.run_id<>''
                or candidate.repo<>'' or candidate.client<>''
                or candidate.agent<>'' or candidate.call_site_id<>''
                or candidate.framework<>'' or candidate.gateway<>''
                or candidate.provider<>'' or candidate.model<>''
                or candidate.project<>'Deleted' or candidate.environment<>'Deleted'
                or candidate.source<>'Deleted')
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_usage_minimized = row_count;

    -- Waitlist prospects never created an account, so no data_subject_requests
    -- scope can reach them: every implemented scope requires an organization_id
    -- or a subject inside one. This is the only retention path they have.
    delete from public.waitlist entry
     where entry.id in (
        select candidate.id from public.waitlist candidate
         where candidate.created_at<v_waitlist_cutoff
         order by candidate.created_at,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_waitlist_deleted = row_count;

    delete from public.warm_decision_log entry
     where entry.id in (
        select candidate.id from public.warm_decision_log candidate
         where candidate.ts<v_warm_decision_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.ts,candidate.id
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_warm_decision_deleted = row_count;

    if to_regclass('public.support_records') is not null then
        execute 'delete from public.support_records support where support.ctid in (select candidate.ctid from public.support_records candidate where candidate.created_at<$1 and not public.compliance_preservation_hold(candidate.organization_id) order by candidate.created_at,candidate.ctid for update skip locked limit $2)'
          using v_support_cutoff,p_batch_limit;
        get diagnostics v_support_deleted = row_count;
    end if;

    select deleted.audit_deleted,deleted.requests_deleted,deleted.prior_run_evidence_deleted
      into v_audit_deleted,v_requests_deleted,v_prior_run_deleted
      from public.compliance_retention_delete_immutable(v_evidence_cutoff,p_batch_limit) deleted;
    select coalesce(array_agg(candidate.id order by candidate.released_at,candidate.id),
                    array[]::uuid[])
      into v_hold_ids
      from (
        select candidate.id,candidate.released_at from public.legal_holds candidate
         where not candidate.active and candidate.released_at<v_evidence_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.released_at,candidate.id
         for update skip locked
         limit p_batch_limit
     ) candidate;
    begin
        execute 'alter table public.legal_hold_actions disable trigger legal_hold_actions_enforce_transition';
        delete from public.legal_hold_actions hold_action
         where hold_action.target_hold_id=any(v_hold_ids);
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
    exception when others then
        execute 'alter table public.legal_hold_actions enable trigger legal_hold_actions_enforce_transition';
        raise;
    end;
    delete from public.legal_holds hold where hold.id=any(v_hold_ids);
    get diagnostics v_holds_deleted = row_count;

    insert into public.compliance_retention_runs(
        id,actor_id,batch_limit,usage_candidates,audit_candidates,support_candidates,
        requests_candidates,holds_candidates,prior_run_evidence_candidates,
        usage_deleted,audit_deleted,support_deleted,
        requests_deleted,holds_deleted,prior_run_evidence_deleted,
        usage_minimize_candidates,waitlist_candidates,
        usage_minimized,waitlist_deleted,
        warm_decision_candidates,warm_decision_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted,
        v_usage_minimize_candidates,v_waitlist_candidates,
        v_usage_minimized,v_waitlist_deleted,
        v_warm_decision_candidates,v_warm_decision_deleted
    );
    perform public.append_company_audit(
        null,p_actor_id,public.compliance_actor_role(p_actor_id),p_run_id::text,
        'compliance.retention.completed','retention_run',p_run_id::text,'committed'
    );
    return jsonb_build_object(
        'schema','brevitas.compliance-retention-result.v1','mode','apply',
        'run_id',p_run_id,'batch_limit',p_batch_limit,
        'usage_candidates',v_usage_candidates,'audit_candidates',v_audit_candidates,
        'support_candidates',v_support_candidates,'requests_candidates',v_request_candidates,
        'holds_candidates',v_hold_candidates,
        'prior_run_evidence_candidates',v_prior_run_candidates,
        'usage_deleted',v_usage_deleted,'audit_deleted',v_audit_deleted,
        'support_deleted',v_support_deleted,'requests_deleted',v_requests_deleted,
        'holds_deleted',v_holds_deleted,'prior_run_evidence_deleted',v_prior_run_deleted,
        'usage_minimize_candidates',v_usage_minimize_candidates,
        'usage_minimized',v_usage_minimized,
        'waitlist_candidates',v_waitlist_candidates,
        'waitlist_deleted',v_waitlist_deleted,
        'warm_decision_candidates',v_warm_decision_candidates,
        'warm_decision_deleted',v_warm_decision_deleted,
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges. 202607280032's contract: every migration that defines a routine
-- restates that routine's own posture rather than inheriting one.
-- ---------------------------------------------------------------------------
revoke all on function public.warm_ttl_tier(integer)
    from public, anon, authenticated;
grant execute on function public.warm_ttl_tier(integer) to service_role;
revoke all on function public.warm_ttl_observe(text, text, text, numeric, text, text)
    from public, anon, authenticated;
grant execute on function public.warm_ttl_observe(text, text, text, numeric, text, text)
    to service_role;
revoke all on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric
) from public, anon, authenticated;
grant execute on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric
) to service_role;
revoke all on function public.warm_decision_settle_outcome(uuid, text)
    from public, anon, authenticated;
grant execute on function public.warm_decision_settle_outcome(uuid, text)
    to service_role;
revoke all on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
) to service_role;
revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb
) to service_role;
revoke all on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) from public, anon, authenticated;
grant execute on function public.warm_ping_settle(
    uuid, uuid, text, text, date, numeric, numeric, text, integer, integer, uuid
) to service_role;
revoke all on function public.purge_warm_state(integer)
    from public, anon, authenticated;
grant execute on function public.purge_warm_state(integer) to service_role;
revoke all on function public.compliance_delete_tenant(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_delete_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_tenant(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_subject(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_subject(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_run_retention(uuid, text, integer, boolean)
    from public, anon, authenticated;
grant execute on function public.compliance_run_retention(uuid, text, integer, boolean)
    to service_role;

comment on table public.warm_decision_log is
    'One row per candidate the warming claim loop scored, claimed or denied, with the belief snapshot behind the verdict. Per-customer behavioral evidence: erased with the tenant, exported with the tenant and the customer, retained 90 days by compliance_run_retention.';
comment on table public.warm_ttl_observations is
    'Censored observations of provider cache TTL from warm pings and from real arrivals. Carries no tenant keys by design, so it is poolable across organizations, is not touched by tenant erasure, and retains on the aggregate 365-day horizon in purge_warm_state.';
comment on function public.warm_ttl_observe(text, text, text, numeric, text, text) is
    'Record one tenant-free TTL observation; strict about its arguments because callers on live request paths guard them first.';
comment on function public.warm_decision_settle_outcome(uuid, text) is
    'Stamp the settle outcome onto the logged decision that produced a claim token; a token with no logged decision is a no-op, not an error.';
comment on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
) is 'Proxy-side warm prefix observation; the warming schedule stays fenced on the claim lease, and each arrival also records a free TTL observation and stamps the provable-touch clock.';
comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores -- claimed and denied -- to public.warm_decision_log.';
comment on function public.purge_warm_state(integer) is
    'Warming maintenance with separate horizons: expired and absolutely-aged prefixes are removed, warm_budget_ledger is retained at least 365 days because settlement recomputes the warm deduction from it, and the tenant-free TTL observations retain on the same 365-day aggregate horizon.';
comment on function public.compliance_delete_tenant(uuid, uuid, text) is
    'Tenant erasure including cache-warming credentials, prefixes and decision log; the content-free warm budget ledger and the tenant-free TTL observations are retained and age out on their own horizons.';
comment on function public.compliance_export_tenant(uuid, uuid, text) is
    'Tenant export including cache-warming credential, prefix, budget-ledger and decision records; ciphertext columns are reported as present and excluded until the portable-export envelope decoder supports their purposes.';

commit;
