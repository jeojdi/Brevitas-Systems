\set ON_ERROR_STOP on

-- Live behaviour of the org-scoped billing claim RPC, the weekly safety cap,
-- and the service_role write posture that makes the lease model meaningful.
--
-- Covers docs/STRIPE_TEST_PLAN.md rows I2, I3 and I6:
--   I2  public.claim_billing_ledger_entries(text,integer,integer,bigint) as
--       redefined by 202607200006_company_billing_authorization.sql:302-434 --
--       fresh claim, reclaim with an UNCHANGED expected_period_microusd, and
--       the p_limit <> 1 rejection. Before this file the function was pinned
--       only by a source grep of the SUPERSEDED user-scoped definition in
--       202607170004_billing_recovery.sql:139-320, so no test ever executed
--       the company-scoped body that production actually calls.
--   I3  the weekly safety cap -> 'capped' transition and the reclaim exemption
--       (202607200006:412-419). This is the only ceiling on what the recovery
--       worker can meter and it had zero coverage.
--   I6  service_role cannot write public.billing_ledger at all
--       (202607220001_service_role_data_plane.sql:61,92) -- the foundation of
--       the whole lease-fencing model: every mutation must go through a
--       SECURITY DEFINER RPC that takes the lease, never through PostgREST.
--
-- Fixture rows are DIRECT public.billing_ledger INSERTs. 202607280006 retired
-- queue_brevitas_fee_after_usage, so usage no longer queues a fee, and
-- billing_ledger carries no BEFORE INSERT trigger (202607170004:501-508 add
-- only a per-statement delete guard and a per-row identity guard).
--
-- The whole file is one transaction that ends in ROLLBACK, so it is rerunnable
-- against a database the other forward suites have already seeded and it
-- leaves no financial rows behind.

begin;

-- Deliberately a NON-UTC session timezone. The recovery worker runs UTC on
-- Railway, but psql inherits whatever the server default is, and every account
-- window below is expressed in exact seconds rather than calendar days. If any
-- part of the claim path ever became wall-clock sensitive, this file is where
-- it surfaces.
set local timezone = 'America/New_York';

-- ---------------------------------------------------------------------------
-- 0. Isolation from rows other forward suites committed earlier in the run.
--
-- claim_billing_ledger_entries claims the globally oldest eligible row; it is
-- not scoped to a caller-supplied organization. Earlier suites leave 'pending'
-- rows behind (migration-assertions.sql, migration-company-billing-…,
-- dr/compliance-workflow-…), so without this the claims below would return
-- another suite's row. next_attempt_at is not one of the immutable identity
-- columns (202607170004:485-497), so pushing it out is legal, and the ROLLBACK
-- undoes it. Every fixture row below is seeded quarantined the same way and
-- activated only for the section that claims it, which also removes any
-- dependence on the (next_attempt_at, occurred_at, id) tie-break order.
-- ---------------------------------------------------------------------------
update public.billing_ledger
   set next_attempt_at = now() + interval '3650 days';

create function pg_temp.ledger_id(p_request_id text) returns bigint
language sql
stable
as $$
    select ledger.id
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.request_id = p_request_id;
$$;

create function pg_temp.activate(p_request_id text) returns void
language sql
as $$
    update public.billing_ledger
       set next_attempt_at = now()
     where id = pg_temp.ledger_id(p_request_id);
$$;

create function pg_temp.quarantine(p_request_id text) returns void
language sql
as $$
    update public.billing_ledger
       set next_attempt_at = now() + interval '3650 days'
     where id = pg_temp.ledger_id(p_request_id);
$$;

-- ---------------------------------------------------------------------------
-- 1. I6 -- service_role has no DML on public.billing_ledger.
--
-- 202607220001 revokes everything and grants back SELECT only. The privilege
-- check runs before any trigger fires, so DELETE must fail with 42501
-- (insufficient_privilege) rather than the P0001 the seven-year delete guard
-- would raise for a privileged caller.
-- ---------------------------------------------------------------------------
do $$
declare
    v_denied text[] := '{}';
    v_seed_usage bigint;
    v_seed_org uuid;
    v_seed_user uuid;
    v_privilege text;
