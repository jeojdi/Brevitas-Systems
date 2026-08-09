-- Phase 1 learned warming, part 2: per-customer decayed hazards, P(alive),
-- periodicity and the erasure suppression list.
--
-- THE GAP. warm_prefixes.hour_histogram is a LIFETIME count of arrivals per
-- hour-of-week, per prefix, and p_return is that count divided by the prefix's
-- lifetime arrival total. Two things follow. A customer whose traffic moved
-- last month is still scored on last month's shape forever, because nothing in
-- the histogram ever forgets. And a prefix with three arrivals carries the same
-- authority as one with three thousand, because a ratio has no sample size --
-- which is why the v1 scorer needs a hard `roi_min_arrivals` cliff to protect
-- itself from its own estimator.
--
-- THE MODEL. public.warm_customer_state holds, per (organization, customer,
-- provider), a pair of sparse hour-of-week maps: hazard_n, the exponentially
-- decayed count of arrivals in each bucket, and hazard_e, the exponentially
-- decayed EXPOSURE (hours the customer was observed at all) in each bucket.
-- Their ratio is an arrival rate in 1/hour, not a share, so a bucket nobody has
-- been exposed to is not evidence of anything. Both decay on a 14-day
-- half-life, so the model forgets at a rate an operator can state.
--
--     h_org_b = (N_b + 0.25) / (E_b + 42.0)            [1/h]
--     h_b     = (n_b + 8.0 * h_org_b) / (e_b + 8.0)    [1/h]
--     p_return_v2 = 1 - exp(-h_b * W / 3600)
--
-- The prior 0.25/42.0 is exactly one arrival per week per bucket-hour
-- (0.25/42 = 1/168), chosen because it is CONTENT-FREE: no cross-organization
-- behaviour is pooled into it, and it is the same number for every tenant on
-- the fleet. The customer shrinks toward its own ORGANIZATION's aggregate with
-- 8 pseudo-hours of weight, and the organization shrinks toward that uniform
-- prior. That is the whole hierarchy: customer -> org -> a constant. There is
-- no fleet-level pooled model here and none is planned; see the two-plane rule
-- in docs/RL_PREDICTIVE_WARMING_PLAN.md Part 0.
--
-- P(ALIVE) REPLACES THE STOP-LOSS. The v1 stop-loss is a counter of consecutive
-- misses that retires a prefix from the candidate query outright, so a prefix
-- that goes quiet for a weekend is never scored again and never recovers. The
-- BG/NBD churn posterior answers the question the counter was approximating --
-- is this session still alive at all -- continuously, from the arrival record
-- itself, and it recovers on its own the moment the customer returns. Under
-- p_hazard_v2 AND p_index_enabled TOGETHER the stop-loss PREDICATE is bypassed
-- (its counter and every writer of it are untouched) and P(alive) multiplies
-- the index instead. Both flags, because P(alive) only reaches a decision
-- through the index block: hazard_v2 is an extension of the index policy, and
-- with the index off it is inert -- retiring the counter there would drop the
-- churn stop-loss with nothing in its place.
--
-- THE CHAIN. A keep-alive is never a single purchase: warming a prefix whose
-- next arrival is four hours away commits the organization to every ping in
-- between. n_chain prices that commitment into the index, and the truncation at
-- I_max = tau * (1/f - 1) is what makes "never warm a dead session" arithmetic
-- rather than policy: past it the index is negative for every p <= 1.
--
-- ERASURE. Deletion of a data subject removes their state row AND writes a
-- public.warm_modeling_suppression row that the observer consults before it
-- writes anything, so an erased customer is never re-modeled by the next
-- arrival. That row is deliberately NOT itself deleted by a subsequent erasure
-- and has no retention horizon: it IS the memory of the deletion, and forgetting
-- it would silently re-admit the customer to the model. The shape is Twilio
-- Segment's deletion-and-suppression contract, the closest B2B2C processor
-- precedent (plan Part 0).
--
-- PROCESS GATE. Per Decision 4 of docs/RL_PREDICTIVE_WARMING_PLAN.md, the
-- behavioral-profile classification of public.warm_customer_state (hour-of-week
-- arrival mass, regime labels, and whether either is a profile under GDPR
-- Art. 4(4) / CCPA) requires JAMES'S PRIVACY SIGN-OFF BEFORE THIS MIGRATION IS
-- APPLIED TO ANY REMOTE PROJECT. It ships in the branch so the code and the
-- classification can be reviewed against each other; applying it remotely
-- before that sign-off is out of contract.
--
-- OFF BY DEFAULT. p_hazard_v2 defaults to false. At false -- or at true with
-- p_index_enabled false -- no state row changes a decision,
-- p_return is the v1 histogram ratio, the stop-loss predicate applies, and the
-- claim loop is 202608100002's byte for byte. State WRITES are unconditional
-- once this migration applies -- they change no admission decision, exactly as
-- the decision log's writes do not -- so the day the flag is turned on the model
-- already has history. At flag ON with no state row (or a suppressed customer)
-- every candidate falls back to the v1 histogram with p_alive = 1 and
-- n_chain = 0, which is Task A's flat-prior form and therefore provably v1: that
-- fallback is the cold-start story and it is an executable test
-- (tests/test_warm_hazard.py::test_hazard_v2_no_state_falls_back_flat), not a
-- comment.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_customer_state and public.warm_modeling_suppression; drop the two compliance_retention_runs warm_customer_state_* columns; drop public.warm_customer_state_touch(uuid,uuid,text,timestamptz), public.warm_customer_state_set_regime(uuid,uuid,text,text,numeric) and public.warm_customer_arrival_series(integer,integer); drop public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean) and re-apply 202608100002_warm_index_claim_ordering.sql's fourteen-argument public.warm_due_claim verbatim with its revoke/grant/comment; re-apply 202608090001_warm_instrumentation_tables.sql's eleven-argument public.warm_prefix_observe verbatim; re-apply 202608090002_warm_reward_join.sql's public.compliance_delete_tenant, public.compliance_delete_subject and public.compliance_run_retention verbatim; re-apply 202608100002_warm_index_claim_ordering.sql's public.compliance_export_tenant and public.compliance_export_subject verbatim

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    -- Either arity satisfies this: 202608100002's fourteen-argument form on a
    -- first apply, this migration's fifteen-argument form on a re-apply.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100003 requires public.warm_due_claim';
    end if;
    foreach required_routine in array array[
        -- Re-created below carrying its own text forward; carrying it forward is
        -- only meaningful if that text is what is installed.
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text)',
        'public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)',
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)',
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_delete_subject(uuid,uuid,text)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_delete_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)',
        'public.compliance_run_retention(uuid,text,integer,boolean)',
        'public.compliance_preservation_hold(uuid)',
        'public.compliance_retention_delete_immutable(timestamptz,integer)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100003 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_budget_ledger') is null
       or to_regclass('public.compliance_retention_runs') is null
       or to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '55000',
            message = '202608100003 requires the warming tables';
    end if;
    -- index_score is 202608100002's column and is enumerated by the export text
    -- this migration carries forward; without it the copy below would silently
    -- be a DIFFERENT export than the one in force.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_decision_log'::regclass
           and attribute.attname = 'index_score'
           and not attribute.attisdropped
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100003 requires 202608100002 to be applied';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- Plane B: per-(organization, customer, provider) behavioural state.
--
-- NO FOREIGN KEY to public.customers, and for two independent reasons. The
-- org-aggregate row uses the nil uuid as its customer, which no customers row
-- can ever satisfy. And an FK would let a concurrent tenant deletion block or
-- abort a warm observation -- which runs inside a live request -- to protect an
-- analytics row, the same trade 202608090001 refused for warm_decision_log.
-- Erasure is handled entirely by the compliance functions re-created below.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_customer_state (
    organization_id uuid not null,
    -- The nil uuid is the ORGANIZATION AGGREGATE row: the same decayed maps,
    -- summed over every customer of this (org, provider). It is what the
    -- customer posterior shrinks toward, and it carries no subject key, which
    -- is why subject erasure deliberately leaves it alone.
    customer_id uuid not null,
    provider text not null check (provider in ('anthropic', 'openai', 'deepseek')),
    -- HMAC-ready hook. Phase 1 writes '' and reads nothing from it. Reserved for
    -- per-organization KMS-domain HMAC keying of the customer reference (EDPB
    -- 01/2025 pseudonymization posture, plan Part 0); populating it is a Phase-2
    -- task with its own key-management review, not a column to fill in here.
    customer_key_hmac text not null default ''
        check (octet_length(customer_key_hmac) <= 128),
    -- Sparse '0'..'167' -> decayed arrival count. Hour-of-week buckets, the same
    -- (isodow-1)*24 + hour formula warm_prefixes.hour_histogram uses.
    hazard_n jsonb not null default '{}'::jsonb,
    -- Sparse '0'..'167' -> decayed exposure HOURS. The denominator: a bucket
    -- with no exposure is not evidence of a low rate, it is no evidence at all.
    hazard_e jsonb not null default '{}'::jsonb,
    -- Raw undecayed arrival count: BG/NBD's x. Deliberately NOT decayed --
    -- the churn posterior is a function of the whole observed history.
    events_total integer not null default 0 check (events_total >= 0),
    first_seen_at timestamptz not null,
    last_seen_at timestamptz not null,
    -- The decay clock. Every touch decays from here, so a row that is never
    -- touched again keeps the mass it had rather than silently aging.
    last_update_at timestamptz not null,
    regime text not null default 'unknown'
        check (regime in ('unknown', 'periodic_daily', 'periodic_weekly', 'aperiodic')),
    regime_score numeric,
    regime_updated_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (organization_id, customer_id, provider)
);

