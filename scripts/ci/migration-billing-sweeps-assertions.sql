\set ON_ERROR_STOP on

-- The three database sweeps that run at the head of every billing claim, and
-- the boundaries at which they must NOT fire.
--
-- Covers docs/STRIPE_TEST_PLAN.md row I4. The sweeps were introduced in
-- 202607170004_billing_recovery.sql:174-207 and are carried forward verbatim
-- into the live company-scoped function
-- 202607200006_company_billing_authorization.sql:344-360, which is the
-- definition this file exercises:
--
--   1. 'pending' older than 34 days   -> 'expired'
--      last_error 'Stripe 35-day reporting window elapsed'
--   2. 'sending' with an expired lease whose outbound send began more than
--      23 hours ago -> 'review'
--      last_error 'ambiguous Stripe send exceeded safe replay window'
--   3. 'sending' with an expired lease that has exhausted max_attempts
--      -> 'review'
--      last_error 'billing recovery attempts exhausted'
--
-- The exact last_error strings are the contract: they are what an operator
-- reads before deciding between a safe same-identifier replay, a manual
-- reconciliation, and writing the money off. The 23-hour bound is the only
-- thing standing between an ambiguous send and a duplicate charge outside
-- Stripe's identifier-deduplication window, so its boundary is pinned in both
-- directions.
--
-- Fixture rows are DIRECT public.billing_ledger INSERTs (202607280006 retired
-- the per-row fee trigger; billing_ledger has no BEFORE INSERT trigger).
-- One transaction, ending in ROLLBACK, so the file is rerunnable and leaves no
-- financial rows behind.

begin;

-- Deliberately a NON-UTC session timezone. The recovery worker runs UTC on
-- Railway, but psql inherits whatever the server default is, and every account
-- window below is expressed in exact seconds rather than calendar days. If any
-- part of the claim path ever became wall-clock sensitive, this file is where
-- it surfaces.
set local timezone = 'America/New_York';

-- ---------------------------------------------------------------------------
-- 0. Isolation. The sweeps are deliberately global -- they carry no
--    organization predicate -- so they will also touch rows earlier forward
--    suites committed. That is fine (the ROLLBACK undoes it), but the claim
--    LOOP must find nothing, otherwise "the claim returned no row" below would
--    be satisfied by an unrelated fixture instead of by the sweeps. Pushing
--    next_attempt_at out excludes every pre-existing row from the loop while
--    leaving the sweeps -- which ignore next_attempt_at -- free to run.
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

create function pg_temp.ledger_row(p_request_id text)
returns public.billing_ledger
language sql
stable
as $$
    select ledger.*
      from public.billing_ledger ledger
     where ledger.id = pg_temp.ledger_id(p_request_id);
$$;

