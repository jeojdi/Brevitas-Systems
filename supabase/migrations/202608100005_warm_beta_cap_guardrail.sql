-- Phase 1 learned warming, part 4: the beta spend cap, the conservative
-- guardrail and the TTL canary.
--
-- THREE THINGS, ONE THEME: none of them makes the learner smarter. Each one
-- bounds what the learner is allowed to cost while it is wrong.
--
-- 1. THE BETA CAP. Warm spend is never billable, so nothing in the settlement
--    path constrains it -- daily_budget_usd is an operator's guess and
--    202608100004's envelopes divide that guess, they do not justify it. The
--    cap ties spend to MEASURED benefit instead: trailing 28-day warm spend may
--    not exceed beta x the control-verified savings the warming actually
--    produced over the same window. public.warm_control_savings_daily is the
--    producer -- treated units (a prefix pinged that day) against control units
--    (a prefix the holdout arm withheld that day), differenced on authoritative
--    non-warm usage cost, and written as zero unless BOTH arms have at least
--    three units, so a starved comparison can never manufacture headroom. The
--    cap is read inside warm_due_claim and denies as 'beta_denied'.
--
--    p_beta DEFAULT 0 IS THE OFF STATE, and it is off because it must be: at
--    beta = 0 the cap is 0 and nothing at all could be claimed, which is the
--    correct behaviour for "the operator asked for a cap and there is no
--    control data" and the wrong behaviour for "nobody asked for a cap". The
--    gate therefore does not run at all at p_beta = 0.
--
-- 2. THE GUARDRAIL. public.warm_org_mode is a per-(organization, provider)
--    kill switch with two states. 'learned' is the default and means the row is
--    absent or says so. 'frozen' means the worker observed the learned policy
--    losing money over the trailing seven days and pinned that pair back to
--    FLAT PRIORS: the hazard model is not read, P(alive) is 1, the keep-alive
--    chain is 0 and the stop-loss predicate applies again. Frozen is not a
--    second system -- the index and the pacing dual still run, and at flat
--    priors the index is provably the v1 policy (202608100002's equivalence
--    test). Unfreezing is MANUAL, by calling warm_org_mode_set: a guardrail
--    that unfreezes itself is a guardrail that oscillates.
--
--    "LOSING MONEY" IS MEASURED, NOT SELF-REPORTED. warm_guardrail_scan
--    differences the randomized control arm's lift (warm_control_savings_daily)
--    against what warming actually cost (warm_budget_ledger reserved+spent) over
--    the same window. It does NOT read warm_decision_log.realized_net_usd --
--    the learned policy's own attribution of its own pings -- because a
--    guardrail whose input is produced by the thing it guards cannot catch the
--    failure that matters: a policy that mis-attributes its savings reports a
--    healthy net exactly while it is losing money. The plan forbids policy
--    attribution as a guardrail input, and that sum survives only as a logged
--    diagnostic (policy_net_7d_usd) so the divergence stays observable. A freeze
--    additionally requires at least three control-measured days, so an
--    unmeasured pair -- zero lift differenced against real spend -- cannot be
--    frozen for the crime of having no experiment yet.
--
-- 3. THE TTL CANARY. public.warm_canary_probes and public.warm_canary_ledger
--    support a probe job that writes a synthetic prefix with Brevitas's OWN
--    provider keys, waits a gap drawn from a ladder around the believed TTL,
--    and reads the provider's cache legs back. It measures cache physics that
--    no tenant traffic can be made to measure without spending a tenant's
--    money on an experiment they did not ask for. Plane G, like
--    warm_ttl_observations: NO TENANT KEYS AT ALL, so the tables are outside
--    tenant and subject erasure by construction and age on purge_warm_state.
--    The job is BREVITAS_TTL_CANARY, default off, and is fenced by a daily
--    dollar ledger that reserves before it sends.
--
-- WHAT THIS MIGRATION DOES NOT TOUCH. usage_log.verified_savings_usd forcing, the
-- settlement sweep, warm_budget_ledger's reserve-then-settle arithmetic, the
-- claim-token fence, billing_period_settlement_evidence. Warm spend stays
-- never-billable; warm_control_savings_daily is an INPUT to a spend ceiling and
-- is never read by anything that bills.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop public.warm_control_savings_daily, public.warm_org_mode, public.warm_canary_probes and public.warm_canary_ledger; drop public.warm_control_savings_refresh(integer), public.warm_org_mode_set(uuid,text,text,text), public.warm_org_mode_list(), public.warm_guardrail_scan(integer), public.warm_canary_reserve(text,date,numeric,numeric), public.warm_canary_settle(text,date,numeric,numeric), public.warm_canary_probe_insert(text,text,text,integer,timestamptz,numeric), public.warm_canary_probe_due(text,integer), public.warm_canary_probe_mark(bigint,text) and public.warm_canary_probe_stats(text); drop the sixteen-argument public.warm_due_claim and re-apply 202608100004_warm_customer_budget_envelopes.sql's fifteen-argument public.warm_due_claim verbatim with its revoke/grant/comment; restore the warm_ttl_observations source check to ('ping','arrival') and re-apply 202608090001_warm_instrumentation_tables.sql's public.warm_ttl_observe verbatim; re-apply 202608090001_warm_instrumentation_tables.sql's public.purge_warm_state verbatim; re-apply 202608100004_warm_customer_budget_envelopes.sql's public.compliance_delete_tenant and public.compliance_export_tenant verbatim

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    foreach required_routine in array array[
        -- Re-created below carrying their own text forward; carrying text
        -- forward is only meaningful if that text is what is installed.
        'public.warm_ttl_observe(text,text,text,numeric,text,text)',
        'public.purge_warm_state(integer)',
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)',
        'public.compliance_delete_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_anonymize_unshared_user(uuid)',
        'public.compliance_actor_role(text)',
        'public.append_company_audit(uuid,text,text,text,text,text,text,text)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100005 requires ' || required_routine;
        end if;
    end loop;
    -- Either shape satisfies this, and deliberately: the fifteen-argument form
    -- is what this migration carries forward, and the sixteen-argument form is
    -- what it installs, so a re-application of an already-applied migration
    -- must not fail its own precondition. Same rule as 202608100002:79-83 and
    -- 202608100003:90-94.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100005 requires public.warm_due_claim';
    end if;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_credentials') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_budget_ledger') is null
       or to_regclass('public.warm_ttl_observations') is null
       or to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '55000',
            message = '202608100005 requires the warming tables';
    end if;
    -- warm_customer_state, warm_modeling_suppression and warm_customer_budget
    -- belong to 202608100003 and 202608100004, and the claim and compliance
    -- texts copied below enumerate all three. Without them the copy would
    -- silently install a DIFFERENT claim path and a DIFFERENT set of
    -- compliance functions than the ones in force.
    if to_regclass('public.warm_customer_state') is null
       or to_regclass('public.warm_modeling_suppression') is null
       or to_regclass('public.warm_customer_budget') is null then
        raise exception using
            errcode = '55000',
            message = '202608100005 requires 202608100003 and 202608100004 to be applied';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- THE CAP'S PRODUCER. One row per (organization, provider, closed UTC day):