-- The erasure memory. A row here means "this subject asked to be deleted"; the
-- observer refuses to write any state for them, for as long as the tenant
-- exists. Deliberately not deleted by a later delete_subject and deliberately
-- not on any retention horizon -- both would defeat the point.
create table if not exists public.warm_modeling_suppression (
    organization_id uuid not null,
    customer_id uuid not null,
    suppressed_at timestamptz not null default now(),
    primary key (organization_id, customer_id)
);

create index if not exists warm_customer_state_tenant_idx
    on public.warm_customer_state (organization_id, provider);
create index if not exists warm_customer_state_retention_idx
    on public.warm_customer_state (last_seen_at);
create index if not exists warm_customer_state_regime_idx
    on public.warm_customer_state (regime_updated_at);

alter table public.warm_customer_state enable row level security;
alter table public.warm_modeling_suppression enable row level security;
revoke all on table public.warm_customer_state
    from public, anon, authenticated, service_role;
revoke all on table public.warm_modeling_suppression
    from public, anon, authenticated, service_role;

-- compliance_retention_runs is immutable per-cycle evidence (UPDATE/DELETE are
-- rejected by trigger), so the new class is added as columns, exactly as
-- 202608090001:244-266 added its own.
alter table public.compliance_retention_runs
    add column if not exists warm_customer_state_candidates integer not null default 0,
    add column if not exists warm_customer_state_deleted integer not null default 0;

do $retention_run_bounds$
declare
    bounded_column text;
