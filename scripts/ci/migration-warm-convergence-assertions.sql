-- Index convergence (202608100010): the simulator-proven learned-index-fixed
-- policy in public.warm_due_claim -- silence-conditioned p_return, the organic
-- and I_max gates, the shared-cache-key skip, the periodicity fast-path, the
-- median-floored chain and the cold-start v1 hold.
--
-- The estimator expectations below are the values api/store.py's mirrors of the
-- same functions produce, which are themselves checked against
-- scripts/warm_replay_sim.py's LearnedIndexFixedPolicy. Three implementations,
-- one set of numbers.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000c0001';
    v_org uuid := '00000000-0000-4000-8000-0000000c0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000c0003';
    v_peer uuid := '00000000-0000-4000-8000-0000000c0004';
    v_quiet uuid := '00000000-0000-4000-8000-0000000c0005';
    v_aggregate uuid := '00000000-0000-0000-0000-000000000000';
    v_hash_a text := repeat('c1', 32);
    v_hash_b text := repeat('c2', 32);
    v_hash_shared text := repeat('c3', 32);
    v_request uuid := '00000000-0000-4000-8000-0000000c0006';
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean,numeric,numeric,numeric,numeric)';
    v_routine text;
    v_post numeric[];
    v_period numeric[];
    v_count integer;
    v_bucket text;
    v_row record;
    v_decisions text[];
    v_export jsonb;
