\set ON_ERROR_STOP on

-- Behavioural contract for the settlement CLAIM/SEND path
-- (202607280029_period_settlement_claim_path.sql).
--
-- Before this migration a settlement promoted to 'pending' was terminal in
-- practice: public.claim_billing_ledger_entries reads only public.billing_ledger,
-- and no RPC anywhere read period_settlement_ledger for sending. Money could be
-- computed and operator-approved and still never reach Stripe. Proven live on the
-- throwaway project: settlement 1000000005, status 'pending', fee 1235092, with
-- no code path able to move it.
--
-- The properties below are the ones that make the path safe rather than merely
-- present. Each is written so that a plausible "simplification" of the migration
-- fails it.
--
-- The whole file is one transaction ending in ROLLBACK, so it is rerunnable and
-- leaves no financial rows behind.

begin;

-- A NON-UTC session timezone on purpose: the worker runs UTC on Railway, psql
-- inherits the server default, and every window below is expressed in exact
-- seconds. If any part of the claim path ever became wall-clock sensitive, this
-- is where it surfaces.
set local timezone = 'America/New_York';

-- ---------------------------------------------------------------------------
-- 0. Isolation. The claim takes the globally oldest eligible settlement, so
--    rows other suites committed earlier in the run would be claimed instead of
--    ours. Push everything else out of reach; ROLLBACK undoes it.
-- ---------------------------------------------------------------------------
update public.period_settlement_ledger
   set next_attempt_at = now() + interval '3650 days'
 where status = 'pending';

insert into auth.users(id, email, created_at, raw_user_meta_data)
values ('5e900000-0000-4000-8000-000000000001',
        'settlement-sender@example.invalid', now(), '{}'::jsonb)
on conflict (id) do nothing;

insert into public.organizations(id, name, billing_owner_id)
values ('5e910000-0000-4000-8000-00000000000a', 'Settlement sender fixture',
        '5e900000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.billing_accounts(
    organization_id, user_id, stripe_customer_id, subscription_status,
    billing_started_at, current_period_start, current_period_end)
values ('5e910000-0000-4000-8000-00000000000a',
        '5e900000-0000-4000-8000-000000000001',
        'cus_settlement_sender', 'active', now() - interval '2 days',
        now() - interval '86400 seconds',
        now() - interval '86400 seconds' + interval '604800 seconds')
on conflict (organization_id) do update set
    stripe_customer_id = excluded.stripe_customer_id,
    subscription_status = excluded.subscription_status,
    current_period_start = excluded.current_period_start,
    current_period_end = excluded.current_period_end;

-- period_settlement_ledger carries a unique index over live (non-void) rows per
-- (organization, period), so each fixture needs its OWN period. p_weeks_back
-- shifts a whole 604800-second window; the account anchor is unchanged, so every
-- window stays exactly one Stripe week wide.
create function pg_temp.new_settlement(p_status text, p_fee bigint,
                                       p_weeks_back integer default 0)
returns bigint language sql as $$
    insert into public.period_settlement_ledger(
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, usage_row_count,
        fee_microusd, billing_owner_id, status, computed_by)
    values ('5e910000-0000-4000-8000-00000000000a',
            now() - interval '86400 seconds'
                  - (p_weeks_back * interval '604800 seconds'),
            now() - interval '86400 seconds' + interval '604800 seconds'
                  - (p_weeks_back * interval '604800 seconds'),
            100, 0, 1, p_fee,
            '5e900000-0000-4000-8000-000000000001', p_status, 'sender-assertions')
    returning id;
$$;

do $$
declare
    v_id bigint;
    v_row record;
    v_ok boolean;
    v_status text;
    v_marker timestamptz;
    v_attempts integer;