begin
    foreach bounded_column in array array[
        'warm_customer_state_candidates', 'warm_customer_state_deleted'
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
-- The touch. One arrival, folded into the decayed maps.
--
-- Constants are LITERALS in both backends and not environment knobs: a hazard
-- posterior is only comparable across replicas if every writer decays it
-- identically, and an operator who changes a half-life mid-flight silently
-- rewrites the meaning of every stored number.
--
-- TWO DOCUMENTED APPROXIMATIONS, both in the same direction:
--   * For a gap of at most 672 hours the walk credits each hour its full
--     duration and ignores decay WITHIN the gap, overstating exposure by at
--     most 2x (the mass at the far end of a gap shorter than one half-life).
--   * For a longer gap every bucket is credited E_CAP_HOURS/168, the supremum
--     of exponentially decayed exposure spread uniformly, rather than walking
--     thousands of hours.
-- Exposure is the DENOMINATOR of the hazard, so overstating it understates the
-- rate, which understates p_return, which warms less. That is the safe
-- direction and it is why neither approximation needed a flag.
-- ---------------------------------------------------------------------------
create or replace function public.warm_customer_state_touch(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_ts timestamptz default null
) returns jsonb as $$
declare
    -- 14 days.
    c_t_half constant numeric := 1209600.0;
    -- T_HALF / (3600 * ln 2): the supremum of integral 0.5^(t/T_HALF) dt, in
    -- hours. No amount of exposure can decay to more than this.
    c_e_cap_hours constant numeric := 484.8;
    c_max_walk_hours constant numeric := 672;
    -- Sparsity floor. Entries below this are dropped rather than kept forever
    -- at eleven decimal places of nothing.
    c_sparsity constant numeric := 0.000001;
    v_ts timestamptz := coalesce(p_ts, clock_timestamp());
    v_bucket text;
    v_row public.warm_customer_state%rowtype;
    v_delta numeric;
    v_decay numeric;
    v_n jsonb;
    v_e jsonb;
    v_cursor timestamptz;
    v_next timestamptz;
    v_key text;
    v_hours numeric;
    v_index integer;
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek') then
        raise exception 'warm customer state arguments are invalid';
    end if;
    -- Erasure memory, checked here as well as at the call site: a direct RPC
    -- caller must not be able to re-admit a deleted subject to the model.
    if exists (
        select 1 from public.warm_modeling_suppression suppression
         where suppression.organization_id = p_organization_id
           and suppression.customer_id = p_customer_id
    ) then
        return jsonb_build_object(
            'schema', 'brevitas.warm-customer-state.v1', 'status', 'suppressed');
    end if;

    v_bucket := ((extract(isodow from (v_ts at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (v_ts at time zone 'utc'))::integer)::text;

    select * into v_row
      from public.warm_customer_state state
     where state.organization_id = p_organization_id
       and state.customer_id = p_customer_id
       and state.provider = p_provider
     for update;
    if not found then
        -- A first arrival has no exposure history: hazard_e stays empty, so the
        -- shrinkage prior (not this row) carries the whole estimate until the
        -- second observation gives the walk something to accrue over.
        insert into public.warm_customer_state (
            organization_id, customer_id, provider, hazard_n, hazard_e,
            events_total, first_seen_at, last_seen_at, last_update_at,
            created_at, updated_at
        ) values (
            p_organization_id, p_customer_id, p_provider,
            jsonb_build_object(v_bucket, 1), '{}'::jsonb,
            1, v_ts, v_ts, v_ts, v_ts, v_ts
        )
        on conflict (organization_id, customer_id, provider) do nothing;
        return jsonb_build_object(
            'schema', 'brevitas.warm-customer-state.v1', 'status', 'created');
    end if;

    -- A clock that went backwards decays nothing rather than amplifying.
    v_delta := greatest(0, extract(epoch from (v_ts - v_row.last_update_at)));
    v_decay := power(0.5, v_delta / c_t_half);

    select coalesce(pg_catalog.jsonb_object_agg(
               entry.key, round(entry.value::numeric * v_decay, 12)), '{}'::jsonb)
      into v_n
      from jsonb_each_text(v_row.hazard_n) entry
     where entry.value::numeric * v_decay >= c_sparsity;
    select coalesce(pg_catalog.jsonb_object_agg(
               entry.key, round(entry.value::numeric * v_decay, 12)), '{}'::jsonb)
      into v_e
      from jsonb_each_text(v_row.hazard_e) entry
     where entry.value::numeric * v_decay >= c_sparsity;

    if v_delta / 3600.0 > c_max_walk_hours then
        for v_index in 0..167 loop
            v_key := v_index::text;
            v_e := jsonb_set(v_e, array[v_key], to_jsonb(round(
                coalesce((v_e ->> v_key)::numeric, 0) + c_e_cap_hours / 168.0, 12)));
        end loop;
    else
        v_cursor := v_row.last_update_at;
        while v_cursor < v_ts loop
            v_next := least(
                v_ts,
                (date_trunc('hour', v_cursor at time zone 'utc') at time zone 'utc')
                    + interval '1 hour');
            v_hours := extract(epoch from (v_next - v_cursor)) / 3600.0;
            v_key := ((extract(isodow from (v_cursor at time zone 'utc'))::integer - 1) * 24
                      + extract(hour from (v_cursor at time zone 'utc'))::integer)::text;
            v_e := jsonb_set(v_e, array[v_key], to_jsonb(round(
                coalesce((v_e ->> v_key)::numeric, 0) + v_hours, 12)));
            v_cursor := v_next;
        end loop;
    end if;

    v_n := jsonb_set(v_n, array[v_bucket], to_jsonb(round(
        coalesce((v_n ->> v_bucket)::numeric, 0) + 1, 12)));

    update public.warm_customer_state state
       set hazard_n = v_n,
           hazard_e = v_e,
           events_total = state.events_total + 1,
           last_seen_at = v_ts,
           last_update_at = v_ts,
           updated_at = v_ts
     where state.organization_id = p_organization_id
       and state.customer_id = p_customer_id
       and state.provider = p_provider;
    return jsonb_build_object(
        'schema', 'brevitas.warm-customer-state.v1', 'status', 'touched');
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- The regime label writer. UPDATE only: a classification with no state row
-- behind it is a label for a customer the model has never seen.
create or replace function public.warm_customer_state_set_regime(
    p_organization_id uuid,
    p_customer_id uuid,
    p_provider text,
    p_regime text,
    p_regime_score numeric default null
) returns jsonb as $$
declare
    v_updated integer;
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_regime, '') not in (
            'unknown', 'periodic_daily', 'periodic_weekly', 'aperiodic') then
        raise exception 'warm customer regime arguments are invalid';
    end if;
    update public.warm_customer_state state
       set regime = p_regime,
           regime_score = p_regime_score,
           regime_updated_at = clock_timestamp(),
           updated_at = clock_timestamp()
     where state.organization_id = p_organization_id
       and state.customer_id = p_customer_id
       and state.provider = p_provider;
    get diagnostics v_updated = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-customer-regime.v1',
        'status', case when v_updated > 0 then 'recorded' else 'missing' end);
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- The regime job's read side: hourly arrival counts over the lookback window
-- for the state rows whose label is stale. Counts come from AUTHORITATIVE
-- usage_log rows that are not themselves keep-alives -- the same predicate the
-- reward join uses (202608090002) -- so a warming ping can never be mistaken
-- for the customer traffic it was meant to anticipate.
--
-- The organization-aggregate row is excluded: the nil uuid matches no customer,
-- and a regime label on an aggregate is not a thing the scheduler could use.
create or replace function public.warm_customer_arrival_series(
    p_lookback_days integer default 28,
    p_limit integer default 500
) returns setof jsonb as $$
declare
    v_days integer := least(greatest(coalesce(p_lookback_days, 28), 1), 90);
    v_limit integer := least(greatest(coalesce(p_limit, 500), 1), 10000);
    v_hours integer;
    v_start timestamptz;
    v_state record;
    v_counts integer[];
    v_slot record;
begin
    v_hours := v_days * 24;
    -- Aligned to the hour so slot boundaries are stable across cycles.
    v_start := (date_trunc('hour', clock_timestamp() at time zone 'utc')
                at time zone 'utc') - make_interval(hours => v_hours - 1);
    for v_state in
        select state.organization_id, state.customer_id, state.provider
          from public.warm_customer_state state
         where state.customer_id <> '00000000-0000-0000-0000-000000000000'::uuid
           and (state.regime_updated_at is null
                or state.regime_updated_at < clock_timestamp() - interval '7 days')
           and not exists (
                select 1 from public.warm_modeling_suppression suppression
                 where suppression.organization_id = state.organization_id
                   and suppression.customer_id = state.customer_id)
         order by state.regime_updated_at nulls first,
                  state.organization_id, state.customer_id, state.provider
         limit v_limit
    loop
        v_counts := array_fill(0, array[v_hours]);
        for v_slot in
            select floor(extract(epoch from (usage.ts - v_start)) / 3600.0)::integer as slot,
                   count(*)::integer as arrivals
              from public.usage_log usage
             where usage.organization_id = v_state.organization_id
               and usage.customer_id = v_state.customer_id
               and usage.provider = v_state.provider
               and usage.strategy <> 'cache_warm'
               and usage.authoritative
               and usage.ts >= v_start
             group by 1
        loop
            if v_slot.slot between 0 and v_hours - 1 then
                v_counts[v_slot.slot + 1] := v_slot.arrivals;
            end if;
        end loop;
        return next jsonb_build_object(
            'organization_id', v_state.organization_id,
            'customer_id', v_state.customer_id,
            'provider', v_state.provider,
            'counts', to_jsonb(v_counts));
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_prefix_observe (202608090001:422-609) + the Plane B state touch. The
-- eleven-argument signature is unchanged, so this is a plain create-or-replace
-- and no caller moves.
-- ---------------------------------------------------------------------------
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

    -- PLANE B STATE (202608100003), written UNCONDITIONALLY -- there is no flag
    -- on this block and that is deliberate. It changes no admission decision:
    -- nothing reads warm_customer_state unless p_hazard_v2 is passed to the
    -- claim, so these writes are pure instrumentation in exactly the sense
    -- 202608090001's decision-log writes are. The flag day is not the day the
    -- model starts learning; it is the day the model starts being read, and a
    -- posterior with a 14-day half-life needs the history to already exist.
    --
    -- The suppression check fences BOTH touches on the REAL customer: an erased
    -- subject must not contribute to the organization aggregate either, or the
    -- aggregate becomes the place their behaviour survives erasure. The
    -- aggregate's existing mass from an erased subject is not retroactively
    -- removed -- it is not attributable to anyone and fades on the 14-day
    -- half-life -- which is the same accounting the plan's two-plane rule
    -- applies to any organization-level aggregate.
    if not exists (
        select 1 from public.warm_modeling_suppression suppression
         where suppression.organization_id = p_organization_id
           and suppression.customer_id = p_customer_id
    ) then
        perform public.warm_customer_state_touch(
            p_organization_id, p_customer_id, p_provider, v_now);
        perform public.warm_customer_state_touch(
            p_organization_id,
            '00000000-0000-0000-0000-000000000000'::uuid, p_provider, v_now);
    end if;

    return jsonb_build_object(
        'schema', 'brevitas.warm-observe.v1', 'status', 'observed',
        'cache_read', p_cache_read
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- warm_due_claim (202608100002:321-755) + the hazard read side. Every existing
-- gate, reservation, claim token, the lambda dual and the holdout draw are
-- carried forward unchanged and in the same order; p_hazard_v2 adds no gate at
-- all -- it changes what p_return MEANS, supplies p_alive and n_chain to the
-- index Task A already computes, and bypasses the stop-loss predicate. The
-- fourteen-argument signature is replaced, not overloaded.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer,
    integer, jsonb, double precision, boolean, numeric, numeric
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
    p_roi_break_even_by_provider jsonb default null,
    p_holdout_fraction double precision default 0,
    -- false (and null, from a caller that predates Phase 1) means no index is
    -- computed, no lambda row is touched, the ordering key is all-NULL and this
    -- function behaves exactly as 202608100001's did.
    p_index_enabled boolean default false,
    -- Pacing gain and ceiling. Only read when p_index_enabled.
    p_lambda_eta numeric default 0.2,
    p_lambda_max numeric default 1000,
    -- false (and null, from a caller that predates 202608100003) means the
    -- hazard model is not read at all: p_return is the lifetime histogram
    -- ratio, the stop-loss predicate applies, and this function behaves exactly
    -- as 202608100002's did.
    p_hazard_v2 boolean default false
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
    v_floor numeric;
    v_reserve numeric;
    v_reserved numeric;
    v_spent numeric;
    v_token uuid;
    v_holdout_digest bytea;
    v_holdout_bucket bigint;
    -- Nulls coalesced once, so the formula below never has to repeat the
    -- default and the two spellings cannot drift apart.
    v_eta numeric := coalesce(p_lambda_eta, 0.2);
    v_lambda_ceiling numeric := coalesce(p_lambda_max, 1000);
    -- Index belief snapshot for the candidate in hand. All null when the
    -- machinery is off; reset every iteration because plpgsql locals survive it.
    v_index numeric;
    v_index_density numeric;
    -- Index economics (202608100002). Derived per candidate from
    -- warm_prefixes.ping_reserve_usd, never from the floored
    -- ledger reservation.
    v_write_mult numeric;
    v_read_fraction numeric;
    v_price_base numeric;
    v_v_hit numeric;
    v_chain_cost numeric;
    v_c_belief numeric;
    v_p_alive numeric;
    v_organic numeric;
    v_p_eff numeric;
    v_n_chain integer;
    -- The pacing dual, per (org, provider), updated at most once per
    -- invocation. v_lambda is the value in force for the candidate in hand.
    v_lambda numeric;
    v_lambda_by_pair jsonb := '{}'::jsonb;
    v_pair_key text;
    v_lambda_old numeric;
    v_lambda_spend numeric;
    v_lambda_u numeric;
    v_lambda_exponent numeric;
    -- HAZARD READ SIDE (202608100003). All null/zero unless p_hazard_v2 found a
    -- state row for this candidate's customer, which is what makes the
    -- no-state fallback identical to 202608100002's flat priors.
    v_state public.warm_customer_state%rowtype;
    v_org_state public.warm_customer_state%rowtype;
    v_hazard_found boolean;
    v_h_org numeric;
    v_h numeric;
    v_hazard_p_alive numeric;
    v_hazard_chain integer;
    v_bg_x numeric;
    v_bg_tx numeric;
    v_bg_t numeric;
    v_bg_z numeric;
    v_chain_age numeric;
    v_chain_gap numeric;
    v_chain_tau numeric;
    v_chain_f numeric;
    v_chain_imax numeric;
begin
    if coalesce(p_claim_limit, 0) not between 1 and 500
       or coalesce(p_reserve_usd_per_mtok, -1) not between 0 and 1000
       or coalesce(p_roi_min_arrivals, 0) not between 1 and 1000
       or coalesce(p_roi_min_p, -1) not between 0 and 1
       or coalesce(p_roi_break_even_p, -1) not between 0 and 1
       or coalesce(p_stop_loss, 0) not between 1 and 100
       or coalesce(p_max_gap_seconds, 0) not between 1 and 604800
       or coalesce(p_safety_margin_seconds, -1) not between 0 and 3600
       or coalesce(p_claim_lease_seconds, 0) not between 60 and 7200
       or coalesce(p_holdout_fraction, 0) not between 0 and 1
       or v_eta not between 0.01 and 2
       or v_lambda_ceiling not between 0 and 1000000 then
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

    -- CANDIDATE WINDOW. The predicate is 202608090001's, unchanged; only the
    -- order within the window moves. The key is THE INDEX ITSELF -- the same
    -- arithmetic the loop performs below (with n_chain = 0, p_alive = 1 and
    -- organic = 1, which is all this migration can produce), evaluated inline
    -- so the window is index-selected rather than FIFO-selected -- a window
    -- ordered by next_due_at and then re-sorted would still be the soonest-due
    -- claim_limit*4 rows, which is the behaviour this replaces.
    --
    -- NOT index per reserved dollar. The index is ALREADY value per
    -- belief-dollar (it divides by c_belief by construction), so dividing it a
    -- second time by the reservation makes the key value-per-dollar-squared:
    -- under a binding budget that systematically prefers small cheap arms over
    -- the large valuable ones the budget exists to allocate. The reservation
    -- enters the claim exactly once, at the budget gate, where it belongs.
    --
    -- The CASE short-circuits: at p_index_enabled = false nothing is computed,
    -- the key is NULL for every row, and the effective order is
    -- (next_due_at, prefix_hash). prefix_hash is the documented tiebreak -- ties
    -- on next_due_at were previously broken by whatever the plan produced.
    --
    -- 202608100003: this expression stays the LIFETIME-HISTOGRAM index even
    -- under p_hazard_v2, and deliberately. The window is a recall heuristic
    -- over claim_limit*4 rows; the loop below rescores every row it visits with
    -- the hazard model and that rescore is what gates. Pulling the hazard maps
    -- into the ORDER BY would mean joining warm_customer_state twice and
    -- evaluating two jsonb lookups per candidate row inside a sort key, to
    -- reorder a window whose membership the flag already widened. Phase 2 can
    -- revisit it with the state row joined once.
    for v_row in
        select candidate.*
          from (
            select prefix.*, cred.credential_ciphertext, cred.daily_budget_usd,
                   cred.max_pings_per_customer_day,
                   case when p_index_enabled then
                       (case
                            when coalesce(
                                (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                p_roi_break_even_p) <= 1e-9
                            then 1000000::numeric
                            else least(1000000, greatest(-1000000,
                                least(1, coalesce(
                                    (prefix.hour_histogram ->> v_bucket_key)::numeric, 0)
                                    / greatest(prefix.arrival_count, 1))
                                / coalesce(
                                    (p_roi_break_even_by_provider ->> prefix.provider)::numeric,
                                    p_roi_break_even_p)
                                - 1))
                        end)
                   end as sort_index
              from public.warm_prefixes prefix
              join public.warm_credentials cred
                on cred.organization_id = prefix.organization_id
               and cred.provider = prefix.provider
             where prefix.state = 'active'
               and prefix.next_due_at <= v_now
               and prefix.expires_at > v_now
               -- STOP-LOSS RETIREMENT (202608100003). The counter and every
               -- writer of it (warm_ping_settle, warm_prefix_observe) are
               -- untouched; only this gate is bypassed under the flag, because
               -- P(alive) answers the same question continuously and recovers
               -- when the customer returns, which a monotone counter cannot.
               --
               -- p_index_enabled IS PART OF THE PREDICATE, not decoration.
               -- P(alive) -- the thing that replaces the counter -- only ever
               -- reaches a decision through the index block, which runs under
               -- p_index_enabled. With the hazard flag on and the index flag
               -- off, retiring the counter here would remove the churn
               -- stop-loss and put NOTHING in its place: a dead prefix would be
               -- re-claimed forever. hazard_v2 is an EXTENSION of the index
               -- policy and is inert without it. At p_hazard_v2 = false, or at
               -- p_index_enabled = false, the predicate is 202608100002's,
               -- exactly.
               and ((p_hazard_v2 and p_index_enabled)
                    or prefix.consecutive_misses < p_stop_loss)
               and coalesce(prefix.ewma_interarrival_s <= p_max_gap_seconds, true)
               and cred.enabled
               and cred.credential_state = 'active'
          ) candidate
         order by candidate.sort_index desc nulls last,
                  candidate.next_due_at asc,
                  candidate.prefix_hash asc
         limit p_claim_limit * 4
    loop
        exit when v_claimed >= p_claim_limit;
        v_pings_today := null;
        v_token := null;
        -- Every index local is reset, not carried: a candidate that exits
        -- before the lambda block would otherwise log the previous candidate's
        -- dual.
        v_index := null;
        v_index_density := null;
        v_v_hit := null;
        v_chain_cost := null;
        v_c_belief := null;
        v_p_alive := null;
        v_organic := null;
        v_lambda := null;

        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);

        -- THE HAZARD MODEL (202608100003). Read only under the flag, and only
        -- when this customer HAS a state row that is not suppressed. With no
        -- row -- a new customer, or one erased and suppressed -- every value
        -- below keeps its 202608100002 default: the lifetime histogram
        -- p_return, p_alive = 1 and n_chain = 0, which is the flat-prior form
        -- Task A proved equal to v1. That fallback IS the cold-start story, and
        -- it is why turning this flag on cannot change a decision for a
        -- customer the model has never seen.
        v_hazard_found := false;
        v_hazard_p_alive := null;
        v_hazard_chain := 0;
        if p_hazard_v2 then
            select * into v_state
              from public.warm_customer_state state
             where state.organization_id = v_row.organization_id
               and state.customer_id = v_row.customer_id
               and state.provider = v_row.provider;
            v_hazard_found := found;
            if v_hazard_found and exists (
                select 1 from public.warm_modeling_suppression suppression
                 where suppression.organization_id = v_row.organization_id
                   and suppression.customer_id = v_row.customer_id
            ) then
                -- An erased subject is scored as if the model had never seen
                -- them, which is the same treatment a brand-new customer gets.
                v_hazard_found := false;
            end if;
        end if;
        if v_hazard_found then
            -- Absent aggregate row: SELECT INTO leaves every field null and the
            -- coalesces below read it as zero mass, so the org term degenerates
            -- to the uniform prior rather than failing.
            select * into v_org_state
              from public.warm_customer_state state
             where state.organization_id = v_row.organization_id
               and state.customer_id = '00000000-0000-0000-0000-000000000000'::uuid
               and state.provider = v_row.provider;
            -- 0.25 / 42.0 = 1/168: one arrival per week per bucket-hour. This
            -- is the "global" level of the customer -> org -> global shrinkage
            -- and it is a CONSTANT on purpose -- content-free, identical for
            -- every tenant, so no cross-organization behaviour is ever pooled.
            v_h_org := (coalesce((v_org_state.hazard_n ->> v_bucket_key)::numeric, 0) + 0.25)
                     / (coalesce((v_org_state.hazard_e ->> v_bucket_key)::numeric, 0) + 42.0);
            -- 8 pseudo-exposure-hours of organization-prior weight: a customer
            -- with a full week of exposure in this bucket dominates its own
            -- estimate, one with an hour of it does not.
            v_h := (coalesce((v_state.hazard_n ->> v_bucket_key)::numeric, 0) + 8.0 * v_h_org)
                 / (coalesce((v_state.hazard_e ->> v_bucket_key)::numeric, 0) + 8.0);
            -- Single-bucket approximation: a TTL window spanning more than one
            -- hour-of-week bucket is priced entirely at the bucket it starts
            -- in. Phase-2 integrates the rate across the window. The exponent
            -- clamp only keeps exp() inside numeric's domain -- at -50 the
            -- value is 1 to twenty digits.
            --
            -- This value REPLACES p_return everywhere below: in the v1 ROI gate
            -- and in the index alike, and warm_decision_log.p_return records
            -- what was actually used, so a row is never scored on one number
            -- and logged with another.
            v_p_return := least(1, greatest(0, 1 - exp(greatest(-50,
                -v_h * v_row.provider_ttl_seconds / 3600.0))));

            -- P(ALIVE), BG/NBD with fixed hyperparameters r = 0.5,
            -- alpha = 7 days, a = 1, b = 2.5. x is the raw undecayed arrival
            -- count, t_x the age of the last arrival relative to the first, T
            -- the age of the record. The ratio ((alpha + T)/(alpha + t_x))
            -- raised to (r + x) is the likelihood that a customer this active
            -- would have gone this quiet by chance; the more arrivals, the less
            -- forgiving the silence.
            v_bg_x := coalesce(v_state.events_total, 0);
            v_bg_tx := greatest(0, extract(epoch from
                (v_state.last_seen_at - v_state.first_seen_at)) / 86400.0);
            v_bg_t := greatest(v_bg_tx, extract(epoch from
                (v_now - v_state.first_seen_at)) / 86400.0);
            v_bg_z := ln(1.0 / (2.5 + greatest(v_bg_x - 1, 0)))
                + least(50, 0.5 + v_bg_x) * ln((7.0 + v_bg_t) / (7.0 + v_bg_tx));
            v_hazard_p_alive := greatest(0.01, least(1.0,
                1.0 / (1.0 + exp(least(50, greatest(-50, v_bg_z))))));

            -- THE CHAIN. A keep-alive now commits the organization to every
            -- further keep-alive before the expected next arrival, so the index
            -- must price the whole chain, not this one ping.
            --
            --   E_gap = expected seconds to the next arrival, net of how long
            --           this session has already been idle
            --   tau   = the keep-alive period (TTL less the safety margin)
            --   f     = the provider read-cost fraction, recovered from the
            --           break-even b = f/(1-f) as f = b/(1+b)
            --   I_max = tau * (1/f - 1): the longest gap a chain can bridge
            --           before its cost exceeds everything a hit could save
            --
            -- The truncation at I_max is what makes "never warm a dead session"
            -- ARITHMETIC rather than policy: at E_gap >= I_max the chain alone
            -- drives index = p_eff/b - 1 - n_chain below zero for every p <= 1.
            v_chain_age := greatest(0, extract(epoch from
                (v_now - v_state.last_seen_at)));
            v_chain_gap := least(2592000, greatest(0,
                3600.0 / greatest(v_h, 0.000001) - v_chain_age));
            v_chain_tau := greatest(1,
                v_row.provider_ttl_seconds - p_safety_margin_seconds);
            v_chain_f := v_break_even / (1 + v_break_even);
            v_chain_imax := v_chain_tau
                * greatest(0, 1.0 / greatest(v_chain_f, 0.000000001) - 1);
            v_hazard_chain := greatest(0,
                ceil(least(v_chain_gap, v_chain_imax) / v_chain_tau)::integer - 1);
        end if;

        v_floor := case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end;
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));

        -- THE INDEX, computed above the first gate so that even a candidate the
        -- ROI floor rejects records what the index thought of it.
        --
        -- p_alive: P(the customer session is still alive). No producer here.
        --
        -- organic_multiplier: the incremental-probability netting multiplier
        -- P(warm|act) - P(warm|skip) normalized against P(return). Until the
        -- (org, prefix) control arm produces per-(provider, hour) organic
        -- baselines there is no unbiased estimator for it, so it is pinned to
        -- 1.0 (no netting) and logged, so the day it becomes real is visible in
        -- the data. Pinning it at 1.0 can only OVERSTATE the index, and the
        -- index gates spending, not billing -- billing continues to flow
        -- exclusively through verified_savings/settlement, untouched.
        --
        -- n_chain: the count of future keep-alives this ping commits the org to
        -- before the session's expected next arrival. 0 here; its producer
        -- arrives with the hazard model, under its own flag.
        if p_index_enabled then
            -- 202608100003: the hazard model fills the p_alive and n_chain
            -- slots this block declared. Coalesced, not branched: with the
            -- hazard flag off, or on with no state row, they are exactly the
            -- 1.0 and 0 Task A pinned, so the index is unchanged and a non-null
            -- p_alive < 1 or a non-zero chain_cost_usd in the log is the mark
            -- of a v2-scored row.
            v_p_alive := coalesce(v_hazard_p_alive, 1.0);
            v_organic := 1.0;
            v_n_chain := coalesce(v_hazard_chain, 0);
            v_p_eff := v_p_return * v_p_alive * v_organic;
            -- THE DOLLARS. ONE definition, and it is not the ledger's.
            -- warm_prefixes.ping_reserve_usd is the observer-priced TRUE worst
            -- case for one keep-alive: api/server.py prices it per row against
            -- the provider catalog as a full cache write at the model+TTL
            -- premium. Dividing it by that same write multiplier recovers the
            -- prefix's base input dollars, and every index component is priced
            -- from there:
            --
            --   price_base = ping_reserve_usd / w
            --   v_hit      = price_base * (1 - f)   -- what a return saves
            --   c_belief   = price_base * f         -- what the keep-alive costs
            --   chain      = n_chain * c_belief
            --   index      = (p_eff*v_hit - chain - c_belief) / c_belief
            --              = p_eff/b - 1 - n_chain
            --
            -- w is the provider write multiplier, mirroring the ping_rate CASE
            -- in api/server.py that produced ping_reserve_usd: anthropic on the
            -- 1h tier 2.0, anthropic on 5m 1.25, automatic-cache providers
            -- (deepseek) 1.0. f is api/worker.py WARM_PROVIDER_SPECS'
            -- read_cost_fraction, recovered from the SAME per-provider
            -- break-even this claim already gates on -- the worker derives
            -- b = f/(1-f), which inverts to f = b/(1+b) exactly -- so the
            -- dollars and the gate cannot drift, and v_hit/c_belief =
            -- (1-f)/f = 1/b keeps the index above algebraically identical to
            -- p_eff/b - 1 - n_chain.
            --
            -- v_reserve (the LEDGER reservation, floored by the
            -- anthropic-calibrated p_reserve_usd_per_mtok) deliberately does
            -- NOT appear here. That floor is money safety for the daily
            -- ceiling, not economics: folding it into the index over-prices
            -- DeepSeek roughly 14x and would rank providers by how badly the
            -- flat floor missed them. It is still logged on every row as
            -- warm_decision_log.reserve_usd, so the pessimistic full-write
            -- dollars and the expected keep-alive dollars are BOTH auditable
            -- and neither is mislabelled as the other.
            --
            -- b <= 0 would mean a provider whose reads are free: every ping
            -- pays for itself and the ratio is unbounded. Clamped rather than
            -- divided, and no dollar component is derivable, so all three stay
            -- NULL rather than being invented.
            if v_break_even <= 1e-9 then
                v_index := 1000000;
                v_v_hit := null;
                v_c_belief := null;
                v_chain_cost := null;
            else
                v_read_fraction := v_break_even / (1 + v_break_even);
                v_write_mult := case
                    when v_row.provider = 'anthropic'
                         and v_row.provider_ttl_seconds > 300 then 2.0
                    when v_row.provider = 'anthropic' then 1.25
                    else 1.0 end;
                v_price_base := coalesce(v_row.ping_reserve_usd, 0)
                                / v_write_mult;
                v_v_hit := least(99999999,
                    round(v_price_base * (1 - v_read_fraction), 10));
                v_c_belief := round(v_price_base * v_read_fraction, 10);
                v_chain_cost := round(v_n_chain * v_c_belief, 10);
                -- Written in ratio form, which is the identical number: an
                -- unpriced model carries ping_reserve_usd = 0, and a row with
                -- no dollars must still rank by its beliefs rather than divide
                -- zero by zero.
                v_index := least(1000000, greatest(-1000000,
                    v_p_eff / v_break_even - 1 - v_n_chain));
            end if;
            -- DIAGNOSTIC ONLY, and NOT the ordering key. See the candidate
            -- window above: index_score is already value per belief-dollar, so
            -- ordering by index/reserve prefers cheap arms over valuable ones
            -- under a binding budget. Kept in the log so the two rankings stay
            -- comparable in the data.
            v_index_density := v_index / greatest(v_reserve, 1e-10);
        end if;

        -- ROI gate, verbatim from 202608090001 and evaluated FIRST. At the
        -- default configuration roi_min_p (0.35) >= b for every spec'd
        -- provider, so at flat priors and lambda = 0 the lambda gate can never
        -- deny a candidate this gate admitted: p >= floor >= b implies
        -- index >= 0 = lambda. An operator who configures roi_min_p < b makes
        -- the lambda gate the stricter -- and economically correct -- one, and
        -- the flag-on/flag-off equivalence intentionally does not cover that
        -- misconfiguration.
        if v_p_return < v_floor then
            -- lambda is logged NULL here on purpose: the pacing block below has
            -- not run for this (org, provider) yet, so no dual was in force.
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'skipped_roi', v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, null);
            continue;
        end if;

        -- THE PACING DUAL. lambda lives in index units, per
        -- (org, provider, UTC day), and is updated at most once per pair per
        -- invocation -- at the first candidate of that pair to reach this gate.
        -- Recomputing it per candidate would compound the same intraday reading
        -- once per row and turn a pacing signal into a step function.
        --
        --   lambda' = min(cap, max(0, (1 + lambda) * exp(eta*(S - B*u)/max(B,0.01)) - 1))
        --
        -- with S the day's reserved+spent, B the daily budget, u the fraction
        -- of the UTC day elapsed. Multiplicative in (1 + lambda) space so
        -- lambda can rise from exactly 0; on pace or under pace (S <= B*u) the
        -- factor is <= 1 and the max(0, .) floor holds it at 0, which IS the
        -- provider break-even floor (index = 0 <=> p_eff = f/(1-f)). Over pace
        -- raises it until projected end-of-day spend is about the budget.
        --
        -- B*u is a LINEAR intraday target: the Phase-1 instantiation. Pacing
        -- against the remaining forecast hazard mass is plan section 4.4 and is
        -- deliberately out of scope here.
        if p_index_enabled then
            v_pair_key := v_row.organization_id || ':' || v_row.provider;
            if v_lambda_by_pair ? v_pair_key then
                v_lambda := (v_lambda_by_pair ->> v_pair_key)::numeric;
            else
                -- The ledger row must exist to carry lambda. This is the same
                -- upsert the budget gate performs below; at p_index_enabled =
                -- false it does NOT run early, which is what keeps a flag-off
                -- claim from creating a ledger row one gate sooner than v1 did.
                insert into public.warm_budget_ledger (organization_id, provider, day)
                values (v_row.organization_id, v_row.provider, v_day)
                on conflict (organization_id, provider, day) do nothing;
                select ledger.reserved_usd + ledger.spent_usd, ledger.lambda_index
                  into v_lambda_spend, v_lambda_old
                  from public.warm_budget_ledger ledger
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day = v_day;
                v_lambda_u := extract(epoch from
                        (v_now at time zone 'utc')
                        - date_trunc('day', v_now at time zone 'utc')) / 86400.0;
                -- The exponent is clamped only to keep exp() inside numeric's
                -- domain. It is outcome-identical: at +50 the result exceeds
                -- any admissible p_lambda_max (<= 1e6) and is clamped by it
                -- anyway, and at -50 the factor drives (1 + lambda) * f - 1
                -- below 0 for any lambda <= 1e6, which the floor takes to 0.
                v_lambda_exponent := least(50, greatest(-50,
                    v_eta * (v_lambda_spend
                             - coalesce(v_row.daily_budget_usd, 0) * v_lambda_u)
                    / greatest(coalesce(v_row.daily_budget_usd, 0), 0.01)));
                v_lambda := least(v_lambda_ceiling, greatest(0,
                    (1 + coalesce(v_lambda_old, 0)) * exp(v_lambda_exponent) - 1));
                update public.warm_budget_ledger ledger
                   set lambda_index = v_lambda,
                       lambda_updated_at = v_now
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day = v_day;
                v_lambda_by_pair := jsonb_set(
                    v_lambda_by_pair, array[v_pair_key], to_jsonb(v_lambda));
            end if;
            if v_index < v_lambda then
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'skipped_lambda', v_p_return, v_floor,
                    v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null, null,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
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
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
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
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
            continue;
        end if;
        -- CONTROL ARM (202608100001). Still the LAST gate before the
        -- reservation: its causal validity is exactly "every other gate has
        -- already passed", so the lambda gate had to go above it, not below.
        if coalesce(p_holdout_fraction, 0) > 0 then
            v_holdout_digest := sha256(convert_to(
                lower(v_row.organization_id::text)
                || lower(v_row.prefix_hash)
                || to_char(v_day, 'YYYY-MM-DD'), 'UTF8'));
            v_holdout_bucket := get_byte(v_holdout_digest, 0)::bigint * 16777216
                              + get_byte(v_holdout_digest, 1) * 65536
                              + get_byte(v_holdout_digest, 2) * 256
                              + get_byte(v_holdout_digest, 3);
            if v_holdout_bucket::double precision
               < p_holdout_fraction * 4294967296::double precision then
                update public.warm_prefixes prefix
                   set next_due_at = v_now + make_interval(secs => greatest(
                           1, prefix.provider_ttl_seconds - p_safety_margin_seconds))
                 where prefix.organization_id = v_row.organization_id
                   and prefix.customer_id = v_row.customer_id
                   and prefix.provider = v_row.provider
                   and prefix.prefix_hash = v_row.prefix_hash;
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'holdout', v_p_return, v_floor, v_reserve,
                    v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null,
                    p_holdout_fraction::numeric,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
        end if;

        update public.warm_budget_ledger ledger
           set reserved_usd = ledger.reserved_usd + v_reserve,
               updated_at = v_now
         where ledger.organization_id = v_row.organization_id
           and ledger.provider = v_row.provider
           and ledger.day = v_day;

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
            v_row.arrival_count, v_pings_today, v_token, null,
            case when coalesce(p_holdout_fraction, 0) > 0
                then (1 - p_holdout_fraction)::numeric else null end,
            v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
            v_p_alive, v_organic, v_lambda);
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
            'last_touch_at', coalesce(v_row.last_touch_at, v_row.last_seen_at)
        );
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores to public.warm_decision_log. Under p_index_enabled candidates are ranked by the dimensionless dollar index itself and paced by the lambda dual. Under p_hazard_v2 p_return is the decayed hierarchical hazard from public.warm_customer_state rather than the lifetime histogram, P(alive) and the keep-alive chain enter the index, and (only when p_index_enabled, which is what carries P(alive) into a decision) the stop-loss predicate is bypassed in favour of P(alive); a customer with no state row (or a suppressed one) falls back to the v1 histogram at flat priors, which is provably the v1 policy.';

