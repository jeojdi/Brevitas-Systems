-- Phase 1 learned warming, part 1: the dollar index, claim ordering and the
-- lambda pacing dual.
--
-- THE GAP. warm_due_claim scores every due candidate against a scalar ROI floor
-- and then serves them first-come-first-served by next_due_at. Two candidates
-- that both clear the floor are indistinguishable to it, even when one returns
-- ten times the expected value per reserved dollar. Under a binding daily
-- budget that is not a neutral policy: FIFO spends the budget on whichever rows
-- happened to come due first, and the rows that would have paid for themselves
-- are denied by a ceiling the cheap rows already consumed.
--
-- THE INDEX. Per candidate, with prefix base dollar value V, provider
-- read-cost fraction f, and break-even return probability b = f/(1-f):
--
--     V_hit    = V * (1 - f)      -- what a return saves if the entry is warm
--     c_belief = V * f            -- what the keep-alive ping costs
--     chain    = n_chain * c_belief
--     index    = [p_eff * V_hit - chain - c_belief] / c_belief
--              = p_eff / b - 1 - n_chain
--
-- The prefix dollar value cancels: V_hit / c_belief = (1 - f)/f = 1/b. What is
-- left is dimensionless and comparable across providers and prefix sizes, and
-- index >= 0 is exactly "this ping pays for itself in expectation".
--
-- WHERE V COMES FROM, AND WHERE IT DOES NOT. V is
-- warm_prefixes.ping_reserve_usd / w -- the observer-priced worst case for one
-- keep-alive (api/server.py prices it against the provider catalog as a full
-- cache write at the model+TTL premium) divided back down by that same write
-- multiplier w, which recovers the prefix's base input dollars. w mirrors
-- api/server.py's ping_rate CASE: anthropic 1h-tier 2.0, anthropic 5m 1.25,
-- automatic-cache providers 1.0. f is api/worker.py WARM_PROVIDER_SPECS'
-- read_cost_fraction, recovered from the per-provider break-even the claim
-- already carries (b = f/(1-f) inverts to f = b/(1+b)), so one number governs
-- both the gate and the dollars.
--
-- V is NOT the ledger reservation. The reservation is max(ping_reserve_usd,
-- p_reserve_usd_per_mtok * tokens) -- an anthropic-calibrated flat floor whose
-- only job is to upper-bound what settle can book against the daily ceiling.
-- Money safety and economics are separate concerns: the floor over-reserves
-- DeepSeek roughly 14x, and pricing the index from it would rank providers by
-- how badly a flat floor missed them and would overstate v_hit_usd 12-100x in
-- the compliance exports that are this organization's audit trail. The
-- reservation is logged unchanged as warm_decision_log.reserve_usd; the
-- economics are logged as v_hit_usd / c_belief_usd / chain_cost_usd; neither
-- is mislabelled as the other.
--
-- THE ORDERING KEY IS THE INDEX ITSELF, not index per reserved dollar. The
-- index already divides by c_belief, so it is already value per belief-dollar;
-- dividing again by the reservation yields value-per-dollar-squared, which
-- under a binding budget systematically prefers small cheap arms to the large
-- valuable ones the budget exists to allocate. index_density is still logged,
-- as a diagnostic, so the two rankings stay comparable in the data.
--
-- WHAT IS NOT HERE. p_alive and the organic-warmth netting multiplier are
-- pinned to 1.0 and n_chain to 0 in this migration; their producers arrive with
-- the customer-state hazard model. Pinned at 1.0 they can only OVERSTATE the
-- index, and the index gates spending, not billing: verified savings and the
-- settlement sweep are untouched, warm spend is still never billable.
--
-- THE PACING DUAL. warm_budget_ledger gains a per-(org, provider, day)
-- multiplier lambda in index units. It rises when the day's spend runs ahead of
-- a linear intraday target and decays toward its floor of 0 when it does not,
-- and the claim denies any candidate whose index falls below it. lambda = 0 IS
-- the provider break-even floor: index = 0 <=> p_eff = f/(1-f). So the dual can
-- only ever be STRICTER than the economic floor, never looser, and the daily
-- budget remains the hard ceiling it always was -- lambda paces spending inside
-- the budget, it cannot raise it. The learner produces rankings and scores; it
-- has no channel to any budget, cap or fee.
--
-- OFF BY DEFAULT. p_index_enabled defaults to false. At false no index is
-- computed, no lambda row is touched, the ordering key is all-NULL and the
-- claim loop is 202608100001's, byte for byte, apart from one documented
-- change: the candidate ordering gains prefix_hash as a tiebreak after
-- next_due_at. Ties on next_due_at were previously resolved by whatever order
-- the plan produced, which is not a decision anybody made; making it
-- deterministic is what lets the flag-on/flag-off equivalence be an executable
-- test (tests/test_warm_index.py::test_index_flat_priors_equivalence_v1) rather
-- than a comment. It cannot change WHICH rows are claimed, only the order among
-- simultaneously-due ones.
--
-- NULL MEANS OFF. All eight new warm_decision_log columns are nullable and are
-- written NULL whenever the index machinery did not run, so a flag-off row is
-- byte-identical to a pre-Phase-1 row and the data itself records which policy
-- produced it.
--
-- DECISION VOCABULARY. The decision CHECK is rewritten once, here, to admit
-- 'skipped_lambda', 'envelope_denied' and 'beta_denied' -- the last two have no
-- producer until later migrations in this series, declared now for exactly the
-- reason 202608090001 declared 'holdout' ahead of its producer: so the
-- migration that adds them does not rewrite a constraint on a hot append-only
-- table.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: drop columns index_score, index_density, v_hit_usd, chain_cost_usd, c_belief_usd, p_alive, organic_multiplier, lambda_index from public.warm_decision_log; drop columns lambda_index, lambda_updated_at from public.warm_budget_ledger; drop constraint warm_decision_log_decision_check on public.warm_decision_log and re-add check (decision in ('pinged','skipped_roi','budget_denied','cap_denied','stopped','holdout')); drop public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric) and re-apply 202608100001_warm_holdout_arm.sql's eleven-argument public.warm_due_claim verbatim with its revoke/grant/comment; drop public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric) and re-apply 202608090001_warm_instrumentation_tables.sql's fifteen-argument public.warm_decision_record verbatim with its revoke/grant; re-apply 202608090002_warm_reward_join.sql's public.compliance_export_tenant and public.compliance_export_subject verbatim

