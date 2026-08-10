-- Phase 1 learned warming (202608100002): the dollar index, density claim
-- ordering and the lambda pacing dual.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000d0001';
    v_org_off uuid := '00000000-0000-4000-8000-0000000d0002';
    v_org_on uuid := '00000000-0000-4000-8000-0000000d0003';
    v_cust_off uuid := '00000000-0000-4000-8000-0000000d0004';
    v_cust_on uuid := '00000000-0000-4000-8000-0000000d0005';
    v_hash_a text := repeat('d1', 32);
    v_hash_b text := repeat('d2', 32);
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric,boolean,numeric,numeric,numeric,numeric)';
    v_record_signature text := 'public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,uuid,text,integer,text,numeric)';
    v_column text;
    v_count integer;
    v_off_count integer;
    v_on_count integer;
    v_ledger record;
    v_row record;
    v_decision text;
    v_raised boolean;
begin
    -- ------------------------------------------------------------------
    -- 0. Signatures. Both are REPLACED, never overloaded: two candidates make
    -- every existing call ambiguous and PostgREST would 300.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric)') is not null then
        raise exception 'warm_due_claim must be the seventeen-argument dedup form only';
    end if;
    if to_regprocedure(v_record_signature) is null
       or to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)') is not null
       or to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric)') is not null then
        raise exception 'warm_decision_record must be the twenty-seven-argument form only';
    end if;
    if has_function_privilege('anon', v_claim_signature, 'execute')
       or has_function_privilege('authenticated', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_claim_signature, 'execute')
       or has_function_privilege('anon', v_record_signature, 'execute')
       or has_function_privilege('authenticated', v_record_signature, 'execute')
       or not has_function_privilege('service_role', v_record_signature, 'execute') then
        raise exception 'warming RPC privileges are wrong after the signature change';
    end if;

    -- ------------------------------------------------------------------
    -- 1. Columns and their bounds. All eight are nullable: NULL is what a row
    -- the index machinery did not score looks like, and that is what keeps a
    -- flag-off row indistinguishable from a pre-Phase-1 row.
    -- ------------------------------------------------------------------
    foreach v_column in array array[
        'index_score', 'index_density', 'v_hit_usd', 'chain_cost_usd',
        'c_belief_usd', 'p_alive', 'organic_multiplier', 'lambda_index'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute attribute
             where attribute.attrelid = 'public.warm_decision_log'::regclass
               and attribute.attname = v_column
               and not attribute.attisdropped
               and not attribute.attnotnull
        ) then
            raise exception 'warm_decision_log.% must exist and be nullable', v_column;
        end if;
    end loop;
    foreach v_column in array array['lambda_index', 'lambda_updated_at'] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute attribute
             where attribute.attrelid = 'public.warm_budget_ledger'::regclass
               and attribute.attname = v_column
               and not attribute.attisdropped
        ) then
            raise exception 'warm_budget_ledger.% must exist', v_column;
        end if;
    end loop;
    -- The bounds are the contract the writer validates against; without them
    -- the writer's checks are the only line and a direct DML path has none.
    for v_column in
        select unnest(array['v_hit_usd', 'chain_cost_usd', 'c_belief_usd',
                            'p_alive', 'organic_multiplier', 'lambda_index'])
    loop
        if not exists (
            select 1
              from pg_catalog.pg_constraint constraint_row
             where constraint_row.conrelid = 'public.warm_decision_log'::regclass
               and constraint_row.contype = 'c'
               and pg_catalog.pg_get_constraintdef(constraint_row.oid)
                   like '%' || v_column || '%'
        ) then
            raise exception 'warm_decision_log.% has no CHECK', v_column;
        end if;
    end loop;
    if not exists (
        select 1 from pg_catalog.pg_constraint constraint_row
         where constraint_row.conrelid = 'public.warm_budget_ledger'::regclass
           and constraint_row.contype = 'c'
           and pg_catalog.pg_get_constraintdef(constraint_row.oid) like '%lambda_index%'
    ) then
        raise exception 'warm_budget_ledger.lambda_index has no CHECK';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'warm-index-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org_off, 'Warm index control'), (v_org_on, 'Warm index treatment');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust_off, v_org_off, 'warm-index-off'),
           (v_cust_on, v_org_on, 'warm-index-on');
    perform public.warm_credentials_upsert(
        v_org_off, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);
    perform public.warm_credentials_upsert(
        v_org_on, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    -- ------------------------------------------------------------------
    -- 2. Decision vocabulary. The three Phase 1 values are admitted and an
    -- unknown one is still refused -- the constraint was widened, not dropped.
    -- ------------------------------------------------------------------
    foreach v_decision in array array['skipped_lambda', 'envelope_denied', 'beta_denied'] loop
        perform public.warm_decision_record(
            v_org_off, v_cust_off, 'anthropic', v_hash_a, v_decision,
            0.5, 0.11, 0.375, 100000, null, 10, null, null, null, null,
            3.5, 9.3, 3.41, 0, 0.375, 1.0, 1.0, 0.25);
    end loop;
    if (select count(*) from public.warm_decision_log) <> 3 then
        raise exception 'the widened decision vocabulary did not persist';
    end if;
    v_raised := false;
    begin
        perform public.warm_decision_record(
            v_org_off, v_cust_off, 'anthropic', v_hash_a, 'bogus',
            0.5, 0.11, 0.375, 100000, null, 10);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'an unknown decision value must still be refused';
    end if;
    -- The table's own CHECK, not just the writer's validation.
    v_raised := false;
    begin
        insert into public.warm_decision_log (
            organization_id, customer_id, provider, prefix_hash, decision,
            p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count
        ) values (v_org_off, v_cust_off, 'anthropic', v_hash_a, 'bogus',
                  0.5, 0.11, 0.375, 100000, 10);
    exception when check_violation then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'warm_decision_log.decision must still be constrained';
    end if;
    delete from public.warm_decision_log;

    -- ------------------------------------------------------------------
    -- 3. Two identical two-candidate orgs, one claimed with the index off and
    -- one with it on. At flat priors the index reduces to p/b - 1 and lambda
    -- starts at 0, which IS the provider break-even the v1 ROI gate applied, so
    -- the two runs must agree candidate for candidate.
    -- ------------------------------------------------------------------
    perform public.warm_prefix_observe(
        v_org_off, v_cust_off, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org_off, v_cust_off, 'anthropic', v_hash_b, 'enc:payload', 40000,
        300, 60, false, 0.15, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org_on, v_cust_on, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org_on, v_cust_on, 'anthropic', v_hash_b, 'enc:payload', 40000,
        300, 60, false, 0.15, 'claude-sonnet-4-5');

    -- Only the control org is due, so the claim below scores exactly its rows.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.organization_id = v_org_off;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() + interval '1 hour'
     where prefix.organization_id = v_org_on;
    select count(*) into v_off_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, false) as t(value);

    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org_off
           and (entry.index_score is not null or entry.index_density is not null
                or entry.v_hit_usd is not null or entry.chain_cost_usd is not null
                or entry.c_belief_usd is not null or entry.p_alive is not null
                or entry.organic_multiplier is not null
                or entry.lambda_index is not null)
    ) then
        raise exception 'a flag-off decision must carry no index belief';
    end if;
    select * into v_ledger from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org_off;
    if v_ledger.lambda_index <> 0 or v_ledger.lambda_updated_at is not null then
        raise exception 'a flag-off claim must not touch the pacing dual: %',
            to_jsonb(v_ledger);
    end if;

    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() + interval '1 hour'
     where prefix.organization_id = v_org_off;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.organization_id = v_org_on;
    select count(*) into v_on_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000)
        as t(value);

    if v_off_count <> v_on_count then
        raise exception 'the index must claim the same rows at flat priors: % vs %',
            v_off_count, v_on_count;
    end if;
    if v_off_count = 0 then
        raise exception 'the equivalence fixture claimed nothing and is vacuous';
    end if;
    if exists (
        select 1
          from public.warm_decision_log control
          join public.warm_decision_log treatment
            on treatment.organization_id = v_org_on
           and treatment.prefix_hash = control.prefix_hash
         where control.organization_id = v_org_off
           and control.decision <> treatment.decision
    ) then
        raise exception 'flag-on and flag-off disagreed on a decision label';
    end if;

    -- ------------------------------------------------------------------
    -- 4. What the flag-on rows carry. The index is dimensionless (p/b - 1 at
    -- flat priors), the dollar components price off ping_reserve_usd and the
    -- provider write premium (NOT the floored ledger reservation), and the dual
    -- in force is logged on every scored row.
    -- ------------------------------------------------------------------
    for v_row in
        select * from public.warm_decision_log entry
         where entry.organization_id = v_org_on
    loop
        if v_row.index_score is null or v_row.index_density is null
           or v_row.p_alive is null or v_row.organic_multiplier is null
           or v_row.c_belief_usd is null or v_row.chain_cost_usd is null
           or v_row.v_hit_usd is null then
            raise exception 'a flag-on decision must carry the index belief: %',
                to_jsonb(v_row);
        end if;
        if v_row.p_alive <> 1 or v_row.organic_multiplier <> 1
           or v_row.chain_cost_usd <> 0 then
            raise exception 'Phase 1 priors must be flat: %', to_jsonb(v_row);
        end if;
        if abs(v_row.index_score - (v_row.p_return / 0.11 - 1)) > 1e-9 then
            raise exception 'index_score must be p/b - 1 at flat priors: %',
                to_jsonb(v_row);
        end if;
        -- THE DOLLARS PRICE OFF ping_reserve_usd AND THE WRITE PREMIUM, not
        -- off the (floored) ledger reservation. This fixture seeds
        -- ping_reserve_usd equal to the flat-floor reservation, so
        -- reserve_usd / 1.25 is the prefix's base input dollars; f inverts the
        -- break-even, f = b/(1+b) = 0.11/1.11.
        --
        -- c_belief used to be the WHOLE reservation and v_hit used to be
        -- reserve/b, which overstated what a return actually saves by 12x here
        -- (and 50x on deepseek) in the compliance exports that are the
        -- organization's audit trail.
        if v_row.c_belief_usd
               <> round(v_row.reserve_usd / 1.25 * (0.11 / 1.11), 10)
           or v_row.v_hit_usd
               <> round(v_row.reserve_usd / 1.25 * (1 - 0.11 / 1.11), 10) then
            raise exception 'the dollar components must price off ping_reserve_usd / w: %',
                to_jsonb(v_row);
        end if;
        -- The identity the flag-on/flag-off equivalence rests on: the index is
        -- the same number computed from the dollars or from the ratio, because
        -- v_hit / c_belief = (1-f)/f = 1/b exactly.
        -- Relative tolerance: the dollar components are stored at 10 decimal
        -- places, so on a sub-cent c_belief the last rounded digit is worth
        -- ~1e-8 of the ratio. The identity is exact in real arithmetic.
        if abs((v_row.p_return * v_row.v_hit_usd
                - v_row.chain_cost_usd - v_row.c_belief_usd)
               / v_row.c_belief_usd - v_row.index_score)
           > 1e-6 * greatest(1, abs(v_row.index_score)) then
            raise exception 'index_score must equal its own dollar components: %',
                to_jsonb(v_row);
        end if;
        -- index_density is a DIAGNOSTIC now, not the ordering key, but it is
        -- still the index over the reservation and still logged.
        if abs(v_row.index_density
               - v_row.index_score / greatest(v_row.reserve_usd, 1e-10)) > 1e-9 then
            raise exception 'index_density must be index per reserved dollar: %',
                to_jsonb(v_row);
        end if;
        if v_row.lambda_index is null then
            raise exception 'a scored row must record the dual in force: %',
                to_jsonb(v_row);
        end if;
    end loop;
    -- Under pace, the dual never leaves its floor -- which is exactly the
    -- provider break-even (index = 0 <=> p_eff = f/(1-f)).
    select * into v_ledger from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org_on;
    if v_ledger.lambda_index <> 0 or v_ledger.lambda_updated_at is null then
        raise exception 'an under-paced claim must persist lambda = 0: %',
            to_jsonb(v_ledger);
    end if;

    -- ------------------------------------------------------------------
    -- 5. The pacing gate denies, and it denies before any money moves.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log;
    update public.warm_budget_ledger ledger
       set reserved_usd = 0, spent_usd = 0, lambda_index = 100000
     where ledger.organization_id = v_org_on;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null
     where prefix.organization_id = v_org_on;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000)
        as t(value);
    if v_count <> 0 then
        raise exception 'a dual above every index must claim nothing';
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org_on
           and (entry.decision <> 'skipped_lambda' or entry.lambda_index is null
                or entry.claim_token is not null)
    ) then
        raise exception 'the pacing denial is malformed';
    end if;
    select * into v_ledger from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org_on;
    if v_ledger.reserved_usd <> 0 or v_ledger.spent_usd <> 0 then
        raise exception 'a pacing denial must reserve and spend nothing: %',
            to_jsonb(v_ledger);
    end if;
    if v_ledger.lambda_index > 1000 then
        raise exception 'the dual must respect its ceiling: %', to_jsonb(v_ledger);
    end if;

    -- ------------------------------------------------------------------
    -- 5b. THE ORDERING KEY IS index_score, NOT index_score / reserve.
    --
    -- index_score already divides by c_belief, so it is already value per
    -- belief-dollar; dividing it again by the reservation makes the key
    -- value-per-dollar-squared, and under a binding budget that prefers small
    -- cheap arms to the large valuable ones the budget exists to allocate.
    --
    -- The two candidates below are built so the two keys DISAGREE. hash_a
    -- carries 100000 tokens (reserve 0.375) with p = 0.8, hash_b carries 40000
    -- (reserve 0.15) with p = 0.4:
    --
    --   index:          a = 0.8/0.11 - 1 = 6.27   b = 0.4/0.11 - 1 = 2.64
    --   index/reserve:  a = 16.7                  b = 17.6
    --
    -- The daily budget admits exactly one, so WHICH one is claimed is the
    -- whole assertion.
    -- ------------------------------------------------------------------
    delete from public.warm_decision_log;
    delete from public.warm_budget_ledger;
    update public.warm_credentials cred
       set daily_budget_usd = 0.4
     where cred.organization_id = v_org_on;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null, pings_today = 0,
           arrival_count = 10,
           hour_histogram = jsonb_build_object(
               ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text,
               case when prefix.prefix_hash = v_hash_a then 8 else 4 end)
     where prefix.organization_id = v_org_on;
    -- hash_b comes due FIRST, so a FIFO or density order would take it.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '10 minutes'
     where prefix.organization_id = v_org_on and prefix.prefix_hash = v_hash_a;
    select count(*) into v_count from public.warm_due_claim(
        10, 3.75, 5, 0.11, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000)
        as t(value);
    if v_count <> 1 then
        raise exception 'the binding budget must admit exactly one candidate, admitted %',
            v_count;
    end if;
    if not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org_on
           and entry.prefix_hash = v_hash_a and entry.decision = 'pinged'
    ) or not exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org_on
           and entry.prefix_hash = v_hash_b and entry.decision = 'budget_denied'
    ) then
        raise exception 'the claim must order by index_score, not by index per reserved dollar: %',
            (select jsonb_agg(jsonb_build_object(
                        'hash', entry.prefix_hash, 'decision', entry.decision,
                        'index', entry.index_score, 'density', entry.index_density))
               from public.warm_decision_log entry
              where entry.organization_id = v_org_on);
    end if;
    -- The premise, asserted so the case above cannot quietly stop
    -- discriminating: the density key ranks these two the OTHER way round.
    if not exists (
        select 1 from public.warm_decision_log winner, public.warm_decision_log loser
         where winner.organization_id = v_org_on and winner.prefix_hash = v_hash_a
           and loser.organization_id = v_org_on and loser.prefix_hash = v_hash_b
           and winner.index_score > loser.index_score
           and winner.reserve_usd > loser.reserve_usd
           and winner.index_density < loser.index_density
    ) then
        raise exception 'the ordering fixture no longer discriminates index from density';
    end if;
    update public.warm_credentials cred
       set daily_budget_usd = 10.0
     where cred.organization_id = v_org_on;
    delete from public.warm_decision_log;
    delete from public.warm_budget_ledger;

    -- ------------------------------------------------------------------
    -- 6. Bounds. Out-of-range pacing arguments are refused; null reads as the
    -- default, so a caller that predates Phase 1 cannot fail a claim.
    -- ------------------------------------------------------------------
    v_raised := false;
    begin
        perform public.warm_due_claim(
            10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0, 1000);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a zero pacing gain must be rejected';
    end if;
    v_raised := false;
    begin
        perform public.warm_due_claim(
            10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000001);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'a pacing ceiling above 1e6 must be rejected';
    end if;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, null, null, null);

    -- ------------------------------------------------------------------
    -- 7. The exports enumerate the new belief, or the org cannot audit the
    -- policy that spent its money.
    -- ------------------------------------------------------------------
    if pg_catalog.pg_get_functiondef('public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
       not like '%index_score%'
       or pg_catalog.pg_get_functiondef('public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       not like '%lambda_index%' then
        raise exception 'the compliance exports do not carry the index belief';
    end if;

    raise notice '202608100002 index assertions passed';
end;
$$;

rollback;