-- ---------------------------------------------------------------------------
-- compliance_delete_tenant (202608090002:407-532) + the Plane B state and the
-- erasure suppression list.
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
    -- public.warm_customer_state (202608100003) is the learned behavioural
    -- profile itself -- decayed hour-of-week arrival mass, churn counters and a
    -- periodicity label, per customer. It carries no foreign key, for the two
    -- reasons stated at the table, so nothing cascades it away and this is the
    -- only thing that erases it. The organization-aggregate row (nil customer
    -- uuid) goes with the rest: the whole tenant is leaving.
    delete from public.warm_customer_state state
     where state.organization_id = p_organization_id;
    -- The suppression list is deleted HERE and only here. It exists to keep an
    -- erased subject out of the model for the lifetime of the tenant; once the
    -- tenant itself is gone there is no model left for it to fence, and keeping
    -- a list of customer ids belonging to a deleted organization would be the
    -- exact residue tenant erasure is for.
    delete from public.warm_modeling_suppression suppression
     where suppression.organization_id = p_organization_id;
    -- usage_log.warm_prefix_hash (202608090002) is a sha256 of this customer's
    -- own prompt prefix, so it is a pseudonymous identifier and not content-free
    -- financial evidence. The frozen inner body enumerates the columns it
    -- minimizes and was written before this column existed, so it clears every
    -- other identifier on the rows the settlement invariant forces us to keep
    -- and leaves this one behind. Nulling a column changes no row count, so both
    -- preservation invariants below still hold.
    update public.usage_log usage
       set warm_prefix_hash = null
     where usage.organization_id = p_organization_id
       and usage.warm_prefix_hash is not null;
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
-- compliance_delete_subject (202608090002:538-638) + the subject's state row
-- and the suppression entry that keeps them out of the model for good.
-- ---------------------------------------------------------------------------
create or replace function public.compliance_delete_subject(
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
    v_request_scope text;
    v_subject_id uuid;
    v_billing_before bigint;
    v_billing_after bigint;
    v_settlement_before bigint;
    v_settlement_after bigint;
begin
    select request.request_scope, request.subject_id
      into v_request_scope, v_subject_id
      from public.data_subject_requests request
     where request.id = p_request_id
       and request.organization_id = p_organization_id;
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

    v_result := public.compliance_delete_subject_pre_company_identity(
        p_organization_id, p_request_id, p_actor_id
    );

    -- public.warm_decision_log (202608090001) is per-customer behavioral
    -- evidence and carries NO foreign key on purpose -- an FK to
    -- public.customers would let an erasure block or abort a warming claim,
    -- which is a money path. 202608090001 wired the tenant path and not this
    -- one, so a deleted customer's scored prefixes survived their customer row.
    -- Scoped to the customer path: a 'member' request erases an operator of the
    -- organization, not one of its end customers, and warm_decision_log has no
    -- member dimension to erase.
    if v_request_scope <> 'member' and v_subject_id is not null then
        delete from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
           and entry.customer_id = v_subject_id;
        -- public.warm_customer_state (202608100003) is the learned profile for
        -- this subject, across every provider. Deleting it is necessary and NOT
        -- sufficient: the very next arrival would rebuild it from scratch,
        -- because warm_prefix_observe writes state unconditionally. The
        -- suppression row below is what makes the deletion stick.
        delete from public.warm_customer_state state
         where state.organization_id = p_organization_id
           and state.customer_id = v_subject_id;
        -- THE SUPPRESSION ROW IS DELIBERATELY NOT DELETED BY THIS FUNCTION, NOW
        -- OR ON ANY LATER RUN. It is the memory of the deletion request, and a
        -- deletion path that erased its own memory would re-admit the subject
        -- to the model on their next request. Twilio Segment's
        -- deletion-and-suppression contract, the closest B2B2C processor
        -- precedent (plan Part 0). It leaves only with the tenant.
        insert into public.warm_modeling_suppression (
            organization_id, customer_id)
        values (p_organization_id, v_subject_id)
        on conflict (organization_id, customer_id) do nothing;
        -- The organization-aggregate row (nil customer uuid) is intentionally
        -- untouched. It is an aggregate over the whole tenant and carries no
        -- subject key; the erased subject's decayed contribution to it is not
        -- attributable to anyone and fades on the 14-day half-life. Subtracting
        -- it would require retaining exactly the per-subject mass this request
        -- deletes.
    end if;
    -- usage_log.warm_prefix_hash (202608090002) survives on the rows the
    -- financial-preservation invariant forces us to retain, and it is derived
    -- from this subject's own prompt prefix. The frozen inner body enumerates
    -- the columns it minimizes and predates the column. Nulling it changes no
    -- row count, so both invariants below still hold. It is cleared for both
    -- scopes: the member path minimizes rows by owner_id, and those rows can
    -- carry a hash too.
    update public.usage_log usage
       set warm_prefix_hash = null
     where usage.organization_id = p_organization_id
       and usage.warm_prefix_hash is not null
       and ((v_request_scope = 'member'
             and usage.owner_id = v_subject_id::text)
            or (v_request_scope <> 'member'
                and usage.customer_id = v_subject_id));

    if v_request_scope = 'member' then
        update public.billing_accounts account
           set checkout_session_id = null,
               updated_at = pg_catalog.clock_timestamp()
         where account.organization_id = p_organization_id
           and account.user_id = v_subject_id;
        update public.billing_events event
           set session_id = ''
         where event.organization_id = p_organization_id
           and event.user_id = v_subject_id;
        perform public.compliance_anonymize_unshared_user(v_subject_id);
    end if;

    select count(*) into v_billing_after
      from public.billing_ledger ledger
     where ledger.organization_id = p_organization_id;
    if v_billing_after <> v_billing_before then
        raise exception 'subject company financial preservation invariant failed'
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
        raise exception 'subject company settlement evidence preservation invariant failed'
            using errcode = '55000';
    end if;
    return v_result;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_tenant (202608100002:778-995) + warm_customer_state and
-- warm_modeling_suppression. The projection is enumerated, so a table nobody
-- names is absent from every export.
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

    -- The ledger projection is 202608090002's, unchanged. The two new ledger
    -- columns are deliberately NOT added here: the pacing dual in force for a
    -- candidate is already carried, per decision, by warm_decision_log.lambda_index
    -- below, which is the row the org audits a decision from. Widening this
    -- projection is a separate change with its own review.
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
    -- class 202608090002 registered in compliance_run_retention. The eight
    -- index fields are the belief the scorer acted on, and an export that drops
    -- them is an export the org cannot audit the policy from.
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
                'realized_net_usd', entry.realized_net_usd,
                'organic_counterfactual', entry.organic_counterfactual,
                'index_score', entry.index_score,
                'index_density', entry.index_density,
                'v_hit_usd', entry.v_hit_usd,
                'chain_cost_usd', entry.chain_cost_usd,
                'c_belief_usd', entry.c_belief_usd,
                'p_alive', entry.p_alive,
                'organic_multiplier', entry.organic_multiplier,
                'lambda_index', entry.lambda_index
            )
        )
          from public.warm_decision_log entry
         where entry.organization_id = p_organization_id
         order by entry.ts, entry.id;

    -- The learned behavioural state (202608100003). Portable in full: decayed
    -- counts, exposure hours, timestamps and a regime label -- no prompt or
    -- response material, so nothing here is reported-but-omitted. The
    -- organization-aggregate row IS included at tenant scope: it is the
    -- organization's own aggregate and the organization is the subject here.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_customer_state',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', state.organization_id,
                'customer_id', state.customer_id,
                'provider', state.provider,
                'customer_key_hmac', state.customer_key_hmac,
                'hazard_n', state.hazard_n,
                'hazard_e', state.hazard_e,
                'events_total', state.events_total,
                'first_seen_at', state.first_seen_at,
                'last_seen_at', state.last_seen_at,
                'last_update_at', state.last_update_at,
                'regime', state.regime,
                'regime_score', state.regime_score,
                'regime_updated_at', state.regime_updated_at,
                'created_at', state.created_at,
                'updated_at', state.updated_at
            )
        )
          from public.warm_customer_state state
         where state.organization_id = p_organization_id
         order by state.customer_id, state.provider;

    -- The suppression list. An organization is entitled to see which of its own
    -- customers it has told us never to model again -- that is the record of
    -- deletion requests it processed, and withholding it would make the
    -- deletions unauditable.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_modeling_suppression',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', suppression.organization_id,
                'customer_id', suppression.customer_id,
                'suppressed_at', suppression.suppressed_at
            )
        )
          from public.warm_modeling_suppression suppression
         where suppression.organization_id = p_organization_id
         order by suppression.customer_id;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_subject (202608100002:1000-1173) + the subject's own state