-- the control-arm comparison for that day, in dollars.
--
-- Organization-keyed and nothing finer. A unit here is a PREFIX HASH, which is
-- the holdout arm's own unit of assignment, and the row stores only counts and
-- means over those units -- no customer id, no prefix hash, no per-unit cost.
-- It is org-level analytics, which is why it rides tenant erasure (an
-- organization's own aggregates go with it) but has no business in a SUBJECT
-- export: there is no subject key on it to export.
--
-- The counts are stored even when the savings are forced to zero for want of
-- power. A cap that silently reads 0 because a comparison was too small looks
-- exactly like a cap reading 0 because warming saved nothing, and those are
-- very different operational states.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_control_savings_daily (
    organization_id uuid not null,
    provider text not null
        check (provider in ('anthropic', 'openai', 'deepseek')),
    day date not null,
    treated_units integer not null default 0 check (treated_units >= 0),
    control_units integer not null default 0 check (control_units >= 0),
    treated_mean_cost_usd numeric(18,10) not null default 0
        check (treated_mean_cost_usd >= 0),
    control_mean_cost_usd numeric(18,10) not null default 0
        check (control_mean_cost_usd >= 0),
    -- The control-arm lift: (control_mean - treated_mean) * treated_units,
    -- floored at 0 and forced to 0 when either arm is under-powered.
    -- DELIBERATELY NOT NAMED verified_savings_usd. That name belongs to
    -- public.usage_log, where it is the billing-authoritative quantity the
    -- period settlement sums; this column is an experiment readout that
    -- gates a SPEND ceiling and can never reach a fee. Two columns with one
    -- name across two tables is an audit trap, so this one carries its own.
    control_lift_usd numeric(18,10) not null default 0
        check (control_lift_usd >= 0),
    computed_at timestamptz not null default now(),
    primary key (organization_id, provider, day)
);
create index if not exists warm_control_savings_daily_window_idx
    on public.warm_control_savings_daily (organization_id, provider, day);
create index if not exists warm_control_savings_daily_retention_idx
    on public.warm_control_savings_daily (day);

-- ---------------------------------------------------------------------------
-- THE GUARDRAIL'S STATE. Absent row means 'learned', so an empty table is a
-- LEFT JOIN that changes nothing -- which is what makes this migration's claim
-- path identical to 202608100004's until the guardrail actually fires.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_org_mode (
    organization_id uuid not null,
    provider text not null
        check (provider in ('anthropic', 'openai', 'deepseek')),
    mode text not null default 'learned'
        check (mode in ('learned', 'frozen')),
    reason text not null default '' check (octet_length(reason) <= 512),
    updated_at timestamptz not null default now(),
    primary key (organization_id, provider)
);

-- ---------------------------------------------------------------------------
-- PLANE G. Both canary tables carry NO organization, customer or prefix key of
-- any kind: a probe is a prefix Brevitas generated from a seed, sent with
-- Brevitas's own provider credential, against Brevitas's own account. There is
-- no data subject anywhere in them, which is exactly why they are absent from
-- compliance_delete_tenant, compliance_delete_subject and both exports -- the
-- same posture warm_ttl_observations has carried since 202608090001 -- and why
-- they age on purge_warm_state rather than in a retention class.
-- ---------------------------------------------------------------------------
create table if not exists public.warm_canary_probes (
    id bigint generated always as identity primary key,
    -- Two providers, both Brevitas-owned probe keys. OpenAI is structurally
    -- absent: it stays measurement-only, and the worker's provider tuple is a
    -- literal pair so no key in the environment can widen it.
    provider text not null check (provider in ('anthropic', 'deepseek')),
    model text not null default '' check (octet_length(model) <= 128),
    -- The seed the prefix was generated from. The prefix itself is a pure
    -- function of this seed over a fixed English word list, so the probe body
    -- is reproducible without storing it -- and can never contain customer
    -- data, by construction rather than by policy.
    prefix_seed text not null default '' check (octet_length(prefix_seed) <= 128),
    prefix_tokens integer not null default 0
        check (prefix_tokens between 0 and 2000000),
    written_at timestamptz not null,
    probe_due_at timestamptz not null,
    gap_target_s numeric(18,3) not null
        check (gap_target_s between 0 and 2592000),
    state text not null default 'pending'
        check (state in ('pending', 'done', 'failed')),
    created_at timestamptz not null default now()
);
create index if not exists warm_canary_probes_due_idx
    on public.warm_canary_probes (state, probe_due_at);

create table if not exists public.warm_canary_ledger (
    day date not null,
    provider text not null check (provider in ('anthropic', 'deepseek')),
    probes integer not null default 0 check (probes >= 0),
    spent_usd numeric(18,10) not null default 0 check (spent_usd >= 0),
    primary key (day, provider)
);

alter table public.warm_control_savings_daily enable row level security;
alter table public.warm_org_mode enable row level security;
alter table public.warm_canary_probes enable row level security;
alter table public.warm_canary_ledger enable row level security;
revoke all on table public.warm_control_savings_daily
    from public, anon, authenticated, service_role;
revoke all on table public.warm_org_mode
    from public, anon, authenticated, service_role;
revoke all on table public.warm_canary_probes
    from public, anon, authenticated, service_role;
revoke all on table public.warm_canary_ledger
    from public, anon, authenticated, service_role;
-- Identity columns hand out sequence privileges separately from the table.
revoke all on sequence public.warm_canary_probes_id_seq
    from public, anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- The canary is a THIRD observation source. 'ping' is a keep-alive the worker
-- sent on a tenant's behalf, 'arrival' is real customer traffic at zero cost,
-- and 'canary' is a Brevitas-funded probe against a synthetic prefix. Keeping
-- them distinguishable is the whole point: a TTL curve fitted across sources
-- must be able to say which observations cost a tenant money and which did not.
-- The constraint is auto-named by 202608090001, so it is discovered rather
-- than guessed.
-- ---------------------------------------------------------------------------
do $observation_source_vocabulary$
declare
    v_constraint_name text;
begin
    for v_constraint_name in
        select constraint_row.conname
          from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_ttl_observations'::regclass
           and constraint_row.contype = 'c'
           and pg_catalog.pg_get_constraintdef(constraint_row.oid) like '%arrival%'
    loop
        execute format(
            'alter table public.warm_ttl_observations drop constraint %I',
            v_constraint_name);
    end loop;
    alter table public.warm_ttl_observations
        add constraint warm_ttl_observations_source_check
        check (source in ('ping', 'arrival', 'canary'));
end;
$observation_source_vocabulary$;

