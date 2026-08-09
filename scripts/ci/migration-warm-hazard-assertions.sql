-- Phase 1 learned warming (202608100003): the decayed hierarchical hazard,
-- P(alive), the keep-alive chain, the erasure suppression list and the
-- periodicity flag.
--
-- Everything runs inside one transaction and rolls back, so the file is
-- rerunnable and leaves no fixture row behind.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000e0001';
    v_org uuid := '00000000-0000-4000-8000-0000000e0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000e0003';
    v_peer uuid := '00000000-0000-4000-8000-0000000e0004';
    v_aggregate uuid := '00000000-0000-0000-0000-000000000000';
    v_hash_a text := repeat('e1', 32);
    v_hash_b text := repeat('e2', 32);
    v_request uuid := '00000000-0000-4000-8000-0000000e0005';
    v_claim_signature text := 'public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean,numeric)';
    v_table text;
    v_column text;
    v_count integer;
    v_off_count integer;
    v_on_count integer;
    v_state public.warm_customer_state%rowtype;
    v_aggregate_state public.warm_customer_state%rowtype;
    v_row record;
    v_raised boolean;
    v_bucket text;
begin
    -- ------------------------------------------------------------------
    -- 0. Signature. REPLACED, never overloaded: two candidates make every
    -- existing call ambiguous and PostgREST would 300.
    -- ------------------------------------------------------------------
    if to_regprocedure(v_claim_signature) is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric,boolean)') is not null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb,double precision,boolean,numeric,numeric)') is not null then
        raise exception 'warm_due_claim must be the sixteen-argument beta form only';
    end if;
    if has_function_privilege('anon', v_claim_signature, 'execute')
       or has_function_privilege('authenticated', v_claim_signature, 'execute')
       or not has_function_privilege('service_role', v_claim_signature, 'execute') then
        raise exception 'warm_due_claim privileges are wrong after the signature change';
    end if;
    foreach v_column in array array[
        'public.warm_customer_state_touch(uuid,uuid,text,timestamptz)',
        'public.warm_customer_state_set_regime(uuid,uuid,text,text,numeric)',
        'public.warm_customer_arrival_series(integer,integer)'
    ] loop
        if to_regprocedure(v_column) is null then
            raise exception 'the hazard RPC % is missing', v_column;
        end if;
        if has_function_privilege('anon', v_column, 'execute')
           or has_function_privilege('authenticated', v_column, 'execute')
           or not has_function_privilege('service_role', v_column, 'execute') then
            raise exception 'the hazard RPC % has the wrong privileges', v_column;
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 1. Tables. RLS on, zero policies, zero direct DML for every PostgREST
    -- role: behavioural state is reachable only through the definer routines.
    -- ------------------------------------------------------------------
    foreach v_table in array array[
        'warm_customer_state', 'warm_modeling_suppression'
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
        foreach v_column in array array['anon', 'authenticated', 'service_role'] loop
            if has_table_privilege(v_column, 'public.' || v_table, 'select')
               or has_table_privilege(v_column, 'public.' || v_table, 'insert')
               or has_table_privilege(v_column, 'public.' || v_table, 'update')
               or has_table_privilege(v_column, 'public.' || v_table, 'delete') then
                raise exception 'public.% must grant % nothing', v_table, v_column;
            end if;
        end loop;
    end loop;
    -- The retention class's evidence columns, bounded like every other class.
    foreach v_column in array array[
        'warm_customer_state_candidates', 'warm_customer_state_deleted'
    ] loop
        if not exists (
            select 1 from pg_catalog.pg_attribute attribute
             where attribute.attrelid = 'public.compliance_retention_runs'::regclass
               and attribute.attname = v_column
               and not attribute.attisdropped
        ) then
            raise exception 'compliance_retention_runs.% must exist', v_column;
        end if;
        if not exists (
            select 1 from pg_catalog.pg_constraint
             where conrelid = 'public.compliance_retention_runs'::regclass
               and conname = 'compliance_retention_runs_' || v_column || '_check'
        ) then
            raise exception 'compliance_retention_runs.% has no bound', v_column;
        end if;
    end loop;

    insert into auth.users (id, email)
    values (v_actor, 'warm-hazard-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm hazard fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-hazard-subject'),
           (v_peer, v_org, 'warm-hazard-peer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 24);

    -- ------------------------------------------------------------------
    -- 2. The observer writes BOTH rows, unconditionally. The organization
    -- aggregate is what the per-customer posterior shrinks toward, so a
    -- migration that wrote only one of them would leave the hierarchy with no
    -- middle level.
    -- ------------------------------------------------------------------
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash_a, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_b, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');

    select * into v_state from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_cust
       and state.provider = 'anthropic';
    select * into v_aggregate_state from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_aggregate
       and state.provider = 'anthropic';
    if v_state.events_total <> 1 or v_aggregate_state.events_total <> 2 then
        raise exception 'the observer must touch the customer and the aggregate: % / %',
            to_jsonb(v_state), to_jsonb(v_aggregate_state);
    end if;
    v_bucket := ((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer)::text;
    if coalesce((v_state.hazard_n ->> v_bucket)::numeric, 0) < 1 then
        raise exception 'the arrival did not land in its hour-of-week bucket: %',
            to_jsonb(v_state);
    end if;
    -- A first observation accrues no exposure: there is no gap to accrue over.
    if v_state.hazard_e <> '{}'::jsonb then
        raise exception 'a first observation must accrue no exposure: %',
            to_jsonb(v_state);
    end if;

    -- ------------------------------------------------------------------
    -- 3. The stop-loss predicate, per flag. This is the ONE behavioural change
    -- the hazard flag makes to which rows are even candidates, so it is
    -- asserted in both directions on the same fixture rather than argued.
    -- ------------------------------------------------------------------
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null,
           arrival_count = 20,
           hour_histogram = jsonb_build_object(v_bucket, 20)
     where prefix.organization_id = v_org;
    -- The peer is retired by the v1 stop-loss and must not be scored at all.
    update public.warm_prefixes prefix
       set consecutive_misses = 5
     where prefix.organization_id = v_org and prefix.customer_id = v_peer;

    select count(*) into v_off_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        false) as t(value);
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_peer
    ) then
        raise exception 'a stop-lossed prefix must never be scored at hazard_v2 = false';
    end if;
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org
           and (entry.p_alive <> 1 or entry.chain_cost_usd <> 0)
    ) then
        raise exception 'hazard_v2 = false must leave the priors flat';
    end if;

    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null
     where prefix.organization_id = v_org;
    -- Give both customers a posterior with real mass, and enough silence for
    -- P(alive) to be strictly below 1.
    update public.warm_customer_state state
       set hazard_n = jsonb_build_object(v_bucket, 20),
           hazard_e = jsonb_build_object(v_bucket, 5),
           events_total = 20,
           first_seen_at = clock_timestamp() - interval '30 days',
           last_seen_at = clock_timestamp() - interval '1 second',
           last_update_at = clock_timestamp() - interval '1 second'
     where state.organization_id = v_org and state.customer_id <> v_aggregate;

    select count(*) into v_on_count from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true) as t(value);
    -- THE POINT: the retired arm is scored again, by exactly one row.
    select count(*) into v_count from public.warm_decision_log entry
     where entry.organization_id = v_org and entry.customer_id = v_peer;
    if v_count <> 1 then
        raise exception 'hazard_v2 must score the stop-lossed arm: %', v_count;
    end if;
    for v_row in
        select * from public.warm_decision_log entry
         where entry.organization_id = v_org
    loop
        if v_row.p_alive is null or v_row.p_alive >= 1 then
            raise exception 'a v2-scored row must carry P(alive) < 1: %',
                to_jsonb(v_row);
        end if;
        if coalesce(v_row.chain_cost_usd, 0) <= 0 then
            raise exception 'a v2-scored row must price its keep-alive chain: %',
                to_jsonb(v_row);
        end if;
        -- p_return is the hazard posterior, not the lifetime histogram ratio
        -- (which this fixture pinned at 1.0).
        if v_row.p_return >= 1 then
            raise exception 'p_return must be the hazard posterior: %',
                to_jsonb(v_row);
        end if;
        -- chain_cost is n_chain PINGS priced at c_belief each -- the expected
        -- keep-alive dollars -- not at the (floored) ledger reservation, which
        -- is money safety for the daily ceiling and a different quantity.
        if abs(v_row.index_score
               - (v_row.p_return * v_row.p_alive / 0.11 - 1
                  - round(v_row.chain_cost_usd / v_row.c_belief_usd))) > 1e-9 then
            raise exception 'index_score must be p_eff/b - 1 - n_chain: %',
                to_jsonb(v_row);
        end if;
    end loop;

    -- ------------------------------------------------------------------
    -- 4. Suppression. The observer refuses to write for an erased subject, and
    -- the scorer treats them as one it has never seen.
    -- ------------------------------------------------------------------
    insert into public.warm_modeling_suppression (organization_id, customer_id)
    values (v_org, v_peer);
    select events_total into v_count from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_aggregate
       and state.provider = 'anthropic';
    perform public.warm_prefix_observe(
        v_org, v_peer, 'anthropic', v_hash_b, 'enc:payload', 100000,
        300, 60, false, 0.375, 'claude-sonnet-4-5');
    if (select events_total from public.warm_customer_state state
         where state.organization_id = v_org and state.customer_id = v_aggregate
           and state.provider = 'anthropic') <> v_count then
        raise exception 'a suppressed subject must not reach the organization aggregate';
    end if;
    if public.warm_customer_state_touch(v_org, v_peer, 'anthropic') ->> 'status'
       <> 'suppressed' then
        raise exception 'a direct touch must refuse a suppressed subject';
    end if;

    delete from public.warm_decision_log where organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           claim_token = null
     where prefix.organization_id = v_org;
    perform public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null, 0, true, 0.2, 1000,
        true);
    if exists (
        select 1 from public.warm_decision_log entry
         where entry.organization_id = v_org and entry.customer_id = v_peer
           and (entry.p_alive <> 1 or entry.chain_cost_usd <> 0)
    ) then
        raise exception 'a suppressed subject must be scored at flat priors';
    end if;

    -- ------------------------------------------------------------------
    -- 5. The regime flag, informational in Phase 1.
    -- ------------------------------------------------------------------
    if public.warm_customer_state_set_regime(
           v_org, v_cust, 'anthropic', 'periodic_daily', 0.77) ->> 'status'
       <> 'recorded' then
        raise exception 'the regime writer did not update an existing row';
    end if;
    select * into v_state from public.warm_customer_state state
     where state.organization_id = v_org and state.customer_id = v_cust
       and state.provider = 'anthropic';
    if v_state.regime <> 'periodic_daily' or v_state.regime_score <> 0.77
       or v_state.regime_updated_at is null then
        raise exception 'the regime label did not persist: %', to_jsonb(v_state);
    end if;
    v_raised := false;
    begin
        perform public.warm_customer_state_set_regime(
            v_org, v_cust, 'anthropic', 'bogus', 0.1);
    exception when others then
        v_raised := true;
    end;
    if not v_raised then
        raise exception 'an unknown regime must be refused';
    end if;
    -- A freshly labelled row drops out of the scan; the aggregate and the
    -- suppressed subject are never in it.
    if exists (
        select 1
          from public.warm_customer_arrival_series(28, 100) as series(record)
         where (series.record ->> 'organization_id')::uuid = v_org
           and (series.record ->> 'customer_id')::uuid in (v_cust, v_peer, v_aggregate)
    ) then
        raise exception 'the arrival series must skip fresh, suppressed and aggregate rows';
    end if;

    -- ------------------------------------------------------------------
    -- 6. Erasure. Subject deletion removes the state AND remembers the
    -- deletion; a second deletion must not forget it.
    -- ------------------------------------------------------------------
    delete from public.warm_modeling_suppression
     where organization_id = v_org and customer_id = v_peer;
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_request, v_org, 'delete', 'customer', v_cust, 'processing',
        'evidence:warm-hazard:delete', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-hazard',
        'system:warm-hazard', now());
    perform public.compliance_delete_subject(
        v_org, v_request, 'brevitas_admin:assertion');
    if exists (
        select 1 from public.warm_customer_state state
         where state.organization_id = v_org and state.customer_id = v_cust
    ) then
        raise exception 'subject erasure must delete the learned state';
    end if;
    if not exists (
        select 1 from public.warm_modeling_suppression suppression
         where suppression.organization_id = v_org and suppression.customer_id = v_cust
    ) then
        raise exception 'subject erasure must record the suppression';
    end if;
    -- The organization aggregate is intentionally untouched: it carries no
    -- subject key and its decayed mass fades on the 14-day half-life.
    if not exists (
        select 1 from public.warm_customer_state state
         where state.organization_id = v_org and state.customer_id = v_aggregate
    ) then
        raise exception 'subject erasure must not delete the organization aggregate';
    end if;
    -- Idempotent, and the memory survives the replay. A deletion path that
    -- erased its own memory would re-admit the subject on their next request.
    perform public.compliance_delete_subject(
        v_org, v_request, 'brevitas_admin:assertion');
    if not exists (
        select 1 from public.warm_modeling_suppression suppression
         where suppression.organization_id = v_org and suppression.customer_id = v_cust
    ) then
        raise exception 'a second erasure must not delete the suppression row';
    end if;

    -- ------------------------------------------------------------------
    -- 7. Exports enumerate both tables, or the org cannot audit the model that
    -- spent its money.
    -- ------------------------------------------------------------------
    if pg_catalog.pg_get_functiondef('public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
       not like '%warming_customer_state%'
       or pg_catalog.pg_get_functiondef('public.compliance_export_tenant(uuid,uuid,text)'::regprocedure)
       not like '%warming_modeling_suppression%'
       or pg_catalog.pg_get_functiondef('public.compliance_export_subject(uuid,uuid,text)'::regprocedure)
       not like '%warming_customer_state%' then
        raise exception 'the compliance exports do not carry the learned state';
    end if;

    -- ------------------------------------------------------------------
    -- 8. Tenant erasure clears both tables outright.
    -- ------------------------------------------------------------------
    insert into public.warm_modeling_suppression (organization_id, customer_id)
    values (v_org, v_peer) on conflict do nothing;
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        '00000000-0000-4000-8000-0000000e0006'::uuid, v_org, 'delete', 'tenant',
        'approved', 'evidence:warm-hazard:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-hazard',
        'system:warm-hazard');
    perform public.compliance_delete_tenant(
        v_org, '00000000-0000-4000-8000-0000000e0006'::uuid,
        'brevitas_admin:assertion');
    if exists (select 1 from public.warm_customer_state state
                where state.organization_id = v_org)
       or exists (select 1 from public.warm_modeling_suppression suppression
                   where suppression.organization_id = v_org) then
        raise exception 'tenant erasure must clear the learned state and the suppression list';
    end if;

    raise notice '202608100003 hazard assertions passed';
end;
$$;

rollback;