-- row and suppression entry. The organization-aggregate row is NOT exported: it
-- is an aggregate over the whole tenant and carries no subject key.
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
                    'realized_net_usd', entry.realized_net_usd,
                    'organic_counterfactual', entry.organic_counterfactual,
                    'index_score', entry.index_score,
                    'index_density', entry.index_density,
                    'v_hit_usd', entry.v_hit_usd,
                    'chain_cost_usd', entry.chain_cost_usd,
                    'c_belief_usd', entry.c_belief_usd,
                    'p_alive', entry.p_alive,
                    'organic_multiplier', entry.organic_multiplier,
                    'lambda_index', entry.lambda_index
                )
            )
              from public.warm_decision_log entry
             where entry.organization_id = p_organization_id
               and entry.customer_id = v_request.subject_id
             order by entry.ts, entry.id;

        -- The subject's own learned state (202608100003), keyed by
        -- (organization, customer) exactly as warm_prefixes is. The
        -- organization-aggregate row is deliberately NOT reachable here: its
        -- customer_id is the nil uuid, which can never equal a subject id, so
        -- the predicate excludes it by construction rather than by a filter
        -- somebody could drop.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_customer_state',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', state.organization_id,
                    'customer_id', state.customer_id,
                    'provider', state.provider,
                    'customer_key_hmac', state.customer_key_hmac,
                    'hazard_n', state.hazard_n,
                    'hazard_e', state.hazard_e,
                    'events_total', state.events_total,
                    'first_seen_at', state.first_seen_at,
                    'last_seen_at', state.last_seen_at,
                    'last_update_at', state.last_update_at,
                    'regime', state.regime,
                    'regime_score', state.regime_score,
                    'regime_updated_at', state.regime_updated_at,
                    'created_at', state.created_at,
                    'updated_at', state.updated_at
                )
            )
              from public.warm_customer_state state
             where state.organization_id = p_organization_id
               and state.customer_id = v_request.subject_id
             order by state.provider;

        -- Whether this subject is suppressed is a fact about them, and an
        -- export that omitted it would leave them unable to confirm that an
        -- earlier deletion request is still being honoured.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_modeling_suppression',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', suppression.organization_id,
                    'customer_id', suppression.customer_id,
                    'suppressed_at', suppression.suppressed_at
                )
            )
              from public.warm_modeling_suppression suppression
             where suppression.organization_id = p_organization_id
               and suppression.customer_id = v_request.subject_id;
    end if;
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_run_retention (202608090002:1023-1372) + the warm_customer_state
-- class.
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
    -- The learned state gets the SAME horizon as the decisions that produced
    -- it, keyed on last_seen_at: a customer nobody has been observed making a
    -- request as for a full season has no posterior worth keeping, and keeping
    -- one would be a behavioural profile of a relationship that ended.
    -- public.warm_modeling_suppression deliberately has NO horizon: it must
    -- outlive every row it fences or the erasure it records stops holding.
    v_warm_customer_state_cutoff timestamptz := v_warm_decision_cutoff;
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
    v_warm_customer_state_candidates integer := 0;
    v_warm_customer_state_deleted integer := 0;
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
            'warm_customer_state_candidates',v_existing.warm_customer_state_candidates,
            'warm_customer_state_deleted',v_existing.warm_customer_state_deleted,
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
                or candidate.source<>'Deleted'
                -- 202608090002: warm_prefix_hash joins the SET below, so it
                -- must join the convergence predicate in BOTH the dry-run count
                -- and the apply, or the evidence row would count rows the apply
                -- does not touch (or the batch would never converge).
                or candidate.warm_prefix_hash is not null)
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
    -- Same preservation-hold fence as every other tenant class. Ordered by the
    -- same key the apply below uses, so the evidence row counts the rows the
    -- apply actually touches.
    select count(*)::integer into v_warm_customer_state_candidates from (
        select 1 from public.warm_customer_state candidate
         where candidate.last_seen_at<v_warm_customer_state_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.last_seen_at,candidate.organization_id,
                  candidate.customer_id,candidate.provider
         limit p_batch_limit
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
            'warm_customer_state_candidates',v_warm_customer_state_candidates,
            'usage_deleted',0,'audit_deleted',0,'support_deleted',0,
            'requests_deleted',0,'holds_deleted',0,'prior_run_evidence_deleted',0,
            'usage_minimized',0,'waitlist_deleted',0,'warm_decision_deleted',0,
            'warm_customer_state_deleted',0,
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
           usage_raw = '',
           -- A sha256 of the customer's prompt prefix is a pseudonymous
           -- identifier, not content-free financial evidence.
           warm_prefix_hash = null
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
                or candidate.source<>'Deleted'
                -- 202608090002: warm_prefix_hash joins the SET below, so it
                -- must join the convergence predicate in BOTH the dry-run count
                -- and the apply, or the evidence row would count rows the apply
                -- does not touch (or the batch would never converge).
                or candidate.warm_prefix_hash is not null)
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

    -- The learned state. Keyed by its composite primary key rather than an id,
    -- which the table deliberately does not have. warm_modeling_suppression is
    -- NOT swept here and has no class at all: it must outlive the state rows it
    -- fences, or an erased subject silently becomes modelable again.
    delete from public.warm_customer_state state
     where (state.organization_id,state.customer_id,state.provider) in (
        select candidate.organization_id,candidate.customer_id,candidate.provider
          from public.warm_customer_state candidate
         where candidate.last_seen_at<v_warm_customer_state_cutoff
           and not public.compliance_preservation_hold(candidate.organization_id)
         order by candidate.last_seen_at,candidate.organization_id,
                  candidate.customer_id,candidate.provider
         for update skip locked
         limit p_batch_limit
     );
    get diagnostics v_warm_customer_state_deleted = row_count;

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
        warm_decision_candidates,warm_decision_deleted,
        warm_customer_state_candidates,warm_customer_state_deleted
    ) values (
        p_run_id,p_actor_id,p_batch_limit,v_usage_candidates,v_audit_candidates,v_support_candidates,
        v_request_candidates,v_hold_candidates,v_prior_run_candidates,
        v_usage_deleted,v_audit_deleted,v_support_deleted,
        v_requests_deleted,v_holds_deleted,v_prior_run_deleted,
        v_usage_minimize_candidates,v_waitlist_candidates,
        v_usage_minimized,v_waitlist_deleted,
        v_warm_decision_candidates,v_warm_decision_deleted,
        v_warm_customer_state_candidates,v_warm_customer_state_deleted
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
        'warm_customer_state_candidates',v_warm_customer_state_candidates,
        'warm_customer_state_deleted',v_warm_customer_state_deleted,
        'idempotent_replay',false,'evidence_contains_customer_content',false
    );