-- ---------------------------------------------------------------------------
-- warm_ttl_observe (202608090001:295-327) + the 'canary' source. Strict, as
-- before: the worker calls this directly and a silently dropped observation is
-- worse than a logged failure.
-- ---------------------------------------------------------------------------
create or replace function public.warm_ttl_observe(
    p_provider text,
    p_model_class text,
    p_ttl_tier text,
    p_gap_seconds numeric,
    p_outcome text,
    p_source text
) returns jsonb as $$
begin
    if p_provider not in ('anthropic', 'openai', 'deepseek')
       or octet_length(coalesce(p_model_class, '')) > 128
       or coalesce(p_ttl_tier, '') not in ('5m', '1h', 'auto')
       or coalesce(p_gap_seconds, -1) not between 0 and 2592000
       or coalesce(p_outcome, '') not in ('warm', 'expired')
       or coalesce(p_source, '') not in ('ping', 'arrival', 'canary') then
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

-- ---------------------------------------------------------------------------
-- THE CONTROL COMPARISON. For each closed UTC day in the trailing window that
-- has no row yet, and each (organization, provider) that scored anything that
-- day:
--
--   unit          = warm_decision_log.prefix_hash (the holdout arm's own unit
--                   of assignment, so treatment is constant within a unit-day)
--   treated       = the unit has at least one 'pinged' row that day
--   control       = the unit has at least one 'holdout' row and NO 'pinged' row
--   unit_cost(u)  = sum of authoritative non-warm usage_log.actual_cost_usd for
--                   that (organization, provider, prefix hash) inside the day
--   savings       = max(0, control_mean - treated_mean) * treated_units,
--                   but ONLY when both arms have >= 3 units; otherwise 0
--
-- The `and no 'pinged' row` half of the control predicate is written out rather
-- than assumed. The daily-stable hash assignment cannot produce a mixed unit,
-- but an operator who changes BREVITAS_WARM_HOLDOUT_FRACTION mid-day can, and
-- a mixed unit silently counted as control would understate treated cost and
-- MANUFACTURE savings -- which raises a spend ceiling. Mixed units are counted
-- and reported instead (see v_mixed): a warning rather than an exception,
-- because raising here would abort the whole sweep and starve the cap of every
-- other day and organization too.
--
-- Days are closed twice over: `today - 2` is the newest day considered, so a
-- day is never scored while late usage rows can still land in it.
-- ---------------------------------------------------------------------------
create or replace function public.warm_control_savings_refresh(
    p_max_days integer default 28
) returns jsonb as $$
declare
    v_today date := (now() at time zone 'utc')::date;
    v_day date;
    v_written integer := 0;
    v_days integer := 0;
    v_mixed integer := 0;
    v_day_mixed integer;
    v_day_written integer;
