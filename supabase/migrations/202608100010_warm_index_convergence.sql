-- Index convergence: the simulator-proven learned-index-fixed policy, ported
-- into the shipped scheduler.
--
-- WHAT THIS IS. scripts/warm_replay_sim.py measured the Phase-1 learned index
-- at -$1.89 net against v1's +$0.007 on the 21-day synthetic-company benchmark.
-- docs/INDEX_FIX_ROUND.md diagnosed four defects (F1..F4); the convergence run
-- proved three more were needed (F5..F7). This migration ports the converged
-- policy -- scripts/warm_replay_sim.py's LearnedIndexFixedPolicy, which is the
-- binding reference for every number below -- into public.warm_due_claim and
-- the estimator functions around it.
--
--   F1  p_return is SILENCE-CONDITIONED. The shipped index asks "how often does
--       this customer arrive in this hour-of-week bucket"; this asks "given
--       they have been silent for s seconds, what is P(arrival in (s, s+W])".
--       The hazard state already carries the Gamma sufficient statistics; the
--       elapsed silence is folded in as expected-arrivals-that-did-not-happen
--       and the answer falls out of the negative-binomial survival. 68% of
--       Phase-1 pings fired at customers silent more than six hours; silence is
--       evidence, and the shipped query threw it away.
--   F2  HARD ORGANIC-SUPPRESSION GATE. An EWMA inter-arrival below the provider
--       TTL means the customer refreshes their own entry for free: skip with
--       'skipped_organic' before anything is scored. A gate, not a multiplier
--       -- it does not wait for control-arm data, and the learned
--       organic_multiplier stays stubbed at 1.0.
--   F3  I_max ABANDON RULE. Past I_max = ttl * (w/f - 1) the keep-alive chain
--       costs more than the lapse it prevents, whatever the probability: skip
--       with 'skipped_abandon'. And n_chain becomes the sum of the CONDITIONAL
--       survival curve over the windows the ping commits to, instead of
--       E_gap - chain_age, which fell toward zero exactly as a session died.
--   F4  COLD-START PRIOR. Eight pseudo-exposure-hours of organization prior
--       become two (p_prior_hours), and the prior is retired in proportion to
--       the customer's own accumulated exposure (p_exposure_majority_hours), so
--       their own evidence carries majority weight after ~2 active days instead
--       of ~3 weeks. Plus the cold-start hold below.
--   F5  PERIODICITY FAST-PATH. A scheduled agent is a clock, not a renewal
--       process. When an arm's recent gaps cluster tightly (median/MAD over at
--       least three gaps) it is routed to a deterministic phase-minus-lead-time
--       schedule, and the index prices the keep-alives that actually stand
--       between now and the forecast arrival. If two predicted arrivals pass
--       unanswered the phase model is falsified and the arm falls back to the
--       index path -- which is what stops a departed cron agent being sustained
--       forever. public.warm_customer_state.recent_gaps is the evidence.
--   F6  MEDIAN-FLOORED CHAIN. On the index path n_chain is a mean over a
--       heavy-tailed residual-life distribution, dragged up by improbable long
--       silences the I_max rule would abandon long before reaching. Inside an
--       active session the chain is capped at the MEDIAN of the same survival
--       curve -- the number of keep-alives the typical session actually needs.
--   F7  SHARED-CACHE-KEY TOUCH. Provider caches are ORG-key-scoped, not
--       customer-scoped, so twenty customers on one shared system prompt are
--       twenty arms bidding to keep ONE entry warm. An arm whose entry is
--       already warm past its own next decision point is skipped with
--       'skipped_shared_warm'. The touch state is DERIVED, not invented and not
--       stored: public.warm_prefixes.last_touch_at (202608090001) is already
--       stamped by warm_prefix_observe on every arrival and by warm_ping_settle
--       on every provably-warmed ping, which is exactly the simulator's
--       cache_warm_until, and the claim takes its max over the organization's
--       rows sharing (provider, prefix_hash). One deliberate difference, in the
--       conservative direction: a 'spent_unknown' settle does not stamp the
--       clock, because we do not know the provider processed that ping, so a
--       sibling is re-pinged rather than skipped on warmth nobody can prove.
--
-- COLD START HOLDS v1, and holds it longer than Phase 1 did. Phase 1 fell back
-- to the histogram only when the hazard ROW was absent -- for exactly one
-- arrival -- after which it scored from a posterior built on a single
-- observation and went quiet, which is what left the learned policy earning
-- nothing over a customer's first session while v1 was already bridging gaps.
-- Here the hold runs until the arm clears p_roi_min_arrivals, which is the same
-- threshold v1 itself uses to admit that it has no evidence.
--
-- THE FLAG PAIR IS UNCHANGED AND STILL DEFAULT-OFF. Every line of the delta is
-- reachable only when BOTH p_index_enabled (BREVITAS_WARM_INDEX) and
-- p_hazard_v2 (BREVITAS_WARM_HAZARD_V2) are true and the pair is not frozen in
-- public.warm_org_mode. At either flag off, at flat priors, on a frozen pair,
-- on a customer with no state row, and on an arm below p_roi_min_arrivals, this
-- function computes exactly what 202608100007's did -- which is the equivalence
-- the six v1-equivalence tests hold it to and the only claim a flag on a claim
-- path is allowed to make.
--
-- FOUR NEW TUNABLES, all with the converged simulator's values as defaults, all
-- passed as arguments rather than read from a GUC so the two backends and this
-- migration cannot drift on them: p_prior_hours (2.0), p_exposure_majority_hours
-- (48.0), p_period_max_dispersion (0.20), p_period_miss_limit (1.5). The
-- remaining constants -- the 0.25 prior floor, the 3-gap minimum, the 12-gap
-- window, the 0.98 confidence ceiling and the 240-hour walk guard -- are
-- LITERALS on both sides of the port, exactly as the 14-day half-life is: an
-- estimator is only comparable across replicas if every writer uses the same
-- ones.
--
-- COMPLIANCE. Exactly ONE column is added -- public.warm_customer_state.recent_gaps
-- -- and no table. F7 needed no storage at all: warm_prefixes.last_touch_at
-- already carries the shared-key touch clock, so the only new state anywhere in
-- this migration is the twelve inter-arrival gaps F5 reasons over.
--
-- recent_gaps creates no new class. warm_customer_state is already erased
-- wholesale by compliance_delete_tenant (every row of the organization) and by
-- compliance_delete_subject (the subject's rows, with a suppression row written
-- so the next arrival cannot rebuild them), and it already ages on last_seen_at
-- inside compliance_run_retention's warm_customer_state class. A column added
-- to that table is therefore erased and aged by the row that carries it, and
-- there is nothing to wire on either path -- which is a claim this migration's
-- assertion suite checks rather than asserts.
--
-- The EXPORTS are the exception, and the reason the house rule exists: they are
-- ENUMERATED projections, so a column nobody names is silently absent from
-- every data right. compliance_export_tenant and compliance_export_subject are
-- both carried forward here with recent_gaps added to the warming_customer_state
-- projection and nothing else changed. It is behavioural timing about a
-- subject -- when they showed up, twelve times over -- and it is exported as
-- such, in the same record the decayed hour-of-week mass already ships in.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop the twenty-one-argument public.warm_due_claim (integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean,numeric,numeric,numeric,numeric) and re-apply 202608100007_warm_parent_dedup.sql's seventeen-argument public.warm_due_claim verbatim with its revoke/grant/comment; re-apply 202608100003_warm_customer_state_hazard.sql's public.warm_customer_state_touch(uuid,uuid,text,timestamptz) verbatim with its revoke/grant/comment; re-apply 202608100008_warm_prefix_tree_attribution.sql's public.compliance_export_tenant(uuid,uuid,text) and public.compliance_export_subject(uuid,uuid,text) verbatim with their revoke/grant; drop functions public.warm_conv_posterior(numeric,numeric,numeric,numeric,numeric,numeric,numeric), public.warm_conv_survival(numeric,numeric,numeric,numeric), public.warm_conv_i_max(numeric,numeric,numeric), public.warm_conv_median(numeric[]), public.warm_conv_period(numeric[],numeric), public.warm_conv_lambda_to(numeric[],numeric[],numeric), public.warm_conv_round_half_even(numeric), public.warm_conv_push_gap(numeric[],numeric,integer) and public.warm_hour_of_week(timestamptz); delete from public.warm_decision_log where decision in ('skipped_organic','skipped_abandon','skipped_shared_warm'); alter table public.warm_decision_log drop constraint warm_decision_log_decision_check and re-add 202608100007_warm_parent_dedup.sql's vocabulary check verbatim, and re-apply that migration's twenty-seven-argument public.warm_decision_record verbatim; drop index if exists public.warm_prefixes_cache_key_idx; alter table public.warm_decision_log drop constraint warm_decision_log_p_eff_check, drop column p_eff; alter table public.warm_customer_state drop constraint warm_customer_state_recent_gaps_check, drop column recent_gaps

begin;

-- ---------------------------------------------------------------------------
-- PRECONDITIONS. Everything this migration carries forward is asserted to be
-- installed first: carrying text forward is only meaningful if the text being
-- carried is what is actually there. Each routine that this migration replaces
-- at a NEW arity accepts either the pre- or the post-state, because the upgrade
-- harness applies every migration past 202607170011 twice and re-applying an
-- applied migration must not fail its own precondition. Same rule as
-- 202608100007:75-78.
-- ---------------------------------------------------------------------------
do $migration_precondition$
declare
    required_routine text;
    required_table text;
begin
    foreach required_routine in array array[
        'public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text,jsonb,text,integer)',
        'public.warm_customer_state_touch(uuid,uuid,text,timestamptz)',
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_delete_tenant(uuid,uuid,text)',
        'public.compliance_delete_subject(uuid,uuid,text)',
        'public.compliance_run_retention(uuid,text,integer,boolean)',
        'public.purge_warm_state(integer)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100010 requires ' || required_routine;
        end if;
    end loop;

    -- warm_due_claim: seventeen arguments before this migration, twenty-one
    -- after. Either is proof that 202608100007 landed.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean,numeric,numeric,numeric,numeric)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100010 requires 202608100007 to be applied';
    end if;
    -- F7 reads warm_prefixes.last_touch_at and writes nothing: 202608090001's
    -- provable-touch clock IS the shared-key touch state, stamped by
    -- warm_prefix_observe on every arrival and by warm_ping_settle on every
    -- warmed ping. Its absence would make the gate read a null and never fire,
    -- which is a silent half-application rather than a failure.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_prefixes'::regclass
           and attribute.attname = 'last_touch_at'
           and not attribute.attisdropped) then
        raise exception using
            errcode = '55000',
            message = '202608100010 requires public.warm_prefixes.last_touch_at';
    end if;
    -- warm_decision_record: twenty-seven arguments before this migration,
    -- twenty-eight after. Either is proof that 202608100007 landed, and
    -- accepting both is what lets the upgrade harness apply this file twice --
    -- re-applying an applied migration must not fail its own precondition.
    if to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text)') is null
       and to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text,numeric)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100010 requires 202608100007''s public.warm_decision_record';
    end if;

    foreach required_table in array array[
        'public.warm_prefixes', 'public.warm_customer_state',
        'public.warm_modeling_suppression', 'public.warm_decision_log',
        'public.warm_budget_ledger', 'public.warm_org_mode'
    ] loop
        if to_regclass(required_table) is null then
            raise exception using
                errcode = '55000',
                message = '202608100010 requires the Phase 1 warming tables';
        end if;
    end loop;

    -- 202608100006's chain column, which 202608100007's pre-pass groups on and
    -- which the warm_due_claim body carried forward here still reads.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_prefixes'::regclass
           and attribute.attname = 'chain_path'
           and not attribute.attisdropped) then
        raise exception using
            errcode = '55000',
            message = '202608100010 requires public.warm_prefixes.chain_path';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- THE ONE COLUMN.