begin;

do $migration_precondition$
declare
    required_routine text;
begin
    -- Either arity satisfies this: 202608100001's eleven-argument form on a
    -- first apply, this migration's fourteen-argument form on a re-apply.
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)') is null
       and to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100002 requires public.warm_due_claim';
    end if;
    -- Same tolerance for the decision writer: fifteen arguments before this
    -- migration, twenty-three after it.
    if to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric)') is null
       and to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)') is null then
        raise exception using
            errcode = '55000',
            message = '202608100002 requires public.warm_decision_record';
    end if;
    foreach required_routine in array array[
        'public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)',
        -- Both exports are re-created below to carry the new columns; carrying
        -- 202608090002's text forward is only meaningful if that text is what
        -- is installed.
        'public.compliance_export_tenant(uuid,uuid,text)',
        'public.compliance_export_subject(uuid,uuid,text)',
        'public.compliance_export_tenant_pre_company_identity(uuid,uuid,text)',
        'public.compliance_export_subject_pre_company_identity(uuid,uuid,text)'
    ] loop
        if to_regprocedure(required_routine) is null then
            raise exception using
                errcode = '55000',
                message = '202608100002 requires ' || required_routine;
        end if;
    end loop;
    if to_regclass('public.warm_prefixes') is null
       or to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_budget_ledger') is null then
        raise exception using
            errcode = '55000',
            message = '202608100002 requires the warming tables';
    end if;
    -- organic_counterfactual is 202608090002's column and is enumerated by the
    -- export text this migration carries forward; without it the copy below
    -- would silently be a DIFFERENT export than the one in force.
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_decision_log'::regclass
           and attribute.attname = 'organic_counterfactual'
           and not attribute.attisdropped
    ) then
        raise exception using
            errcode = '55000',
            message = '202608100002 requires 202608090002 to be applied';
    end if;
end;
$migration_precondition$;

