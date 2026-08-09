-- Phase 0 warming instrumentation (202608090002): the per-ping reward join.
--
-- NOT YET REGISTERED in scripts/ci/run-migration-tests.sh, for the same reason
-- 202608090001's suite is not: 202608010001_openrouter_reported_cost.sql was
-- landed without manifest entries and has no begin;/commit;, so
-- verifyAtomicForwardMigrations fails before any of this can be registered.
-- Run by hand against a database with the full chain applied:
--   psql "$DATABASE_URL" --set ON_ERROR_STOP=1 -f this file
--
-- Everything runs inside one transaction and rolls back.

begin;

do $$
declare
    v_actor uuid := '00000000-0000-4000-8000-0000000e0001';
    v_org uuid := '00000000-0000-4000-8000-0000000e0002';
    v_cust uuid := '00000000-0000-4000-8000-0000000e0003';
    v_other uuid := '00000000-0000-4000-8000-0000000e0004';
    v_hash text := repeat('e1', 32);
    v_hash2 text := repeat('e2', 32);
    v_key text := 'kh-reward-join-assertion';
    v_request uuid := '00000000-0000-4000-8000-0000000e0005';
    v_run uuid := '00000000-0000-4000-8000-0000000e0006';
    v_tenant_request uuid := '00000000-0000-4000-8000-0000000e0008';
    v_delete_request uuid := '00000000-0000-4000-8000-0000000e0009';
    v_ping timestamptz := clock_timestamp() - interval '2 hours';
    v_decision bigint;
    v_organic bigint;
    v_result jsonb;
    v_count integer;
    v_net numeric;