--
-- warm_customer_state.recent_gaps is the F5 evidence: the customer's last
-- twelve inter-arrival gaps in seconds, newest last, maintained by
-- warm_customer_state_touch. Twelve because the detector needs three and the
-- median wants headroom against a missed firing; storing more would only let a
-- schedule the customer abandoned a fortnight ago outvote the one they keep.
--
-- It is DURATIONS, not timestamps. A gap says how regular a customer is; it
-- does not say when they were there, which the hour-of-week mass beside it
-- already does at a coarser grain. That is the least this fast-path can be
-- built on and still work.
-- ---------------------------------------------------------------------------
alter table public.warm_customer_state
    add column if not exists recent_gaps numeric[] not null
        default array[]::numeric[];

do $recent_gaps_bounds$
begin
    -- A bounded array, checked rather than trusted: warm_customer_state_touch
    -- is the only writer and it trims to twelve, but the constraint is what
    -- makes that true of a row some future writer touches.
    --
    -- The LENGTH is all a check constraint can enforce here -- element-wise
    -- non-negativity needs a subquery, which CHECK forbids. It is enforced
    -- instead at the only door: warm_conv_push_gap discards a non-positive gap
    -- rather than storing it, on the same rule the decay already follows, that
    -- a clock which went backwards must leave nothing behind rather than
    -- negative evidence. A negative gap that did somehow land would be
    -- harmless anyway: warm_conv_period's median would move, its MAD would
    -- widen, and a wider MAD only ever REFUSES the fast path.
    if not exists (
        select 1 from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_customer_state'::regclass
           and constraint_row.conname = 'warm_customer_state_recent_gaps_check') then
        alter table public.warm_customer_state
            add constraint warm_customer_state_recent_gaps_check
            check (coalesce(array_length(recent_gaps, 1), 0) <= 12);
    end if;
end;
$recent_gaps_bounds$;

-- ---------------------------------------------------------------------------
-- ONE MORE COLUMN, on the decision log: p_eff, the probability the index was
-- actually charged for.
--
-- Phase 1 logged p_return and p_alive and the identity
-- index_score = p_return * p_alive * organic / b - 1 - n_chain held, so any row
-- could be re-derived from the row itself. F1 breaks that identity on purpose:
-- the ROI floor gates on the single-TTL-window return probability (which is
-- what p_return still records, because a row must never be scored on one number
-- and logged with another) while the index is charged over the 1 + n_chain
-- windows the chain actually commits money to. Those are different numbers by
-- design, and without this column the second one would appear in no row of the
-- database.
--
-- A decision that cannot be re-derived from its own log row is not evidence,
-- and this log is what an operator reads to find out why their money moved.
-- Null when the index machinery was off, exactly like index_score, so a
-- flag-off row stays indistinguishable from a pre-Phase-1 one.
-- ---------------------------------------------------------------------------
alter table public.warm_decision_log
    add column if not exists p_eff numeric;

do $decision_p_eff_bounds$
begin
    if not exists (
        select 1 from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_decision_log'::regclass
           and constraint_row.conname = 'warm_decision_log_p_eff_check') then
        alter table public.warm_decision_log
            add constraint warm_decision_log_p_eff_check
            check (p_eff is null or p_eff between 0 and 1);
    end if;
end;
$decision_p_eff_bounds$;

-- The F7 lookup key: the max last_touch_at over an organization's rows sharing
-- one provider cache entry. warm_prefixes_org_idx is (organization_id,
-- provider, last_seen_at desc), which cannot serve a prefix_hash equality, and
-- the claim runs this lookup once per candidate.
create index if not exists warm_prefixes_cache_key_idx
    on public.warm_prefixes (organization_id, provider, prefix_hash);

-- ---------------------------------------------------------------------------
-- THE VOCABULARY. Three decisions join the closed set: 'skipped_organic' (F2),
-- 'skipped_abandon' (F3) and 'skipped_shared_warm' (F7). The existing
-- constraint is DISCOVERED rather than assumed, dropped, and re-added widened.
-- A widening is satisfied by every existing row by construction, so NOT VALID +
-- VALIDATE costs one scan and buys the guarantee that it really was a widening.
-- ---------------------------------------------------------------------------
do $decision_vocabulary$
declare
    v_constraint_name text;
begin
    for v_constraint_name in
        select constraint_row.conname
          from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_decision_log'::regclass
           and constraint_row.contype = 'c'
           and pg_catalog.pg_get_constraintdef(constraint_row.oid) like '%skipped_roi%'
    loop
        execute format(
            'alter table public.warm_decision_log drop constraint %I',
            v_constraint_name);
    end loop;
    alter table public.warm_decision_log
        add constraint warm_decision_log_decision_check
        check (decision in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout', 'skipped_lambda', 'envelope_denied',
            'beta_denied', 'dedup_deferred',
            'skipped_organic', 'skipped_abandon', 'skipped_shared_warm'))
        not valid;
    alter table public.warm_decision_log
        validate constraint warm_decision_log_decision_check;
end;
$decision_vocabulary$;

-- ---------------------------------------------------------------------------
-- THE ESTIMATORS. Seven pure functions, each the SQL spelling of one function
-- in scripts/warm_replay_sim.py and each mirrored byte-for-meaning in
-- api/store.py. They are separate routines rather than inline arithmetic for
-- one reason: an estimator that cannot be called on its own cannot be tested on
-- its own, and every number below is load-bearing on money.
--
-- All are IMMUTABLE and read no table. None of them decides anything; they are
-- called only from public.warm_due_claim, and only when both flags are on.
-- ---------------------------------------------------------------------------

-- The 168-bucket hour-of-week key, '0'..'167', identical to the expression
-- warm_due_claim already computes inline for v_bucket_key and to
-- api/store.py's _utc_hour_bucket. Hoisted into a function because the two
-- hazard walks below need it at an arbitrary instant rather than at v_now.
create or replace function public.warm_hour_of_week(p_at timestamptz)
returns text as $$
    select ((extract(isodow from (p_at at time zone 'utc'))::integer - 1) * 24
            + extract(hour from (p_at at time zone 'utc'))::integer)::text;
$$ language sql immutable;

-- (alpha, beta) of the Gamma posterior over one hour-of-week bucket's arrival
-- rate, returned as a two-element array.
--
-- The Phase-1 hazard rate is exactly alpha/beta with the prior pinned at 8.0
-- and no exposure cap. Keeping the SHAPE as well as the mean is what makes the
-- silence conditioning possible at all: a rate you are certain about and a rate
-- you merely guessed decay differently under evidence of silence.
--
-- F4, BOTH CLAUSES. The prior decays as the customer accumulates engaged
-- exposure (majority weight by ~2 active days), AND it is capped at the
-- customer's own exposure in THIS bucket, so their own evidence can never be
-- outvoted by the prior once they have any. Without the cap the shipped eight
-- pseudo-hours -- and even two -- still drag a six-arrivals-per-hour cron agent
-- down to two an hour for its whole first week, which is the inertness F4
-- exists to remove. The 0.25-hour floor is what stops the cap collapsing the
-- prior to nothing in a bucket the customer has never been exposed to, where
-- the organization term is the only information there is.
create or replace function public.warm_conv_posterior(
    p_n numeric, p_e numeric, p_org_n numeric, p_org_e numeric,
    p_exposure_total numeric, p_prior_hours numeric, p_majority_hours numeric
) returns numeric[] as $$
declare
    v_h_org numeric;
    v_own numeric := coalesce(p_e, 0);
    v_ramped numeric;
    v_k numeric;
begin
    -- 0.25 / 42.0 = 1/168, one arrival per week per bucket-hour: the global
    -- level, a CONSTANT on purpose, content-free and identical for every tenant
    -- so no cross-organization behaviour is ever pooled.
    v_h_org := (coalesce(p_org_n, 0) + 0.25) / (coalesce(p_org_e, 0) + 42.0);
    v_ramped := coalesce(p_prior_hours, 2.0)
                * (1 - least(1, coalesce(p_exposure_total, 0)
                                / greatest(coalesce(p_majority_hours, 48.0), 0.000000001)));
    v_k := greatest(0.25, least(v_ramped, greatest(v_own, 0.25)));
    return array[coalesce(p_n, 0) + v_k * v_h_org, v_own + v_k];
end;
$$ language plpgsql immutable;

-- S = P(NO arrival in the stretch worth `p_lam_ahead` expected arrivals |
-- silent so far), under that Gamma posterior.
--
--   P(no arrival) = E_theta[exp(-theta*h)] = (1 + h/beta)^(-alpha)
--
-- the Gamma-Poisson (negative binomial) survival. p_beta_silent already carries
-- the elapsed silence, which is the entire point of F1: a customer who should
-- have arrived five times by now and did not has a posterior rate five times'
-- worth lower, and THAT is what makes the probability silence-conditioned
-- rather than the unconditional hourly hazard the Phase-1 index used.
--
-- The exponent clamp at -50 is the same one every other exp() in this schema
-- carries and is outcome-identical: at -50 the survival is 0 to twenty digits.
create or replace function public.warm_conv_survival(
    p_alpha numeric, p_beta_silent numeric, p_lam_ahead numeric, p_rate numeric
) returns numeric as $$
declare
    v_hours numeric;
begin
    if coalesce(p_alpha, 0) <= 0
       or coalesce(p_rate, 0) <= 0.000000000001
       or coalesce(p_lam_ahead, 0) <= 0 then
        return 1;
    end if;
    v_hours := p_lam_ahead / p_rate;
    return exp(greatest(-50, -p_alpha * ln(
        1 + least(1000000000, v_hours / greatest(p_beta_silent, 0.000000001)))));
end;
$$ language plpgsql immutable;

-- I_max = ttl * (w/f - 1): sustaining a chain longer than this costs more in
-- read-priced keep-alives than the one write-priced miss it prevents, whatever
-- the probability. This is what makes "never warm a dead session" ARITHMETIC
-- rather than policy.
--
-- f is recovered from the same per-provider break-even the claim already gates
-- on -- the worker derives b = f/(1-f), which inverts to f = b/(1+b) exactly --
-- for the same reason the index dollars are: the gate and the economics cannot
-- then drift apart. NOTE that for 'anthropic' the break-even is the operator's
-- calibrated scalar (0.11) rather than a derived one, so the recovered f is
-- 0.0991 against a true read fraction of 0.10 and this horizon runs about one
-- percent long. That is the same approximation 202608100003's chain truncation
-- already carries, in the same direction, and it is stated here rather than
-- silently inherited.
create or replace function public.warm_conv_i_max(
    p_ttl_seconds numeric, p_write_multiplier numeric, p_break_even numeric
) returns numeric as $$
    select greatest(0, coalesce(p_ttl_seconds, 0)
        * (coalesce(p_write_multiplier, 1)
           / greatest(coalesce(p_break_even, 0) / (1 + coalesce(p_break_even, 0)),
                      0.000000001) - 1));