begin
    -- ------------------------------------------------------------------
    -- 0. Signatures. REPLACED, never overloaded: two candidates for one name
    -- make every existing call ambiguous, which on this path is a runtime
    -- error the first time money is about to move.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null then
        raise exception 'warm_due_claim must be the twenty-one-argument convergence form';
    end if;
    if to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is not null then
        raise exception 'an older warm_due_claim signature survived the replacement';
    end if;
    if (select count(*) from pg_catalog.pg_proc proc
         where proc.proname = 'warm_decision_record'
           and proc.pronamespace = 'public'::regnamespace) <> 1 then
        raise exception 'warm_decision_record must have exactly one signature';
    end if;
    if has_function_privilege('anon', v_claim_signature, 'execute')
       or has_function_privilege('authenticated', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_claim_signature, 'execute') then
        raise exception 'warm_due_claim privileges are wrong after the signature change';
    end if;
    foreach v_routine in array array[
        'public.warm_hour_of_week(timestamptz)',
        'public.warm_conv_posterior(numeric,numeric,numeric,numeric,numeric,numeric,numeric)',
        'public.warm_conv_survival(numeric,numeric,numeric,numeric)',
        'public.warm_conv_i_max(numeric,numeric,numeric)',
        'public.warm_conv_median(numeric[])',
        'public.warm_conv_period(numeric[],numeric)',
        'public.warm_conv_lambda_to(numeric[],numeric[],numeric)',
        'public.warm_conv_round_half_even(numeric)',
        'public.warm_conv_push_gap(numeric[],numeric,integer)'
    ] loop
        if to_regprocedure(v_routine) is null then
            raise exception 'the convergence estimator % is missing', v_routine;
        end if;
        if has_function_privilege('anon', v_routine, 'execute')
           or has_function_privilege('authenticated', v_routine, 'execute')
           or not has_function_privilege('service_role', v_routine, 'execute') then
            raise exception 'the convergence estimator % has the wrong privileges', v_routine;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 1. The schema delta: two columns, their bounds, and the widened
    -- decision vocabulary.
    -- ------------------------------------------------------------------
    if not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_customer_state'::regclass
           and attribute.attname = 'recent_gaps' and not attribute.attisdropped)
       or not exists (
        select 1 from pg_catalog.pg_attribute attribute
         where attribute.attrelid = 'public.warm_decision_log'::regclass
           and attribute.attname = 'p_eff' and not attribute.attisdropped) then
        raise exception 'the convergence columns are missing';
    end if;
    if not exists (
        select 1 from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_customer_state'::regclass
           and constraint_row.conname = 'warm_customer_state_recent_gaps_check')
       or not exists (
        select 1 from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_decision_log'::regclass
           and constraint_row.conname = 'warm_decision_log_p_eff_check') then
        raise exception 'the convergence columns are unbounded';
    end if;
    foreach v_routine in array array[
        'skipped_organic', 'skipped_abandon', 'skipped_shared_warm'
    ] loop
        if pg_catalog.pg_get_constraintdef((
                select constraint_row.oid from pg_catalog.pg_constraint constraint_row
                 where constraint_row.conrelid = 'public.warm_decision_log'::regclass
                   and constraint_row.conname = 'warm_decision_log_decision_check'))
           not like '%' || v_routine || '%' then
            raise exception 'the decision vocabulary does not admit %', v_routine;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 2. The estimators, against the numbers api/store.py produces. These are
    -- the whole policy: if any of them drifts, the two backends are running
    -- different schedulers and nothing downstream will say so.
    -- ------------------------------------------------------------------
    -- F4, the ordinary case: 20 arrivals over 5 exposure-hours, the prior
    -- ramped down by that exposure and capped at it.
    v_post := public.warm_conv_posterior(20, 5, 0, 0, 5, 2.0, 48.0);
    if abs(v_post[1] - 20.010664682539684) > 1e-12
       or abs(v_post[2] - 6.791666666666667) > 1e-12 then
        raise exception 'warm_conv_posterior drifted on the ordinary case: %', v_post;
    end if;
    -- F4's floor: a customer with no exposure in this bucket keeps a quarter
    -- pseudo-hour of organization prior rather than none, because there the
    -- organization term is the only information there is.
    v_post := public.warm_conv_posterior(1, 0, 0, 0, 0, 2.0, 48.0);
    if abs(v_post[2] - 0.25) > 1e-12 then
        raise exception 'warm_conv_posterior lost the prior floor: %', v_post;
    end if;
    -- F4's ramp: past the majority horizon the prior is fully retired, so the
    -- customer's own evidence is all that is left (plus the floor).
    v_post := public.warm_conv_posterior(20, 5, 10, 100, 60, 2.0, 48.0);
    if abs(v_post[2] - 5.25) > 1e-12 then
        raise exception 'warm_conv_posterior did not retire the prior: %', v_post;
    end if;

    if abs(public.warm_conv_survival(4.0, 2.0, 1.5, 0.8) - 0.07096319412336048) > 1e-12 then
        raise exception 'warm_conv_survival drifted: %',
            public.warm_conv_survival(4.0, 2.0, 1.5, 0.8);
    end if;
    -- Degenerate inputs mean "no information", which is survival 1, not an
    -- error and not zero -- a zero would read as "they have certainly returned".
    if public.warm_conv_survival(0, 2, 1, 1) <> 1
       or public.warm_conv_survival(4, 2, 0, 1) <> 1
       or public.warm_conv_survival(4, 2, 1, 0) <> 1 then
        raise exception 'warm_conv_survival must return 1 on no information';
    end if;

    if abs(public.warm_conv_i_max(300, 1.25, 0.11) - 3484.0909090909095) > 1e-6 then
        raise exception 'warm_conv_i_max drifted: %',
            public.warm_conv_i_max(300, 1.25, 0.11);
    end if;

    if public.warm_conv_median(array[4, 1, 3, 2]::numeric[]) <> 2.5
       or public.warm_conv_median(array[5, 1, 3]::numeric[]) <> 3 then
        raise exception 'warm_conv_median drifted';
    end if;

    -- F5's detector. A tight clock is claimed at the confidence ceiling; a
    -- ragged series is refused; two gaps are never enough evidence.
    v_period := public.warm_conv_period(array[1200, 1210, 1190, 1205]::numeric[], 0.20);
    if v_period is null or v_period[1] <> 1202.5 or abs(v_period[2] - 0.98) > 1e-12 then
        raise exception 'warm_conv_period missed a clock: %', v_period;
    end if;
    if public.warm_conv_period(array[1200, 600, 2400, 900]::numeric[], 0.20) is not null then
        raise exception 'warm_conv_period called a ragged series a clock';
    end if;
    if public.warm_conv_period(array[1200, 1200]::numeric[], 0.20) is not null then
        raise exception 'warm_conv_period claimed a period from two gaps';
    end if;

    -- Python's round() is half-to-EVEN and Postgres's is half-away-from-zero;
    -- n_chain is an integer count of money, so the two must agree exactly.
    if public.warm_conv_round_half_even(0.5) <> 0
       or public.warm_conv_round_half_even(1.5) <> 2
       or public.warm_conv_round_half_even(2.5) <> 2
       or public.warm_conv_round_half_even(2.4) <> 2
       or public.warm_conv_round_half_even(2.6) <> 3 then
        raise exception 'warm_conv_round_half_even is not half-to-even';
    end if;

    -- The piecewise integral: two one-hour segments at 2/h then 4/h, sampled
    -- ninety minutes in -- one full hour of the first plus half of the second.
    if public.warm_conv_lambda_to(array[3600, 7200]::numeric[],
                                  array[2, 4]::numeric[], 5400) <> 4 then
        raise exception 'warm_conv_lambda_to mis-integrates the rate curve';
    end if;

    -- The gap window: bounded at twelve, oldest dropped, non-positive refused.
    if public.warm_conv_push_gap(array[1, 2]::numeric[], 3) <> array[1, 2, 3]::numeric[]
       or public.warm_conv_push_gap(array[1, 2]::numeric[], -5) <> array[1, 2]::numeric[]
       or public.warm_conv_push_gap(
            array[1,2,3,4,5,6,7,8,9,10,11,12]::numeric[], 13)
          <> array[2,3,4,5,6,7,8,9,10,11,12,13]::numeric[] then
        raise exception 'warm_conv_push_gap does not keep the last twelve gaps';
    end if;

    -- ------------------------------------------------------------------
    -- 3. The fixture.
    -- ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_actor, 'warm-convergence-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm convergence fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'convergence-subject'),
           (v_peer, v_org, 'convergence-peer'),
           (v_quiet, v_org, 'convergence-quiet');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    -- The observer maintains the gap window: three arrivals leave two gaps.
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    update public.warm_customer_state state
       set last_seen_at = clock_timestamp() - interval '20 minutes',
           last_update_at = clock_timestamp() - interval '20 minutes'
     where state.organization_id = v_org and state.customer_id = v_cust;
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    select * into v_row from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_cust
       and state.provider = 'anthropic';
    if coalesce(array_length(v_row.recent_gaps, 1), 0) <> 1
       or v_row.recent_gaps[1] < 1100 or v_row.recent_gaps[1] > 1300 then
        raise exception 'the observer did not record the closed gap: %',
            v_row.recent_gaps;
    end if;

    v_bucket := ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text;

    -- ------------------------------------------------------------------
    -- 4. F2 -- the organic gate. An arm arriving more often than its entry
    -- expires refreshes it for free, and every keep-alive bought for it is a
    -- duplicate of a write the customer's own traffic already made.
    -- ------------------------------------------------------------------
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20),
           ewma_interarrival_s = 30,
           last_seen_at = clock_timestamp() - interval '30 seconds',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '30 seconds'
     where prefix.organization_id = v_org;
    update public.warm_customer_state state
       set hazard_n = jsonb_build_object(v_bucket, 20),
           hazard_e = jsonb_build_object(v_bucket, 5),
           events_total = 20,
           first_seen_at = clock_timestamp() - interval '30 days'
     where state.organization_id = v_org and state.customer_id <> v_aggregate;

    delete from public.warm_decision_log where organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    select array_agg(distinct entry.decision) into v_decisions
      from public.warm_decision_log entry where entry.organization_id = v_org;
    if v_decisions <> array['skipped_organic']::text[] then
        raise exception 'a self-refreshing arm must be skipped as organic: %',
            v_decisions;
    end if;
    -- The gate is INSIDE the flag pair. With the index off it must not fire at
    -- all, or the flag-off policy would have changed.
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second', claim_token = null
     where prefix.organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        true) as t(value);
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org
                  and entry.decision like 'skipped_organic') then
        raise exception 'the organic gate fired with the index flag off';
    end if;

    -- ------------------------------------------------------------------
    -- 5. F3 -- the I_max abandon rule. Silence past ttl*(w/f - 1) means the
    -- chain needed to bridge it costs more than the single miss it prevents,
    -- for every p <= 1, so this is arithmetic and not a judgement.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           ewma_interarrival_s = 1800,
           last_seen_at = clock_timestamp() - interval '4 hours',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '4 hours'
     where prefix.organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 14400, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    select array_agg(distinct entry.decision) into v_decisions
      from public.warm_decision_log entry where entry.organization_id = v_org;
    if v_decisions <> array['skipped_abandon']::text[] then
        raise exception 'silence past I_max must be abandoned: %', v_decisions;
    end if;
    -- A gated row carries no index: nothing was scored, so there is nothing to
    -- report, and an invented one would be evidence of a decision never made.
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org
                  and (entry.index_score is not null or entry.p_eff is not null)) then
        raise exception 'a gated candidate must not carry an index';
    end if;

    -- ------------------------------------------------------------------
    -- 6. F7 -- the shared cache key. Two customers, one prefix_hash, one
    -- provider entry: the second arm is buying warmth the first already holds.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_prefixes where organization_id = v_org;
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_shared, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_shared, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    -- Both due; the peer's own touch is stale, the subject's is fresh. The
    -- entry they SHARE is therefore warm for another four minutes.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20),
           ewma_interarrival_s = 1200,
           last_seen_at = clock_timestamp() - interval '10 minutes',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '10 minutes'
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20),
           ewma_interarrival_s = 1200,
           last_seen_at = clock_timestamp() - interval '1 minute',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '1 minute'
     where prefix.organization_id = v_org and prefix.customer_id = v_cust;
    update public.warm_customer_state state
       set hazard_n = jsonb_build_object(v_bucket, 20),
           hazard_e = jsonb_build_object(v_bucket, 5),
           events_total = 20,
           first_seen_at = clock_timestamp() - interval '30 days',
           last_seen_at = clock_timestamp() - interval '1 minute',
           last_update_at = clock_timestamp() - interval '1 minute'
     where state.organization_id = v_org and state.customer_id <> v_aggregate;

    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    -- EXACTLY ONE, and it is the peer -- the arm whose sibling refreshed the
    -- shared entry more recently than its own schedule assumes. Skipping BOTH
    -- would let the entry every one of them reads lapse, which is the opposite
    -- of the point; skipping neither is the duplicate purchase F7 removes.
    select count(*) into v_count from public.warm_decision_log entry
     where entry.organization_id = v_org
       and entry.decision = 'skipped_shared_warm';
    if v_count <> 1 then
        raise exception 'exactly one arm on a warm shared key must be skipped: %', v_count;
    end if;
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org
           and entry.decision = 'skipped_shared_warm'
           and entry.customer_id = v_peer) then
        raise exception 'the wrong arm was skipped on the shared key';
    end if;
    -- And the gate is not vacuous: let the shared entry go cold and the same
    -- two arms are scored again.
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_touch_at = clock_timestamp() - interval '4 minutes 50 seconds'
     where prefix.organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org
                  and entry.decision = 'skipped_shared_warm') then
        raise exception 'the shared-key gate fired on a cold entry';
    end if;

    -- ------------------------------------------------------------------
    -- 7. THE INDEX IDENTITY, which F1 would otherwise have broken. The ROI
    -- floor gates on the single-window p_return; the index is charged over the
    -- 1 + n_chain windows the chain commits to. Those are different numbers by
    -- design, so p_eff is recorded and the row stays re-derivable.
    -- ------------------------------------------------------------------
    for v_row in
        select * from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.index_score is not null
    loop
        if v_row.p_eff is null then
            raise exception 'a scored row must record the probability it was charged for: %',
                to_jsonb(v_row);
        end if;
        if abs(v_row.index_score
               - (v_row.p_eff / 0.11 - 1
                  - round(v_row.chain_cost_usd / v_row.c_belief_usd))) > 1e-9 then
            raise exception 'index_score must be p_eff/b - 1 - n_chain: %',
                to_jsonb(v_row);
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 8. THE COLD-START HOLD. An arm below p_roi_min_arrivals is scored
    -- exactly as v1 scores it EVEN THOUGH a state row exists -- the case
    -- Phase 1 got wrong, where a posterior built on a single observation went
    -- quiet while v1 was already bridging the customer's gaps.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 2,
           hour_histogram = jsonb_build_object(v_bucket, 2),
           last_touch_at = clock_timestamp() - interval '4 minutes 50 seconds'
     where prefix.organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    for v_row in
        select * from public.warm_decision_log entry
         where entry.organization_id = v_org
    loop
        -- v1's answer, exactly: the lifetime histogram ratio (2/2 = 1), no
        -- P(alive) discount and no chain.
        if v_row.p_return <> 1 or v_row.p_alive <> 1
           or coalesce(v_row.chain_cost_usd, 0) <> 0 then
            raise exception 'a cold-start arm must be scored exactly as v1: %',
                to_jsonb(v_row);
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 8b. F5 -- the periodicity fast-path. A scheduled agent is a clock, not a
    -- renewal process: with a dozen arrivals of evidence the negative-binomial
    -- tail is wide enough that n_chain exceeds the break-even chain length and
    -- the policy abandons MID-CHAIN, which is the one spend pattern strictly
    -- worse than doing nothing.
    --
    -- Gaps of ~1202.5s at a 300s TTL, 300s of silence -- which is also exactly
    -- when the shared entry lapses, so F7 does not fire and the arm is really
    -- due. The next arrival is forecast in 902.5s, and at tau = 240s exactly
    -- three further keep-alives stand between now and it.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_customer_state state
       set recent_gaps = array[1200, 1210, 1190, 1205]::numeric[],
           last_seen_at = clock_timestamp() - interval '300 seconds',
           last_update_at = clock_timestamp() - interval '300 seconds'
     where state.organization_id = v_org and state.customer_id <> v_aggregate;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20),
           ewma_interarrival_s = 1200,
           last_seen_at = clock_timestamp() - interval '300 seconds',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '300 seconds'
     where prefix.organization_id = v_org;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    for v_row in
        select * from public.warm_decision_log entry
         where entry.organization_id = v_org
    loop
        -- The phase model reports its own confidence rather than a hazard, and
        -- prices the keep-alives that actually stand between now and the
        -- forecast arrival.
        if v_row.p_return <> 0.98 then
            raise exception 'a detected clock must be scored on its phase: %',
                to_jsonb(v_row);
        end if;
        if round(v_row.chain_cost_usd / v_row.c_belief_usd) <> 3 then
            raise exception 'the phase chain must be the windows before the forecast: %',
                to_jsonb(v_row);
        end if;
    end loop;

    -- FALSIFICATION. Past p_period_miss_limit periods of silence two predicted
    -- arrivals have gone unanswered: this is a departed agent, not a slow one,
    -- and the arm goes back to the index path to be priced and abandoned.
    -- Without this a churned cron agent is sustained to I_max forever.
    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           last_seen_at = clock_timestamp() - interval '2000 seconds',
           expires_at = clock_timestamp() + interval '6 days',
           last_touch_at = clock_timestamp() - interval '2000 seconds'
     where prefix.organization_id = v_org;
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org) then
        raise exception 'the falsification fixture did not start clean';
    end if;
    perform count(*) from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    if not exists (select 1 from public.warm_decision_log entry
                    where entry.organization_id = v_org) then
        raise exception 'the falsified arm was not scored at all';
    end if;
    if exists (select 1 from public.warm_decision_log entry
                where entry.organization_id = v_org and entry.p_return = 0.98) then
        raise exception 'two missed arrivals must falsify the phase model';
    end if;

    -- ------------------------------------------------------------------
    -- 9. COMPLIANCE. recent_gaps and p_eff are behavioural evidence about a
    -- subject; a projection that does not name them omits them from every data
    -- right, silently.
    -- ------------------------------------------------------------------
    if pg_catalog.pg_get_functiondef(
           'public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
           not like '%recent_gaps%'
       or pg_catalog.pg_get_functiondef(
           'public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
           not like '%recent_gaps%'
       or pg_catalog.pg_get_functiondef(
           'public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
           not like '%''p_eff'', entry.p_eff%'
       or pg_catalog.pg_get_functiondef(
           'public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
           not like '%''p_eff'', entry.p_eff%' then
        raise exception 'the compliance exports do not carry the convergence evidence';
    end if;

    -- Tenant erasure takes the gap window with the row that carries it, which
    -- is why this migration adds no delete statement of its own.
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-convergence:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-convergence',
        'system:warm-convergence');
    perform public.compliance_delete_tenant(
        v_org, v_request, 'brevitas_admin:assertion');
    if exists (select 1 from public.warm_customer_state state
                where state.organization_id = v_org) then
        raise exception 'tenant erasure must take the learned state, gaps and all';
    end if;

    raise notice '202608100010 convergence assertions passed';
end;
$$;

rollback;