begin
    if coalesce(p_max_days, 0) not between 1 and 28 then
        raise exception 'warm control savings bounds are invalid';
    end if;
    for v_day in
        select generate_series(v_today - p_max_days - 1, v_today - 2,
                               interval '1 day')::date
    loop
        v_days := v_days + 1;
        select count(*)::integer into v_day_mixed
          from (
            select 1
              from public.warm_decision_log entry
             where entry.ts >= (v_day::timestamp at time zone 'utc')
               and entry.ts < ((v_day + 1)::timestamp at time zone 'utc')
             group by entry.organization_id, entry.provider, entry.prefix_hash
            having bool_or(entry.decision = 'pinged')
               and bool_or(entry.decision = 'holdout')
          ) mixed;
        v_mixed := v_mixed + coalesce(v_day_mixed, 0);
        with unit as (
            select entry.organization_id,
                   entry.provider,
                   entry.prefix_hash,
                   bool_or(entry.decision = 'pinged') as treated,
                   bool_or(entry.decision = 'holdout') as held_out
              from public.warm_decision_log entry
             where entry.ts >= (v_day::timestamp at time zone 'utc')
               and entry.ts < ((v_day + 1)::timestamp at time zone 'utc')
             group by entry.organization_id, entry.provider, entry.prefix_hash
        ),
        costed as (
            select unit.organization_id,
                   unit.provider,
                   unit.treated,
                   (unit.held_out and not unit.treated) as control,
                   coalesce((
                       select sum(coalesce(usage.actual_cost_usd, 0))::numeric
                         from public.usage_log usage
                        where usage.organization_id = unit.organization_id
                          and usage.provider = unit.provider
                          and usage.warm_prefix_hash = unit.prefix_hash
                          and usage.strategy <> 'cache_warm'
                          and usage.authoritative
                          and usage.ts >= (v_day::timestamp at time zone 'utc')
                          and usage.ts < ((v_day + 1)::timestamp at time zone 'utc')
                   ), 0) as cost_usd
              from unit
             where unit.treated or unit.held_out
        ),
        arm as (
            select costed.organization_id,
                   costed.provider,
                   count(*) filter (where costed.treated)::integer as treated_units,
                   count(*) filter (where costed.control)::integer as control_units,
                   coalesce(avg(costed.cost_usd)
                            filter (where costed.treated), 0)::numeric as treated_mean,
                   coalesce(avg(costed.cost_usd)
                            filter (where costed.control), 0)::numeric as control_mean
              from costed
             group by costed.organization_id, costed.provider
        )
        insert into public.warm_control_savings_daily (
            organization_id, provider, day, treated_units, control_units,
            treated_mean_cost_usd, control_mean_cost_usd, control_lift_usd
        )
        select arm.organization_id, arm.provider, v_day,
               arm.treated_units, arm.control_units,
               round(greatest(arm.treated_mean, 0), 10),
               round(greatest(arm.control_mean, 0), 10),
               -- Under-powered comparisons are written as ZERO savings with
               -- their counts intact. Zero can only shrink a spend ceiling.
               case when arm.treated_units >= 3 and arm.control_units >= 3
                   then round(greatest(0, arm.control_mean - arm.treated_mean)
                              * arm.treated_units, 10)
                   else 0 end
          from arm
         where arm.treated_units > 0 or arm.control_units > 0
        -- REVISABLE FOR 14 DAYS, FROZEN AFTER. `do nothing` froze every day at
        -- the first sweep that saw it, which is two days after the fact -- and
        -- an authoritative usage row that settles late (a retried receipt, a
        -- backfill) then never reaches the arm means at all. That silently
        -- starves the beta cap's denominator, and a denominator that is too
        -- small only ever tightens a spend ceiling in the wrong direction.
        -- Recomputing inside a 14-day revision horizon lets late rows land;
        -- outside it the WHERE is false, the row is untouched, and the history
        -- the cap has already priced against stays stable.
        on conflict (organization_id, provider, day) do update
           set treated_units = excluded.treated_units,
               control_units = excluded.control_units,
               treated_mean_cost_usd = excluded.treated_mean_cost_usd,
               control_mean_cost_usd = excluded.control_mean_cost_usd,
               control_lift_usd = excluded.control_lift_usd,
               computed_at = now()
         where warm_control_savings_daily.day >= v_today - 14;
        get diagnostics v_day_written = row_count;
        v_written := v_written + coalesce(v_day_written, 0);
    end loop;
    if v_mixed > 0 then
        raise warning 'warm_control_savings_refresh saw % mixed treated/control units', v_mixed;
    end if;
    return jsonb_build_object(
        'schema', 'brevitas.warm-control-savings.v1', 'status', 'refreshed',
        'days_scanned', v_days, 'rows_written', v_written,
        'mixed_units', v_mixed
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- The guardrail's writer. Called by the worker to FREEZE, and by an operator
-- (psql, or a store call) to unfreeze -- there is deliberately no API endpoint
-- and no automatic thaw. A pair that lost money on the learned policy is
-- unfrozen only after somebody has looked at why.
-- ---------------------------------------------------------------------------
create or replace function public.warm_org_mode_set(
    p_organization_id uuid,
    p_provider text,
    p_mode text,
    p_reason text default ''
) returns jsonb as $$
declare
    v_row public.warm_org_mode%rowtype;
begin
    if p_organization_id is null
       or coalesce(p_provider, '') not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_mode, '') not in ('learned', 'frozen')
       or octet_length(coalesce(p_reason, '')) > 512 then
        raise exception 'warm org mode arguments are invalid';
    end if;
    insert into public.warm_org_mode (organization_id, provider, mode, reason)
    values (p_organization_id, p_provider, p_mode, coalesce(p_reason, ''))
    on conflict (organization_id, provider) do update
       set mode = excluded.mode,
           reason = excluded.reason,
           updated_at = now()
    returning * into v_row;
    return jsonb_build_object(
        'schema', 'brevitas.warm-org-mode.v1', 'status', 'set',
        'organization_id', v_row.organization_id,
        'provider', v_row.provider,
        'mode', v_row.mode,
        'reason', v_row.reason,
        'updated_at', v_row.updated_at
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- The guardrail's read side. One row per (organization, provider) that spent
-- anything in the trailing window, carrying the CONTROL-MEASURED net and the
-- pair's current mode.
--
-- NOT THE POLICY'S OWN ATTRIBUTION. net_7d_usd used to sum
-- warm_decision_log.realized_net_usd -- the number the learned policy assigns
-- to its own pings. A guardrail whose input is the thing it is guarding cannot
-- catch the failure that matters: a policy mis-attributing its savings reports
-- a healthy net precisely while it is losing money, and the freeze never fires.
-- The plan forbids policy attribution as a guardrail input for exactly that
-- reason. So the net is now differenced from the holdout experiment:
--
--   net_7d_usd = sum(warm_control_savings_daily.control_lift_usd)
--              - sum(warm_budget_ledger.reserved_usd + spent_usd)
--
-- over the same trailing window per (organization, provider) -- measured lift
-- minus what warming actually cost to produce it. Both terms come from outside
-- the policy: the lift from the randomized control arm, the spend from the
-- ledger the money moved through.
--
-- control_days COUNTS THE EVIDENCE and is the freeze's licence. A pair with no
-- control rows differences 0 lift against real spend and looks catastrophic,
-- when in truth it is simply unmeasured -- so the caller must require at least
-- three control-measured days before acting. Days are counted only where the
-- refresher wrote a powered comparison (control_units >= 3, the same threshold
-- it forces the lift to zero below), so an under-powered day cannot buy a
-- freeze. control_units is the summed control-arm size over the window.
--
-- policy_net_7d_usd carries the old realized_net_usd sum forward as a LOGGED
-- DIAGNOSTIC. It is deliberately not the gate: keeping it visible is how the
-- divergence between what the policy claims and what the experiment measures
-- becomes observable instead of arguable.
--
-- The ledger drives the outer loop rather than the decision log because the
-- question the guardrail asks is "did this pair spend money", and the ledger is
-- the only record of that.
-- ---------------------------------------------------------------------------
create or replace function public.warm_guardrail_scan(
    p_lookback_days integer default 7
) returns setof jsonb as $$
declare
    v_days integer := least(greatest(coalesce(p_lookback_days, 7), 1), 90);
    v_since date := (now() at time zone 'utc')::date - v_days;
begin
    return query
        select jsonb_build_object(
            'organization_id', pair.organization_id,
            'provider', pair.provider,
            'mode', coalesce(mode.mode, 'learned'),
            'net_7d_usd', round(coalesce(measured.lift_usd, 0)
                                - coalesce(spend.spend_usd, 0), 10),
            'control_lift_usd', round(coalesce(measured.lift_usd, 0), 10),
            'warm_spend_usd', round(coalesce(spend.spend_usd, 0), 10),
            'control_days', coalesce(measured.control_days, 0),
            'control_units', coalesce(measured.control_units, 0),
            -- Diagnostic only. The policy scoring itself, kept visible so a
            -- divergence from the measured net is observable.
            'policy_net_7d_usd', coalesce((
                select sum(entry.realized_net_usd)
                  from public.warm_decision_log entry
                 where entry.organization_id = pair.organization_id
                   and entry.provider = pair.provider
                   and entry.decision = 'pinged'
                   and entry.realized_net_usd is not null
                   and entry.ts >= now() - make_interval(days => v_days)
            ), 0)
        )
          from (
            select distinct ledger.organization_id, ledger.provider
              from public.warm_budget_ledger ledger
             where ledger.day >= v_since
          ) pair
          left join (
            select savings.organization_id, savings.provider,
                   sum(savings.control_lift_usd) as lift_usd,
                   count(*) filter (
                       where savings.control_units >= 3)::integer as control_days,
                   sum(savings.control_units)::integer as control_units
              from public.warm_control_savings_daily savings
             where savings.day >= v_since
             group by savings.organization_id, savings.provider
          ) measured
            on measured.organization_id = pair.organization_id
           and measured.provider = pair.provider
          left join (
            select ledger.organization_id, ledger.provider,
                   sum(ledger.reserved_usd + ledger.spent_usd) as spend_usd
              from public.warm_budget_ledger ledger
             where ledger.day >= v_since
             group by ledger.organization_id, ledger.provider
          ) spend
            on spend.organization_id = pair.organization_id
           and spend.provider = pair.provider
          left join public.warm_org_mode mode
            on mode.organization_id = pair.organization_id
           and mode.provider = pair.provider
         order by pair.organization_id, pair.provider;
end;
$$ language plpgsql stable security definer set search_path = pg_catalog, public;

create or replace function public.warm_org_mode_list()
returns setof jsonb as $$
    select jsonb_build_object(
        'organization_id', mode.organization_id,
        'provider', mode.provider,
        'mode', mode.mode,
        'reason', mode.reason,
        'updated_at', mode.updated_at
    )
      from public.warm_org_mode mode
     order by mode.organization_id, mode.provider;
$$ language sql stable security definer set search_path = pg_catalog, public;

-- ---------------------------------------------------------------------------
-- THE CANARY'S DOLLAR FENCE. Reserve before sending, settle to actual after.
-- The reserve is a single conditional UPDATE so two replicas racing the last
-- few cents of the daily cap cannot both win: the row is locked by the update,
-- and the loser sees zero rows affected and stands down.
-- ---------------------------------------------------------------------------
create or replace function public.warm_canary_reserve(
    p_provider text,
    p_day date,
    p_est_usd numeric,
    p_cap_usd numeric
) returns jsonb as $$
declare
    v_spent numeric;
begin
    if coalesce(p_provider, '') not in ('anthropic', 'deepseek')
       or p_day is null
       or coalesce(p_est_usd, -1) not between 0 and 1000
       or coalesce(p_cap_usd, -1) not between 0 and 1000 then
        raise exception 'warm canary reserve arguments are invalid';
    end if;
    insert into public.warm_canary_ledger (day, provider)
    values (p_day, p_provider)
    on conflict (day, provider) do nothing;
    update public.warm_canary_ledger ledger
       set spent_usd = ledger.spent_usd + p_est_usd,
           probes = ledger.probes + 1
     where ledger.day = p_day
       and ledger.provider = p_provider
       and ledger.spent_usd + p_est_usd <= p_cap_usd
    returning ledger.spent_usd into v_spent;
    if not found then
        select ledger.spent_usd into v_spent
          from public.warm_canary_ledger ledger
         where ledger.day = p_day and ledger.provider = p_provider;
        return jsonb_build_object(
            'schema', 'brevitas.warm-canary-reserve.v1', 'allowed', false,
            'spent_usd', coalesce(v_spent, 0));
    end if;
    return jsonb_build_object(
        'schema', 'brevitas.warm-canary-reserve.v1', 'allowed', true,
        'spent_usd', v_spent);
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- The settle adjusts the estimate to what the call actually cost. Floored at
-- zero: an over-estimate that would drive the day negative simply lands at 0,
-- it never hands back headroom the ledger never had.
create or replace function public.warm_canary_settle(
    p_provider text,
    p_day date,
    p_est_usd numeric,
    p_actual_usd numeric
) returns jsonb as $$
declare
    v_spent numeric;
begin
    if coalesce(p_provider, '') not in ('anthropic', 'deepseek')
       or p_day is null
       or coalesce(p_est_usd, -1) not between 0 and 1000
       or coalesce(p_actual_usd, -1) not between 0 and 1000 then
        raise exception 'warm canary settle arguments are invalid';
    end if;
    update public.warm_canary_ledger ledger
       set spent_usd = greatest(0, ledger.spent_usd + p_actual_usd - p_est_usd)
     where ledger.day = p_day and ledger.provider = p_provider
    returning ledger.spent_usd into v_spent;
    return jsonb_build_object(
        'schema', 'brevitas.warm-canary-settle.v1', 'status', 'settled',
        'spent_usd', coalesce(v_spent, 0));
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

create or replace function public.warm_canary_probe_insert(
    p_provider text,
    p_model text,
    p_prefix_seed text,
    p_prefix_tokens integer,
    p_written_at timestamptz,
    p_gap_target_s numeric
) returns jsonb as $$
declare
    v_id bigint;
begin
    if coalesce(p_provider, '') not in ('anthropic', 'deepseek')
       or octet_length(coalesce(p_model, '')) > 128
       or octet_length(coalesce(p_prefix_seed, '')) > 128
       or coalesce(p_prefix_tokens, -1) not between 0 and 2000000
       or p_written_at is null
       or coalesce(p_gap_target_s, -1) not between 0 and 2592000 then
        raise exception 'warm canary probe arguments are invalid';
    end if;
    insert into public.warm_canary_probes (
        provider, model, prefix_seed, prefix_tokens, written_at,
        probe_due_at, gap_target_s
    ) values (
        p_provider, coalesce(p_model, ''), coalesce(p_prefix_seed, ''),
        p_prefix_tokens, p_written_at,
        p_written_at + make_interval(secs => p_gap_target_s),
        round(p_gap_target_s, 3)
    ) returning id into v_id;
    return jsonb_build_object(
        'schema', 'brevitas.warm-canary-probe.v1', 'status', 'pending',
        'id', v_id);
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

create or replace function public.warm_canary_probe_due(
    p_provider text default null,
    p_limit integer default 50
) returns setof jsonb as $$
    select jsonb_build_object(
        'id', probe.id,
        'provider', probe.provider,
        'model', probe.model,
        'prefix_seed', probe.prefix_seed,
        'prefix_tokens', probe.prefix_tokens,
        'written_at', probe.written_at,
        'probe_due_at', probe.probe_due_at,
        'gap_target_s', probe.gap_target_s
    )
      from public.warm_canary_probes probe
     where probe.state = 'pending'
       and probe.probe_due_at <= now()
       and (p_provider is null or probe.provider = p_provider)
     order by probe.probe_due_at, probe.id
     limit least(greatest(coalesce(p_limit, 50), 1), 500);
$$ language sql stable security definer set search_path = pg_catalog, public;

create or replace function public.warm_canary_probe_mark(
    p_id bigint,
    p_state text
) returns jsonb as $$
begin
    if p_id is null or coalesce(p_state, '') not in ('done', 'failed') then
        raise exception 'warm canary probe state is invalid';
    end if;
    update public.warm_canary_probes probe
       set state = p_state
     where probe.id = p_id and probe.state = 'pending';
    return jsonb_build_object(
        'schema', 'brevitas.warm-canary-probe.v1', 'status', p_state,
        'id', p_id);
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- The gap ladder's index. 'done' probes are the ones that produced an
-- observation, so the ladder advances only on measurement, never on failure.
create or replace function public.warm_canary_probe_stats(
    p_provider text
) returns jsonb as $$
    select jsonb_build_object(
        'provider', p_provider,
        'pending', count(*) filter (where probe.state = 'pending'),
        'done', count(*) filter (where probe.state = 'done'),
        'failed', count(*) filter (where probe.state = 'failed')
    )
      from public.warm_canary_probes probe
     where probe.provider = p_provider;
$$ language sql stable security definer set search_path = pg_catalog, public;

-- --------------------------------------------------------------------------
-- warm_due_claim (202608100004:426-1083) + the beta cap gate and the frozen
-- LEFT JOIN. Every existing gate, the lambda dual, the hazard read side, the
-- envelope gate, the reservation, the claim token and the holdout draw are
-- carried forward unchanged and in the same order; the beta gate is inserted
-- between the envelope and the control arm and nothing else moves.
--
-- The signature grows, so the fifteen-argument form is DROPPED first: two
-- candidate signatures for one name is how a caller silently keeps talking to
-- the old policy.
-- --------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean
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
    p_hazard_v2 boolean default false,
    -- The trailing-28-day spend cap, as a multiple of control-verified
    -- savings. 0 (and null, from a caller that predates 202608100005) means
    -- the cap does not run at all -- not that the cap is zero. A zero cap
    -- would deny every candidate, which is what an operator who armed the cap
    -- with no control data should get and what an operator who never armed it
    -- must never get.
    p_beta numeric default 0
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
    -- ENVELOPE (202608100004). The period is derived from the SAME UTC day
    -- v_day is, so a claim can only ever move money on the month its budget_day
    -- belongs to and warm_ping_settle -- which derives it from p_budget_day --
    -- lands on the same row even when the settle happens the next month.
    v_period date := date_trunc(
        'month', ((clock_timestamp() at time zone 'utc')::date)::timestamp)::date;
    v_env_found boolean;
    v_env numeric;
    v_env_reserved numeric;
    v_env_day date;
    v_env_spent numeric;
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
    -- THE GUARDRAIL (202608100005). Per candidate, from the LEFT JOIN below:
    -- true when the worker froze this (organization, provider) pair. Frozen
    -- means FLAT PRIORS, not "warming off" -- see the hazard block.
    v_frozen boolean;
    -- THE BETA CAP (202608100005). Cap and running trailing-window spend, per
    -- (organization, provider), resolved once per invocation. v_beta_w28 rises
    -- locally with every reservation this invocation makes, so a single batch
    -- cannot walk past the cap by reading a stale ledger sum once.
    v_beta_key text;
    v_beta_cap numeric;
    v_beta_w28 numeric;
    v_beta_cap_by_pair jsonb := '{}'::jsonb;
    v_beta_w28_by_pair jsonb := '{}'::jsonb;
    v_beta_c28 numeric;
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
       or v_lambda_ceiling not between 0 and 1000000
       or coalesce(p_beta, 0) not between 0 and 100 then
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
                   -- Absent row means 'learned', so an empty warm_org_mode
                   -- makes this join and everything downstream of it a no-op.
                   coalesce(mode.mode, 'learned') = 'frozen' as warm_frozen,
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
              left join public.warm_org_mode mode
                on mode.organization_id = prefix.organization_id
               and mode.provider = prefix.provider
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
               -- 202608100005 SUPERSEDES 202608100003's form: the
               -- stop-loss is retired in favour of P(alive) only while the
               -- pair is LEARNED. A frozen pair is scored at flat priors, and
               -- flat priors have no P(alive) to retire it with, so the
               -- counter has to come back with them.
               and ((p_hazard_v2 and p_index_enabled
                     and coalesce(mode.mode, 'learned') = 'learned')
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
        -- FROZEN IS FLAT PRIORS, NOT A SECOND SYSTEM (202608100005). The state
        -- row is not read, so p_return stays the lifetime histogram, p_alive
        -- stays 1 and the chain stays 0 -- exactly the fallback a customer the
        -- model has never seen gets, which 202608100002 proved equal to v1.
        -- The index and the pacing dual keep running on top of it.
        v_frozen := coalesce(v_row.warm_frozen, false);
        if p_hazard_v2 and not v_frozen then
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
        -- PER-CUSTOMER ENVELOPE (202608100004). Positioned after the
        -- organization budget and before the control arm: it is a hard spend
        -- gate of the same kind as the budget above it, and the holdout coin
        -- must remain the LAST thing between a candidate and its reservation.
        --
        -- NO ROW MEANS NO CONSTRAINT, and that is the default-off story rather
        -- than a loophole. A spend ceiling cannot take a boolean argument
        -- without giving a caller a way to switch it off, so the check is
        -- unconditional and simply vacuous until a row exists -- and rows exist
        -- only because an operator PUT one or because the allocator coroutine
        -- (BREVITAS_WARM_ENVELOPE_ALLOCATOR, default off) ran. A deployment
        -- that has done neither behaves exactly as 202608100003 did.
        v_env_found := null;
        v_env := null;
        v_env_reserved := null;
        v_env_spent := null;
        v_env_day := null;
        select true, budget.envelope_usd, budget.reserved_usd, budget.spent_usd,
               budget.reserved_day
          into v_env_found, v_env, v_env_reserved, v_env_spent, v_env_day
          from public.warm_customer_budget budget
         where budget.organization_id = v_row.organization_id
           and budget.provider = v_row.provider
           and budget.period_start = v_period
           and budget.customer_ref = v_row.customer_id::text;
        -- STALE-RESERVATION SELF-HEAL. A reservation written before today was
        -- never released -- the worker holding it died, or its customer was
        -- erased and settle can no longer name the row. On the DAY-keyed
        -- organization ledger that heals itself, because tomorrow is a
        -- different row; on this MONTH-keyed table it would hold the
        -- customer's envelope hostage until the 1st. So a reservation from a
        -- previous UTC day is zeroed BEFORE the check reads it, exactly as if
        -- the day had rolled the row over. SPENT is untouched: spent dollars
        -- are settled money and the envelope is a monthly ceiling on them.
        if coalesce(v_env_found, false) and v_env_day < v_day::date then
            update public.warm_customer_budget budget
               set reserved_usd = 0,
                   reserved_day = v_day::date,
                   updated_at = v_now
             where budget.organization_id = v_row.organization_id
               and budget.provider = v_row.provider
               and budget.period_start = v_period
               and budget.customer_ref = v_row.customer_id::text;
            v_env_reserved := 0;
        end if;
        if coalesce(v_env_found, false)
           and v_env_reserved + v_env_spent + v_reserve > v_env then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'envelope_denied', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, v_lambda);
            continue;
        end if;
        -- THE BETA CAP (202608100005). Trailing 28-day warm spend may not
        -- exceed beta x the control-verified savings measured over the same
        -- window. Positioned after every per-organization and per-customer
        -- ceiling and before the control arm, because the holdout coin must
        -- stay the LAST thing between a candidate and its reservation.
        --
        --   C28 = sum of control_lift_usd over [day-28, day-2]
        --         (the closed days warm_control_savings_refresh has scored)
        --   W28 = sum of reserved+spent over [day-27, day]
        --         (the open window that spend actually landed in)
        --   cap = beta * C28
        --
        -- The two windows deliberately do not line up. Savings can only be
        -- measured on days whose usage rows have settled, and spend has to be
        -- counted the moment it is reserved; asking them to share endpoints
        -- would either count spend nobody has measured the benefit of yet or
        -- credit savings against a day whose spend has not been counted.
        --
        -- p_beta = 0 skips the block ENTIRELY rather than computing a zero cap.
        if coalesce(p_beta, 0) > 0 then
            v_beta_key := v_row.organization_id || ':' || v_row.provider;
            if not (v_beta_cap_by_pair ? v_beta_key) then
                select coalesce(sum(savings.control_lift_usd), 0)
                  into v_beta_c28
                  from public.warm_control_savings_daily savings
                 where savings.organization_id = v_row.organization_id
                   and savings.provider = v_row.provider
                   and savings.day between v_day - 28 and v_day - 2;
                select coalesce(sum(ledger.reserved_usd + ledger.spent_usd), 0)
                  into v_beta_w28
                  from public.warm_budget_ledger ledger
                 where ledger.organization_id = v_row.organization_id
                   and ledger.provider = v_row.provider
                   and ledger.day between v_day - 27 and v_day;
                v_beta_cap_by_pair := jsonb_set(
                    v_beta_cap_by_pair, array[v_beta_key],
                    to_jsonb(p_beta * v_beta_c28));
                v_beta_w28_by_pair := jsonb_set(
                    v_beta_w28_by_pair, array[v_beta_key], to_jsonb(v_beta_w28));
            end if;
            v_beta_cap := (v_beta_cap_by_pair ->> v_beta_key)::numeric;
            v_beta_w28 := (v_beta_w28_by_pair ->> v_beta_key)::numeric;
            if v_beta_w28 + v_reserve > v_beta_cap then
                perform public.warm_decision_record(
                    v_row.organization_id, v_row.customer_id, v_row.provider,
                    v_row.prefix_hash, 'beta_denied', v_p_return, v_floor,
                    v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                    v_row.arrival_count, v_pings_today, v_token, null, null,
                    v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                    v_p_alive, v_organic, v_lambda);
                continue;
            end if;
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
        -- The envelope reservation is written IMMEDIATELY ADJACENT to the
        -- organization ledger's, deliberately: they are the same dollars seen
        -- at two granularities, and any code between them would be code that
        -- could reserve one without the other.
        if coalesce(v_env_found, false) then
            update public.warm_customer_budget budget
               set reserved_usd = budget.reserved_usd + v_reserve,
                   reserved_day = v_day::date,
                   updated_at = v_now
             where budget.organization_id = v_row.organization_id
               and budget.provider = v_row.provider
               and budget.period_start = v_period
               and budget.customer_ref = v_row.customer_id::text;
        end if;

        -- The cap's in-invocation accounting. The ledger sum above was read
        -- once for this pair; without this line a batch of candidates would
        -- each be measured against the same pre-batch W28 and the batch as a
        -- whole could walk straight past the cap.
        if coalesce(p_beta, 0) > 0 then
            v_beta_w28_by_pair := jsonb_set(
                v_beta_w28_by_pair, array[v_beta_key],
                to_jsonb(v_beta_w28 + v_reserve));
        end if;

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
    double precision, boolean, numeric, numeric, boolean, numeric
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores to public.warm_decision_log. Under p_index_enabled candidates are ranked by the dimensionless dollar index itself and paced by the lambda dual. Under p_hazard_v2 p_return is the decayed hierarchical hazard from public.warm_customer_state rather than the lifetime histogram, P(alive) and the keep-alive chain enter the index, and (only when p_index_enabled, which is what carries P(alive) into a decision) the stop-loss predicate is bypassed in favour of P(alive); a customer with no state row (or a suppressed one) falls back to the v1 histogram at flat priors, which is provably the v1 policy. A candidate whose reservation would carry its customer past that customer''s public.warm_customer_budget envelope for the month is denied as ''envelope_denied''; a customer with no envelope row is unconstrained, which is what keeps a deployment with no envelopes identical to 202608100003. Under p_beta > 0 a candidate is denied as ''beta_denied'' when the organization''s trailing 28-day warm spend plus this reservation would exceed p_beta times the control-verified savings in public.warm_control_savings_daily; at the default p_beta = 0 the cap does not run. A pair frozen in public.warm_org_mode is scored at FLAT PRIORS -- no hazard read, P(alive) 1, no chain, stop-loss predicate re-applied -- which is provably the v1 policy, not a second policy.';

-- ---------------------------------------------------------------------------
-- purge_warm_state (202608090001:935-988) + the canary and control-savings
-- horizons.
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
    -- 202608100005. Three more aggregate horizons on the same sweeper and for
    -- the same reason: none of these tables carries a tenant key, so no data
    -- subject can hold, export or erase them and they have no business in
    -- compliance_run_retention's class list.
    --
    -- Canary probes are operational state -- a pending row is an experiment in
    -- flight -- and 30 days is well past the longest gap the ladder can ask
    -- for. The canary dollar ledger and the control-savings comparison are
    -- both financial evidence: the ledger is what the probes cost Brevitas,
    -- and the comparison is the measurement a spend ceiling was set from.
    -- Both retain on the 400-day evidence horizon the rest of the accounting
    -- record uses, and neither is tied to p_retention_days for the reason
    -- 202607280017 established for the budget ledger.
    v_canary_probe_retention_days integer := 30;
    v_canary_evidence_retention_days integer := 400;
    v_prefixes_deleted integer;
    v_prefixes_absolute_deleted integer;
    v_ledger_deleted integer;
    v_observations_deleted integer;
    v_canary_probes_deleted integer;
    v_canary_ledger_deleted integer;
    v_control_savings_deleted integer;
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
    delete from public.warm_canary_probes probe
     where probe.created_at
           < now() - make_interval(days => v_canary_probe_retention_days);
    get diagnostics v_canary_probes_deleted = row_count;
    delete from public.warm_canary_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date
                        - v_canary_evidence_retention_days;
    get diagnostics v_canary_ledger_deleted = row_count;
    delete from public.warm_control_savings_daily savings
     where savings.day < (now() at time zone 'utc')::date
                         - v_canary_evidence_retention_days;
    get diagnostics v_control_savings_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-purge.v1', 'status', 'purged',
        'canary_probes_deleted', v_canary_probes_deleted,
        'canary_ledger_deleted', v_canary_ledger_deleted,
        'control_savings_deleted', v_control_savings_deleted,
        'prefixes_deleted', v_prefixes_deleted,
        'prefixes_absolute_deleted', v_prefixes_absolute_deleted,
        'ledger_retention_days', v_ledger_retention_days,
        'ledger_deleted', v_ledger_deleted,
        'observation_retention_days', v_observation_retention_days,
        'observations_deleted', v_observations_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

-- --------------------------------------------------------------------------
-- compliance_delete_tenant (202608100004:1219-1367) + the two organization
-- level tables 202608100005 adds.
-- --------------------------------------------------------------------------
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
    -- public.warm_customer_budget (202608100004) is this organization's own
    -- per-customer money: envelope ceilings and the reserved/spent booked
    -- against them. A SUBJECT erasure tombstones the key and keeps the dollars,
    -- because the organization's accounting must survive one of its customers
    -- leaving. A TENANT deletion is the opposite case -- there is no
    -- organization left to account to -- so the rows go outright.
    delete from public.warm_customer_budget budget
     where budget.organization_id = p_organization_id;
    -- public.warm_control_savings_daily (202608100005) is this organization's
    -- own treated-versus-control comparison: counts and dollar means over
    -- prefix-hash units, with no customer key on it. It is org-level analytics
    -- about this tenant, so it leaves with the tenant.
    delete from public.warm_control_savings_daily savings
     where savings.organization_id = p_organization_id;
    -- public.warm_org_mode (202608100005) is the guardrail's per-provider
    -- freeze state for this organization. Leaving it behind would freeze -- or
    -- silently un-freeze -- a future organization that happened to reuse the
    -- id, and it names a tenant that no longer exists.
    delete from public.warm_org_mode mode
     where mode.organization_id = p_organization_id;
    -- public.warm_canary_probes and public.warm_canary_ledger (202608100005)
    -- are deliberately NOT touched, exactly as public.warm_ttl_observations is
    -- not: Plane G. A canary probe is a synthetic prefix Brevitas generated
    -- from a seed and sent with Brevitas's own credential against Brevitas's
    -- own account. There is no organization, customer or prefix key anywhere
    -- in either table, so there is nothing here belonging to this tenant to
    -- erase, and both age on public.purge_warm_state.
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

-- --------------------------------------------------------------------------
-- compliance_export_tenant (202608100004:1526-1816) + the control comparison
-- and the guardrail verdict.
-- --------------------------------------------------------------------------
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

    -- The spend envelopes (202608100004). Ceilings, the money booked against
    -- them and where each ceiling came from, for every period. Tombstoned rows
    -- are INCLUDED at tenant scope: the dollars are the organization's own
    -- accounting and the key on them is already unlinkable to anybody, so
    -- withholding them would only make the organization's spend unauditable.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_customer_budget',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', budget.organization_id,
                'provider', budget.provider,
                'period_start', budget.period_start,
                'customer_ref', budget.customer_ref,
                'erased', budget.customer_ref like 'erased:%',
                'envelope_usd', budget.envelope_usd,
                'reserved_usd', budget.reserved_usd,
                'spent_usd', budget.spent_usd,
                'source', budget.source,
                'created_at', budget.created_at,
                'updated_at', budget.updated_at
            )
        )
          from public.warm_customer_budget budget
         where budget.organization_id = p_organization_id
         order by budget.period_start, budget.provider, budget.customer_ref;

    -- The control comparison (202608100005). This is the measurement the
    -- organization's warming spend ceiling is derived from, so an organization
    -- that wants to know why its warming was throttled -- or was not -- is
    -- entitled to the arithmetic. Counts and means only; the per-unit costs
    -- behind them were never stored.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_control_savings',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', savings.organization_id,
                'provider', savings.provider,
                'day', savings.day,
                'treated_units', savings.treated_units,
                'control_units', savings.control_units,
                'treated_mean_cost_usd', savings.treated_mean_cost_usd,
                'control_mean_cost_usd', savings.control_mean_cost_usd,
                'control_lift_usd', savings.control_lift_usd,
                'computed_at', savings.computed_at
            )
        )
          from public.warm_control_savings_daily savings
         where savings.organization_id = p_organization_id
         order by savings.day, savings.provider;

    -- The guardrail's verdict (202608100005), with the reason string the
    -- worker wrote. An organization frozen back to flat priors should be able
    -- to see that it was, and why.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_org_mode',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', mode.organization_id,
                'provider', mode.provider,
                'mode', mode.mode,
                'reason', mode.reason,
                'updated_at', mode.updated_at
            )
        )
          from public.warm_org_mode mode
         where mode.organization_id = p_organization_id
         order by mode.provider;
    -- public.warm_canary_probes and public.warm_canary_ledger are absent for
    -- the same reason public.warm_ttl_observations is: Plane G carries no
    -- tenant key, so there is no row in either table that belongs to this
    -- organization to export.