$$ language sql immutable;

-- Median of a numeric array, even-length case averaged. Spelled out rather than
-- delegated to percentile_cont because that aggregate returns double precision
-- and interpolates as lo + (hi-lo)*0.5, which is not bit-identical to the
-- 0.5*(lo+hi) the simulator and api/store.py compute.
create or replace function public.warm_conv_median(p_values numeric[])
returns numeric as $$
declare
    v_sorted numeric[];
    v_n integer;
begin
    if p_values is null or array_length(p_values, 1) is null then
        return 0;
    end if;
    select array_agg(value order by value) into v_sorted
      from unnest(p_values) as entry(value);
    v_n := array_length(v_sorted, 1);
    if v_n % 2 = 1 then
        return v_sorted[(v_n + 1) / 2];
    end if;
    return 0.5 * (v_sorted[v_n / 2] + v_sorted[v_n / 2 + 1]);
end;
$$ language plpgsql immutable;

-- F5's detector: {period_seconds, confidence} if this arm looks like a clock,
-- null if it does not.
--
-- Robust autocorrelation on a series this short is just a noisy way of
-- computing what the median inter-arrival already says, so the detector is
-- median/MAD: the period is the median gap, and the evidence for calling it a
-- period is that the gaps cluster tightly around it. MAD rather than standard
-- deviation because one missed cron firing -- a double-length gap -- must not
-- disqualify an otherwise perfect clock, and the median absorbs it.
--
-- Confidence is reported honestly rather than asserted: tight clustering is
-- high confidence, and the edge of the band is barely more than a coin flip,
-- which is what the index arithmetic then has to price.
create or replace function public.warm_conv_period(
    p_gaps numeric[], p_max_dispersion numeric
) returns numeric[] as $$
declare
    v_period numeric;
    v_deviations numeric[];
    v_dispersion numeric;
    v_max numeric := coalesce(p_max_dispersion, 0.20);
begin
    if p_gaps is null or coalesce(array_length(p_gaps, 1), 0) < 3 then
        return null;
    end if;
    v_period := public.warm_conv_median(p_gaps);
    if v_period <= 0 then
        return null;
    end if;
    select array_agg(abs(gap - v_period)) into v_deviations
      from unnest(p_gaps) as entry(gap);
    v_dispersion := public.warm_conv_median(v_deviations) / v_period;
    if v_dispersion > v_max then
        return null;
    end if;
    return array[v_period,
                 least(0.98, greatest(0.5, 1 - v_dispersion / v_max * 0.5))];
end;
$$ language plpgsql immutable;

-- Expected arrivals over the first `p_seconds` of a horizon, from a piecewise
-- hour-of-week rate curve. p_seg_end carries each segment's END offset in
-- seconds from the start of the horizon; the segments are contiguous, so the
-- first starts at zero and each subsequent one starts where the last ended.
--
-- The curve is kept PIECEWISE rather than flattened to an average rate because
-- flattening is wrong in exactly the case that matters: a horizon that runs off
-- the end of a customer's active window averages their real rate with buckets
-- they have never been seen in, and the near-window probability collapses for
-- no reason.
create or replace function public.warm_conv_lambda_to(
    p_seg_end numeric[], p_seg_rate numeric[], p_seconds numeric
) returns numeric as $$
declare
    v_total numeric := 0;
    v_start numeric := 0;
    v_index integer;
begin
    if p_seg_end is null or array_length(p_seg_end, 1) is null then
        return 0;
    end if;
    for v_index in 1 .. array_length(p_seg_end, 1) loop
        exit when coalesce(p_seconds, 0) <= v_start;
        v_total := v_total + coalesce(p_seg_rate[v_index], 0)
                   * (least(p_seconds, p_seg_end[v_index]) - v_start) / 3600.0;
        v_start := p_seg_end[v_index];
    end loop;
    return v_total;
end;
$$ language plpgsql immutable;

-- Python's round(), spelled out. round() in Python is half-to-EVEN; round() in
-- Postgres is half-away-from-zero. The two disagree only at an exact .5, which
-- a sum of survival probabilities essentially never lands on -- but "essentially
-- never" is not the standard the two backends of this scheduler are held to,
-- and the equivalence tests compare decisions, not distributions.
create or replace function public.warm_conv_round_half_even(p_value numeric)
returns integer as $$
declare
    v_floor numeric := floor(coalesce(p_value, 0));
    v_remainder numeric := coalesce(p_value, 0) - floor(coalesce(p_value, 0));
begin
    if v_remainder > 0.5 then
        return (v_floor + 1)::integer;
    end if;
    if v_remainder < 0.5 then
        return v_floor::integer;
    end if;
    if (v_floor::bigint) % 2 = 0 then
        return v_floor::integer;
    end if;
    return (v_floor + 1)::integer;
end;
$$ language plpgsql immutable;

-- F5's evidence writer: append one inter-arrival gap to a customer's rolling
-- window, oldest dropped, keeping at most p_window. A non-positive gap is not
-- evidence and is discarded rather than stored -- a clock that went backwards
-- must leave nothing behind, which is the same rule warm_customer_state_touch
-- already applies to the decay.
create or replace function public.warm_conv_push_gap(
    p_gaps numeric[], p_gap numeric, p_window integer default 12
) returns numeric[] as $$
declare
    v_all numeric[];
    v_n integer;
begin
    if p_gap is null or p_gap <= 0 then
        return coalesce(p_gaps, array[]::numeric[]);
    end if;
    v_all := coalesce(p_gaps, array[]::numeric[]) || p_gap;
    v_n := coalesce(array_length(v_all, 1), 0);
    if v_n <= coalesce(p_window, 12) then
        return v_all;
    end if;
    return (select array_agg(gap order by position)
              from unnest(v_all) with ordinality as entry(gap, position)
             where position > v_n - coalesce(p_window, 12));
end;
$$ language plpgsql immutable;

revoke all on function public.warm_conv_push_gap(numeric[], numeric, integer) from public, anon, authenticated;
grant execute on function public.warm_conv_push_gap(numeric[], numeric, integer) to service_role;
revoke all on function public.warm_hour_of_week(timestamptz) from public, anon, authenticated;
revoke all on function public.warm_conv_posterior(numeric, numeric, numeric, numeric, numeric, numeric, numeric) from public, anon, authenticated;
revoke all on function public.warm_conv_survival(numeric, numeric, numeric, numeric) from public, anon, authenticated;
revoke all on function public.warm_conv_i_max(numeric, numeric, numeric) from public, anon, authenticated;
revoke all on function public.warm_conv_median(numeric[]) from public, anon, authenticated;
revoke all on function public.warm_conv_period(numeric[], numeric) from public, anon, authenticated;
revoke all on function public.warm_conv_lambda_to(numeric[], numeric[], numeric) from public, anon, authenticated;
revoke all on function public.warm_conv_round_half_even(numeric) from public, anon, authenticated;
grant execute on function public.warm_hour_of_week(timestamptz) to service_role;
grant execute on function public.warm_conv_posterior(numeric, numeric, numeric, numeric, numeric, numeric, numeric) to service_role;
grant execute on function public.warm_conv_survival(numeric, numeric, numeric, numeric) to service_role;
grant execute on function public.warm_conv_i_max(numeric, numeric, numeric) to service_role;
grant execute on function public.warm_conv_median(numeric[]) to service_role;
grant execute on function public.warm_conv_period(numeric[], numeric) to service_role;
grant execute on function public.warm_conv_lambda_to(numeric[], numeric[], numeric) to service_role;
grant execute on function public.warm_conv_round_half_even(numeric) to service_role;

comment on function public.warm_conv_posterior(numeric, numeric, numeric, numeric, numeric, numeric, numeric)
    is 'F4 of docs/INDEX_FIX_ROUND.md: the (alpha, beta) Gamma posterior over one hour-of-week bucket''s arrival rate, with the organization prior weighted at p_prior_hours pseudo-exposure-hours, retired in proportion to the customer''s own accumulated exposure against p_majority_hours, and capped at the customer''s own exposure in this bucket. Phase 1''s hazard rate is exactly alpha/beta at a pinned prior of 8.0 with neither the ramp nor the cap.';
comment on function public.warm_conv_survival(numeric, numeric, numeric, numeric)
    is 'F1 of docs/INDEX_FIX_ROUND.md: P(no arrival over a stretch worth p_lam_ahead expected arrivals) under the Gamma posterior, i.e. the negative-binomial survival (1 + h/beta)^(-alpha). p_beta_silent carries the elapsed silence, which is what makes the probability silence-conditioned rather than the unconditional hourly hazard.';
comment on function public.warm_conv_i_max(numeric, numeric, numeric)
    is 'F3 of docs/INDEX_FIX_ROUND.md: I_max = ttl * (w/f - 1), the longest silence a keep-alive chain can bridge before its cost exceeds the single write-priced miss it prevents. f is recovered from the break-even as b/(1+b).';
comment on function public.warm_conv_period(numeric[], numeric)
    is 'F5 of docs/INDEX_FIX_ROUND.md: {period_seconds, confidence} when an arm''s recent inter-arrival gaps cluster tightly enough (median/MAD, at least three gaps, dispersion at or under p_max_dispersion) to be treated as a clock rather than a renewal process; null otherwise.';