begin
    for v_privilege in
        select unnest(array['INSERT','UPDATE','DELETE','TRUNCATE',
                            'REFERENCES','TRIGGER'])
    loop
        if has_table_privilege('service_role','public.billing_ledger',v_privilege) then
            raise exception 'service_role holds % on billing_ledger', v_privilege;
        end if;
    end loop;
    if not has_table_privilege('service_role','public.billing_ledger','SELECT') then
        raise exception 'service_role lost SELECT on billing_ledger';
    end if;

    -- An existing row/identity so the statements below are rejected for the
    -- privilege, never for a missing foreign key.
    select ledger.usage_log_id, ledger.organization_id, ledger.user_id
      into v_seed_usage, v_seed_org, v_seed_user
      from public.billing_ledger ledger
     order by ledger.id
     limit 1;

    set local role service_role;

    begin
        if v_seed_usage is null then
            insert into public.billing_ledger(
                usage_log_id,organization_id,user_id,occurred_at,fee_microusd
            ) values (
                -1,'00000000-0000-4000-8000-000000000000',
                '00000000-0000-4000-8000-000000000000',now(),1
            );
        else
            insert into public.billing_ledger(
                usage_log_id,organization_id,user_id,occurred_at,fee_microusd
            ) values (v_seed_usage,v_seed_org,v_seed_user,now(),1);
        end if;
        raise exception 'service_role inserted into billing_ledger';
    exception when insufficient_privilege then
        v_denied := v_denied || 'insert'::text;
    end;

    begin
        update public.billing_ledger set last_error = 'service_role write';
        raise exception 'service_role updated billing_ledger';
    exception when insufficient_privilege then
        v_denied := v_denied || 'update'::text;
    end;

    begin
        delete from public.billing_ledger;
        raise exception 'service_role deleted from billing_ledger';
    exception when insufficient_privilege then
        v_denied := v_denied || 'delete'::text;
    end;

    reset role;

    if v_denied <> array['insert','update','delete']::text[] then
        raise exception 'service_role billing_ledger denial set was %', v_denied;
    end if;

    -- The RPCs it may reach are SECURITY DEFINER, which is the only reason the
    -- worker can move a row at all.
    if not (select prosecdef
              from pg_proc
             where oid = to_regprocedure(
                 'public.claim_billing_ledger_entries(text,integer,integer,bigint)')) then
        raise exception 'claim_billing_ledger_entries is not SECURITY DEFINER';
    end if;
    if not has_function_privilege('service_role',
        'public.claim_billing_ledger_entries(text,integer,integer,bigint)','EXECUTE') then
        raise exception 'service_role cannot execute the billing claim RPC';
    end if;
    if has_function_privilege('anon',
           'public.claim_billing_ledger_entries(text,integer,integer,bigint)','EXECUTE')
       or has_function_privilege('authenticated',
           'public.claim_billing_ledger_entries(text,integer,integer,bigint)','EXECUTE') then
        raise exception 'a browser role can execute the billing claim RPC';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. Fixtures. Three independent companies sharing one billing owner so a
--    cross-company mistake in the claim's committed sum would be visible.
--
--    Every account window is exactly seven days wide and brackets now(), which
--    is what billing_period_for_occurrence requires (202607170004:74-80).
-- ---------------------------------------------------------------------------
insert into auth.users(id,email) values
    ('bd000000-0000-4000-8000-000000000001','billing-claim-owner@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('bd100000-0000-4000-8000-00000000000a','Billing claim fixture A',
     'bd000000-0000-4000-8000-000000000001'),
    ('bd100000-0000-4000-8000-00000000000b','Billing claim fixture B',
     'bd000000-0000-4000-8000-000000000001'),
    ('bd100000-0000-4000-8000-00000000000c','Billing claim fixture C',
     'bd000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status,
    billing_started_at,current_period_start,current_period_end
) values
    ('bd100000-0000-4000-8000-00000000000a','bd000000-0000-4000-8000-000000000001',
     'cus_billing_claim_a','active',now()-interval '2 days',
     now()-interval '86400 seconds',now()-interval '86400 seconds'+interval '604800 seconds'),
    ('bd100000-0000-4000-8000-00000000000b','bd000000-0000-4000-8000-000000000001',
     'cus_billing_claim_b','trialing',now()-interval '2 days',
     now()-interval '86400 seconds',now()-interval '86400 seconds'+interval '604800 seconds'),
    ('bd100000-0000-4000-8000-00000000000c','bd000000-0000-4000-8000-000000000001',
     'cus_billing_claim_c','active',now()-interval '2 days',
     now()-interval '86400 seconds',now()-interval '86400 seconds'+interval '604800 seconds')