end;
$function$;


-- ---------------------------------------------------------------------------
-- Privileges. 202607280032's contract: every migration that defines a routine
-- restates that routine's own posture rather than inheriting one.
-- ---------------------------------------------------------------------------
revoke all on function public.warm_control_savings_refresh(integer)
    from public, anon, authenticated;
grant execute on function public.warm_control_savings_refresh(integer)
    to service_role;
revoke all on function public.warm_org_mode_set(uuid, text, text, text)
    from public, anon, authenticated;
grant execute on function public.warm_org_mode_set(uuid, text, text, text)
    to service_role;
revoke all on function public.warm_org_mode_list()
    from public, anon, authenticated;
grant execute on function public.warm_org_mode_list() to service_role;
revoke all on function public.warm_guardrail_scan(integer)
    from public, anon, authenticated;
grant execute on function public.warm_guardrail_scan(integer) to service_role;

comment on function public.warm_guardrail_scan(integer) is
    'One row per (organization, provider) that spent anything in the trailing window. net_7d_usd is the CONTROL-MEASURED net -- public.warm_control_savings_daily.control_lift_usd minus public.warm_budget_ledger reserved+spent over the same window -- deliberately not the policy''s own warm_decision_log.realized_net_usd attribution, which is carried as the policy_net_7d_usd diagnostic instead. control_days counts the powered control comparisons behind the number; a caller must not freeze a pair on fewer than three.';
