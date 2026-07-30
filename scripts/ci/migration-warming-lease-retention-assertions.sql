\set ON_ERROR_STOP on

-- Behavioural contract for 202607280017_warm_evidence_retention_floor.sql and
-- 202607280018_warm_claim_lease_fence.sql. Every fixture row is discarded by the
-- trailing rollback, so this file is re-runnable at any point in the replay and
-- is order-independent with respect to every other assertion file.
begin;

do $$
declare
    v_org uuid := '7a000000-0000-4000-8000-000000000001';
    v_cust uuid := '7a000000-0000-4000-8000-000000000002';
    v_actor uuid := '7a000000-0000-4000-8000-000000000003';
    v_hash text := repeat('7a', 32);
    v_stale_hash text := repeat('7b', 32);
    v_purged jsonb;
    v_row record;
    v_lease timestamptz;
    v_token uuid;
    v_claim jsonb;
begin
    insert into auth.users (id, email)
    values (v_actor, 'warm-retention-assertion@example.invalid');
    insert into public.organizations (id, name)
    values (v_org, 'Warm retention assertion fixture');
    insert into public.customers (id, organization_id, external_id)
    values (v_cust, v_org, 'warm-retention-assertion-customer');
    perform public.warm_credentials_upsert(
        v_org, 'anthropic', 'warm-retention-ciphertext', true, v_actor,
        50.0, 100, 288);

    -- THE MONEY ASSERTION. A 7-day maintenance window must not be able to
    -- delete the warm-spend evidence a 7-day billing period is settled against.
    -- 400 days is outside even the floor and must still age out, so retention
    -- is not simply disabled.
    insert into public.warm_budget_ledger (
        organization_id, provider, day, reserved_usd, spent_usd
    ) values
        (v_org, 'anthropic', (now() at time zone 'utc')::date - 8, 0, 4),
        (v_org, 'anthropic', (now() at time zone 'utc')::date - 100, 0, 7),
        (v_org, 'anthropic', (now() at time zone 'utc')::date - 400, 0, 9);

    v_purged := public.purge_warm_state(7);
    if (v_purged ->> 'ledger_retention_days')::integer <> 365 then
        raise exception 'purge_warm_state did not floor the ledger horizon: %',
            v_purged;
    end if;
    if not exists (
        select 1 from public.warm_budget_ledger ledger
         where ledger.organization_id = v_org
           and ledger.day = (now() at time zone 'utc')::date - 8
    ) or not exists (
        select 1 from public.warm_budget_ledger ledger
         where ledger.organization_id = v_org
           and ledger.day = (now() at time zone 'utc')::date - 100
    ) then
        raise exception
            'a 7-day retention call deleted warm spend inside the settlement window';
    end if;
    if exists (
        select 1 from public.warm_budget_ledger ledger
         where ledger.organization_id = v_org
           and ledger.day = (now() at time zone 'utc')::date - 400
    ) then
        raise exception 'warm budget ledger retention is disabled entirely';
    end if;

    -- Absolute retention bound on the encrypted prefix payload, independent of
    -- how often the prefix is re-observed. The rolling TTL alone left a
    -- recurring prefix stored forever.
    insert into public.warm_prefixes (
        organization_id, customer_id, provider, prefix_hash,
        payload_ciphertext, prefix_tokens, provider_ttl_seconds,
        created_at, last_seen_at, next_due_at, expires_at
    ) values (
        v_org, v_cust, 'anthropic', v_stale_hash,
        'warm-retention-stale-payload', 1000, 300,
        now() - interval '40 days', now(), now() + interval '1 hour',
        now() + interval '1 day'
    );
    perform public.purge_warm_state(7);
    if exists (
        select 1 from public.warm_prefixes prefix
         where prefix.prefix_hash = v_stale_hash
    ) then
        raise exception 'warm prefix payloads have no absolute retention bound';
    end if;

    -- THE LEASE FENCE. warm_due_claim pushes next_due_at out past a full
    -- sequential batch; a live arrival on the same prefix must not cancel it.
    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'warm-retention-payload',
        100000, 300, 60, false, 0.5);
    update public.warm_prefixes prefix
       set next_due_at = now() - interval '1 minute',
           arrival_count = 50,
           hour_histogram = jsonb_build_object(
               ((extract(isodow from (now() at time zone 'utc'))::integer - 1) * 24
                + extract(hour from (now() at time zone 'utc'))::integer)::text, 50)
     where prefix.prefix_hash = v_hash;
    v_claim := public.warm_due_claim(1, 1, 1, 0, 0, 5, 604800, 60, 900, null);
    if coalesce(v_claim ->> 'status', '') <> 'claimed' then
        raise exception 'warm claim fixture did not claim the prefix: %', v_claim;
    end if;
    select prefix.next_due_at, prefix.claim_token into v_lease, v_token
      from public.warm_prefixes prefix where prefix.prefix_hash = v_hash;
    if v_token is null then
        raise exception 'warm claim did not rotate a claim token';
    end if;

    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'warm-retention-payload',
        100000, 300, 60, true, 0.5);
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.next_due_at <> v_lease then
        raise exception
            'a live arrival erased the warming claim lease (% -> %)',
            v_lease, v_row.next_due_at;
    end if;
    if v_row.last_seen_at is null or v_row.arrival_count <> 51 then
        raise exception 'the lease fence also suppressed the arrival accounting';
    end if;

    -- Settling hands the schedule back: 'warmed' assigns the real next_due_at
    -- and clears the token, and a released claim clears it too so the row is
    -- not pinned at the lease horizon.
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash,
        (now() at time zone 'utc')::date, 0.5, 0.4, 'warmed', 300, 60, v_token);
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.claim_token is not null or v_row.next_due_at = v_lease then
        raise exception 'settling a warmed ping did not release the lease';
    end if;

    perform public.warm_prefix_observe(
        v_org, v_cust, 'anthropic', v_hash, 'warm-retention-payload',
        100000, 300, 60, true, 0.5);
    select * into v_row from public.warm_prefixes prefix
     where prefix.prefix_hash = v_hash;
    if v_row.next_due_at = v_lease then
        raise exception
            'the schedule stayed fenced after the claim was released';
    end if;

    update public.warm_prefixes prefix
       set claim_token = gen_random_uuid()
     where prefix.prefix_hash = v_hash
    returning prefix.claim_token into v_token;
    perform public.warm_ping_settle(
        v_org, v_cust, 'anthropic', v_hash,
        (now() at time zone 'utc')::date, 0.5, 0, 'release', 300, 60, v_token);
    if (select prefix.claim_token from public.warm_prefixes prefix
         where prefix.prefix_hash = v_hash) is not null then
        raise exception 'a released claim kept its token and froze the schedule';
    end if;
end;
$$;

rollback;