begin
    ------------------------------------------------------------------
    -- 1. THE DEFINING PROPERTY: the claim must NOT move the row to 'sending'.
    --
    -- 202607280010 refuses any exit from 'sending'/'reported' unless
    -- outbound_started_at survives, and enforces it even for a row that never
    -- carried a marker. A claim that set status='sending' would therefore make
    -- every abandoned claim unrecoverable -- the fee could never return to
    -- 'pending' and no release RPC could exist. If someone "tidies" the claim to
    -- look like the per-row one, this fails.
    ------------------------------------------------------------------
    v_id := pg_temp.new_settlement('pending', 1234567);
    select * into v_row
      from public.claim_period_settlement_entries('assert-worker', 120, 1, 2000000000);
    if v_row.id is distinct from v_id then
        raise exception 'a promoted settlement was not claimable (got %)', v_row.id;
    end if;
    select status, lease_owner is not null into v_status, v_ok
      from public.period_settlement_ledger where id = v_id;
    if v_status <> 'pending' then
        raise exception 'the claim moved status to %; it must stay pending', v_status;
    end if;
    if not v_ok then
        raise exception 'the claim did not take a lease';
    end if;
    if v_row.fee_microusd <> 1234567 or v_row.stripe_customer_id <> 'cus_settlement_sender' then
        raise exception 'claim returned the wrong money or customer: %', v_row;
    end if;
    if v_row.reclaimed then
        raise exception 'a first claim reported reclaimed=true';
    end if;

    ------------------------------------------------------------------
    -- 2. Renew must accept 'pending'. The worker heartbeats BEFORE begin_send,
    --    while the row is still pending. A copied status='sending' predicate
    --    would make every pre-send heartbeat fail, every settlement would look
    --    lease-lost, and nothing would ever send.
    ------------------------------------------------------------------
    if not public.renew_period_settlement_lease(v_id, 'assert-worker', 120) then
        raise exception 'renew refused a pending row; the pre-send heartbeat would fail';
    end if;
    if public.renew_period_settlement_lease(v_id, 'other-worker', 120) then
        raise exception 'renew accepted a stale owner';
    end if;

    ------------------------------------------------------------------
    -- 3. begin_send moves status and marker in ONE update -- the only
    --    transition into 'sending' the latch permits.
    ------------------------------------------------------------------
    if not public.mark_period_settlement_outbound_started(v_id, 'assert-worker') then
        raise exception 'begin_send refused a leased pending row';
    end if;
    select status, outbound_started_at into v_status, v_marker
      from public.period_settlement_ledger where id = v_id;
    if v_status <> 'sending' or v_marker is null then
        raise exception 'begin_send left status=% marker=%', v_status, v_marker;
    end if;
    -- Idempotent for a reclaimed marker-carrying row: a second call must not
    -- re-stamp the marker (the latch would RAISE, aborting the caller).
    if not public.mark_period_settlement_outbound_started(v_id, 'assert-worker') then
        raise exception 'begin_send was not idempotent for the same owner';
    end if;
    if (select outbound_started_at from public.period_settlement_ledger where id = v_id)
       <> v_marker then
        raise exception 'begin_send re-stamped an existing outbound marker';
    end if;

    ------------------------------------------------------------------
    -- 4. Lease fencing on completion.
    ------------------------------------------------------------------
    if public.complete_period_settlement_entry(v_id, 'other-worker', 'reported') then
        raise exception 'a stale owner completed the settlement';
    end if;

    ------------------------------------------------------------------
    -- 5. The 429 path returns the row to 'pending' and refunds the attempt
    --    WITHOUT clearing the send marker. The marker is what keeps the fee
    --    inside 202607280008's committed sum, so the period cannot be
    --    re-billed while a possibly-ingested send is outstanding.
    ------------------------------------------------------------------
    select attempts into v_attempts
      from public.period_settlement_ledger where id = v_id;
    if not public.release_period_settlement_claim(v_id, 'assert-worker') then
        raise exception 'the 429 release refused a leased sending row';
    end if;
    select status, outbound_started_at, attempts
      into v_status, v_marker, v_ok
      from public.period_settlement_ledger where id = v_id;
    if v_status <> 'pending' then
        raise exception 'the 429 release left status=%', v_status;
    end if;
    if v_marker is null then
        raise exception 'the 429 release CLEARED the outbound marker; the period could be re-billed';
    end if;
    if (select attempts from public.period_settlement_ledger where id = v_id)
       <> v_attempts - 1 then
        raise exception 'the 429 release did not refund the attempt';
    end if;

    ------------------------------------------------------------------
    -- 6. A reclaim reports reclaimed=true so the worker reconciles before
    --    re-sending, then completion is terminal and stamps both times.
    ------------------------------------------------------------------
    update public.period_settlement_ledger
       set next_attempt_at = now() where id = v_id;
    select * into v_row
      from public.claim_period_settlement_entries('assert-worker2', 120, 1, 2000000000);
    if v_row.id is distinct from v_id or not v_row.reclaimed then
        raise exception 'a marker-carrying row did not report reclaimed=true';
    end if;
    perform public.mark_period_settlement_outbound_started(v_id, 'assert-worker2');
    if not public.complete_period_settlement_entry(v_id, 'assert-worker2', 'reported') then
        raise exception 'completion refused the lease owner';
    end if;
    select status into v_status from public.period_settlement_ledger where id = v_id;
    if v_status <> 'reported' then
        raise exception 'completion left status=%', v_status;
    end if;
    if (select reported_at is null or settled_at is null
          from public.period_settlement_ledger where id = v_id) then
        raise exception 'reported completion did not stamp reported_at and settled_at';
    end if;

    ------------------------------------------------------------------
    -- 7. A reported settlement is never claimed again.
    ------------------------------------------------------------------
    select * into v_row
      from public.claim_period_settlement_entries('assert-worker3', 120, 1, 2000000000);
    if v_row.id is not null then
        raise exception 'a reported settlement was re-claimed as %', v_row.id;
    end if;

    ------------------------------------------------------------------
    -- 8. Only promoted rows are claimable. A 'draft' has not been approved by an
    --    operator and a 'void' has been retracted; sending either would bill
    --    money nobody authorised.
    ------------------------------------------------------------------
    perform pg_temp.new_settlement('draft', 500000, 1);
    select * into v_row
      from public.claim_period_settlement_entries('assert-worker4', 120, 1, 2000000000);
    if v_row.id is not null then
        raise exception 'a draft settlement was claimable as %', v_row.id;
    end if;

    ------------------------------------------------------------------
    -- 9. Parameter validation, mirroring the per-row claim.
    ------------------------------------------------------------------
    begin
        perform public.claim_period_settlement_entries('assert-worker', 120, 2, 2000000000);
        raise exception 'the claim accepted p_limit <> 1';
    exception when raise_exception then
        if position('exactly one row' in sqlerrm) = 0 then raise; end if;
    end;
    begin
        perform public.claim_period_settlement_entries('  ', 120, 1, 2000000000);
        raise exception 'the claim accepted a blank owner';
    exception when raise_exception then
        if position('non-blank owner' in sqlerrm) = 0 then raise; end if;
    end;

    raise notice 'settlement sender: all behavioural assertions passed';