-- warm_decision_record (202608100007:241-363) + the three convergence
-- decisions. Carried forward verbatim; the ONLY change is the vocabulary
-- the body validates, widened to match the table constraint widened above.
-- Restated in full because a migration that carries text forward must carry
-- the text it is carrying.
--
-- ORIGINAL HEADER FOLLOWS.
-- warm_decision_record (202608100002:258-345) + the four group columns.
--
-- The twenty-three-argument form is DROPPED, not overloaded: two candidates
-- for one name is how half the callers keep writing rows without the stamp.
-- All four new arguments DEFAULT NULL, so every existing twenty-three-argument
-- call site -- every gate in the claim loop -- resolves unchanged and writes
-- exactly the row it wrote before.
--
-- The validator is the TABLE'S OWN constraints and nothing more. It runs
-- inside warm_due_claim's advisory-locked transaction, so a stricter check
-- here aborts a claim the table would have accepted.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric
);
-- And 202608100007's own twenty-seven-argument form, which THIS migration
-- replaces at twenty-eight. A default on the new argument does not replace the
-- old function, it OVERLOADS it, and two candidates for one name make every
-- existing twenty-three-argument call ambiguous -- which is a runtime error on
-- a money path, discovered at the first claim rather than at deploy.
drop function if exists public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text
);

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
    p_propensity numeric default null,
    -- Null on every one of these means "the index machinery was off for this
    -- row", which is the state a caller that predates Phase 1 produces.
    p_index_score numeric default null,
    p_index_density numeric default null,
    p_v_hit_usd numeric default null,
    p_chain_cost_usd numeric default null,
    p_c_belief_usd numeric default null,
    p_p_alive numeric default null,
    p_organic_multiplier numeric default null,
    p_lambda_index numeric default null,
    -- Null on every one of these means "this candidate was not grouped", which
    -- is the state p_parent_dedup = false produces for every candidate.
    p_dedup_group uuid default null,
    p_dedup_role text default null,
    p_dedup_group_size integer default null,
    p_dedup_node_digest text default null,
    -- 202608100010. The probability the index was actually charged for. Phase 1
    -- could re-derive it as p_return * p_alive * organic; F1 makes the ROI
    -- floor's probability and the index's probability different numbers on
    -- purpose, so the second one is recorded rather than inferred. Null when
    -- the index machinery was off, exactly like p_index_score.
    p_p_eff numeric default null
) returns jsonb as $$
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_prefix_hash, '') !~ '^[0-9a-f]{64}$'
       or coalesce(p_decision, '') not in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout', 'skipped_lambda', 'envelope_denied',
            'beta_denied', 'dedup_deferred',
            'skipped_organic', 'skipped_abandon', 'skipped_shared_warm')
       or coalesce(p_p_return, -1) not between 0 and 1
       or coalesce(p_roi_floor, -1) not between 0 and 1
       or coalesce(p_reserve_usd, -1) not between 0 and 99999999
       or coalesce(p_prefix_tokens, -1) not between 0 and 2000000000
       or coalesce(p_arrival_count, -1) < 0
       or coalesce(p_ewma_interarrival_s, 0) < 0
       or coalesce(p_pings_today, 0) < 0
       or coalesce(p_propensity, 0) not between 0 and 1
       or coalesce(p_v_hit_usd, 0) not between 0 and 99999999
       or coalesce(p_chain_cost_usd, 0) < 0
       or coalesce(p_c_belief_usd, 0) < 0
       or coalesce(p_p_alive, 0) not between 0 and 1
       or coalesce(p_p_eff, 0) not between 0 and 1
       or coalesce(p_organic_multiplier, 0) < 0
       or coalesce(p_lambda_index, 0) < 0
       or (p_claim_token is not null and p_decision <> 'pinged')
       or (p_dedup_role is not null
           and p_dedup_role not in ('leader', 'deferred'))
       or (p_dedup_group is null) <> (p_dedup_role is null)
       or coalesce(p_dedup_group_size, 2) < 2
       or (p_dedup_node_digest is not null
           and p_dedup_node_digest !~ '^[0-9a-f]{64}$') then
        raise exception 'warm decision arguments are invalid';
    end if;
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, ewma_interarrival_s,
        arrival_count, pings_today, claim_token, rng_seed, propensity,
        index_score, index_density, v_hit_usd, chain_cost_usd, c_belief_usd,
        p_alive, organic_multiplier, lambda_index,
        dedup_group, dedup_role, dedup_group_size, dedup_node_digest,
        p_eff
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash, p_decision,
        p_p_return, p_roi_floor, p_reserve_usd, p_prefix_tokens,
        p_ewma_interarrival_s, p_arrival_count, p_pings_today, p_claim_token,
        p_rng_seed, p_propensity,
        p_index_score, p_index_density, p_v_hit_usd, p_chain_cost_usd,
        p_c_belief_usd, p_p_alive, p_organic_multiplier, p_lambda_index,
        p_dedup_group, p_dedup_role, p_dedup_group_size, p_dedup_node_digest,
        p_p_eff
    );
    return jsonb_build_object(
        'schema', 'brevitas.warm-decision.v1', 'status', 'recorded',
        'decision', p_decision
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text, numeric
) from public, anon, authenticated;
grant execute on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text, numeric
) to service_role;

comment on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric, uuid, text, integer, text, numeric
) is 'Append one scored warming candidate to public.warm_decision_log. Validates exactly the table''s own constraints and nothing more, because it runs inside warm_due_claim''s advisory-locked transaction and a stricter check here would abort a claim the table would have accepted. The four trailing group arguments are 202608100007''s shared-parent stamp and are null on every candidate the dedup pre-pass did not group, which is every candidate at p_parent_dedup = false.';

-- --------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- warm_customer_state_touch (202608100003:271-395) + the F5 gap window. Every
-- line of the decay, the exposure walk and the arrival accrual is carried
-- forward unchanged and in the same order; the only new statement is one column
-- in the UPDATE's SET list.
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
           -- F5 (202608100010). The gap this arrival just closed, appended to
           -- the rolling window the periodicity detector reads. state.last_seen_at
           -- is still the PREVIOUS arrival here -- an UPDATE reads the old row --
           -- which is exactly the gap the simulator records. The INSERT branch
           -- above writes no gap because a first arrival closes none.
           recent_gaps = public.warm_conv_push_gap(
               state.recent_gaps,
               extract(epoch from (v_ts - state.last_seen_at))::numeric),
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
revoke all on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz)
    from public, anon, authenticated;
grant execute on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz)
    to service_role;

comment on function public.warm_customer_state_touch(uuid, uuid, text, timestamptz)
    is 'Fold one arrival into public.warm_customer_state: decay the hour-of-week hazard maps by the 14-day half-life, accrue exposure over the elapsed hours, increment the bucket count and the raw BG/NBD event counter, and (202608100010) append the closed inter-arrival gap to the twelve-entry recent_gaps window the periodicity fast-path reads. Called only from warm_prefix_observe, and never for a suppressed subject.';