on conflict (organization_id) do update set
    user_id=excluded.user_id,
    stripe_customer_id=excluded.stripe_customer_id,
    subscription_status=excluded.subscription_status,
    billing_started_at=excluded.billing_started_at,
    current_period_start=excluded.current_period_start,
    current_period_end=excluded.current_period_end;

insert into public.usage_log(
    key_hash,owner_id,organization_id,request_id,authoritative,
    pricing_status,verified_savings_usd,brevitas_fee_usd,receipt_source,ts
)
select 'billing-claim-key',
       'bd000000-0000-4000-8000-000000000001',
       seed.organization_id,
       seed.request_id,
       true,'priced',4,1,'proxy',now()
  from (values
      ('billing-claim-a1','bd100000-0000-4000-8000-00000000000a'::uuid),
      ('billing-claim-b-reported','bd100000-0000-4000-8000-00000000000b'::uuid),
      ('billing-claim-b-review-sent','bd100000-0000-4000-8000-00000000000b'::uuid),
      ('billing-claim-b-review-unsent','bd100000-0000-4000-8000-00000000000b'::uuid),
      ('billing-claim-b-pending','bd100000-0000-4000-8000-00000000000b'::uuid),
      ('billing-claim-c1','bd100000-0000-4000-8000-00000000000c'::uuid),
      ('billing-claim-c2','bd100000-0000-4000-8000-00000000000c'::uuid)
  ) as seed(request_id,organization_id)
on conflict (key_hash, request_id, authoritative) where request_id<>'' do nothing;

-- Direct ledger seed. Every row starts quarantined (next_attempt_at far in the
-- future); each section activates exactly the row it intends to claim.
insert into public.billing_ledger(
    usage_log_id,organization_id,user_id,occurred_at,fee_microusd,
    status,attempts,lease_owner,lease_expires_at,outbound_started_at,
    next_attempt_at
)
select usage.id,
       usage.organization_id,
       'bd000000-0000-4000-8000-000000000001',
       usage.ts,
       seed.fee_microusd,
       seed.status,
       seed.attempts,
       seed.lease_owner,
       seed.lease_expires_at,
       seed.outbound_started_at,
       now() + interval '3650 days'
  from (values
      -- fresh claim / reclaim subject
      ('billing-claim-a1',250000::bigint,'pending',0,null::text,
       null::timestamptz,null::timestamptz),
      -- committed-sum composition for company B
      ('billing-claim-b-reported',300000::bigint,'reported',1,null::text,
       null::timestamptz,now()-interval '2 hours'),
      ('billing-claim-b-review-sent',70000::bigint,'review',2,null::text,
       null::timestamptz,now()-interval '3 hours'),
      ('billing-claim-b-review-unsent',90000::bigint,'review',2,null::text,
       null::timestamptz,null::timestamptz),
      ('billing-claim-b-pending',100000::bigint,'pending',0,null::text,
       null::timestamptz,null::timestamptz),
      -- weekly cap pair for company C
      ('billing-claim-c1',600000::bigint,'pending',0,null::text,
       null::timestamptz,null::timestamptz),
      ('billing-claim-c2',600000::bigint,'pending',0,null::text,
       null::timestamptz,null::timestamptz)
  ) as seed(request_id,fee_microusd,status,attempts,lease_owner,
            lease_expires_at,outbound_started_at)
  join public.usage_log usage on usage.request_id = seed.request_id
on conflict (usage_log_id) do nothing;

do $$
begin
    if (select count(*) from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id like 'billing-claim-%') <> 7 then
        raise exception 'billing claim fixture did not seed seven ledger rows';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. I2 -- parameter validation. p_limit <> 1 is the load-bearing one: the