-- ---------------------------------------------------------------------------
-- The index belief snapshot. Every column is nullable and every column is NULL
-- on a row the index machinery did not score, which is what keeps a flag-off
-- row indistinguishable from a pre-Phase-1 row.
-- ---------------------------------------------------------------------------
alter table public.warm_decision_log
    -- Dimensionless: p_eff / b - 1 - n_chain, clamped to +/- 1e6 by the writer
    -- so a degenerate break-even cannot push a value the numeric type would
    -- accept but no human would read.
    add column if not exists index_score numeric,
    -- 1/USD: index per reserved dollar. DIAGNOSTIC ONLY -- it is deliberately
    -- NOT the claim's ordering key. index_score already divides by c_belief, so
    -- it is already value per belief-dollar; dividing it again by the
    -- reservation prefers cheap arms over valuable ones under a binding budget.
    -- Retained so the two rankings stay comparable in the data.
    add column if not exists index_density numeric,
    -- USD: the saving a return realizes on this prefix, price_base * (1 - f)
    -- with price_base = ping_reserve_usd / w. Real dollars off the provider
    -- catalog -- NOT reserve / b, which the floored reservation inflates 12-100x.
    add column if not exists v_hit_usd numeric(18,10)
        check (v_hit_usd is null or (v_hit_usd >= 0 and v_hit_usd <= 99999999)),
    -- USD: n_chain future pings priced at c_belief each. 0 until the chain
    -- producer exists.
    add column if not exists chain_cost_usd numeric(18,10)
        check (chain_cost_usd is null or chain_cost_usd >= 0),
    -- USD: what THIS keep-alive costs, price_base * f. The ONE definition of
    -- c_belief in this series. The pessimistic full-write dollars the daily
    -- ceiling is enforced against are a different quantity and are logged
    -- separately, unchanged, as reserve_usd.
    add column if not exists c_belief_usd numeric(18,10)
        check (c_belief_usd is null or c_belief_usd >= 0),
    -- Probability the customer session is still alive. Pinned to 1 here.
    add column if not exists p_alive numeric
        check (p_alive is null or p_alive between 0 and 1),
    -- Organic-warmth netting multiplier. Pinned to 1 here (no netting).
    add column if not exists organic_multiplier numeric
        check (organic_multiplier is null or organic_multiplier >= 0),
    -- The pacing dual in force when this candidate was scored, in index units.
    add column if not exists lambda_index numeric
        check (lambda_index is null or lambda_index >= 0);

-- ---------------------------------------------------------------------------
-- Decision vocabulary. Rewritten ONCE for the whole Phase 1 series: the two
-- values with no producer yet ('envelope_denied', 'beta_denied') are declared
-- here so their migrations do not have to rewrite this constraint under load,
-- exactly as 202608090001 declared 'holdout' ahead of 202608100001.
--
-- The existing constraint is auto-named, so it is discovered rather than
-- assumed. Re-applying this migration drops the constraint it just added (it
-- matches the same predicate) and re-adds it, which is what makes it idempotent.
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
            'beta_denied'));
end;
$decision_vocabulary$;

-- ---------------------------------------------------------------------------
-- The pacing dual's state. Per (organization, provider, UTC day), alongside the
-- reservations it paces. A fresh daily row starts at 0, which is the provider
-- break-even floor and therefore the no-op value.
-- ---------------------------------------------------------------------------
alter table public.warm_budget_ledger
    add column if not exists lambda_index numeric(18,10) not null default 0
        check (lambda_index >= 0),
    add column if not exists lambda_updated_at timestamptz;