-- --------------------------------------------------------------------------
-- warm_due_claim (202608100007:368-1700) + the seven convergence fixes. Every
-- existing gate, the dedup pre-pass and deferral, the lambda dual, the hazard
-- read side, the envelope gate, the beta cap, the reservation, the claim token
-- and the holdout draw are carried forward unchanged and in the same order.
-- THREE things are inserted and nothing else moves:
--
--   1. the F2/F3/F7 gate block, ABOVE the hazard read, because none of the
--      three depends on the model and all three answer "this arm should not be
--      bought at any price". The simulator gates in the same place and for the
--      same reason. Scoring first so the log could carry an index would spend a
--      240-hour hazard walk on a candidate whose answer is already known, and
--      an index computed for an arm that is not for sale is not evidence of
--      anything.
--   2. the cold-start hold, which restores the v1 histogram p_return, p_alive
--      of 1 and a chain of 0 for an arm below p_roi_min_arrivals even though a
--      state row exists -- the case Phase 1 got wrong.
--   3. the F1/F4/F5/F6 scoring block, which REPLACES v_p_return and
--      v_hazard_chain for an arm that has cleared p_roi_min_arrivals, and
--      publishes v_conv_p_eff for the index block below to use in place of
--      v_p_return * v_p_alive.
--
-- The signature grows by the four tunables, so the seventeen-argument form is
-- DROPPED first: two candidate signatures for one name is how a caller silently
-- keeps talking to the old policy.
--
-- ORIGINAL HEADER FOLLOWS.
-- warm_due_claim (202608100005:786-1648) + the shared-parent pre-pass, the
-- group''s combined return probability and the deferral. Every existing gate,
-- the lambda dual, the hazard read side, the envelope gate, the beta cap, the
-- reservation, the claim token and the holdout draw are carried forward
-- unchanged and in the same order; the deferral is inserted ABOVE the first
-- gate and nothing else moves.
--
-- The signature grows, so the sixteen-argument form is DROPPED first: two
-- candidate signatures for one name is how a caller silently keeps talking to
-- the old policy.
-- --------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean
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
    p_beta numeric default 0,
    -- SHARED-PARENT DEDUP (202608100007), default OFF and null-safe for a
    -- caller that predates it. At false the pre-pass does not run, no temp
    -- table is created, every group local stays null and this function is
    -- BEHAVIOURALLY IDENTICAL to 202608100005's -- which is the only claim
    -- a flag on a claim path is allowed to make.
    p_parent_dedup boolean default false,
    -- INDEX CONVERGENCE (202608100010). All four are inert unless BOTH
    -- p_index_enabled and p_hazard_v2 are true, and all four default to the
    -- value the converged simulator run used, so a caller that predates this
    -- migration gets the converged policy rather than an unconfigured one.
    --
    -- F4: pseudo-exposure-hours of organization-prior weight on a customer's
    -- own bucket estimate, down from the 8.0 Phase 1 pinned.
    p_prior_hours numeric default 2.0,
    -- F4: engaged exposure-hours at which that prior is fully retired.
    p_exposure_majority_hours numeric default 48.0,
    -- F5: robust MAD/median above which an arm's gaps are not a clock.
    p_period_max_dispersion numeric default 0.20,
    -- F5: predicted arrivals that may pass unanswered before the phase model is
    -- treated as falsified and the arm is handed back to the index path.
    p_period_miss_limit numeric default 1.5
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
    -- SHARED-PARENT DEDUP (202608100007). All null on every candidate the
    -- pre-pass did not group, which is every candidate at p_parent_dedup =
    -- false, so the reset block below is what keeps a flag-off row from
    -- inheriting the previous candidate's group.
    v_parent_dedup boolean := coalesce(p_parent_dedup, false);
    v_dedup_group uuid;
    v_dedup_role text;
    v_dedup_group_size integer;
    v_dedup_node_digest text;
    v_dedup_node_token_cum integer;
    v_dedup_peer_h numeric[];
    v_dedup_peer numeric;
    v_dedup_peer_product numeric;
    -- DIAGNOSTIC ONLY. The group-combined return probability. It is reported
    -- on the leader's claim payload and gates NOTHING; see the block that
    -- computes it for why.
    v_p_group numeric;
    -- Group ids whose LEADER was actually claimed in this invocation.
    -- Deferral reads only this: a leader denied by any gate, or not yet
    -- visited, leaves its members to proceed exactly as they would have
    -- without the flag. Dedup removes provably redundant pings; it never
    -- denies a ping the old policy would have made.
    v_dedup_leaders jsonb := '{}'::jsonb;
    -- INDEX CONVERGENCE (202608100010). Every one of these is null, zero or
    -- false on a claim that does not reach the convergence block, which is
    -- every claim with either flag off -- the property the v1-equivalence
    -- tests hold this function to.
    --
    -- Coalesced once, so the formula below never repeats a default and the two
    -- spellings cannot drift apart.
    v_prior_hours numeric := coalesce(p_prior_hours, 2.0);
    v_majority_hours numeric := coalesce(p_exposure_majority_hours, 48.0);
    v_period_dispersion numeric := coalesce(p_period_max_dispersion, 0.20);
    v_period_miss_limit numeric := coalesce(p_period_miss_limit, 1.5);
    -- The F2/F3/F7 verdict for the candidate in hand, null when none fired.
    v_conv_gate text;
    -- v1's lifetime-histogram p_return, kept because the cold-start hold has to
    -- put it back after the hazard block has already overwritten v_p_return.
    v_p_return_v1 numeric;
    -- True once the convergence block has scored this candidate. The index
    -- block below reads v_conv_p_eff instead of v_p_return * v_p_alive only
    -- when this is true, so a candidate the block did not reach keeps
    -- 202608100007's arithmetic exactly.
    v_converged boolean;
    v_conv_p_eff numeric;
    v_conv_path text;
    v_conv_exposure numeric;
    v_conv_silence numeric;
    v_conv_i_max numeric;
    v_conv_tau numeric;
    v_conv_k_max integer;
    v_conv_post numeric[];
    v_conv_alpha numeric;
    v_conv_beta numeric;
    v_conv_rate numeric;
    v_conv_beta_silent numeric;
    v_conv_lam_silence numeric;
    v_conv_seg_end numeric[];
    v_conv_seg_rate numeric[];
    v_conv_horizon numeric;
    v_conv_end timestamptz;
    v_conv_cursor timestamptz;
    v_conv_next timestamptz;
    v_conv_guard integer;
    v_conv_chain_expected numeric;
    v_conv_chain_median integer;
    v_conv_still_silent numeric;
    v_conv_k integer;
    v_conv_period numeric[];
    v_conv_wait numeric;
    v_conv_touch timestamptz;
    v_conv_write_mult numeric;
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

    -- ----------------------------------------------------------------------
    -- THE SHARED-PARENT PRE-PASS (202608100007). Runs ONCE per invocation,
    -- before the loop, and only under the flag.
    --
    -- WHAT IT IS. Two customers of the same organization whose requests share
    -- a long system prompt share a PROVIDER CACHE ENTRY for that shared span:
    -- the provider keys on content, not on who sent it. 202608100006's chain
    -- tree is what makes that shareable span nameable -- a chain node is a
    -- content-derived block boundary, and two arms under the same node have
    -- provably identical bytes up to it. One keep-alive on the shared node
    -- therefore keeps the entry warm for EVERY arm beneath it, and the second
    -- ping is not a second unit of warmth, it is a duplicate purchase.
    --
    -- WHAT IT IS NOT. It does not deny anything. A group's leader is claimed
    -- exactly as it would have been; the members are DEFERRED to the horizon
    -- the leader's ping just bought them, and only after the leader was
    -- actually claimed in this same invocation. Every existing gate keeps its
    -- position and its meaning.
    --
    -- WINDOW IDENTITY. The temp table is materialized from the SAME subquery,
    -- in the SAME order, under the SAME limit and inside the SAME transaction
    -- snapshot as the loop below, so a member of one is a member of the other.
    -- Anything the two could still disagree on (a tie at the limit boundary
    -- between rows sharing sort key, next_due_at and prefix_hash) can only
    -- leave a row UNGROUPED, which is the no-op.
    -- ----------------------------------------------------------------------
    if v_parent_dedup then
        -- to_regclass rather than DROP ... IF EXISTS: the latter raises a
        -- NOTICE for a temp schema that does not exist yet, and this runs on
        -- every invocation. The drop is needed at all because two claims can
        -- share one transaction, and ON COMMIT DROP only fires at commit.
        if to_regclass('pg_temp.warm_dedup_window') is not null then
            drop table pg_temp.warm_dedup_window;
        end if;
        create temp table warm_dedup_window on commit drop as
        select candidate.organization_id, candidate.customer_id,
               candidate.provider, candidate.prefix_hash,
               candidate.prefix_tokens, candidate.provider_ttl_seconds,
               candidate.chain_path, candidate.chain_salt_version,
               candidate.hour_histogram, candidate.arrival_count
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
         limit p_claim_limit * 4;

        if to_regclass('pg_temp.warm_dedup_groups') is not null then
            drop table pg_temp.warm_dedup_groups;
        end if;
        create temp table warm_dedup_groups on commit drop as
        with member as (
            -- Only arms 202608100006 actually stamped. A null chain_path is a
            -- request whose chain was absent (no exact tokenizer, no salt,
            -- kill switch, unserialisable element) and it is UNGROUPABLE by
            -- construction rather than by policy.
            select window_row.*,
                   least(1, coalesce(
                       (window_row.hour_histogram ->> v_bucket_key)::numeric, 0)
                       / greatest(window_row.arrival_count, 1)) as h
              from pg_temp.warm_dedup_window window_row
             where window_row.chain_path is not null
               and window_row.chain_salt_version is not null
        ),
        ancestor as (
            -- Every ancestor prefix of the member's path at label-depth >= 4:
            -- two header labels (scheme, salt version), the root, then at
            -- least one BLOCK label. Depth 3 is the seed alone -- shared by
            -- every arm with the same model/ttl/vary and carrying no content
            -- -- so grouping there would group strangers. The member's own
            -- full path is included: two arms at the identical leaf are the
            -- most shareable case there is.
            select member_row.organization_id, member_row.customer_id,
                   member_row.provider, member_row.prefix_hash,
                   member_row.prefix_tokens, member_row.provider_ttl_seconds,
                   member_row.chain_salt_version, member_row.h,
                   label_depth.d,
                   array_to_string(
                       (string_to_array(member_row.chain_path, '.'))[1:label_depth.d],
                       '.') as anc_path
              from member member_row,
                   lateral generate_series(4, coalesce(array_length(
                       string_to_array(member_row.chain_path, '.'), 1), 0))
                       as label_depth(d)
        ),
        qualified as (
            select ancestor_row.*, node.node_digest, node.token_cum
              from ancestor ancestor_row
              join public.warm_prefix_node node
                on node.organization_id = ancestor_row.organization_id
               and node.provider = ancestor_row.provider
               and node.salt_version = ancestor_row.chain_salt_version
               and node.path is not null
               and node.path::text = ancestor_row.anc_path
             where
               -- (b) THE PROVIDER FLOOR, ON A DEFENSIVE BASIS. Below the
               -- provider's minimum the provider caches nothing, so a
               -- keep-alive on this node buys no shared warmth and the group
               -- would be an accounting fiction. Hard-coded on purpose: a
               -- caller-supplied floor is a caller-supplied way to manufacture
               -- groups.
               --
               -- token_cum is NOT counted on the provider's basis. It is
               -- count_tokens() over canonical JSON, which on measured real
               -- prefixes runs 4-12% above the text-only count the provider
               -- actually sees (and far higher on many short elements).
               -- Comparing it to the raw minimum admits nodes whose true
               -- cacheable span is ~915-985 tokens -- under the minimum, so
               -- the provider caches nothing and the group is precisely the
               -- fiction this floor exists to prevent. The 1.25 headroom
               -- covers the measured band with margin; erring high costs a few
               -- marginal groups, erring low costs the whole premise.
               node.token_cum >= ceil(1.25 * case ancestor_row.provider
                   when 'anthropic' then 1024
                   when 'openai' then 1024
                   when 'deepseek' then 64
                   else 1024 end)
               -- (a) AT LEAST TWO window members at or below this node, in the
               -- same (organization, provider, salt version) key space. The
               -- seed binds provider, model, ttl tier, vary headers and the
               -- cache-identity extras, so a shared block label past the root
               -- already implies all of them -- there is nothing left to
               -- re-check, and re-checking it would only invite drift.
               --
               -- The descendant test is a LEFT() prefix comparison, not LIKE:
               -- a collision-escaped label carries an underscore, and '_' is a
               -- LIKE wildcard that would silently over-match sibling paths.
               and (select count(*) from member peer
                     where peer.organization_id = ancestor_row.organization_id
                       and peer.provider = ancestor_row.provider
                       and peer.chain_salt_version = ancestor_row.chain_salt_version
                       and (peer.chain_path = ancestor_row.anc_path
                            or left(peer.chain_path,
                                    length(ancestor_row.anc_path) + 1)
                               = ancestor_row.anc_path || '.')) >= 2
               -- (c) WARMED. Some arm under this node was touched inside this
               -- member's own TTL, so the shared entry the group would ride is
               -- believed to exist. Without this a "group" is two cold arms
               -- agreeing to send one ping between them, which is exactly the
               -- ping the old policy would have made and this one would not.
               and exists (
                    select 1 from public.warm_prefixes warm
                     where warm.organization_id = ancestor_row.organization_id
                       and warm.provider = ancestor_row.provider
                       and warm.chain_path is not null
                       and (warm.chain_path = ancestor_row.anc_path
                            or left(warm.chain_path,
                                    length(ancestor_row.anc_path) + 1)
                               = ancestor_row.anc_path || '.')
                       and warm.last_touch_at >= v_now - make_interval(
                               secs => ancestor_row.provider_ttl_seconds))
        ),
        deepest as (
            -- V(m): the DEEPEST qualifying ancestor. Deeper means more shared
            -- bytes, so the group that shares the most is the group that
            -- forms.
            select distinct on (organization_id, customer_id, provider, prefix_hash)
                   organization_id, customer_id, provider, prefix_hash,
                   chain_salt_version, prefix_tokens, h, anc_path,
                   node_digest, token_cum
              from qualified
             order by organization_id, customer_id, provider, prefix_hash, d desc
        ),
        grouped as (
            select deepest_row.*,
                   count(*) over group_window as group_size,
                   -- Leader = the CHEAPEST member. The whole group rides one
                   -- write, so buy the smallest one; prefix_hash breaks ties
                   -- deterministically.
                   row_number() over (partition by deepest_row.organization_id,
                                      deepest_row.provider,
                                      deepest_row.chain_salt_version,
                                      deepest_row.anc_path
                                      order by deepest_row.prefix_tokens asc,
                                               deepest_row.prefix_hash asc)
                       as leader_rank,
                   -- Hazard membership is capped at 64 members ordered by
                   -- prefix_hash. Members past the cap still DEFER (deferral
                   -- is a correctness property of the shared entry) but
                   -- contribute no probability mass, which can only understate
                   -- the group's return probability. Conservative in the
                   -- direction that spends less.
                   row_number() over (partition by deepest_row.organization_id,
                                      deepest_row.provider,
                                      deepest_row.chain_salt_version,
                                      deepest_row.anc_path
                                      order by deepest_row.prefix_hash asc)
                       as hazard_rank
              from deepest deepest_row
            window group_window as (partition by deepest_row.organization_id,
                                    deepest_row.provider,
                                    deepest_row.chain_salt_version,
                                    deepest_row.anc_path)
        ),
        sized as (
            -- A group of one is not a group. Condition (a) counts members at
            -- or below a node; a member can still be alone at its own DEEPEST
            -- node, and a size-1 group must reduce exactly to current
            -- behaviour rather than log a group of one.
            select * from grouped where group_size >= 2
        ),
        identified as (
            select sized_row.*, group_id.dedup_group
              from sized sized_row
              join (select organization_id, provider, chain_salt_version,
                           anc_path, gen_random_uuid() as dedup_group
                      from sized
                     group by organization_id, provider, chain_salt_version,
                              anc_path) group_id
                on group_id.organization_id = sized_row.organization_id
               and group_id.provider = sized_row.provider
               and group_id.chain_salt_version = sized_row.chain_salt_version
               and group_id.anc_path = sized_row.anc_path
        )
        select identified_row.organization_id, identified_row.customer_id,
               identified_row.provider, identified_row.prefix_hash,
               identified_row.dedup_group,
               case when identified_row.leader_rank = 1
                    then 'leader' else 'deferred' end as dedup_role,
               identified_row.group_size::integer as dedup_group_size,
               identified_row.node_digest as dedup_node_digest,
               identified_row.token_cum as dedup_node_token_cum,
               -- The peers' survival factors (1 - h_i), carried as an ARRAY
               -- rather than a product: the leader's combined hazard is
               -- multiplied out in numeric inside the loop, so the value the
               -- ROI gate sees is exact rather than exp(sum(ln)))-approximate.
               case when identified_row.leader_rank = 1 then (
                    select array_agg(1 - peer.h order by peer.prefix_hash)
                      from identified peer
                     where peer.organization_id = identified_row.organization_id
                       and peer.provider = identified_row.provider
                       and peer.chain_salt_version = identified_row.chain_salt_version
                       and peer.anc_path = identified_row.anc_path
                       and peer.leader_rank > 1
                       and peer.hazard_rank <= 64) end as dedup_peer_h
          from identified identified_row;
    end if;

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
        v_dedup_group := null;
        v_dedup_role := null;
        v_dedup_group_size := null;
        v_dedup_node_digest := null;
        v_dedup_node_token_cum := null;
        v_dedup_peer_h := null;
        v_p_group := null;
        if v_parent_dedup then
            select groups.dedup_group, groups.dedup_role,
                   groups.dedup_group_size, groups.dedup_node_digest,
                   groups.dedup_node_token_cum, groups.dedup_peer_h
              into v_dedup_group, v_dedup_role, v_dedup_group_size,
                   v_dedup_node_digest, v_dedup_node_token_cum,
                   v_dedup_peer_h
              from pg_temp.warm_dedup_groups groups
             where groups.organization_id = v_row.organization_id
               and groups.customer_id = v_row.customer_id
               and groups.provider = v_row.provider
               and groups.prefix_hash = v_row.prefix_hash;
        end if;
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
        -- 202608100010: logged on every row now, so it must be reset with
        -- the rest of them or a gated candidate inherits the previous
        -- candidate's effective probability.
        v_p_eff := null;
        v_lambda := null;

        v_p_return := least(1, coalesce(
            (v_row.hour_histogram ->> v_bucket_key)::numeric, 0)
            / greatest(v_row.arrival_count, 1));
        v_break_even := coalesce(
            (p_roi_break_even_by_provider ->> v_row.provider)::numeric,
            p_roi_break_even_p);
        -- v1's answer, kept before anything can overwrite it. The cold-start
        -- hold below has to be able to put it back.
        v_p_return_v1 := v_p_return;
        -- Both are recomputed identically below; hoisted so a gated candidate's
        -- decision row carries the same floor and the same reservation a scored
        -- one would have, which is what makes the gates auditable against the
        -- rows beside them.
        v_floor := case when v_row.arrival_count < p_roi_min_arrivals
            then p_roi_min_p else v_break_even end;
        v_reserve := greatest(
            v_row.ping_reserve_usd,
            round(p_reserve_usd_per_mtok * v_row.prefix_tokens / 1000000.0, 10));
        v_converged := false;
        v_conv_p_eff := null;
        v_conv_path := null;
        -- Hoisted from the hazard block below, which sets it again identically.
        -- The convergence gates need it, and a frozen pair is scored at flat
        -- priors, which means none of them may fire.
        v_frozen := coalesce(v_row.warm_frozen, false);

        -- ==================================================================
        -- THE CONVERGENCE GATES (202608100010). F2, F3 and F7.
        --
        -- All three are pure functions of the candidate row and the provider
        -- constants -- no model, no state row, no hazard walk -- and all three
        -- say the same thing: this arm is not worth buying at any probability.
        -- They run before the hazard read for that reason, and they are the
        -- only place in this function where a decision is recorded with no
        -- index attached, which is honest: there is no index, because nothing
        -- was scored.
        --
        -- Inside the flag pair and the frozen check, so a claim with either
        -- flag off, or on a frozen pair, cannot reach any of them.
        -- ==================================================================
        v_conv_gate := null;
        if p_index_enabled and p_hazard_v2 and not v_frozen then
            v_conv_write_mult := case
                when v_row.provider = 'anthropic'
                     and v_row.provider_ttl_seconds > 300 then 2.0
                when v_row.provider = 'anthropic' then 1.25
                else 1.0 end;
            v_conv_i_max := public.warm_conv_i_max(
                v_row.provider_ttl_seconds, v_conv_write_mult, v_break_even);
            v_conv_silence := greatest(0, extract(epoch from
                (v_now - v_row.last_seen_at)));
            if v_row.ewma_interarrival_s is not null
               and v_row.ewma_interarrival_s < v_row.provider_ttl_seconds then
                -- F2. The customer arrives more often than the entry expires,
                -- so they refresh it themselves for free and every keep-alive
                -- we buy is a duplicate of a write their own traffic already
                -- made. A GATE, not a multiplier: it does not wait for the
                -- control arm to produce an organic baseline, because the
                -- region it removes is catastrophic rather than uncertain.
                v_conv_gate := 'skipped_organic';
            elsif v_conv_silence > v_conv_i_max then
                -- F3. Past I_max the chain of keep-alives needed to bridge this
                -- silence costs more than the single write-priced miss it would
                -- prevent, for every p <= 1. Letting the entry lapse and
                -- re-warming on the next real arrival is arithmetically better,
                -- so this is not a judgement about the customer.
                v_conv_gate := 'skipped_abandon';
            else
                -- F7. The provider's cache is keyed by (organization, prefix),
                -- not by customer, so every one of an organization's arms on
                -- this prefix_hash is bidding to keep ONE entry warm.
                -- warm_prefixes.last_touch_at is stamped by warm_prefix_observe
                -- on every arrival and by warm_ping_settle on every warmed
                -- ping, so its max over the sibling rows IS the moment that
                -- entry was last refreshed by anybody. If the warmth already
                -- bought outlasts the point where this arm next gets to decide,
                -- buying it again adds nothing.
                --
                -- Without this, N customers on one organization-wide system
                -- prompt each pay a full keep-alive chain for the single entry
                -- all of them read, N-1 of those chains are pure waste, and the
                -- periodicity fast-path makes all N fire in lockstep.
                -- OTHER arms only. An arm can never skip on warmth it bought
                -- itself: its own touch and its own next_due_at move together
                -- (both writers set due = touch + ttl - safety), so at the
                -- moment it becomes due its own warmth has exactly the safety
                -- margin left and the comparison below is an equality, not a
                -- strict inequality. Excluding self says that out loud instead
                -- of leaving it to arithmetic -- and keeps the gate from firing
                -- on a row whose due time and touch time were set independently,
                -- which no writer produces but which every hand-built fixture
                -- does.
                select max(coalesce(sibling.last_touch_at, sibling.last_seen_at))
                  into v_conv_touch
                  from public.warm_prefixes sibling
                 where sibling.organization_id = v_row.organization_id
                   and sibling.provider = v_row.provider
                   and sibling.prefix_hash = v_row.prefix_hash
                   and sibling.customer_id <> v_row.customer_id;
                if v_conv_touch is not null
                   and extract(epoch from (v_conv_touch - v_now))
                       + v_row.provider_ttl_seconds > p_safety_margin_seconds then
                    v_conv_gate := 'skipped_shared_warm';
                end if;
            end if;
        end if;
        if v_conv_gate is not null then
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, v_conv_gate, v_p_return, v_floor, v_reserve,
                v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                null, null, null, null, null, null, null, null,
                p_p_eff => v_p_eff);
            continue;
        end if;

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

            -- ==============================================================
            -- INDEX CONVERGENCE (202608100010). F1, F4, F5 and F6, plus the
            -- cold-start hold. Everything above this point is 202608100007's
            -- and stays computed, because a claim that does not reach here
            -- must be able to use it unchanged.
            -- ==============================================================
            if p_index_enabled and v_row.arrival_count < p_roi_min_arrivals then
                -- COLD START HOLDS v1, and holds it for as long as v1 itself
                -- admits it has no evidence. Phase 1 took its flat-prior branch
                -- only when the state ROW was absent -- for exactly one arrival
                -- -- and then scored from a posterior built on a single
                -- observation, which is what left the learned policy earning
                -- nothing over a customer's first session while v1 was already
                -- bridging their gaps. Putting back all three v1 values, not
                -- just p_return, is what makes the decision byte-identical.
                v_p_return := v_p_return_v1;
                v_hazard_p_alive := null;
                v_hazard_chain := 0;
            elsif p_index_enabled then
                v_converged := true;
                v_conv_tau := greatest(1,
                    v_row.provider_ttl_seconds - p_safety_margin_seconds);
                v_conv_path := 'index';

                -- ---- F5: the clock path, before any renewal arithmetic ----
                -- A scheduled agent is not a renewal process with an uncertain
                -- rate; it is a clock, and the right forecast is its phase.
                -- With a dozen arrivals of evidence the negative-binomial tail
                -- is wide enough that n_chain exceeds the break-even chain
                -- length and the policy abandons MID-CHAIN -- buying the first
                -- keep-alive and then not the second, which is the one spend
                -- pattern strictly worse than doing nothing.
                v_conv_period := public.warm_conv_period(
                    v_state.recent_gaps, v_period_dispersion);
                if v_conv_period is not null
                   -- A period inside the TTL is a self-refreshing arm, which F2
                   -- has already skipped outright.
                   and v_conv_period[1] > v_row.provider_ttl_seconds
                   -- Two predicted arrivals have not passed unanswered, so the
                   -- clock model still stands.
                   and v_conv_silence <= v_period_miss_limit * v_conv_period[1] then
                    -- Phase: the next multiple of the period after the last
                    -- real arrival, and the keep-alives that actually stand
                    -- between now and it. Same index arithmetic every other
                    -- candidate faces -- it just gets an honest chain length
                    -- instead of one inflated by rate uncertainty the
                    -- customer's own regularity has already resolved.
                    v_conv_wait := (floor(v_conv_silence / v_conv_period[1]) + 1)
                                   * v_conv_period[1] - v_conv_silence;
                    if v_conv_wait <= v_conv_i_max then
                        v_conv_path := 'periodic';
                        v_p_return := v_conv_period[2];
                        v_hazard_chain := greatest(0,
                            ceil(v_conv_wait / v_conv_tau - 0.000000001)::integer - 1);
                        v_conv_p_eff := v_p_return * v_hazard_p_alive;
                    end if;
                end if;

                if v_conv_path = 'index' then
                    -- ---- F4: the posterior for the bucket in hand ----------
                    select coalesce(sum(entry.value::numeric), 0)
                      into v_conv_exposure
                      from jsonb_each_text(
                          coalesce(v_state.hazard_e, '{}'::jsonb)) as entry;
                    v_conv_post := public.warm_conv_posterior(
                        coalesce((v_state.hazard_n ->> v_bucket_key)::numeric, 0),
                        coalesce((v_state.hazard_e ->> v_bucket_key)::numeric, 0),
                        coalesce((v_org_state.hazard_n ->> v_bucket_key)::numeric, 0),
                        coalesce((v_org_state.hazard_e ->> v_bucket_key)::numeric, 0),
                        v_conv_exposure, v_prior_hours, v_majority_hours);
                    v_conv_alpha := v_conv_post[1];
                    v_conv_beta := v_conv_post[2];
                    v_conv_rate := case when v_conv_beta > 0
                        then v_conv_alpha / v_conv_beta else 0 end;

                    -- ---- F1: fold the elapsed silence into the posterior ----
                    -- lam_silence is the arrivals this customer's own history
                    -- says should have happened while they were quiet; dividing
                    -- by the current rate expresses it in this bucket's
                    -- exposure-hours so it can be added to beta. That is the
                    -- whole of "multiply the bucket hazards along the elapsed
                    -- silence path", carried in log space where it is a sum.
                    --
                    -- The hour boundary is computed on the epoch rather than
                    -- with date_trunc, which would truncate in the session's
                    -- TimeZone and put a half-hour-offset replica on different
                    -- buckets from every other one.
                    v_conv_lam_silence := 0;
                    v_conv_cursor := v_now - make_interval(secs => v_conv_silence);
                    v_conv_guard := 0;
                    while v_conv_cursor < v_now and v_conv_guard < 240 loop
                        v_conv_next := least(v_now, to_timestamp(
                            floor(extract(epoch from v_conv_cursor) / 3600.0) * 3600.0
                            + 3600.0));
                        v_conv_post := public.warm_conv_posterior(
                            coalesce((v_state.hazard_n ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_state.hazard_e ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_org_state.hazard_n ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_org_state.hazard_e ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            v_conv_exposure, v_prior_hours, v_majority_hours);
                        v_conv_lam_silence := v_conv_lam_silence
                            + (case when v_conv_post[2] > 0
                                    then v_conv_post[1] / v_conv_post[2] else 0 end)
                              * extract(epoch from (v_conv_next - v_conv_cursor)) / 3600.0;
                        v_conv_cursor := v_conv_next;
                        v_conv_guard := v_conv_guard + 1;
                    end loop;
                    v_conv_beta_silent := v_conv_beta
                        + case when v_conv_rate > 0.000000000001
                               then v_conv_lam_silence / v_conv_rate else 0 end;

                    -- ---- the forward horizon, kept PIECEWISE ---------------
                    -- One walk over the whole span the chain could commit to,
                    -- as hour-of-week segments. Flattening it to an average
                    -- rate is wrong in exactly the case that matters: a horizon
                    -- running off the end of a customer's active window would
                    -- average their real rate with buckets they have never been
                    -- seen in, and the near-window probability would collapse
                    -- for no reason.
                    v_conv_k_max := greatest(0,
                        floor(v_conv_i_max / v_conv_tau)::integer);
                    v_conv_horizon := greatest(v_row.provider_ttl_seconds,
                                               v_conv_k_max * v_conv_tau);
                    v_conv_end := v_now + make_interval(secs => v_conv_horizon);
                    v_conv_seg_end := array[]::numeric[];
                    v_conv_seg_rate := array[]::numeric[];
                    v_conv_cursor := v_now;
                    v_conv_guard := 0;
                    while v_conv_cursor < v_conv_end and v_conv_guard < 240 loop
                        v_conv_next := least(v_conv_end, to_timestamp(
                            floor(extract(epoch from v_conv_cursor) / 3600.0) * 3600.0
                            + 3600.0));
                        v_conv_post := public.warm_conv_posterior(
                            coalesce((v_state.hazard_n ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_state.hazard_e ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_org_state.hazard_n ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            coalesce((v_org_state.hazard_e ->> public.warm_hour_of_week(v_conv_cursor))::numeric, 0),
                            v_conv_exposure, v_prior_hours, v_majority_hours);
                        v_conv_seg_end := v_conv_seg_end
                            || extract(epoch from (v_conv_next - v_now))::numeric;
                        v_conv_seg_rate := v_conv_seg_rate
                            || (case when v_conv_post[2] > 0
                                     then v_conv_post[1] / v_conv_post[2] else 0 end);
                        v_conv_cursor := v_conv_next;
                        v_conv_guard := v_conv_guard + 1;
                    end loop;

                    -- F1 proper: the single-TTL-window CONDITIONAL return
                    -- probability. This is what the ROI floor gates on and what
                    -- warm_decision_log.p_return records, so a row is never
                    -- scored on one number and logged with another.
                    v_p_return := least(1, greatest(0, 1 - public.warm_conv_survival(
                        v_conv_alpha, v_conv_beta_silent,
                        public.warm_conv_lambda_to(v_conv_seg_end, v_conv_seg_rate,
                                                   v_row.provider_ttl_seconds),
                        v_conv_rate)));

                    -- F3: n_chain from the survival curve of that same
                    -- conditional distribution -- the EXPECTED number of
                    -- further keep-alives, sum_k P(still silent after k
                    -- windows), truncated at I_max. It RISES as a session goes
                    -- quiet, where E_gap - chain_age fell toward zero exactly
                    -- as the session died, which was an inverted incentive.
                    --
                    -- F6: and it is then capped at the MEDIAN of the same
                    -- curve. The mean is dragged up by improbable long silences
                    -- that the I_max gate above would have abandoned long
                    -- before reaching; the median is the number of keep-alives
                    -- the typical session actually needs. Past the median
                    -- horizon the mean still governs, so this cannot resurrect
                    -- a dead arm -- and the gate above guarantees we are inside
                    -- a live one.
                    v_conv_chain_expected := 0;
                    v_conv_chain_median := 0;
                    for v_conv_k in 1 .. v_conv_k_max loop
                        v_conv_still_silent := public.warm_conv_survival(
                            v_conv_alpha, v_conv_beta_silent,
                            public.warm_conv_lambda_to(
                                v_conv_seg_end, v_conv_seg_rate,
                                v_conv_k * v_conv_tau),
                            v_conv_rate);
                        v_conv_chain_expected := v_conv_chain_expected
                                                 + v_conv_still_silent;
                        if v_conv_still_silent >= 0.5 then
                            -- The last window the typical session is still
                            -- waiting through.
                            v_conv_chain_median := v_conv_k;
                        end if;
                    end loop;
                    v_hazard_chain := least(
                        public.warm_conv_round_half_even(v_conv_chain_expected),
                        v_conv_chain_median);

                    -- The probability the index is charged for is measured over
                    -- the horizon the chain actually commits money to -- the
                    -- 1 + n_chain windows being bought -- rather than over one
                    -- TTL window while being charged for all of them. The
                    -- simulator holds the strict single-window reading as a
                    -- separate arm so the difference is a measured number
                    -- rather than an argument; this is the arm that converged.
                    v_conv_p_eff := least(1, greatest(0, 1 - public.warm_conv_survival(
                        v_conv_alpha, v_conv_beta_silent,
                        public.warm_conv_lambda_to(
                            v_conv_seg_end, v_conv_seg_rate,
                            (1 + v_hazard_chain) * v_conv_tau),
                        v_conv_rate))) * v_hazard_p_alive;
                end if;
            end if;
        end if;

        -- THE GROUP'S RETURN PROBABILITY (202608100007), DIAGNOSTIC ONLY.
        -- The leader's ping is arguably worth a return by ANY member of
        -- the group, because any member's arrival reads the shared entry
        -- the ping refreshed:
        --
        --   p_group = 1 - (1 - p_own) * prod_{i != leader} (1 - h_i)
        --
        -- h_i is member i's own lifetime-histogram return probability for
        -- this hour bucket, read off the window row -- no extra query and
        -- no model.
        --
        -- IT DOES NOT GATE. Substituting p_group for p_own before the ROI
        -- floor lets the flag BUY a ping: two arms at p_own = 0.06 under a
        -- 0.11 floor are both skipped_roi with the flag off, and combine to
        -- p_group = 0.1164 -- over the floor -- with it on. That strictly
        -- INCREASES warm spend, contradicting this migration's own promise
        -- that the single charge it makes is the SAME charge the leader
        -- would have made alone. Independence across members is an
        -- approximation and a generous one; it is not a licence to spend.
        -- Every gate below therefore runs on p_own, exactly as it does with
        -- the flag off, and dedup can only ever REMOVE a redundant ping.
        -- p_group rides out on the leader's claim payload for the analysis
        -- that would justify promoting it to a gate; nothing reads it yet.
        --
        -- At group size 1 the product is empty and p_group = p_own exactly.
        if v_dedup_role = 'leader' and v_dedup_peer_h is not null then
            v_dedup_peer_product := 1;
            foreach v_dedup_peer in array v_dedup_peer_h loop
                v_dedup_peer_product := v_dedup_peer_product * v_dedup_peer;
            end loop;
            v_p_group := least(1, greatest(0,
                1 - (1 - v_p_return) * v_dedup_peer_product));
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
            -- 202608100010: when the convergence block scored this candidate,
            -- the probability the index is charged for spans the 1 + n_chain
            -- windows the chain commits to, and it already carries P(alive).
            -- Otherwise this is 202608100007's line, unchanged.
            v_p_eff := case when v_converged
                then v_conv_p_eff * v_organic
                else v_p_return * v_p_alive * v_organic end;
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
                -- GROUPED: the value a return realizes is the value of the
                -- SHARED span, which is the group node's token_cum, not the
                -- leader's whole prefix. Scaled down; v_reserve stays the
                -- leader's full-prefix worst-case write. Conservative in
                -- both directions: less claimed value, unchanged claimed
                -- cost. v_index keeps its ratio form (p_eff/b - 1 -
                -- n_chain), in which the prefix's dollar value cancels, so
                -- this is a LOGGED quantity and not a second ranking.
                if v_dedup_node_token_cum is not null then
                    v_v_hit := round(v_v_hit * least(1,
                        v_dedup_node_token_cum::numeric
                        / greatest(v_row.prefix_tokens, 1)), 10);
                end if;
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

        -- DEFERRAL (202608100007). Placed ABOVE every gate, because a
        -- deferred member must move no ledger, no envelope, no cap
        -- accounting and no ping counter -- the leader already bought the
        -- warmth, and charging twice for one write is the whole thing this
        -- removes. next_due_at advances by the horizon that write bought,
        -- the same horizon warm_ping_settle would have set, so the member
        -- comes back when the SHARED entry is due rather than immediately.
        --
        -- Gated on the leader having been CLAIMED in this invocation. A
        -- leader denied by any gate, or simply not yet visited (the loop
        -- order is the index order, not the group order), leaves its
        -- members to run every gate exactly as they would have without the
        -- flag. Dedup only ever removes a provably redundant ping.
        --
        -- Also gated on COVERAGE. The leader's ping warms the leader's own
        -- prefix and nothing past the fork, so a deferred member keeps warmth
        -- over token_cum tokens and loses everything beyond it. The leader is
        -- chosen by prefix_tokens ASC -- the cheapest ping available -- which
        -- maximizes exactly that loss: an arm sharing 4k of a 200k-token
        -- prefix would otherwise be deferred to save the cheapest ping in the
        -- group. Requiring the shared span to be at least a QUARTER of the
        -- member's prefix keeps the deferral a substitution rather than a
        -- downgrade.
        -- A member under the ratio simply runs every gate as it would with
        -- the flag off, which still cannot spend more than the flag-off
        -- policy.
        if v_dedup_role = 'deferred'
           and v_dedup_leaders ? (v_dedup_group::text)
           and coalesce(v_dedup_node_token_cum, 0)
               >= 0.25 * greatest(v_row.prefix_tokens, 1) then
            update public.warm_prefixes prefix
               set next_due_at = v_now + make_interval(secs => greatest(
                       60, prefix.provider_ttl_seconds - p_safety_margin_seconds))
             where prefix.organization_id = v_row.organization_id
               and prefix.customer_id = v_row.customer_id
               and prefix.provider = v_row.provider
               and prefix.prefix_hash = v_row.prefix_hash;
            perform public.warm_decision_record(
                v_row.organization_id, v_row.customer_id, v_row.provider,
                v_row.prefix_hash, 'dedup_deferred', v_p_return, v_floor,
                v_reserve, v_row.prefix_tokens, v_row.ewma_interarrival_s,
                v_row.arrival_count, v_pings_today, v_token, null, null,
                v_index, v_index_density, v_v_hit, v_chain_cost, v_c_belief,
                v_p_alive, v_organic, null,
                v_dedup_group, 'deferred', v_dedup_group_size,
                v_dedup_node_digest,
                p_p_eff => v_p_eff);
            continue;
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
                v_p_alive, v_organic, null,
                p_p_eff => v_p_eff);
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
                    v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
                v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
                v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
                v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
                    v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
                    v_p_alive, v_organic, v_lambda,
                p_p_eff => v_p_eff);
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
        -- The leader is claimed: from here its members may defer. Written
        -- AFTER the reservation, so a leader that never reserved never
        -- authorizes a deferral.
        if v_dedup_role = 'leader' then
            v_dedup_leaders := jsonb_set(
                v_dedup_leaders, array[v_dedup_group::text], to_jsonb(true));
        end if;
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
            v_p_alive, v_organic, v_lambda,
            case when v_dedup_role = 'leader' then v_dedup_group end,
            case when v_dedup_role = 'leader' then 'leader' end,
            case when v_dedup_role = 'leader' then v_dedup_group_size end,
            case when v_dedup_role = 'leader' then v_dedup_node_digest end,
                p_p_eff => v_p_eff);
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
        )
        -- Additive and ONLY on a leader, so a flag-off claimed row is
        -- byte-identical to 202608100005's. The worker ignores unknown
        -- keys; nothing downstream reads these yet.
        || case when v_dedup_role = 'leader'
                then jsonb_build_object(
                    'dedup_group', v_dedup_group,
                    'dedup_group_size', v_dedup_group_size,
                    -- Reported, never gated. See the p_group block above.
                    'dedup_p_group', v_p_group)
                else '{}'::jsonb end;
    end loop;
    return;
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean,
    numeric, numeric, numeric, numeric
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean,
    numeric, numeric, numeric, numeric
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric, boolean, numeric, boolean,
    numeric, numeric, numeric, numeric
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores to public.warm_decision_log. Under p_index_enabled candidates are ranked by the dimensionless dollar index itself and paced by the lambda dual. Under p_hazard_v2 p_return is the decayed hierarchical hazard from public.warm_customer_state rather than the lifetime histogram, P(alive) and the keep-alive chain enter the index, and (only when p_index_enabled, which is what carries P(alive) into a decision) the stop-loss predicate is bypassed in favour of P(alive); a customer with no state row (or a suppressed one) falls back to the v1 histogram at flat priors, which is provably the v1 policy. A candidate whose reservation would carry its customer past that customer''s public.warm_customer_budget envelope for the month is denied as ''envelope_denied''; a customer with no envelope row is unconstrained, which is what keeps a deployment with no envelopes identical to 202608100003. Under p_beta > 0 a candidate is denied as ''beta_denied'' when the organization''s trailing 28-day warm spend plus this reservation would exceed p_beta times the control-verified savings in public.warm_control_savings_daily; at the default p_beta = 0 the cap does not run. A pair frozen in public.warm_org_mode is scored at FLAT PRIORS -- no hazard read, P(alive) 1, no chain, stop-loss predicate re-applied -- which is provably the v1 policy, not a second policy. Under p_parent_dedup, candidates whose 202608100006 chain paths share a warmed ancestor node above the provider''s cache floor are grouped: the cheapest member is claimed once, scored on its OWN return probability exactly as it would be with the flag off (the group''s combined return probability is reported on the claim payload as ''dedup_p_group'' and gates nothing, so the flag can never buy a ping the flag-off policy would have skipped), and every other member of the group is deferred as ''dedup_deferred'' to the horizon that one ping bought -- reserving nothing, spending nothing and consuming no ping cap. A member whose leader was denied or not yet reached proceeds through every gate unchanged, so the flag can only ever remove a redundant ping. At the default p_parent_dedup = false the pre-pass does not run at all. Under BOTH p_index_enabled and p_hazard_v2 (202608100010) the scorer is the converged learned-index-fixed policy of docs/INDEX_FIX_ROUND.md: p_return is conditioned on the arm''s elapsed silence rather than read off the unconditional hourly hazard, n_chain is the expected number of further keep-alives from that same conditional survival curve capped at its median, an arm whose EWMA inter-arrival is under the provider TTL is skipped as ''skipped_organic'', one silent longer than I_max = ttl*(w/f - 1) as ''skipped_abandon'', and one whose organization already holds the shared provider entry warm past its own next decision point as ''skipped_shared_warm''. An arm whose recent inter-arrival gaps cluster tightly enough to be a clock is scheduled off its phase instead, and falls back to the index path once p_period_miss_limit predicted arrivals have passed unanswered. An arm below p_roi_min_arrivals is scored exactly as v1 scores it even when a state row exists, so the cold-start decision is byte-identical to the flag-off one.';