--    worker's whole duplicate-send story assumes one row per claim, and the
--    SQL guard is the only thing enforcing it (there is no reader for
--    BREVITAS_BILLING_BATCH_SIZE).
-- ---------------------------------------------------------------------------
do $$
declare
    v_case record;
    v_message text;
begin
    for v_case in
        select *
          from (values
              ('p_limit=2','claim-owner',60,2,1000000::bigint),
              ('p_limit=0','claim-owner',60,0,1000000::bigint),
              ('p_limit=-1','claim-owner',60,-1,1000000::bigint),
              ('blank owner','   ',60,1,1000000::bigint),
              ('null owner',null,60,1,1000000::bigint),
              ('lease 14s','claim-owner',14,1,1000000::bigint),
              ('lease 901s','claim-owner',901,1,1000000::bigint),
              ('cap 0','claim-owner',60,1,0::bigint),
              ('cap negative','claim-owner',60,1,-1::bigint)
          ) as t(label,owner,lease_seconds,limit_rows,cap)
    loop
        begin
            perform * from public.claim_billing_ledger_entries(
                v_case.owner,v_case.lease_seconds,v_case.limit_rows,v_case.cap);
            raise exception 'claim accepted invalid parameters (%)', v_case.label;
        exception when raise_exception then
            v_message := sqlerrm;
            if v_message <> 'invalid billing claim parameters' then
                raise exception 'claim rejected % with the wrong message: %',
                    v_case.label, v_message;
            end if;
        end;
    end loop;

    -- The accepted boundaries of the same guard.
    perform * from public.claim_billing_ledger_entries('claim-owner-probe',15,1,1);
    perform * from public.claim_billing_ledger_entries('claim-owner-probe',900,1,1);
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. I2 -- a fresh claim of a 'pending' row.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
    v_row public.billing_ledger%rowtype;
    v_account public.billing_accounts%rowtype;
begin
    perform pg_temp.activate('billing-claim-a1');
    select * into v_account from public.billing_accounts
     where organization_id = 'bd100000-0000-4000-8000-00000000000a';

    select * into v_claim
      from public.claim_billing_ledger_entries('claim-owner-fresh',600,1,1000000);
    if not found then
        raise exception 'a fresh pending ledger row was not claimed';
    end if;
    if v_claim.id <> pg_temp.ledger_id('billing-claim-a1') then
        raise exception 'the claim returned an unexpected ledger row (%)', v_claim.id;
    end if;
    if v_claim.reclaimed is not false then
        raise exception 'a pending row was reported as reclaimed';
    end if;
    if v_claim.fee_microusd <> 250000 then
        raise exception 'the claim reported fee % instead of 250000',
            v_claim.fee_microusd;
    end if;
    -- The fresh row is not yet part of the committed sum, so the function adds
    -- its own fee: expected_period_microusd is exactly this row's fee.
    if v_claim.expected_period_microusd <> 250000 then
        raise exception 'a fresh claim expected % rather than its own 250000 microUSD',
            v_claim.expected_period_microusd;
    end if;
    if v_claim.attempts <> 1 then
        raise exception 'the claim reported attempt % instead of 1', v_claim.attempts;
    end if;
    if v_claim.outbound_started_at is not null then
        raise exception 'a never-sent row carried an outbound marker';
    end if;
    if v_claim.stripe_customer_id <> 'cus_billing_claim_a' then
        raise exception 'the claim resolved the wrong Stripe customer (%)',
            v_claim.stripe_customer_id;
    end if;
    if v_claim.period_start <> v_account.current_period_start
       or v_claim.period_end <> v_account.current_period_end then
        raise exception 'the claim did not reconstruct the live Stripe period';
    end if;
    if v_claim.period_end - v_claim.period_start <> interval '7 days' then
        raise exception 'the claim returned a non-seven-day period';
    end if;
    if v_claim.user_id <> 'bd000000-0000-4000-8000-000000000001' then
        raise exception 'the claim returned the wrong billing owner';
    end if;

    select * into v_row from public.billing_ledger
     where id = pg_temp.ledger_id('billing-claim-a1');
    if v_row.status <> 'sending' then
        raise exception 'a claimed row is in status % instead of sending', v_row.status;
    end if;
    if v_row.lease_owner <> 'claim-owner-fresh' then
        raise exception 'the claimed row records lease owner %', v_row.lease_owner;
    end if;
    if v_row.lease_expires_at is null
       or v_row.lease_expires_at <= now()
       or v_row.lease_expires_at > now() + interval '600 seconds' then
        raise exception 'the claimed lease does not expire inside the requested window';
    end if;
    if v_row.attempts <> 1 or v_row.last_error <> '' then
        raise exception 'a claimed row kept attempts %/last_error %',
            v_row.attempts, v_row.last_error;
    end if;
    if v_row.outbound_started_at is not null then
        raise exception 'claiming a row marked an outbound Stripe send';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. I2 -- reclaiming an expired lease leaves expected_period_microusd