-- ---------------------------------------------------------------------------
-- warm_decision_record (202608090001:334-384) + the eight index columns. The
-- fifteen-argument signature is replaced, not overloaded: two candidate
-- signatures make every existing fifteen-argument call ambiguous. Callers that
-- send the old fifteen keep resolving, because every new argument has a default.
--
-- The argument checks are still EXACTLY the table's own constraints and nothing
-- more: this runs inside warm_due_claim's advisory-locked transaction, so a
-- stricter check here aborts a claim the table would have accepted.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric
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
    p_lambda_index numeric default null
) returns jsonb as $$
begin
    if p_organization_id is null or p_customer_id is null
       or p_provider not in ('anthropic', 'openai', 'deepseek')
       or coalesce(p_prefix_hash, '') !~ '^[0-9a-f]{64}$'
       or coalesce(p_decision, '') not in (
            'pinged', 'skipped_roi', 'budget_denied', 'cap_denied',
            'stopped', 'holdout', 'skipped_lambda', 'envelope_denied',
            'beta_denied')
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
       or coalesce(p_organic_multiplier, 0) < 0
       or coalesce(p_lambda_index, 0) < 0
       or (p_claim_token is not null and p_decision <> 'pinged') then
        raise exception 'warm decision arguments are invalid';
    end if;
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, ewma_interarrival_s,
        arrival_count, pings_today, claim_token, rng_seed, propensity,
        index_score, index_density, v_hit_usd, chain_cost_usd, c_belief_usd,
        p_alive, organic_multiplier, lambda_index
    ) values (
        p_organization_id, p_customer_id, p_provider, p_prefix_hash, p_decision,
        p_p_return, p_roi_floor, p_reserve_usd, p_prefix_tokens,
        p_ewma_interarrival_s, p_arrival_count, p_pings_today, p_claim_token,
        p_rng_seed, p_propensity,
        p_index_score, p_index_density, p_v_hit_usd, p_chain_cost_usd,
        p_c_belief_usd, p_p_alive, p_organic_multiplier, p_lambda_index
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
    numeric, numeric, numeric, numeric
) from public, anon, authenticated;
grant execute on function public.warm_decision_record(
    uuid, uuid, text, text, text, numeric, numeric, numeric, integer, numeric,
    integer, integer, uuid, bigint, numeric, numeric, numeric, numeric, numeric,
    numeric, numeric, numeric, numeric
) to service_role;

-- ---------------------------------------------------------------------------
-- warm_due_claim (202608100001:128-399) + the dollar index, density ordering
-- and the lambda pacing dual. Every existing gate, reservation, claim token and
-- the holdout draw are carried forward unchanged and in the same order; the
-- only new exit is 'skipped_lambda', between the ROI gate and the per-customer
-- cap. The eleven-argument signature is replaced, not overloaded.
-- ---------------------------------------------------------------------------
drop function if exists public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer,
    integer, jsonb, double precision
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
    p_lambda_max numeric default 1000
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
               and prefix.consecutive_misses < p_stop_loss
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
            v_p_alive := 1.0;
            v_organic := 1.0;
            v_n_chain := 0;
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
    double precision, boolean, numeric, numeric
) from public, anon, authenticated;
grant execute on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric
) to service_role;

comment on function public.warm_due_claim(
    integer, numeric, integer, numeric, numeric, integer, integer, integer, integer, jsonb,
    double precision, boolean, numeric, numeric
) is 'Claim due warm prefixes under the daily budget and per-customer caps, logging every candidate it scores -- claimed, denied and held out -- to public.warm_decision_log. Under p_index_enabled each candidate is scored by the dimensionless dollar index p_eff/b - 1 - n_chain, claimed in order of that index (NOT index per reserved dollar: the index is already value per belief-dollar, so dividing again by the reservation prefers cheap arms over valuable ones under a binding budget), and denied when the index falls below the per-(organization, provider, day) lambda pacing dual whose floor of 0 is the provider break-even. At p_index_enabled = false no index is computed and the loop is 202608100001''s.';

-- ---------------------------------------------------------------------------
-- compliance_export_tenant (202608090002:645-847) + the eight index columns.
-- The projection is enumerated, so a column nobody names is absent from every
-- export exactly as an unenumerated table would be -- which is why adding
-- columns to warm_decision_log obliges re-creating this function. Everything
-- else is 202608090002's text carried forward verbatim.
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
end;
$function$;

-- ---------------------------------------------------------------------------
-- compliance_export_subject (202608090002:852-1017) + the eight index columns.
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

comment on column public.warm_decision_log.index_score is
    'Dimensionless dollar index p_eff/b - 1 - n_chain, clamped to +/- 1e6. Null when the index machinery was off for this row.';
comment on column public.warm_budget_ledger.lambda_index is
    'Pacing dual in index units for this (organization, provider, UTC day). 0 is the provider break-even floor and the no-op value.';

commit;