revoke all on function public.warm_canary_reserve(text, date, numeric, numeric)
    from public, anon, authenticated;
grant execute on function public.warm_canary_reserve(text, date, numeric, numeric)
    to service_role;
revoke all on function public.warm_canary_settle(text, date, numeric, numeric)
    from public, anon, authenticated;
grant execute on function public.warm_canary_settle(text, date, numeric, numeric)
    to service_role;
revoke all on function public.warm_canary_probe_insert(
    text, text, text, integer, timestamptz, numeric) from public, anon, authenticated;
grant execute on function public.warm_canary_probe_insert(
    text, text, text, integer, timestamptz, numeric) to service_role;
revoke all on function public.warm_canary_probe_due(text, integer)
    from public, anon, authenticated;
grant execute on function public.warm_canary_probe_due(text, integer)
    to service_role;
revoke all on function public.warm_canary_probe_mark(bigint, text)
    from public, anon, authenticated;
grant execute on function public.warm_canary_probe_mark(bigint, text)
    to service_role;
revoke all on function public.warm_canary_probe_stats(text)
    from public, anon, authenticated;
grant execute on function public.warm_canary_probe_stats(text) to service_role;
revoke all on function public.warm_ttl_observe(text, text, text, numeric, text, text)
    from public, anon, authenticated;
