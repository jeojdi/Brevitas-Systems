-- Phase 0 warming instrumentation (202608090001): decision log + TTL sensor.
--
-- NOT YET REGISTERED in scripts/ci/run-migration-tests.sh, because
-- 202608090001 itself is not in scripts/ci/migration-fresh-manifest.txt --
-- registering it is blocked behind 202608010001_openrouter_reported_cost.sql,
-- which was landed without manifest entries and has no begin;/commit; so it
-- fails verifyAtomicForwardMigrations. Run by hand against a database with the
-- full chain applied until that is resolved:
--   psql "$DATABASE_URL" --set ON_ERROR_STOP=1 -f this file
--
-- Everything runs inside one transaction and rolls back.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000f0001';
    v_org uuid := '00000000-0000-4000-8000-0000000f0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000f0003';
    v_other_cust uuid := '00000000-0000-4000-8000-0000000f0004';
    v_hash text := repeat('c1', 32);
    v_hash2 text := repeat('c2', 32);
    v_request uuid := '00000000-0000-4000-8000-0000000f0005';
    v_run uuid := '00000000-0000-4000-8000-0000000f0006';
    v_row record;
    v_claim jsonb;
    v_count integer;
    v_result jsonb;
begin
    -- Signature and privilege surface for everything this migration defines.
    if to_regprocedure('public.warm_ttl_observe(text,text,text,numeric,text,text)') is null
       or to_regprocedure('public.warm_ttl_tier(integer)') is null
       or to_regprocedure('public.warm_decision_settle_outcome(uuid,text)') is null
       or to_regprocedure('public.warm_decision_record(uuid,uuid,text,text,text,numeric,numeric,numeric,integer,numeric,integer,integer,uuid,bigint,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric,numeric)') is null
       or to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric,text)') is null
       or to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)') is not null then
        raise exception 'the instrumentation RPC surface is wrong';
    end if;
    if to_regclass('public.warm_decision_log') is null
       or to_regclass('public.warm_ttl_observations') is null then
        raise exception 'an instrumentation table is missing';
    end if;
    -- Plane G is structural, not conventional: assert the physics table has no
    -- tenant column at all, so no future writer can put one in it.
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'warm_ttl_observations'
           and column_name in ('organization_id', 'customer_id', 'prefix_hash')
    ) then
        raise exception 'warm_ttl_observations must carry no tenant key';
    end if;
    -- Zero policies and zero direct DML for every PostgREST role.
    if exists (select 1 from pg_catalog.pg_policies
                where schemaname = 'public'
                  and tablename in ('warm_decision_log', 'warm_ttl_observations'))
       or not (select relrowsecurity from pg_catalog.pg_class
                where oid = 'public.warm_decision_log'::regclass)
       or not (select relrowsecurity from pg_catalog.pg_class
                where oid = 'public.warm_ttl_observations'::regclass) then
        raise exception 'instrumentation tables must be RLS-on with zero policies';
    end if;
    if has_table_privilege('service_role', 'public.warm_decision_log', 'select')
       or has_table_privilege('anon', 'public.warm_decision_log', 'insert')
       or has_table_privilege('authenticated', 'public.warm_ttl_observations', 'select')
       or has_table_privilege('service_role', 'public.warm_ttl_observations', 'insert') then
        raise exception 'instrumentation tables must have no direct DML grants';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'warm-instrumentation-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm instrumentation fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-instr-1'), (v_other_cust, v_org, 'warm-instr-2');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 1);

    -- Two arrivals on the same prefix: the second is a free TTL observation.
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5-20260514');
    if exists (select 1 from public.warm_ttl_observations) then
        raise exception 'a first arrival has no prior touch and cannot be an observation';
    end if;
    update public.warm_prefixes prefix
       set last_touch_at = clock_timestamp() - interval '90 seconds'
     where prefix.prefix_hash = v_hash;
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        true, 0.375, 'claude-sonnet-4-5-20260514');
    select * into v_row from public.warm_ttl_observations;
    if v_row.source <> 'arrival' or v_row.outcome <> 'warm'
       or v_row.ttl_tier <> '5m' or v_row.provider <> 'anthropic'
       or v_row.model_class <> 'claude-sonnet-4-5-20260514'
       or abs(v_row.gap_seconds - 90) > 5 then
        raise exception 'arrival TTL observation is wrong: %', to_jsonb(v_row);
    end if;
    -- A future touch is a clock jump, not physics, and must be skipped rather
    -- than raised: this runs inside a live request.
    update public.warm_prefixes prefix
       set last_touch_at = clock_timestamp() + interval '1 hour'
     where prefix.prefix_hash = v_hash;
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5-20260514');
    select count(*) into v_count from public.warm_ttl_observations;
    if v_count <> 1 then
        raise exception 'an out-of-bounds gap must not be recorded';
    end if;

    -- A claim writes one 'pinged' decision carrying the claim token.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           last_touch_at = clock_timestamp() - interval '200 seconds'
     where prefix.prefix_hash = v_hash;
    select value into v_claim from public.warm_due_claim(
        10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null) as t(value) limit 1;
    if coalesce(v_claim ->> 'status', '') <> 'claimed' then
        raise exception 'the fixture prefix was not claimed: %', v_claim;
    end if;
    if v_claim ->> 'last_touch_at' is null then
        raise exception 'the claim must report the pre-claim touch clock';
    end if;
    select * into v_row from public.warm_decision_log;
    if v_row.decision <> 'pinged'
       or v_row.claim_token::text <> (v_claim ->> 'claim_token')
       or v_row.pings_today <> 0
       or v_row.reserve_usd <> (v_claim ->> 'reserved_usd')::numeric
       or v_row.settle_outcome is not null then
        raise exception 'the pinged decision is wrong: %', to_jsonb(v_row);
    end if;

    -- Settle stamps the outcome back onto that decision by claim token.
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash, (now() at time zone 'utc')::date,
        (v_claim ->> 'reserved_usd')::numeric, 0.05, 'warmed', 300, 60,
        (v_claim ->> 'claim_token')::uuid);
    if (public.warm_decision_settle_outcome(
            (v_claim ->> 'claim_token')::uuid, 'warmed') ->> 'updated')::integer <> 1 then
        raise exception 'the settle outcome was not stamped';
    end if;
    select * into v_row from public.warm_decision_log;
    if v_row.settle_outcome <> 'warmed' then
        raise exception 'settle_outcome did not persist';
    end if;
    -- An unknown token is a no-op, not an error.
    if (public.warm_decision_settle_outcome(
            gen_random_uuid(), 'release') ->> 'updated')::integer <> 0 then
        raise exception 'an unknown claim token must update nothing';
    end if;
    -- Only 'warmed' advanced the touch clock (checked via the next claim below).

    -- The per-customer cap denies the next due claim, and the denial is logged
    -- with the cap reading that produced it.
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second'
     where prefix.prefix_hash = v_hash;
    perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null);
    select * into v_row from public.warm_decision_log entry
     where entry.decision = 'cap_denied';
    if not found or v_row.pings_today <> 1 or v_row.claim_token is not null then
        raise exception 'cap_denied was not logged correctly';
    end if;

    -- The ROI gate denies before the cap is read, so pings_today stays null.
    perform public.warm_prefix_observe(
        v_org, v_other_cust, 'anthropic', v_hash2, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5-20260514');
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           arrival_count = 10, hour_histogram = '{}'::jsonb
     where prefix.prefix_hash = v_hash2;
    perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null);
    select * into v_row from public.warm_decision_log entry
     where entry.decision = 'skipped_roi';
    if not found or v_row.pings_today is not null
       or v_row.arrival_count <> 10 or v_row.p_return <> 0
       or v_row.roi_floor <> 0.11 or v_row.reserve_usd <= 0 then
        raise exception 'skipped_roi was not logged correctly: %', to_jsonb(v_row);
    end if;

    -- The budget gate denies last, after the reservation is priced.
    update public.warm_credentials cred set daily_budget_usd = 0.0001
     where cred.organization_id = v_org;
    update public.warm_prefixes prefix
       set next_due_at = clock_timestamp() - interval '1 second',
           arrival_count = 10,
           hour_histogram = jsonb_build_object(
               (((extract(isodow from (clock_timestamp() at time zone 'utc'))::integer - 1) * 24
                 + extract(hour from (clock_timestamp() at time zone 'utc'))::integer))::text, 10)
     where prefix.prefix_hash = v_hash2;
    update public.warm_credentials cred set max_pings_per_customer_day = 288
     where cred.organization_id = v_org;
    perform public.warm_due_claim(10, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900, null);
    select * into v_row from public.warm_decision_log entry
     where entry.decision = 'budget_denied';
    if not found or v_row.claim_token is not null then
        raise exception 'budget_denied was not logged';
    end if;

    -- Tenant export carries the decisions; customer-scoped subject export
    -- carries only that customer's.
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_request, v_org, 'export', 'customer', v_cust, 'processing',
        'evidence:warm-instrumentation:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-instrumentation',
        'system:warm-instrumentation', now());
    select count(*) into v_count
      from public.compliance_export_subject(v_org, v_request, 'brevitas_admin:assertion')
        as exported(record)
     where exported.record ->> 'record_type' = 'warming_decision';
    if v_count = 0 then
        raise exception 'customer subject export omits warming decisions';
    end if;
    if exists (
        select 1 from public.compliance_export_subject(
            v_org, v_request, 'brevitas_admin:assertion') as exported(record)
         where exported.record ->> 'record_type' = 'warming_decision'
           and (exported.record -> 'data' ->> 'customer_id')::uuid <> v_cust
    ) then
        raise exception 'subject export leaked another customer''s decisions';
    end if;

    -- Retention: the 90-day class deletes decisions and produces evidence, and
    -- never touches the tenant-free physics table.
    update public.warm_decision_log entry set ts = clock_timestamp() - interval '120 days';
    v_result := public.compliance_run_retention(
        v_run, 'brevitas_admin:assertion', 100, false);
    if (v_result ->> 'warm_decision_candidates')::integer = 0 then
        raise exception 'the dry run found no aged warm decisions';
    end if;
    v_result := public.compliance_run_retention(
        v_run, 'brevitas_admin:assertion', 100, true);
    if (v_result ->> 'warm_decision_deleted')::integer
       <> (v_result ->> 'warm_decision_candidates')::integer then
        raise exception 'retention deleted a different count than it counted: %', v_result;
    end if;
    if exists (select 1 from public.warm_decision_log) then
        raise exception 'aged warm decisions survived retention';
    end if;
    select count(*) into v_count from public.warm_ttl_observations;
    if v_count <> 1 then
        raise exception 'retention must not touch the tenant-free physics table';
    end if;
    select run.warm_decision_deleted into v_count
      from public.compliance_retention_runs run where run.id = v_run;
    if v_count <> (v_result ->> 'warm_decision_deleted')::integer then
        raise exception 'the immutable evidence row disagrees with the result';
    end if;

    -- purge_warm_state owns the aggregate horizon and refuses to shorten it.
    update public.warm_ttl_observations observation
       set observed_at = clock_timestamp() - interval '400 days';
    v_result := public.purge_warm_state(7);
    if (v_result ->> 'observation_retention_days')::integer <> 365
       or (v_result ->> 'observations_deleted')::integer <> 1 then
        raise exception 'the observation horizon is wrong: %', v_result;
    end if;