-- ---------------------------------------------------------------------------
-- THE EXPORTS (202608100008:1465-1895 and :1902-2180) + recent_gaps. The
-- projections are enumerated, so a column nobody names is silently absent from
-- every data right -- which is the entire reason this migration touches them.
-- Every other record type, predicate and ordering is carried forward verbatim.
-- ---------------------------------------------------------------------------

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
                -- 202608100010. The probability the index was charged for,
                -- which since F1 is not the same number as p_return.
                'p_eff', entry.p_eff,
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
                -- 202608100010. The twelve inter-arrival gaps the periodicity
                -- fast-path reasons over: behavioural timing about this
                -- subject, exported in the same record as the hour-of-week
                -- mass it sits beside.
                'recent_gaps', pg_catalog.to_jsonb(state.recent_gaps),
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


    -- The attribution statement (202608100008). This is the answer to "who did
    -- warming actually spend money on", and an organization is entitled to its
    -- own copy of it. Dollars and counts only: no node digest, no path and
    -- nothing else derived from a prompt appears here or anywhere else in an
    -- export. MEASURED-ONLY -- none of these numbers was ever billed.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_attribution_daily',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', statement.organization_id,
                'provider', statement.provider,
                'day', statement.day,
                'customer_ref', statement.customer_ref,
                'erased', statement.customer_ref like 'erased:%',
                'nodes_read', statement.nodes_read,
                'nodes_shared', statement.nodes_shared,
                'avg_split_denominator', statement.avg_split_denominator,
                'warming_cost_share_usd', statement.warming_cost_share_usd,
                'warm_attributed_savings_usd', statement.warm_attributed_savings_usd,
                'verified_savings_usd', statement.verified_savings_usd,
                'net_usd', statement.net_usd,
                'computed_at', statement.computed_at
            )
        )
          from public.warm_attribution_daily statement
         where statement.organization_id = p_organization_id
         order by statement.day, statement.provider, statement.customer_ref;

    -- The footer that says what the statement above did NOT allocate. An
    -- organization reading its attribution is entitled to the whole dollar,
    -- including the part of it Brevitas spent on windows nobody used.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_attribution_residual',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', statement.organization_id,
                'provider', statement.provider,
                'day', statement.day,
                'total_warm_spend_usd', statement.total_warm_spend_usd,
                'allocated_usd', statement.allocated_usd,
                'unallocated_speculative_usd', statement.unallocated_speculative_usd,
                'redundancy_usd', statement.redundancy_usd,
                'unpriced_usd', statement.unpriced_usd,
                'reward_join_delta_usd', statement.reward_join_delta_usd,
                'computed_at', statement.computed_at
            )
        )
          from public.warm_attribution_residual statement
         where statement.organization_id = p_organization_id
         order by statement.day, statement.provider;

    -- public.warm_prefix_cost_miss is deliberately NOT exported. Every row in
    -- it is keyed on a node digest -- a pseudonymous identifier of a block of
    -- somebody's prefix -- and the dollars on it were never charged to this
    -- organization or to anybody in it. Exporting it would hand out tree
    -- structure in exchange for no accounting the organization is owed.

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

    -- The prefix tree (202608100006), as COUNTS AND NOTHING ELSE. A node digest
    -- is a salted, content-derived identifier of one block of one customer's
    -- prompt prefix; exporting the digests or the paths would hand back a
    -- structural fingerprint of the tenant's own traffic in a form that can be
    -- correlated across an export boundary. The counts answer the question an
    -- export is for -- how much structure is held about us -- without being
    -- that fingerprint.
    return query
        select pg_catalog.jsonb_build_object(
            'record_type', 'warming_prefix_tree',
            'data', pg_catalog.jsonb_build_object(
                'organization_id', p_organization_id,
                'provider', counts.provider,
                'salt_version', counts.salt_version,
                'node_count', counts.node_count,
                'edge_count', counts.edge_count,
                'max_depth', counts.max_depth
            )
        )
          from (
            select node.provider,
                   node.salt_version,
                   count(*) as node_count,
                   (select count(*) from public.warm_prefix_edge edge
                     where edge.organization_id = node.organization_id
                       and edge.provider = node.provider
                       and edge.salt_version = node.salt_version) as edge_count,
                   max(node.depth) as max_depth
              from public.warm_prefix_node node
             where node.organization_id = p_organization_id
             group by node.organization_id, node.provider, node.salt_version
          ) counts
         order by counts.provider, counts.salt_version;
    -- public.warm_canary_probes and public.warm_canary_ledger are absent for
    -- the same reason public.warm_ttl_observations is: Plane G carries no
    -- tenant key, so there is no row in either table that belongs to this
    -- organization to export.
