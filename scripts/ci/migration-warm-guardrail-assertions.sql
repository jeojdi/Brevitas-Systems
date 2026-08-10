-- Phase 1 learned warming (202608100005): the beta spend cap, its
-- control-savings producer, the conservative guardrail and the TTL canary.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-000000110001';
    v_org uuid := '00000000-0000-4000-8000-000000110002';
    v_cust uuid := '00000000-0000-4000-8000-000000110003';
    v_peer uuid := '00000000-0000-4000-8000-000000110004';
    v_hash_a text := repeat('a3', 32);
    v_hash_b text := repeat('b3', 32);
    v_tenant_request uuid := '00000000-0000-4000-8000-000000110005';
    v_export_request uuid := '00000000-0000-4000-8000-000000110006';
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean,numeric,numeric,numeric,numeric)';
    v_day date := (clock_timestamp() at time zone 'utc')::date;
    v_bucket text;
    v_table text;
    v_role text;
    v_routine text;
    v_count integer;
    v_reserve numeric;
    v_result jsonb;
    v_raised boolean;
    v_savings public.warm_control_savings_daily%rowtype;
    v_probe_id bigint;
    v_decision text;
    v_p_alive numeric;
    v_chain numeric;
begin
    -- ------------------------------------------------------------------
    -- 0. Signatures. warm_due_claim grows to sixteen arguments and the
    -- fifteen-argument form is DROPPED: two candidates for one name is how a
    -- caller silently keeps talking to the pre-cap policy, and PostgREST would
    -- answer such a call with a 300 rather than a claim.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is not null then
        raise exception 'warm_due_claim must be the seventeen-argument dedup form only';
    end if;
    if has_function_privilege('anon', v_claim_signature, 'execute')
       or has_function_privilege('authenticated', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_claim_signature, 'execute') then
        raise exception 'warm_due_claim privileges are wrong after the signature change';
    end if;
    foreach v_routine in array array[
        'public.warm_control_savings_refresh(integer)',
        'public.warm_org_mode_set(uuid,text,text,text)',
        'public.warm_org_mode_list()',
        'public.warm_guardrail_scan(integer)',
        'public.warm_canary_reserve(text,date,numeric,numeric)',
        'public.warm_canary_settle(text,date,numeric,numeric)',
        'public.warm_canary_probe_insert(text,text,text,integer,timestamptz,numeric)',
        'public.warm_canary_probe_due(text,integer)',
        'public.warm_canary_probe_mark(bigint,text)',
        'public.warm_canary_probe_stats(text)'
    ] loop
        if to_regprocedure(v_routine) is null then
            raise exception 'the 202608100005 RPC % is missing', v_routine;
        end if;
        if has_function_privilege('anon', v_routine, 'execute')
           or has_function_privilege('authenticated', v_routine, 'execute')
           or not has_function_privilege('service_role', v_routine, 'execute') then
            raise exception 'the RPC % has the wrong privileges', v_routine;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 1. Four tables, RLS on, zero policies, zero direct DML for every
    -- PostgREST role.
    -- ------------------------------------------------------------------
    foreach v_table in array array[
        'warm_control_savings_daily', 'warm_org_mode',
        'warm_canary_probes', 'warm_canary_ledger'
    ] loop
        if to_regclass('public.' || v_table) is null then
            raise exception 'public.% is missing', v_table;
        end if;
        if not (select relrowsecurity from pg_catalog.pg_class
                 where oid = ('public.' || v_table)::regclass) then
            raise exception 'public.% must have row level security enabled', v_table;
        end if;
        if exists (select 1 from pg_catalog.pg_policy
                    where polrelid = ('public.' || v_table)::regclass) then
            raise exception 'public.% must carry zero policies', v_table;
        end if;
        foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
            if has_table_privilege(v_role, 'public.' || v_table, 'select')
               or has_table_privilege(v_role, 'public.' || v_table, 'insert')
               or has_table_privilege(v_role, 'public.' || v_table, 'update')
               or has_table_privilege(v_role, 'public.' || v_table, 'delete') then
                raise exception 'public.% must grant % nothing', v_table, v_role;
            end if;
        end loop;
    end loop;

    -- ------------------------------------------------------------------
    -- 2. The canary is a third observation source, and warm_ttl_observe knows
    -- it. 'bogus' still has to be refused: widening a vocabulary is not the
    -- same as removing the check.
    -- ------------------------------------------------------------------
    perform public.warm_ttl_observe(
        'anthropic', 'claude-haiku-4-5', '5m', 120, 'warm', 'canary');
    if not exists (
        select 1 from public.warm_ttl_observations observation
         where observation.source = 'canary'
    ) then
        raise exception 'warm_ttl_observe must accept the canary source';
    end if;
    v_raised := false;
    begin
        perform public.warm_ttl_observe(
            'anthropic', 'claude-haiku-4-5', '5m', 120, 'warm', 'bogus');
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'warm_ttl_observe must still reject an unknown source';
    end if;
    delete from public.warm_ttl_observations where source = 'canary';

    -- ------------------------------------------------------------------
    -- Fixture.
    -- ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_actor, 'warm-guardrail-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm guardrail fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-guardrail-subject'),
           (v_peer, v_org, 'warm-guardrail-peer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_b, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    v_bucket := ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20)
     where prefix.organization_id = v_org;
    select prefix.ping_reserve_usd into v_reserve
      from public.warm_prefixes prefix
     where prefix.organization_id = v_org and prefix.customer_id = v_cust;
    if coalesce(v_reserve, 0) <= 0 then
        raise exception 'the fixture prefix has no observer-priced reservation';
    end if;

    -- ------------------------------------------------------------------
    -- 3. THE DEFAULT STATE. p_beta = 0 and an empty warm_org_mode: the claim
    -- path is 202608100004's, byte for byte. Control savings of exactly zero
    -- are on file, which as a literal cap would deny everything -- proving the
    -- gate does not run rather than merely computing a generous cap.
    -- ------------------------------------------------------------------
    insert into public.warm_control_savings_daily (
        organization_id, provider, day, treated_units, control_units,
        control_lift_usd)
    values (v_org, 'anthropic', v_day - 3, 5, 5, 0);
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0) as t(value);
    if v_count <> 2 then
        raise exception 'p_beta = 0 must constrain nothing, claimed %', v_count;
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.decision <> 'pinged'
    ) then
        raise exception 'p_beta = 0 must deny nothing';
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0
     where prefix.organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 4. THE CAP BINDS. C28 = 1.00 and beta = 0.5 gives a cap of 0.50; the
    -- fixture reserves 0.375 per candidate, so the first is admitted and the
    -- second denied -- which is also the in-invocation W28 accounting, since
    -- both were measured against one ledger read.
    -- ------------------------------------------------------------------
    update public.warm_control_savings_daily
       set control_lift_usd = 1.0
     where organization_id = v_org and day = v_day - 3;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0.5) as t(value);
    if v_count <> 1 then
        raise exception 'the beta cap must admit exactly one candidate, admitted %', v_count;
    end if;
    select count(*) into v_count from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.decision = 'beta_denied';
    if v_count <> 1 then
        raise exception 'the beta cap must log exactly one beta_denied, logged %', v_count;
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0
     where prefix.organization_id = v_org;

    -- The window excludes the open days. Savings dated today and yesterday
    -- cannot count -- their usage rows have not settled -- so moving the only
    -- row there starves the cap and denies everything.
    update public.warm_control_savings_daily
       set day = v_day
     where organization_id = v_org and day = v_day - 3;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false, 0.2, 1000,
        false, 0.5) as t(value);
    if v_count <> 0 then
        raise exception 'savings on an open day must not fund the cap, claimed %', v_count;
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    delete from public.warm_control_savings_daily where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0
     where prefix.organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 5. THE PRODUCER. Three treated units at $1 against three control units
    -- at $3: savings are (3 - 1) x 3 = 6. Under three on either side forces
    -- zero savings with the counts intact.
    -- ------------------------------------------------------------------
    for v_count in 1..6 loop
        insert into public.warm_decision_log (
            organization_id, customer_id, provider, prefix_hash, decision,
            p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count, ts)
        values (v_org, case when v_count <= 3 then v_cust else v_peer end,
                'anthropic', lpad(v_count::text, 64, '0'),
                case when v_count <= 3 then 'pinged' else 'holdout' end,
                0.5, 0.11, 0.375, 1000, 20,
                (v_day - 3)::timestamptz + interval '12 hours');
        insert into public.usage_log (
            organization_id, customer_id, key_hash, provider, model, strategy,
            actual_cost_usd, authoritative, warm_prefix_hash, ts)
        values (v_org, case when v_count <= 3 then v_cust else v_peer end,
                'kh', 'anthropic', 'claude-sonnet-4-5', 'cache_hit',
                case when v_count <= 3 then 1.0 else 3.0 end, true,
                lpad(v_count::text, 64, '0'),
                (v_day - 3)::timestamptz + interval '12 hours');
        -- Warm spend and non-authoritative rows must be invisible to the
        -- comparison: warm spend is the thing being paid FOR, and a
        -- non-authoritative row is not evidence of anything.
        insert into public.usage_log (
            organization_id, customer_id, key_hash, provider, model, strategy,
            actual_cost_usd, authoritative, warm_prefix_hash, ts)
        values (v_org, v_cust, 'kh', 'anthropic', 'claude-sonnet-4-5',
                'cache_warm', 500.0, true, lpad(v_count::text, 64, '0'),
                (v_day - 3)::timestamptz + interval '12 hours'),
               (v_org, v_cust, 'kh', 'anthropic', 'claude-sonnet-4-5',
                'cache_hit', 500.0, false, lpad(v_count::text, 64, '0'),
                (v_day - 3)::timestamptz + interval '12 hours');
    end loop;
    v_result := public.warm_control_savings_refresh(28);
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 3;
    if v_savings.treated_units <> 3 or v_savings.control_units <> 3 then
        raise exception 'the arm counts are wrong: %', to_jsonb(v_savings);
    end if;
    if v_savings.treated_mean_cost_usd <> 1.0
       or v_savings.control_mean_cost_usd <> 3.0
       or v_savings.control_lift_usd <> 6.0 then
        raise exception 'the control comparison arithmetic is wrong: %',
            to_jsonb(v_savings);
    end if;
    -- STABLE, AND REVISABLE FOR 14 DAYS. A second sweep RECOMPUTES a day
    -- inside the revision horizon rather than skipping it -- `do nothing` froze
    -- every day at the first sweep that saw it, two days after the fact, so an
    -- authoritative usage row that settled late never reached the arm means at
    -- all and silently starved the beta cap's denominator. Recomputing the same
    -- inputs must produce the same row.
    v_result := public.warm_control_savings_refresh(28);
    if (v_result ->> 'rows_written')::integer <> 1 then
        raise exception 'a day inside the revision horizon must be recomputed: %',
            v_result;
    end if;
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 3;
    if v_savings.treated_units <> 3 or v_savings.control_units <> 3
       or v_savings.control_lift_usd <> 6.0 then
        raise exception 'a re-sweep of unchanged inputs must not move the row: %',
            to_jsonb(v_savings);
    end if;
    -- Late authoritative usage lands INSIDE the horizon and is picked up; the
    -- same lateness OUTSIDE it leaves the frozen row alone.
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, warm_prefix_hash, ts)
    values (v_org, v_cust, 'kh-late-recent', 'anthropic', 'claude-sonnet-4-5',
            'cache_hit', 300.0, true, lpad('4', 64, '0'),
            (v_day - 3)::timestamptz + interval '13 hours');
    v_result := public.warm_control_savings_refresh(28);
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 3;
    if v_savings.control_mean_cost_usd <= 3.0 then
        raise exception 'late authoritative usage inside the horizon must revise the arm: %',
            to_jsonb(v_savings);
    end if;
    delete from public.usage_log where key_hash = 'kh-late-recent';
    -- Outside the horizon the row is frozen: seed a day-20 comparison, sweep
    -- it, then land late usage on it and confirm nothing moves.
    for v_count in 1..3 loop
        insert into public.warm_decision_log (
            organization_id, customer_id, provider, prefix_hash, decision,
            p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count, ts)
        values (v_org, v_cust, 'anthropic', lpad((v_count + 10)::text, 64, '0'),
                'pinged', 0.5, 0.11, 0.375, 1000, 20,
                (v_day - 20)::timestamptz + interval '11 hours'),
               (v_org, v_cust, 'anthropic', lpad((v_count + 20)::text, 64, '0'),
                'holdout', 0.5, 0.11, 0.375, 1000, 20,
                (v_day - 20)::timestamptz + interval '11 hours');
    end loop;
    v_result := public.warm_control_savings_refresh(28);
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 20;
    if v_savings.control_mean_cost_usd <> 0 then
        raise exception 'the day-20 fixture must start with no measured control cost: %',
            to_jsonb(v_savings);
    end if;
    insert into public.usage_log (
        organization_id, customer_id, key_hash, provider, model, strategy,
        actual_cost_usd, authoritative, warm_prefix_hash, ts)
    values (v_org, v_cust, 'kh-late-ancient', 'anthropic', 'claude-sonnet-4-5',
            'cache_hit', 300.0, true, lpad('21', 64, '0'),
            (v_day - 20)::timestamptz + interval '12 hours');
    v_result := public.warm_control_savings_refresh(28);
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 20;
    if v_savings.control_mean_cost_usd <> 0 then
        raise exception 'a day outside the revision horizon must stay frozen: %',
            to_jsonb(v_savings);
    end if;
    delete from public.usage_log where key_hash = 'kh-late-ancient';
    delete from public.warm_decision_log
     where organization_id = v_org
       and ts < (v_day - 19)::timestamptz;
    delete from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 20;
    -- Under-powered: drop one control unit and the savings must be forced to
    -- zero while the counts stay honest.
    delete from public.warm_control_savings_daily where organization_id = v_org;
    delete from public.warm_decision_log
     where organization_id = v_org and prefix_hash = lpad('6', 64, '0');
    v_result := public.warm_control_savings_refresh(28);
    select * into v_savings from public.warm_control_savings_daily savings
     where savings.organization_id = v_org and savings.day = v_day - 3;
    if v_savings.control_units <> 2 or v_savings.control_lift_usd <> 0 then
        raise exception 'an under-powered comparison must record 0 savings with its counts: %',
            to_jsonb(v_savings);
    end if;
    -- Open days are never scored: today has decision rows and no savings row.
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count, ts)
    values (v_org, v_cust, 'anthropic', repeat('c3', 32), 'pinged',
            0.5, 0.11, 0.375, 1000, 20, clock_timestamp());
    v_result := public.warm_control_savings_refresh(28);
    if exists (select 1 from public.warm_control_savings_daily savings
                where savings.organization_id = v_org and savings.day >= v_day - 1) then
        raise exception 'an open day must never be scored';
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.usage_log where organization_id = v_org;
    delete from public.warm_control_savings_daily where organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 6. THE GUARDRAIL. warm_org_mode_set round-trips, and warm_guardrail_scan
    -- reports the CONTROL-MEASURED trailing net and the mode.
    --
    -- net_7d_usd is control_lift_usd minus the ledger's reserved+spent, NOT the
    -- policy's own warm_decision_log.realized_net_usd attribution -- a
    -- guardrail whose input is produced by the thing it guards cannot catch a
    -- policy that mis-attributes its own savings. That sum survives only as the
    -- policy_net_7d_usd diagnostic, and control_days counts the powered
    -- comparisons that license a freeze at all.
    -- ------------------------------------------------------------------
    insert into public.warm_budget_ledger (
        organization_id, provider, day, reserved_usd, spent_usd)
    values (v_org, 'anthropic', v_day - 1, 0, 1.0);
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count,
        realized_net_usd, ts)
    values (v_org, v_cust, 'anthropic', repeat('d3', 32), 'pinged',
            0.5, 0.11, 0.375, 1000, 20, -1.0, now() - interval '1 day'),
           (v_org, v_cust, 'anthropic', repeat('e3', 32), 'pinged',
            0.5, 0.11, 0.375, 1000, 20, null, now() - interval '1 day'),
           (v_org, v_cust, 'anthropic', repeat('f3', 32), 'holdout',
            0.5, 0.11, 0.375, 1000, 20, -50.0, now() - interval '1 day');
    select entry into v_result from public.warm_guardrail_scan(7) entry
     where (entry ->> 'organization_id')::uuid = v_org;
    if v_result is null then
        raise exception 'the guardrail scan must see a pair that spent';
    end if;
    -- No control rows yet: zero measured lift differenced against 1.0 of
    -- ledger spend. The pair looks catastrophic and is NOT freezable, which is
    -- exactly what control_days is for.
    if (v_result ->> 'net_7d_usd')::numeric <> -1.0
       or (v_result ->> 'control_days')::integer <> 0
       or (v_result ->> 'control_lift_usd')::numeric <> 0
       or (v_result ->> 'warm_spend_usd')::numeric <> 1.0 then
        raise exception 'the guardrail net must be control lift minus ledger spend: %',
            v_result;
    end if;
    -- The policy's own attribution is carried, and is deliberately NOT the net:
    -- one scored ping at -1.0, one unscored (null reads as "not scored", never
    -- as "earned zero") and one holdout row that is not a ping at all.
    if (v_result ->> 'policy_net_7d_usd')::numeric <> -1.0 then
        raise exception 'the policy diagnostic must exclude holdout and unscored rows: %',
            v_result;
    end if;
    -- Measured lift moves the net, and only POWERED days count toward the
    -- freeze licence: an under-powered comparison carries no evidence.
    insert into public.warm_control_savings_daily (
        organization_id, provider, day, treated_units, control_units,
        control_lift_usd)
    values (v_org, 'anthropic', v_day - 1, 5, 5, 0.25),
           (v_org, 'anthropic', v_day - 2, 5, 5, 0.25),
           (v_org, 'anthropic', v_day - 3, 1, 1, 0);
    select entry into v_result from public.warm_guardrail_scan(7) entry
     where (entry ->> 'organization_id')::uuid = v_org;
    if (v_result ->> 'net_7d_usd')::numeric <> -0.5
       or (v_result ->> 'control_days')::integer <> 2
       or (v_result ->> 'control_units')::integer <> 11 then
        raise exception 'the guardrail must count only powered control days: %',
            v_result;
    end if;
    delete from public.warm_control_savings_daily savings
     where savings.organization_id = v_org;
    if (v_result ->> 'mode') <> 'learned' then
        raise exception 'an absent warm_org_mode row must read as learned: %', v_result;
    end if;
    v_result := public.warm_org_mode_set(v_org, 'anthropic', 'frozen', 'guardrail net7=-1.0000');
    if (v_result ->> 'mode') <> 'frozen' then
        raise exception 'warm_org_mode_set must freeze: %', v_result;
    end if;
    select entry into v_result from public.warm_guardrail_scan(7) entry
     where (entry ->> 'organization_id')::uuid = v_org;
    if (v_result ->> 'mode') <> 'frozen' then
        raise exception 'the scan must report the frozen mode: %', v_result;
    end if;
    v_raised := false;
    begin
        perform public.warm_org_mode_set(v_org, 'anthropic', 'off', '');
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'warm_org_mode_set must reject an unknown mode';
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 7. FROZEN IS FLAT PRIORS. With the hazard flag ON and a state row that
    -- would otherwise move P(alive) off 1, a frozen pair is scored exactly as
    -- a customer the model has never seen -- and the stop-loss predicate comes
    -- back with the flat priors, so a retired prefix is not even a candidate.
    -- ------------------------------------------------------------------
    insert into public.warm_customer_state (
        organization_id, customer_id, provider, hazard_n, hazard_e,
        events_total, first_seen_at, last_seen_at, last_update_at)
    -- Shaped so the LEARNED run is unambiguous: a dense hazard in this bucket
    -- puts p_return_v2 far above the ROI floor, while a single arrival six days
    -- ago puts P(alive) near one half -- low enough to prove the model was
    -- read, high enough that the index stays above the pacing floor. A frozen
    -- pair must ignore all of it.
    values (v_org, v_cust, 'anthropic',
            jsonb_build_object(v_bucket, 500.0), jsonb_build_object(v_bucket, 1.0),
            1, now() - interval '6 days', now() - interval '6 days',
            now() - interval '6 days')
    -- warm_prefix_observe already touched this row into existence; the fixture
    -- overwrites it with a shape whose P(alive) is provably below 1.
    on conflict (organization_id, customer_id, provider) do update
       set hazard_n = excluded.hazard_n,
           hazard_e = excluded.hazard_e,
           events_total = excluded.events_total,
           first_seen_at = excluded.first_seen_at,
           last_seen_at = excluded.last_seen_at,
           last_update_at = excluded.last_update_at;
    -- The peer keeps NO state row, so under hazard_v2 it falls back to the
    -- lifetime histogram -- the cold-start path -- and is admitted purely on
    -- the strength of the stop-loss predicate being retired. That is what makes
    -- the claimed-count difference between the two runs attributable to the
    -- predicate and to nothing else.
    delete from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_peer;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0, consecutive_misses = 0
     where prefix.organization_id = v_org;
    update public.warm_prefixes prefix
       set consecutive_misses = 5
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true, 0) as t(value);
    if v_count <> 1 then
        raise exception 'a frozen pair must re-apply the stop-loss, claimed %', v_count;
    end if;
    select entry.p_alive, entry.chain_cost_usd into v_p_alive, v_chain
      from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.prefix_hash = v_hash_a
     order by entry.id desc limit 1;
    if v_p_alive <> 1 or v_chain <> 0 then
        raise exception 'a frozen pair must be scored at flat priors: p_alive=%, chain=%',
            v_p_alive, v_chain;
    end if;
    -- The control: unfrozen, the SAME state row does move P(alive) off 1, so
    -- the assertion above is not vacuous.
    perform public.warm_org_mode_set(v_org, 'anthropic', 'learned', 'assertion control');
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0
     where prefix.organization_id = v_org;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true, 0) as t(value);
    if v_count <> 2 then
        raise exception 'a learned pair under hazard_v2 must retire the stop-loss, claimed %',
            v_count;
    end if;
    select entry.p_alive into v_p_alive
      from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.prefix_hash = v_hash_a
     order by entry.id desc limit 1;
    if v_p_alive >= 1 then
        raise exception 'the control state row must move P(alive) off 1, got %', v_p_alive;
    end if;
    delete from public.warm_decision_log where organization_id = v_org;
    delete from public.warm_budget_ledger where organization_id = v_org;
    delete from public.warm_customer_state where organization_id = v_org;

    -- ------------------------------------------------------------------
    -- 8. THE CANARY'S DOLLAR FENCE. The reserve is one conditional update, so
    -- the second request that would cross the cap loses rather than both
    -- winning. Settle adjusts to actual and floors at zero.
    -- ------------------------------------------------------------------
    v_result := public.warm_canary_reserve('anthropic', v_day, 0.06, 0.10);
    if not (v_result ->> 'allowed')::boolean then
        raise exception 'the first canary reserve must be allowed: %', v_result;
    end if;
    v_result := public.warm_canary_reserve('anthropic', v_day, 0.06, 0.10);
    if (v_result ->> 'allowed')::boolean then
        raise exception 'a canary reserve crossing the cap must be denied: %', v_result;
    end if;
    select spent_usd, probes into v_reserve, v_count
      from public.warm_canary_ledger where day = v_day and provider = 'anthropic';
    if v_reserve <> 0.06 or v_count <> 1 then
        raise exception 'the denied reserve must move nothing: spent=%, probes=%',
            v_reserve, v_count;
    end if;
    perform public.warm_canary_settle('anthropic', v_day, 0.06, 0.02);
    select spent_usd into v_reserve
      from public.warm_canary_ledger where day = v_day and provider = 'anthropic';
    if v_reserve <> 0.02 then
        raise exception 'the canary settle must adjust to actual, got %', v_reserve;
    end if;
    perform public.warm_canary_settle('anthropic', v_day, 5.0, 0.0);
    select spent_usd into v_reserve
      from public.warm_canary_ledger where day = v_day and provider = 'anthropic';
    if v_reserve <> 0 then
        raise exception 'the canary settle must floor at zero, got %', v_reserve;
    end if;
    -- OpenAI is unreachable at the schema level too, not only in the worker.
    v_raised := false;
    begin
        perform public.warm_canary_reserve('openai', v_day, 0.01, 1.0);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'the canary must refuse openai';
    end if;

    -- Probe lifecycle: due only when pending and past its gap, and the mark is
    -- fenced on 'pending' so a duplicate cycle cannot flip a failed probe to
    -- done and advance the ladder on a measurement that never happened.
    v_result := public.warm_canary_probe_insert(
        'anthropic', 'claude-haiku-4-5', 'anthropic:seed:0', 5000,
        now() - interval '10 minutes', 300);
    v_probe_id := (v_result ->> 'id')::bigint;
    perform public.warm_canary_probe_insert(
        'anthropic', 'claude-haiku-4-5', 'anthropic:seed:1', 5000, now(), 3600);
    select count(*) into v_count from public.warm_canary_probe_due('anthropic', 50);
    if v_count <> 1 then
        raise exception 'only a due pending probe may be returned, got %', v_count;
    end if;
    perform public.warm_canary_probe_mark(v_probe_id, 'failed');
    perform public.warm_canary_probe_mark(v_probe_id, 'done');
    select state into v_decision from public.warm_canary_probes where id = v_probe_id;
    if v_decision <> 'failed' then
        raise exception 'the probe mark must be write-once, got %', v_decision;
    end if;
    v_result := public.warm_canary_probe_stats('anthropic');
    if (v_result ->> 'pending')::integer <> 1
       or (v_result ->> 'failed')::integer <> 1 then
        raise exception 'the probe stats are wrong: %', v_result;
    end if;

    -- ------------------------------------------------------------------
    -- 9. RETENTION. purge_warm_state ages all three new aggregate classes.
    -- None carries a tenant key, so none belongs in compliance_run_retention.
    -- ------------------------------------------------------------------
    update public.warm_canary_probes set created_at = now() - interval '31 days';
    insert into public.warm_canary_ledger (day, provider, probes, spent_usd)
    values (v_day - 401, 'deepseek', 1, 0.01);
    insert into public.warm_control_savings_daily (
        organization_id, provider, day, control_lift_usd)
    values (v_org, 'anthropic', v_day - 401, 1.0),
           (v_org, 'anthropic', v_day - 3, 1.0);
    v_result := public.purge_warm_state(7);
    if (v_result ->> 'canary_probes_deleted')::integer < 2
       or (v_result ->> 'canary_ledger_deleted')::integer < 1
       or (v_result ->> 'control_savings_deleted')::integer < 1 then
        raise exception 'purge_warm_state must age the 202608100005 classes: %', v_result;
    end if;
    if not exists (select 1 from public.warm_canary_ledger
                    where day = v_day and provider = 'anthropic') then
        raise exception 'the recent canary ledger row must survive the purge';
    end if;
    if not exists (select 1 from public.warm_control_savings_daily
                    where organization_id = v_org and day = v_day - 3) then
        raise exception 'a recent control comparison must survive the purge';
    end if;

    -- ------------------------------------------------------------------
    -- 10. COMPLIANCE. The two organization-keyed tables ride tenant erasure
    -- and the tenant export. The two canary tables are PLANE G and must be
    -- absent from both -- asserted on the function text, the same way the
    -- harness already asserts the warm_ttl_observations posture.
    -- ------------------------------------------------------------------
    insert into public.warm_control_savings_daily (
        organization_id, provider, day, control_lift_usd)
    values (v_org, 'anthropic', v_day - 4, 2.5);
    perform public.warm_org_mode_set(v_org, 'anthropic', 'frozen', 'export fixture');
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_export_request, v_org, 'export', 'tenant', 'approved',
        'evidence:warm-guardrail:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-guardrail',
        'system:warm-guardrail');
    if not exists (
        select 1 from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
         where record ->> 'record_type' = 'warming_control_savings'
    ) then
        raise exception 'the tenant export must carry the control comparison';
    end if;
    if not exists (
        select 1 from public.compliance_export_tenant(
            v_org, v_export_request, 'brevitas_admin:assertion') record
         where record ->> 'record_type' = 'warming_org_mode'
    ) then
        raise exception 'the tenant export must carry the guardrail verdict';
    end if;
    -- The predicate matches a READ of the table, not a mention of its name:
    -- the export deliberately CARRIES a comment naming both canary tables and
    -- saying why they are absent, and that comment is documentation worth
    -- keeping rather than a violation.
    if pg_catalog.pg_get_functiondef(
           'public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
       like '%from public.warm_canary_%'
       or pg_catalog.pg_get_functiondef(
           'public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       like '%from public.warm_canary_%' then
        raise exception 'the canary tables are Plane G and must not be exported';
    end if;
    if pg_catalog.pg_get_functiondef(
           'public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       like '%from public.warm_control_savings_daily%' then
        raise exception 'the control comparison has no subject key and must not be in the subject export';
    end if;
    if pg_catalog.pg_get_functiondef(
           'public.compliance_delete_tenant(uuid,uuid,text)'::regprocedure)
       not like '%Plane G%' then
        raise exception 'compliance_delete_tenant must state the canary Plane G posture';
    end if;

    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_tenant_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-guardrail:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-guardrail',
        'system:warm-guardrail');
    select count(*) into v_count from public.warm_canary_probes;
    perform public.compliance_delete_tenant(
        v_org, v_tenant_request, 'brevitas_admin:assertion');
    if exists (select 1 from public.warm_control_savings_daily
                where organization_id = v_org) then
        raise exception 'tenant erasure must clear the control comparison';
    end if;
    if exists (select 1 from public.warm_org_mode where organization_id = v_org) then
        raise exception 'tenant erasure must clear the guardrail verdict';
    end if;
    if (select count(*) from public.warm_canary_probes) <> v_count then
        raise exception 'tenant erasure must not touch the Plane G canary tables';
    end if;

    raise notice '202608100005 guardrail assertions passed';
end;
$$;

rollback;