end;
$$;

-- ---------------------------------------------------------------------------
-- Privileges. 202607280032's contract: every migration that defines a routine
-- restates that routine's own posture rather than inheriting one.
-- ---------------------------------------------------------------------------
revoke all on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz)
    from public, anon, authenticated;
grant execute on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz)
    to service_role;
revoke all on function public.warm_customer_state_set_regime(uuid, uuid, text, text, numeric)
    from public, anon, authenticated;
grant execute on function public.warm_customer_state_set_regime(uuid, uuid, text, text, numeric)
    to service_role;
revoke all on function public.warm_customer_arrival_series(integer, integer)
    from public, anon, authenticated;
grant execute on function public.warm_customer_arrival_series(integer, integer)
    to service_role;
revoke all on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
) from public, anon, authenticated;
grant execute on function public.warm_prefix_observe(
    uuid, uuid, text, text, text, integer, integer, integer, boolean, numeric, text
) to service_role;
revoke all on function public.compliance_delete_tenant(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_delete_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_delete_subject(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.compliance_delete_subject(uuid, uuid, text)
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

comment on table public.warm_customer_state is
    'Per-(organization, customer, provider) decayed arrival hazard on a 14-day half-life, plus the BG/NBD churn counters. The nil-uuid customer row is the organization aggregate the customer posterior shrinks toward. No foreign key: the aggregate row could not satisfy one, and an FK would let a tenant deletion abort a live warm observation.';
comment on table public.warm_modeling_suppression is
    'Erased subjects the scorer must never model again. Written by compliance_delete_subject, never deleted by it, and on no retention horizon -- the row IS the memory of the deletion.';
comment on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz) is
    'Fold one arrival into the decayed hour-of-week hazard maps. Skips suppressed subjects. Exposure accrual overstates exposure, which understates the hazard, which warms less -- the safe direction for both documented approximations.';

commit;