end;
$$;

rollback;

-- Tenant erasure runs in its own transaction so the decision rows above cannot
-- confuse it with the retention deletes.
begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000f0011';
    v_org uuid := '00000000-0000-4000-8000-0000000f0012';
    v_cust uuid := '00000000-0000-4000-8000-0000000f0013';
    v_hash text := repeat('d1', 32);
    v_request uuid := '00000000-0000-4000-8000-0000000f0015';
    v_count integer;
begin
    insert into auth.users (id, email)
    values (v_actor, 'warm-instrumentation-erasure@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm instrumentation erasure fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-erase-1');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'enc:credential', true, v_actor, 10.0, 100, 288);
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'enc:payload', 100000, 300, 60,
        false, 0.375, 'claude-sonnet-4-5');
    perform public.warm_decision_record(
        v_org, v_cust, 'anthropic', v_hash, 'pinged', 0.9, 0.11, 0.375,
        100000, 12.5, 4, 0, gen_random_uuid());
    perform public.warm_ttl_observe(
        'anthropic', 'claude-sonnet-4-5', '5m', 240, 'warm', 'ping');

    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by
    ) values (
        v_request, v_org, 'delete', 'tenant', 'approved',
        'evidence:warm-instrumentation:delete', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-instrumentation',
        'system:warm-instrumentation');
    perform public.compliance_delete_tenant(
        v_org, v_request, 'brevitas_admin:assertion');

    select count(*) into v_count from public.warm_decision_log entry
     where entry.organization_id = v_org;
    if v_count <> 0 then
        raise exception 'tenant erasure left % warm decision rows', v_count;
    end if;
    -- The two-plane decision, asserted rather than trusted: the physics table
    -- has no data subject in it, so erasure must leave it alone.
    select count(*) into v_count from public.warm_ttl_observations;
    if v_count <> 1 then
        raise exception 'tenant erasure must not touch warm_ttl_observations';
    end if;
end;
$$;

rollback;