--    UNCHANGED. The row is already counted as 'sending' in the committed sum,
--    so the function must not add its fee a second time; if it did, the
--    worker's reconcile() would compare Stripe's aggregate against an expected
--    total that double-counts the row it is about to replay.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
    v_row public.billing_ledger%rowtype;
begin
    update public.billing_ledger
       set lease_expires_at = now() - interval '1 second'
     where id = pg_temp.ledger_id('billing-claim-a1');

    select * into v_claim
      from public.claim_billing_ledger_entries('claim-owner-reclaim',600,1,1000000);
    if not found then
        raise exception 'an expired sending lease was not reclaimable';
    end if;
    if v_claim.id <> pg_temp.ledger_id('billing-claim-a1') then
        raise exception 'the reclaim returned an unexpected ledger row (%)', v_claim.id;
    end if;
    if v_claim.reclaimed is not true then
        raise exception 'a reclaimed row was not flagged as reclaimed';
    end if;
    if v_claim.expected_period_microusd <> 250000 then
        raise exception 'the reclaim double counted the row: expected %',
            v_claim.expected_period_microusd;
    end if;
    if v_claim.attempts <> 2 then
        raise exception 'the reclaim reported attempt % instead of 2', v_claim.attempts;
    end if;

    select * into v_row from public.billing_ledger
     where id = pg_temp.ledger_id('billing-claim-a1');
    if v_row.status <> 'sending' or v_row.attempts <> 2
       or v_row.lease_owner <> 'claim-owner-reclaim' then
        raise exception 'the reclaim did not re-lease the row to the new owner';
    end if;

    perform pg_temp.quarantine('billing-claim-a1');
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. I2 -- expected_period_microusd counts exactly the amounts that may
--    already be at Stripe: 'sending', 'reported', and 'review' rows whose
--    outbound send was started. A 'review' row with no outbound marker was
--    provably never sent and must stay excluded (202607200006:390-407).
--
--    Company B: reported 300000 + ambiguous-sent review 70000 + the claimed
--    100000 = 470000. The 90000 unsent review row must not appear.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
begin
    perform pg_temp.activate('billing-claim-b-pending');
    select * into v_claim
      from public.claim_billing_ledger_entries('claim-owner-composition',600,1,10000000);
    if not found then
        raise exception 'company B pending row was not claimed';
    end if;
    if v_claim.id <> pg_temp.ledger_id('billing-claim-b-pending') then
        raise exception 'the composition claim crossed companies (%)', v_claim.id;
    end if;
    if v_claim.expected_period_microusd <> 470000 then
        raise exception 'expected_period_microusd was % instead of 470000',
            v_claim.expected_period_microusd;
    end if;
    if v_claim.stripe_customer_id <> 'cus_billing_claim_b' then
        raise exception 'a trialing account was not billable';
    end if;
    if (select status from public.billing_ledger
         where id = pg_temp.ledger_id('billing-claim-b-review-unsent')) <> 'review' then
        raise exception 'the unsent review row was mutated by an unrelated claim';
    end if;

    perform pg_temp.quarantine('billing-claim-b-pending');
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. I3 -- the weekly safety cap. Two 600000-microUSD rows against a 1000000
--    cap: the first claim succeeds, the second must be parked in 'capped' and
--    NOT returned to the worker.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
    v_row public.billing_ledger%rowtype;