begin
    -- Surface.
    if to_regprocedure('public.warm_usage_stamp_prefix(uuid,text,text,text)') is null
       or to_regprocedure('public.warm_reward_join(integer,integer)') is null then
        raise exception 'the reward-join RPC surface is wrong';
    end if;
    if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'usage_log'
           and column_name = 'warm_prefix_hash' and is_nullable = 'YES'
    ) then
        raise exception 'usage_log.warm_prefix_hash must exist and be nullable';
    end if;
    if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'warm_decision_log'
           and column_name = 'organic_counterfactual' and is_nullable = 'NO'
    ) then
        raise exception 'warm_decision_log.organic_counterfactual must be NOT NULL';
    end if;
    -- The stamp and the join are the only writers; no PostgREST role may reach
    -- usage_log's new column by direct DML any more than it could before.
    if exists (
        select 1 from information_schema.role_table_grants
         where table_schema = 'public' and table_name = 'warm_decision_log'
           and grantee in ('anon', 'authenticated', 'service_role', 'public')
    ) then
        raise exception 'warm_decision_log must have no direct DML grants';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'warm-reward-join-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm reward join fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'reward-1'), (v_other, v_org, 'reward-2');
    insert into public.api_keys (key_hash, name, organization_id)
    values (v_key, 'reward-join', v_org);
    insert into public.warm_prefixes (
        organization_id, customer_id, provider, prefix_hash, payload_ciphertext,
        prefix_tokens, provider_ttl_seconds, created_at, last_seen_at,
        next_due_at, expires_at
    ) values
        (v_org, v_cust, 'anthropic', v_hash, 'enc', 100000, 300,
         v_ping, v_ping, clock_timestamp(), clock_timestamp() + interval '1 day'),
        (v_org, v_other, 'anthropic', v_hash2, 'enc', 100000, 300,
         v_ping, v_ping, clock_timestamp(), clock_timestamp() + interval '1 day');

    -- The stamp is write-once, index-scoped and tenant-fenced.
    insert into public.usage_log (key_hash, ts, organization_id, customer_id,
                                  provider, strategy, request_id, authoritative,
                                  actual_cost_usd)
    values (v_key, v_ping + interval '5 seconds', v_org, v_cust, 'anthropic',
            'cache_warm', 'warm:ping-1', false, 0.30);
    if (public.warm_usage_stamp_prefix(v_org, v_key, 'warm:ping-1', v_hash)
        ->> 'updated')::integer <> 1 then
        raise exception 'the stamp did not reach the ping receipt';
    end if;
    if (public.warm_usage_stamp_prefix(v_org, v_key, 'warm:ping-1', v_hash2)
        ->> 'updated')::integer <> 0 then
        raise exception 'the stamp must be write-once';
    end if;
    if (public.warm_usage_stamp_prefix(
            '00000000-0000-4000-8000-0000000e0099', v_key, 'warm:ping-1', v_hash2)
        ->> 'updated')::integer <> 0 then
        raise exception 'the stamp must be tenant-fenced';
    end if;
    begin
        perform public.warm_usage_stamp_prefix(v_org, v_key, 'warm:ping-1', 'nope');
        raise exception 'the stamp accepted a non-sha256 hash';
    exception when others then
        if sqlerrm <> 'warm usage stamp arguments are invalid' then raise; end if;
    end;

    -- CASE 1: an attributed hit. One lone prior arrival, so the organic test
    -- has nothing to pair, then a discounted arrival inside the TTL.
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, ts, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count,
        settle_outcome
    ) values (v_org, v_cust, 'anthropic', v_hash, v_ping, 'pinged',
              0.5, 0.11, 0.375, 100000, 7, 'warmed')
    returning id into v_decision;
    insert into public.usage_log (key_hash, ts, organization_id, customer_id,
                                  provider, strategy, request_id, authoritative,
                                  cache_attributable, native_cache_discount_usd,
                                  warm_prefix_hash)
    values (v_key, v_ping - interval '10 minutes', v_org, v_cust, 'anthropic',
            'native_cache', 'req-old', true, true, 0.0, v_hash),
           (v_key, v_ping + interval '100 seconds', v_org, v_cust, 'anthropic',
            'native_cache', 'req-hit', true, true, 0.90, v_hash);

    v_result := public.warm_reward_join(48, 100);
    if (v_result ->> 'joined')::integer <> 1
       or (v_result ->> 'attributed')::integer <> 1
       or (v_result ->> 'organic')::integer <> 0 then
        raise exception 'attributed hit not credited: %', v_result;
    end if;
    select realized_net_usd into v_net from public.warm_decision_log
     where id = v_decision;
    if v_net <> 0.60 then
        raise exception 'net for an attributed hit was % not 0.60', v_net;
    end if;
    if (select organic_counterfactual from public.warm_decision_log
         where id = v_decision) then
        raise exception 'an attributed hit must not be flagged organic';
    end if;
    -- Idempotent: nothing left with a null reward.
    if (public.warm_reward_join(48, 100) ->> 'scanned')::integer <> 0 then
        raise exception 'the join is not idempotent';
    end if;

    -- CASE 2: a self-refreshing session. Two real arrivals 120s apart on a
    -- 300s TTL, so the entry never needed us; the rich discount that follows
    -- must NOT be credited and the ping is charged in full.
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, ts, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count,
        settle_outcome
    ) values (v_org, v_other, 'anthropic', v_hash2, v_ping, 'pinged',
              0.5, 0.11, 0.375, 100000, 7, 'warmed')
    returning id into v_organic;
    insert into public.usage_log (key_hash, ts, organization_id, customer_id,
                                  provider, strategy, request_id, authoritative,
                                  cache_attributable, native_cache_discount_usd,
                                  actual_cost_usd, warm_prefix_hash)
    values (v_key, v_ping - interval '200 seconds', v_org, v_other, 'anthropic',
            'native_cache', 'req-o-a', true, true, 0.5, 0.02, v_hash2),
           (v_key, v_ping - interval '80 seconds', v_org, v_other, 'anthropic',
            'native_cache', 'req-o-b', true, true, 0.5, 0.02, v_hash2),
           (v_key, v_ping + interval '5 seconds', v_org, v_other, 'anthropic',
            'cache_warm', 'warm:ping-2', false, false, null, 0.30, v_hash2),
           (v_key, v_ping + interval '100 seconds', v_org, v_other, 'anthropic',
            'native_cache', 'req-o-c', true, true, 0.90, 0.02, v_hash2);

    v_result := public.warm_reward_join(48, 100);
    if (v_result ->> 'joined')::integer <> 1
       or (v_result ->> 'organic')::integer <> 1
       or (v_result ->> 'attributed')::integer <> 0 then
        raise exception 'self-refreshing session not detected: %', v_result;
    end if;
    select realized_net_usd into v_net from public.warm_decision_log
     where id = v_organic;
    if v_net <> -0.30 then
        raise exception 'organic net was % not -0.30', v_net;
    end if;
    if not (select organic_counterfactual from public.warm_decision_log
             where id = v_organic) then
        raise exception 'a self-refreshing session must be flagged organic';
    end if;

    -- CASE 3: the attribution horizon. A ping whose TTL window is still open
    -- has an arrival that has not happened yet.
    insert into public.warm_decision_log (
        organization_id, customer_id, provider, prefix_hash, ts, decision,
        p_return, roi_floor, reserve_usd, prefix_tokens, arrival_count,
        settle_outcome
    ) values (v_org, v_cust, 'anthropic', v_hash,
              clock_timestamp() - interval '30 seconds', 'pinged',
              0.5, 0.11, 0.375, 100000, 7, 'warmed');
    if (public.warm_reward_join(48, 100) ->> 'scanned')::integer <> 0 then
        raise exception 'the join scored a ping inside its own TTL window';
    end if;

    -- CASE 4: an outcome that bought no priced receipt stays unscored rather
    -- than being credited a guessed cost.
    update public.warm_decision_log
       set ts = clock_timestamp() - interval '3 hours',
           settle_outcome = 'spent_unknown'
     where settle_outcome = 'warmed' and realized_net_usd is null;
    if (public.warm_reward_join(48, 100) ->> 'scanned')::integer <> 0 then
        raise exception 'spent_unknown must not be scored';
    end if;

    -- The join wrote analytics only: no ledger row, no settlement, no fee.
    if exists (select 1 from public.billing_ledger where organization_id = v_org)
       or exists (select 1 from public.warm_budget_ledger
                   where organization_id = v_org)
       or exists (select 1 from public.usage_log
                   where organization_id = v_org
                     and (verified_savings_usd <> 0 or brevitas_fee_usd <> 0)) then
        raise exception 'the reward join touched money';
    end if;

    -- Export: both projections carry the new flag.
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_request, v_org, 'export', 'customer', v_cust, 'processing',
        'evidence:warm-reward-join:export', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-reward-join',
        'system:warm-reward-join', now());
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_tenant_request, v_org, 'export', 'tenant', null, 'approved',
        'evidence:warm-reward-join:tenant', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-reward-join',
        'system:warm-reward-join', now());
    if exists (
        select 1 from public.compliance_export_subject(
            v_org, v_request, 'brevitas_admin:assertion') as exported(record)
         where exported.record ->> 'record_type' = 'warming_decision'
           and not (exported.record -> 'data' ? 'organic_counterfactual')
    ) then
        raise exception 'subject export omits organic_counterfactual';
    end if;
    if exists (
        select 1 from public.compliance_export_tenant(
            v_org, v_tenant_request, 'brevitas_admin:assertion') as exported(record)
         where exported.record ->> 'record_type' = 'warming_decision'
           and not (exported.record -> 'data' ? 'organic_counterfactual')
    ) then
        raise exception 'tenant export omits organic_counterfactual';
    end if;

    -- Retention minimization clears the hash on rows the ledger forces us to
    -- keep, and the candidate count matches what the apply really touches.
    update public.usage_log set ts = clock_timestamp() - interval '14 months'
     where organization_id = v_org and request_id = 'req-hit';
    insert into public.billing_ledger (organization_id, usage_log_id, user_id,
                                       occurred_at, fee_microusd)
    select v_org, usage.id, v_actor, usage.ts, 0
      from public.usage_log usage
     where usage.organization_id = v_org and usage.request_id = 'req-hit';
    -- An OTHERWISE fully minimized row whose only remaining identifier is the
    -- hash: this is the row that proves the hash joined the convergence
    -- predicate, not just the SET.
    insert into public.usage_log (
        key_hash, ts, organization_id, customer_id, owner_id, project,
        environment, source, repo, client, agent, call_site_id, framework,
        gateway, provider, model, session_id, pipeline, run_id, request_id,
        usage_raw, warm_prefix_hash
    ) values (
        'deleted-' || v_org::text, clock_timestamp() - interval '14 months',
        v_org, null, '', 'Deleted', 'Deleted', 'Deleted', '', '', '', '', '',
        '', '', '', '', '', '', 'req-minimized', '', v_hash);
    insert into public.billing_ledger (organization_id, usage_log_id, user_id,
                                       occurred_at, fee_microusd)
    select v_org, usage.id, v_actor, usage.ts, 0
      from public.usage_log usage
     where usage.organization_id = v_org and usage.request_id = 'req-minimized';
    select ((public.compliance_run_retention(v_run, 'system:assertion', 100, false))
            ->> 'usage_minimize_candidates')::integer into v_count;
    if v_count < 2 then
        raise exception 'a hash-bearing retained row is not a minimization candidate';
    end if;
    perform public.compliance_run_retention(
        '00000000-0000-4000-8000-0000000e0007', 'system:assertion', 100, true);
    if exists (select 1 from public.usage_log
                where organization_id = v_org
                  and request_id in ('req-hit', 'req-minimized')
                  and warm_prefix_hash is not null) then
        raise exception 'retention minimization left the warm prefix hash behind';
    end if;
    -- ...and having cleared it, the row must stop being a candidate, or the
    -- nightly batch would rewrite it forever.
    if ((public.compliance_run_retention(
            '00000000-0000-4000-8000-0000000e000a', 'system:assertion', 100, false))
        ->> 'usage_minimize_candidates')::integer <> 0 then
        raise exception 'minimization does not converge on hash-bearing rows';
    end if;

    -- Subject erasure removes the customer's decisions and their hashes, and
    -- leaves the tenant-free physics table alone.
    perform public.warm_ttl_observe('anthropic', 'claude-sonnet-4-5', '5m',
                                    90.0, 'warm', 'ping');
    insert into public.data_subject_requests (
        id, organization_id, request_type, request_scope, subject_id, status,
        evidence_reference, requested_at, due_at, approved_at, approved_by,
        created_by, started_at
    ) values (
        v_delete_request, v_org, 'delete', 'customer', v_cust, 'processing',
        'evidence:warm-reward-join:delete', now() - interval '1 day',
        now() + interval '29 days', now(), 'system:warm-reward-join',
        'system:warm-reward-join', now());
    perform public.compliance_delete_subject(v_org, v_delete_request,
                                            'brevitas_admin:assertion');
    if exists (select 1 from public.warm_decision_log
                where organization_id = v_org and customer_id = v_cust) then
        raise exception 'subject erasure left the customer''s decisions behind';
    end if;
    if exists (select 1 from public.usage_log
                where organization_id = v_org and customer_id = v_cust
                  and warm_prefix_hash is not null) then
        raise exception 'subject erasure left a warm prefix hash behind';
    end if;
    if not exists (select 1 from public.warm_ttl_observations) then
        raise exception 'subject erasure must not touch the tenant-free physics table';
    end if;

    raise notice '202608090002 reward-join assertions passed';
end;
$$;

rollback;