-- ---------------------------------------------------------------------------
-- 1. Fixtures. Two companies, so one claim call is shown to sweep across
--    company boundaries.
-- ---------------------------------------------------------------------------
insert into auth.users(id,email) values
    ('be000000-0000-4000-8000-000000000001','billing-sweep-owner@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('be100000-0000-4000-8000-00000000000a','Billing sweep fixture A',
     'be000000-0000-4000-8000-000000000001'),
    ('be100000-0000-4000-8000-00000000000b','Billing sweep fixture B',
     'be000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status,
    billing_started_at,current_period_start,current_period_end
) values
    ('be100000-0000-4000-8000-00000000000a','be000000-0000-4000-8000-000000000001',
     'cus_billing_sweep_a','active',now()-interval '90 days',
     now()-interval '86400 seconds',now()-interval '86400 seconds'+interval '604800 seconds'),
    ('be100000-0000-4000-8000-00000000000b','be000000-0000-4000-8000-000000000001',
     'cus_billing_sweep_b','active',now()-interval '90 days',
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
select 'billing-sweep-key',
       'be000000-0000-4000-8000-000000000001',
       seed.organization_id,
       seed.request_id,
       true,'priced',4,1,'proxy',
       seed.occurred_at
  from (values
      ('sweep-expired','be100000-0000-4000-8000-00000000000a'::uuid,
       now()-interval '35 days'),
      ('sweep-expired-boundary','be100000-0000-4000-8000-00000000000a'::uuid,
       now()-interval '34 days'-interval '1 minute'),
      ('sweep-ambiguous','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-exhausted','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-ambiguous-and-exhausted','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-keep-pending-boundary','be100000-0000-4000-8000-00000000000a'::uuid,
       now()-interval '34 days'+interval '1 minute'),
      ('sweep-keep-pending-recent','be100000-0000-4000-8000-00000000000a'::uuid,
       now()-interval '33 days'),
      ('sweep-keep-live-lease','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-keep-outbound-22h','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-keep-attempts-below-max','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '2 days'),
      ('sweep-keep-old-sending','be100000-0000-4000-8000-00000000000b'::uuid,
       now()-interval '40 days')
  ) as seed(request_id,organization_id,occurred_at)
on conflict (key_hash, request_id, authoritative) where request_id<>'' do nothing;

-- Every row is seeded quarantined. The sweeps do not consult next_attempt_at,
-- so quarantining is invisible to them; it only keeps the claim LOOP from
-- picking up the rows that are deliberately left reclaimable.
insert into public.billing_ledger(
    usage_log_id,organization_id,user_id,occurred_at,fee_microusd,
    status,attempts,max_attempts,lease_owner,lease_expires_at,
    outbound_started_at,next_attempt_at,last_error
)
select usage.id,
       usage.organization_id,
       'be000000-0000-4000-8000-000000000001',
       usage.ts,
       120000,
       seed.status,
       seed.attempts,
       seed.max_attempts,
       seed.lease_owner,
       seed.lease_expires_at,
       seed.outbound_started_at,
       now() + interval '3650 days',
       ''
  from (values
      -- sweep 1: never attempted, outside Stripe's reporting window.
      ('sweep-expired','pending',0,5,'ghost-worker'::text,
       now()-interval '1 hour',null::timestamptz),
      ('sweep-expired-boundary','pending',0,5,null::text,
       null::timestamptz,null::timestamptz),
      -- sweep 2: ambiguous send older than 23 hours.
      ('sweep-ambiguous','sending',1,5,'ghost-worker'::text,
       now()-interval '1 minute',now()-interval '24 hours'),
      -- sweep 3: attempts exhausted (outbound marker deliberately recent so
      -- only the attempts sweep can match).
      ('sweep-exhausted','sending',5,5,'ghost-worker'::text,
       now()-interval '1 minute',now()-interval '1 hour'),
      -- matches BOTH sweep 2 and sweep 3: sweep 2 runs first and wins.
      ('sweep-ambiguous-and-exhausted','sending',5,5,'ghost-worker'::text,
       now()-interval '1 minute',now()-interval '30 hours'),
      -- negative controls.
      ('sweep-keep-pending-boundary','pending',0,5,null::text,
       null::timestamptz,null::timestamptz),
      ('sweep-keep-pending-recent','pending',0,5,null::text,
       null::timestamptz,null::timestamptz),
      ('sweep-keep-live-lease','sending',5,5,'live-worker'::text,
       now()+interval '10 minutes',now()-interval '30 hours'),
      ('sweep-keep-outbound-22h','sending',1,5,'ghost-worker'::text,
       now()-interval '1 minute',now()-interval '22 hours'),
      ('sweep-keep-attempts-below-max','sending',4,5,'ghost-worker'::text,
       now()-interval '1 minute',now()-interval '1 hour'),
      -- 40 days old but 'sending' under a live lease: the reporting-window
      -- sweep is pending-only and must not strand a row a worker still holds.
      ('sweep-keep-old-sending','sending',1,5,'live-worker'::text,
       now()+interval '10 minutes',null::timestamptz)
  ) as seed(request_id,status,attempts,max_attempts,lease_owner,
            lease_expires_at,outbound_started_at)
  join public.usage_log usage on usage.request_id = seed.request_id
on conflict (usage_log_id) do nothing;

do $$
begin
    if (select count(*) from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id like 'sweep-%') <> 11 then
        raise exception 'billing sweep fixture did not seed eleven ledger rows';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. ONE claim call performs all three sweeps, and finds no candidate.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
    v_row public.billing_ledger;
    v_case record;
begin
    select * into v_claim
      from public.claim_billing_ledger_entries('sweep-owner',600,1,100000000);
    if found then
        raise exception 'the sweep probe claim unexpectedly leased row %', v_claim.id;
    end if;

    -- Swept rows: exact status and exact last_error, with the stale lease
    -- released so no worker believes it still owns them.
    for v_case in
        select *
          from (values
              ('sweep-expired','expired',
               'Stripe 35-day reporting window elapsed'),
              ('sweep-expired-boundary','expired',
               'Stripe 35-day reporting window elapsed'),
              ('sweep-ambiguous','review',
               'ambiguous Stripe send exceeded safe replay window'),
              ('sweep-exhausted','review',
               'billing recovery attempts exhausted'),
              ('sweep-ambiguous-and-exhausted','review',
               'ambiguous Stripe send exceeded safe replay window')
          ) as t(request_id,expected_status,expected_error)
    loop
        v_row := pg_temp.ledger_row(v_case.request_id);
        if v_row.status <> v_case.expected_status then
            raise exception '% was swept to % instead of %',
                v_case.request_id, v_row.status, v_case.expected_status;
        end if;
        if v_row.last_error <> v_case.expected_error then
            raise exception '% recorded last_error % instead of %',
                v_case.request_id, v_row.last_error, v_case.expected_error;
        end if;
        if v_row.lease_owner is not null or v_row.lease_expires_at is not null then
            raise exception '% kept a lease after being swept', v_case.request_id;
        end if;
    end loop;

    -- An ambiguous send keeps its outbound marker: it is the evidence that
    -- Stripe may already hold the fee, and both the weekly cap and
    -- expected_period_microusd read it (202607200006:390-407).
    v_row := pg_temp.ledger_row('sweep-ambiguous');
    if v_row.outbound_started_at is null then
        raise exception 'the ambiguous sweep erased the outbound send marker';
    end if;
    -- No sweep is allowed to change an amount, an attempt count, or a reported
    -- timestamp: they only quarantine.
    if v_row.attempts <> 1 then
        raise exception 'the ambiguous sweep changed the attempt count to %',
            v_row.attempts;
    end if;

    v_row := pg_temp.ledger_row('sweep-exhausted');
    if v_row.outbound_started_at is null then
        raise exception 'the attempts sweep erased the outbound send marker';
    end if;
    if v_row.attempts <> 5 then
        raise exception 'the attempts sweep changed the attempt count to %',
            v_row.attempts;
    end if;
    if exists (select 1 from public.billing_ledger ledger
                 join public.usage_log usage on usage.id = ledger.usage_log_id
                where usage.request_id like 'sweep-%'
                  and (ledger.fee_microusd <> 120000
                       or ledger.reported_at is not null)) then
        raise exception 'a sweep altered a fee or stamped a reported timestamp';
    end if;

    -- Negative controls: each is one boundary step away from a sweep.
    for v_case in
        select *
          from (values
              ('sweep-keep-pending-boundary','pending',
               'a pending row 34 days old minus one minute was expired'),
              ('sweep-keep-pending-recent','pending',
               'a pending row inside the reporting window was expired'),
              ('sweep-keep-live-lease','sending',
               'a live lease was swept out from under its worker'),
              ('sweep-keep-outbound-22h','sending',
               'a 22-hour-old ambiguous send was sent to review early'),
              ('sweep-keep-attempts-below-max','sending',
               'a row with attempts below max_attempts was sent to review'),
              ('sweep-keep-old-sending','sending',
               'a 40-day-old row under a live lease was expired')
          ) as t(request_id,expected_status,failure)
    loop
        v_row := pg_temp.ledger_row(v_case.request_id);
        if v_row.status <> v_case.expected_status or v_row.last_error <> '' then
            raise exception '%(status %, last_error %)',
                v_case.failure, v_row.status, v_row.last_error;
        end if;
    end loop;

    -- The live lease in particular must be untouched, owner included.
    v_row := pg_temp.ledger_row('sweep-keep-live-lease');
    if v_row.lease_owner <> 'live-worker' or v_row.lease_expires_at <= now() then
        raise exception 'a sweep released a lease that had not expired';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. Swept rows are terminal for the worker: making them due again does not
--    make them claimable. 'expired' and 'review' are outside the loop's
--    (pending | expired-lease sending) candidate set, so only the manual
--    recovery RPC can revive them.
-- ---------------------------------------------------------------------------
do $$
declare
    v_claim record;
begin
    update public.billing_ledger
       set next_attempt_at = now()
     where id in (
        pg_temp.ledger_id('sweep-expired'),
        pg_temp.ledger_id('sweep-expired-boundary'),
        pg_temp.ledger_id('sweep-ambiguous'),
        pg_temp.ledger_id('sweep-exhausted'),
        pg_temp.ledger_id('sweep-ambiguous-and-exhausted'));

    select * into v_claim
      from public.claim_billing_ledger_entries('sweep-owner-retry',600,1,100000000);
    if found then
        raise exception 'a swept row was claimed again as %', v_claim.id;
    end if;

    if (select count(*) from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id like 'sweep-%'
           and ledger.status in ('expired','review')) <> 5 then
        raise exception 'a second claim cycle changed the swept statuses';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. A swept 'review' row cannot be un-quarantined by the worker's own lease
--    release either: release_billing_ledger_leases only frees 'sending' rows
--    with no outbound marker (202607170004:390-407). Only the company-scoped
--    operator RPC can revive one, and that path is covered by
--    scripts/ci/migration-billing-recovery-scope-assertions.sql.
-- ---------------------------------------------------------------------------
do $$
declare
    v_released bigint;
begin
    v_released := public.release_billing_ledger_leases('ghost-worker');
    if v_released <> 0 then
        raise exception 'lease release revived % swept rows', v_released;
    end if;
    if (select count(*) from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id like 'sweep-%'
           and ledger.status in ('expired','review')) <> 5 then
        raise exception 'lease release changed a swept status';
    end if;
end;
$$;

rollback;