end;
$$;

-- ---------------------------------------------------------------------------
-- 10. Privilege posture. Every mutation must go through these SECURITY DEFINER
--     RPCs; direct DML would bypass the lease fence and the latches entirely.
-- ---------------------------------------------------------------------------
do $$
declare
    v_role text;
    v_fn text;
begin
    foreach v_fn in array array[
        'public.claim_period_settlement_entries(text,integer,integer,bigint)',
        'public.mark_period_settlement_outbound_started(bigint,text)',
        'public.renew_period_settlement_lease(bigint,text,integer)',
        'public.complete_period_settlement_entry(bigint,text,text,text)',
        'public.release_period_settlement_leases(text)',
        'public.release_period_settlement_claim(bigint,text)',
        'public.billing_period_settlement_history(uuid,integer)'
    ] loop
        if not (select prosecdef from pg_proc where oid = v_fn::regprocedure) then
            raise exception '% is not SECURITY DEFINER', v_fn;
        end if;
        if not has_function_privilege('service_role', v_fn, 'EXECUTE') then
            raise exception 'service_role cannot execute %', v_fn;
        end if;
        foreach v_role in array array['anon', 'authenticated'] loop
            if has_function_privilege(v_role, v_fn, 'EXECUTE') then
                raise exception 'browser role % can execute %', v_role, v_fn;
            end if;
        end loop;
    end loop;

    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        if has_table_privilege(v_role, 'public.period_settlement_ledger', 'INSERT')
           or has_table_privilege(v_role, 'public.period_settlement_ledger', 'UPDATE')
           or has_table_privilege(v_role, 'public.period_settlement_ledger', 'DELETE') then
            raise exception '% holds direct DML on period_settlement_ledger', v_role;
        end if;
    end loop;

    -- The 429 path must never learn to write the marker. This mirrors the
    -- migration's own self-check so a later edit cannot quietly remove it.
    if position('outbound_started_at' in (
           select prosrc from pg_proc
            where oid = 'public.release_period_settlement_claim(bigint,text)'::regprocedure)) > 0 then
        raise exception 'release_period_settlement_claim references outbound_started_at';
    end if;

    raise notice 'settlement sender: privilege posture intact';
end;
$$;

rollback;
