\set ON_ERROR_STOP on

-- Behavioral contract for 202607280001_cache_warming.sql as superseded by
-- 202607280003_multi_provider_warming.sql. Every fixture row is discarded by
-- the trailing rollback so the assertions are re-runnable at any point in the
-- replay.
begin;

do $$
declare
    v_org uuid := 'feed0001-0000-4000-8000-000000000001';
    v_cust uuid := 'feed0001-0000-4000-8000-000000000002';
    v_actor uuid := 'feed0001-0000-4000-8000-000000000003';
    v_hash text := repeat('ab', 32);
    v_result jsonb;
    v_row record;
    v_token uuid;
    v_raised boolean := false;
begin
    -- The RPC surface the API and worker call must exist with these exact
    -- signatures (a plpgsql syntax error in the migration would have rolled
    -- every one of them back). warm_due_claim is the ten-argument
    -- 202607280003 overload; that migration drops the nine-argument
    -- 202607280001 signature so the call below can never be ambiguous.
    if to_regprocedure('public.warm_prefix_observe(uuid,uuid,text,text,text,integer,integer,integer,boolean,numeric)') is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer,jsonb)') is null
       or to_regprocedure('public.warm_due_claim(integer,numeric,integer,numeric,numeric,integer,integer,integer,integer)') is not null
       or to_regprocedure('public.warm_ping_settle(uuid,uuid,text,text,date,numeric,numeric,text,integer,integer,uuid)') is null
       or to_regprocedure('public.warm_credentials_upsert(uuid,text,text,boolean,uuid,numeric,integer,integer)') is null
       or to_regprocedure('public.warm_credentials_purge(uuid,text)') is null
       or to_regprocedure('public.warm_read_status(uuid)') is null
       or to_regprocedure('public.purge_warm_state(integer)') is null then
        raise exception 'a warming RPC signature is missing';
    end if;

    insert into auth.users (id, email)
    values (v_actor, 'cache-warming-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Cache warming assertion fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'cache-warming-assertion-customer');

    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'assertion-ciphertext', true, v_actor, 5.0, 100, 288);

    -- Keep-existing-key ('') must reach the stored row for both the disable
    -- kill switch and budget changes, not raise the ciphertext CHECK.
    v_result := public.warm_credentials_upsert(
        v_org, 'anthropic', '', false, null, 5.0, 100, 288);
    if (v_result ->> 'enabled')::boolean is not false then
        raise exception 'keep-existing-key disable did not apply: %', v_result;
    end if;
    v_result := public.warm_credentials_upsert(
        v_org, 'anthropic', '', true, v_actor, 2.0, 100, 288);
    select * into v_row from public.warm_credentials cred
     where cred.organization_id = v_org and cred.provider = 'anthropic';
    if v_row.credential_ciphertext <> 'assertion-ciphertext'
       or v_row.daily_budget_usd <> 2.0 or not v_row.enabled then
        raise exception 'keep-existing-key update drifted';
    end if;
    begin
        perform public.warm_credentials_upsert(
            v_org, 'openai', '', false, null, 1.0, 100, 288);
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception 'empty ciphertext without a stored credential was accepted';
    end if;

    -- Observation stores the observer-priced per-ping reserve, and a caller
    -- that omits it must not clobber the stored value.
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'assertion-payload', 100000, 300,
        60, false, 0.5);
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'assertion-payload', 100000, 300,
        60, false);
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.ping_reserve_usd <> 0.5 then
        raise exception 'ping_reserve_usd drifted: %', v_row.ping_reserve_usd;
    end if;

    -- The claim reserves the worst case (0.5), not the flat floor
    -- (3.75 * 100000 / 1e6 = 0.375), leases past a 60s tick, and returns a
    -- settle-fencing token.
    update public.warm_prefixes prefix
       set next_due_at = now() - interval '1 minute'
     where prefix.prefix_hash = v_hash;

    -- 202607280003 per-provider ROI: a break-even map key outside the
    -- admitted provider list must be rejected, and an admitted key must
    -- actually gate the claim (histogram diluted to p = 2/10 against a 0.9
    -- anthropic break-even yields no claim).
    v_raised := false;
    begin
        perform public.warm_due_claim(
            50, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900,
            '{"groq": 0.5}'::jsonb);
    exception when others then v_raised := true;
    end;
    if not v_raised then
        raise exception 'non-admitted provider in the break-even map was accepted';
    end if;
    update public.warm_prefixes prefix
       set arrival_count = 10
     where prefix.prefix_hash = v_hash;
    select value into v_result from public.warm_due_claim(
        50, 3.75, 1, 0.35, 0.11, 3, 3600, 60, 900,
        '{"anthropic": 0.9}'::jsonb) as value limit 1;
    if v_result is not null then
        raise exception 'per-provider break-even did not gate the claim: %', v_result;
    end if;

    select value into v_result from public.warm_due_claim(
        50, 3.75, 5, 0.35, 0.11, 3, 3600, 60, 900) as value limit 1;
    if coalesce(v_result ->> 'status', '') <> 'claimed' then
        raise exception 'warm_due_claim returned no claim: %', v_result;
    end if;
    if (v_result ->> 'reserved_usd')::numeric <> 0.5 then
        raise exception 'reservation is not the observer-priced worst case: %',
            v_result ->> 'reserved_usd';
    end if;
    v_token := (v_result ->> 'claim_token')::uuid;
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_token is null or v_row.claim_token is distinct from v_token then
        raise exception 'claim token missing or not persisted';
    end if;
    if v_row.next_due_at < now() + interval '14 minutes' then
        raise exception 'claim lease does not outlive a worker batch: %',
            v_row.next_due_at;
    end if;

    -- A stale token releases money (both were real) but cannot double-apply
    -- counters or clobber the new owner's schedule.
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash, (now() at time zone 'utc')::date,
        0.5, 0.45, 'warmed', 300, 60, gen_random_uuid());
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.warm_pings <> 0 then
        raise exception 'stale-token settle mutated the prefix row';
    end if;
    select * into v_row from public.warm_budget_ledger ledger
     where ledger.organization_id = v_org and ledger.provider = 'anthropic';
    if v_row.reserved_usd <> 0 or v_row.spent_usd <> 0.45 then
        raise exception 'stale-token settle mis-booked the ledger: % %',
            v_row.reserved_usd, v_row.spent_usd;
    end if;
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash, (now() at time zone 'utc')::date,
        0, 0.45, 'warmed', 300, 60, v_token);
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.warm_pings <> 1 or v_row.claim_token is not null then
        raise exception 'live-token settle did not apply';
    end if;
end;
$$;

rollback;