begin
    perform pg_temp.activate('billing-claim-c1');
    select * into v_claim
      from public.claim_billing_ledger_entries('cap-owner-first',600,1,1000000);
    if not found then
        raise exception 'the first row under the weekly cap was not claimed';
    end if;
    if v_claim.id <> pg_temp.ledger_id('billing-claim-c1')
       or v_claim.expected_period_microusd <> 600000 then
        raise exception 'the first capped-period claim returned % / %',
            v_claim.id, v_claim.expected_period_microusd;
    end if;

    perform pg_temp.activate('billing-claim-c2');
    select * into v_claim
      from public.claim_billing_ledger_entries('cap-owner-second',600,1,1000000);
    if found then
        raise exception 'a claim past the weekly cap was handed to the worker (%)',
            v_claim.id;
    end if;

    select * into v_row from public.billing_ledger
     where id = pg_temp.ledger_id('billing-claim-c2');
    if v_row.status <> 'capped' then
        raise exception 'the over-cap row is in status % instead of capped',
            v_row.status;
    end if;
    if v_row.last_error <> 'weekly safety cap reached' then
        raise exception 'the over-cap row recorded last_error %', v_row.last_error;
    end if;
    if v_row.lease_owner is not null or v_row.lease_expires_at is not null then
        raise exception 'the over-cap row retained a lease';
    end if;
    if v_row.attempts <> 0 then
        raise exception 'the over-cap row burned attempt %', v_row.attempts;
    end if;
    if v_row.outbound_started_at is not null then
        raise exception 'the over-cap row was marked as sent';
    end if;

    select * into v_row from public.billing_ledger
     where id = pg_temp.ledger_id('billing-claim-c1');
    if v_row.status <> 'sending' or v_row.attempts <> 1
       or v_row.lease_owner <> 'cap-owner-first' then
        raise exception 'hitting the cap disturbed the already-claimed row';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 8. I3 -- the reclaim exemption. An in-flight row whose lease expired is
--    re-leased even though its own fee is already inside the committed sum and
--    re-applying the cap test would reject it. Section 7 is the counterfactual:
--    the identical fee and cap parked a FRESH row in 'capped'.
--
--    Without the `not was_reclaimed` guard (202607200006:412) an ambiguous
--    send could never be finished, and the row would rot until the 23h sweep
--    moved it to 'review'.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
    v_committed bigint;
    v_row public.billing_ledger%rowtype;
begin
    update public.billing_ledger
       set lease_expires_at = now() - interval '1 second'
     where id = pg_temp.ledger_id('billing-claim-c1');

    select coalesce(sum(ledger.fee_microusd),0) into v_committed
      from public.billing_ledger ledger
     where ledger.organization_id = 'bd100000-0000-4000-8000-00000000000c'
       and (ledger.status in ('sending','reported')
            or (ledger.status = 'review' and ledger.outbound_started_at is not null));
    if v_committed <> 600000 then
        raise exception 'the capped row leaked into the committed sum (%)', v_committed;
    end if;
    if v_committed + 600000 <= 1000000 then
        raise exception 'the reclaim exemption fixture no longer exceeds the cap';
    end if;

    select * into v_claim
      from public.claim_billing_ledger_entries('cap-owner-reclaim',600,1,1000000);
    if not found then
        raise exception 'the weekly cap blocked a reclaimed in-flight row';
    end if;
    if v_claim.id <> pg_temp.ledger_id('billing-claim-c1')
       or v_claim.reclaimed is not true then
        raise exception 'the cap-exempt claim returned % (reclaimed %)',
            v_claim.id, v_claim.reclaimed;
    end if;
    if v_claim.expected_period_microusd <> 600000 then
        raise exception 'the cap-exempt reclaim expected % instead of 600000',
            v_claim.expected_period_microusd;
    end if;

    select * into v_row from public.billing_ledger
     where id = pg_temp.ledger_id('billing-claim-c1');
    if v_row.status <> 'sending' or v_row.attempts <> 2
       or v_row.last_error <> '' then
        raise exception 'the cap-exempt reclaim left the row as %/%/%',
            v_row.status, v_row.attempts, v_row.last_error;
    end if;
    if (select status from public.billing_ledger
         where id = pg_temp.ledger_id('billing-claim-c2')) <> 'capped' then
        raise exception 'the reclaim un-capped the parked row';
    end if;
end;
$$;

rollback;