grant execute on function public.warm_ttl_observe(text, text, text, numeric, text, text)
    to service_role;
revoke all on function public.purge_warm_state(integer)
    from public, anon, authenticated;
grant execute on function public.purge_warm_state(integer) to service_role;
revoke all on function public.compliance_delete_tenant(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_delete_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_tenant(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_tenant(uuid, uuid, text)
    to service_role;

comment on table public.warm_control_savings_daily is
    'Per (organization, provider, closed UTC day) treated-versus-control comparison of warming: unit counts and mean authoritative non-warm cost per prefix-hash unit, and the resulting control_lift_usd. Deliberately NOT named verified_savings_usd -- that column belongs to public.usage_log and is billing-authoritative, while this one is an experiment readout that can only tighten a spend ceiling. Written as zero lift (with the counts kept) unless both arms have at least three units. Rows stay revisable for 14 days so late-settling authoritative usage still reaches the arm means, and freeze after that. Read only by the beta spend cap inside public.warm_due_claim; never read by anything that bills.';
comment on table public.warm_org_mode is
    'The conservative guardrail''s state per (organization, provider). Absent or ''learned'' is the normal case; ''frozen'' pins the pair to flat priors inside public.warm_due_claim -- no hazard read, P(alive) 1, no keep-alive chain, stop-loss predicate re-applied -- which is provably the v1 policy. Written by the worker guardrail on a negative trailing-7-day CONTROL-MEASURED net (public.warm_control_savings_daily lift minus public.warm_budget_ledger warming spend, never the policy''s own realized_net_usd attribution) and only where at least three control-measured days exist; unfreezing is manual, via public.warm_org_mode_set.';
comment on table public.warm_canary_probes is
    'Plane G. Scheduled TTL probes against synthetic prefixes generated from a seed and sent with Brevitas''s own provider credentials. Carries no organization, customer or prefix key of any kind, so it is outside tenant and subject erasure by construction and ages on public.purge_warm_state.';
comment on table public.warm_canary_ledger is
    'Plane G. The canary''s own daily dollar fence, per (day, provider). Reserved before each probe is sent and settled to actual after, so a replica cannot spend past the cap. Brevitas''s money, never a tenant''s: no organization key exists on it.';

commit;