end;
$function$;

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
                    -- 202608100010. The probability the index was charged for,
                    -- which since F1 is not the same number as p_return.
                    'p_eff', entry.p_eff,
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
                    -- 202608100010. The twelve inter-arrival gaps the periodicity
                    -- fast-path reasons over: behavioural timing about this
                    -- subject, exported in the same record as the hour-of-week
                    -- mass it sits beside.
                    'recent_gaps', pg_catalog.to_jsonb(state.recent_gaps),
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

        -- The subject's own spend envelopes (202608100004). LIVE rows only: a
        -- tombstoned row's customer_ref is a random token that can never equal
        -- a subject id, so it is excluded by construction rather than by a
        -- filter somebody could drop -- and an erased subject has no claim on
        -- the organization's residual accounting anyway.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_customer_budget',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', budget.organization_id,
                    'provider', budget.provider,
                    'period_start', budget.period_start,
                    'customer_ref', budget.customer_ref,
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
               and budget.customer_ref = v_request.subject_id::text
             order by budget.period_start, budget.provider;

        -- The subject's own attribution rows (202608100008): the share of
        -- warming spend their arrivals pulled toward them and the savings
        -- measured against it. LIVE rows only, and by construction rather than
        -- by a filter somebody could drop: a tombstoned customer_ref is a
        -- random token that can never equal a subject id. Counts and dollars;
        -- no node digest and no path.
        return query
            select pg_catalog.jsonb_build_object(
                'record_type', 'warming_attribution_daily',
                'data', pg_catalog.jsonb_build_object(
                    'organization_id', statement.organization_id,
                    'provider', statement.provider,
                    'day', statement.day,
                    'customer_ref', statement.customer_ref,
                    'nodes_read', statement.nodes_read,
                    'nodes_shared', statement.nodes_shared,
                    'avg_split_denominator', statement.avg_split_denominator,
                    'warming_cost_share_usd', statement.warming_cost_share_usd,
                    'warm_attributed_savings_usd', statement.warm_attributed_savings_usd,
                    'verified_savings_usd', statement.verified_savings_usd,
                    'net_usd', statement.net_usd,
                    'computed_at', statement.computed_at
                )
            )
              from public.warm_attribution_daily statement
             where statement.organization_id = p_organization_id
               and statement.customer_ref = v_request.subject_id::text
             order by statement.day, statement.provider;
    end if;
end;
$function$;

revoke all on function public.compliance_export_tenant(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_tenant(uuid, uuid, text)
    to service_role;
revoke all on function public.compliance_export_subject(uuid, uuid, text)
    from public, anon, authenticated;
grant execute on function public.compliance_export_subject(uuid, uuid, text)
    to service_role;

commit;
