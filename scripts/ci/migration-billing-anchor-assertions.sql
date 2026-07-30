\set ON_ERROR_STOP on

-- The Stripe weekly billing-period anchor: half-open boundaries, DST and
-- session-timezone invariance, rejection of any window that is not exactly
-- seven days, and the fail-closed 'review' outcome a bad anchor produces on
-- the live claim path.
--
-- Covers docs/STRIPE_TEST_PLAN.md row I5:
--   * public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)
--     (202607170004_billing_recovery.sql:60-89) rejects a non-seven-day span
--     with SQLSTATE 22023 and reconstructs every historical week by integer
--     division of 604800 seconds.
--   * the four deterministic boundary cases the migration self-checks
--     (202607170004:91-133) are replayed here so a later `create or replace`
--     of the function cannot quietly lose them -- a migration's own DO block
--     only runs when that migration is applied.
--   * invalid anchor -> ledger row 'review' with last_error
--     'invalid Stripe weekly billing-period anchor'
--     (202607200006_company_billing_authorization.sql:386-391).
--
-- Why this matters: every fee the worker meters is attributed to the week this
-- function returns, and the weekly safety cap and expected_period_microusd are
-- both computed over that half-open interval. An off-by-one-microsecond or
-- DST-sensitive boundary would silently move money between billing weeks and
-- make reconciliation against Stripe's aggregate impossible.
--
-- One transaction ending in ROLLBACK, so the file is rerunnable and leaves no
-- financial rows behind.

begin;

-- Deliberately a NON-UTC session timezone. The recovery worker runs UTC on
-- Railway, but psql inherits whatever the server default is, and every account
-- window below is expressed in exact seconds rather than calendar days. If any
-- part of the claim path ever became wall-clock sensitive, this file is where
-- it surfaces.
set local timezone = 'America/New_York';

-- ---------------------------------------------------------------------------
-- 0. Isolation, as in the sibling claim/sweep files: the claim RPC picks the
--    globally oldest eligible row, so rows earlier suites committed are pushed
--    out of the claim window for the duration of this transaction.
-- ---------------------------------------------------------------------------
update public.billing_ledger
   set next_attempt_at = now() + interval '3650 days';

-- ---------------------------------------------------------------------------
-- 1. Markers. The function is IMMUTABLE with a pinned TimeZone, which is the
--    entire mechanism behind DST invariance, and no BROWSER role may execute it.
--    service_role is deliberately not in that list: 202607170004:512 revokes the
--    anchor from `public, anon, authenticated` only, so a live Supabase project
--    -- which grants EXECUTE on every new function to all three PostgREST roles
--    -- leaves service_role able to call it, and this file used to claim
--    otherwise only because migration-bootstrap.sql omitted those default
--    grants. That omission also made the anon/authenticated half of this check
--    vacuous; with the bootstrap now reproducing the production baseline, the
--    two clauses below actually test 202607170004's revoke. Extending the
--    revoke to service_role is NOT a safe substitute: the function is pure
--    IMMUTABLE date arithmetic that reads no table, and settle_billing_period
--    and the claim path reach it as ordinary SQL callers.
-- ---------------------------------------------------------------------------
do $$
declare
    v_proc pg_proc;
begin
    select * into v_proc from pg_proc
     where oid = to_regprocedure(
        'public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)');
    if not found then
        raise exception 'billing_period_for_occurrence is missing';
    end if;
    if v_proc.provolatile <> 'i' then
        raise exception 'the weekly anchor function is no longer IMMUTABLE (%)',
            v_proc.provolatile;
    end if;
    if not (v_proc.proconfig @> array['TimeZone=UTC']) then
        raise exception 'the weekly anchor function no longer pins TimeZone=UTC (%)',
            v_proc.proconfig;
    end if;
    if has_function_privilege('anon',v_proc.oid,'EXECUTE')
       or has_function_privilege('authenticated',v_proc.oid,'EXECUTE') then
        raise exception 'a browser role can execute the weekly anchor function directly';
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. Any window that is not exactly seven days -- and any NULL argument --
--    raises 22023 with the exact message the claim path pattern-matches on.
--    One microsecond in either direction is enough: Stripe's item-level period
--    boundaries are the only trustworthy source of the width.
-- ---------------------------------------------------------------------------
do $$
declare
    v_case record;
begin
    for v_case in
        select *
          from (values
              ('six days','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,'2026-07-21 10:00:00+00'::timestamptz),
              ('eight days','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,'2026-07-23 10:00:00+00'::timestamptz),
              ('thirty days','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,'2026-08-14 10:00:00+00'::timestamptz),
              ('one microsecond short','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 09:59:59.999999+00'::timestamptz),
              ('one microsecond long','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00.000001+00'::timestamptz),
              ('inverted window','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,'2026-07-15 10:00:00+00'::timestamptz),
              ('zero width','2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,'2026-07-15 10:00:00+00'::timestamptz),
              ('null occurrence',null::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,'2026-07-22 10:00:00+00'::timestamptz),
              ('null anchor start','2026-07-16 10:00:00+00'::timestamptz,
               null::timestamptz,'2026-07-22 10:00:00+00'::timestamptz),
              ('null anchor end','2026-07-16 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,null::timestamptz)
          ) as t(label,occurred_at,anchor_start,anchor_end)
    loop
        begin
            perform * from public.billing_period_for_occurrence(
                v_case.occurred_at,v_case.anchor_start,v_case.anchor_end);
            raise exception 'the weekly anchor accepted % as a Stripe period',
                v_case.label;
        exception when invalid_parameter_value then
            if sqlerrm <> 'invalid Stripe weekly billing-period anchor' then
                raise exception '% was rejected with the wrong message: %',
                    v_case.label, sqlerrm;
            end if;
        end;
    end loop;

    -- The accepted width, stated two ways, because the claim path and the
    -- period settlement path spell it differently.
    perform * from public.billing_period_for_occurrence(
        '2026-07-16 10:00:00+00','2026-07-15 10:00:00+00','2026-07-22 10:00:00+00');
    perform * from public.billing_period_for_occurrence(
        '2026-07-16 10:00:00+00','2026-07-15 10:00:00+00',
        '2026-07-15 10:00:00+00'::timestamptz + interval '604800 seconds');
end;
$$;

-- ---------------------------------------------------------------------------
-- 3. The four deterministic boundary cases, replayed. Cases 1-3 are the ones
--    202607170004:91-133 self-checks; case 4 (an occurrence exactly ON
--    anchor_start) is the half-open lower bound the migration does not pin.
-- ---------------------------------------------------------------------------
do $$
declare
    v_case record;
    v_start timestamptz;
    v_end timestamptz;
begin
    for v_case in
        select *
          from (values
              -- prior seven-day period
              ('prior period',
               '2026-07-09 09:59:59+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-08 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz),
              -- the anchor end instant belongs to the NEXT period (half-open)
              ('end boundary enters next period',
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-29 10:00:00+00'::timestamptz),
              -- a US daylight-saving transition is invisible in UTC
              ('DST crossing stays one UTC week',
               '2026-03-09 12:00:00+00'::timestamptz,
               '2026-03-05 10:00:00+00'::timestamptz,
               '2026-03-12 10:00:00+00'::timestamptz,
               '2026-03-05 10:00:00+00'::timestamptz,
               '2026-03-12 10:00:00+00'::timestamptz),
              -- the lower bound is INCLUSIVE
              ('start boundary is inclusive',
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz),
              -- one microsecond before the lower bound falls into the week before
              ('one microsecond before start',
               '2026-07-15 09:59:59.999999+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-08 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz),
              -- one microsecond before the upper bound stays in the anchor week
              ('one microsecond before end',
               '2026-07-22 09:59:59.999999+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz),
              -- many weeks back, well before the anchor
              ('nine weeks before the anchor',
               '2026-05-14 10:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-05-13 10:00:00+00'::timestamptz,
               '2026-05-20 10:00:00+00'::timestamptz),
              -- and a period after the anchor, which is what the settlement
              -- sweep asks for when it walks forward
              ('two weeks after the anchor',
               '2026-07-30 00:00:00+00'::timestamptz,
               '2026-07-15 10:00:00+00'::timestamptz,
               '2026-07-22 10:00:00+00'::timestamptz,
               '2026-07-29 10:00:00+00'::timestamptz,
               '2026-08-05 10:00:00+00'::timestamptz),
              -- a fall-back transition, where local wall clock gains an hour
              ('fall-back crossing stays one UTC week',
               '2026-11-02 06:00:00+00'::timestamptz,
               '2026-10-29 10:00:00+00'::timestamptz,
               '2026-11-05 10:00:00+00'::timestamptz,
               '2026-10-29 10:00:00+00'::timestamptz,
               '2026-11-05 10:00:00+00'::timestamptz)
          ) as t(label,occurred_at,anchor_start,anchor_end,
                 expected_start,expected_end)
    loop
        select period.period_start, period.period_end into v_start, v_end
          from public.billing_period_for_occurrence(
              v_case.occurred_at,v_case.anchor_start,v_case.anchor_end) period;
        if v_start <> v_case.expected_start or v_end <> v_case.expected_end then
            raise exception 'weekly-anchor regression (%): got [%, %), expected [%, %)',
                v_case.label, v_start, v_end,
                v_case.expected_start, v_case.expected_end;
        end if;
        if v_end - v_start <> interval '604800 seconds' then
            raise exception 'weekly-anchor width regression (%): %',
                v_case.label, v_end - v_start;
        end if;
        if v_case.occurred_at < v_start or v_case.occurred_at >= v_end then
            raise exception 'the returned period does not half-open contain % (%)',
                v_case.occurred_at, v_case.label;
        end if;
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. Property sweep across nineteen consecutive weeks, at three offsets inside
--    each week. Every result must be anchor_start + k * 604800 seconds, exactly
--    604800 seconds wide, and must half-open contain the occurrence.
-- ---------------------------------------------------------------------------
do $$
declare
    v_anchor_start constant timestamptz := '2026-03-05 10:00:00+00';
    v_anchor_end constant timestamptz := '2026-03-12 10:00:00+00';
    v_week bigint;
    v_inner interval;
    v_occurred timestamptz;
    v_start timestamptz;
    v_end timestamptz;
    v_checked integer := 0;
begin
    for v_week in select generate_series(-9,9) loop
        foreach v_inner in array array[
            interval '0 seconds',
            interval '302400 seconds',
            interval '604799.999999 seconds'
        ]
        loop
            v_occurred := v_anchor_start + v_week * interval '604800 seconds' + v_inner;
            select period.period_start, period.period_end into v_start, v_end
              from public.billing_period_for_occurrence(
                  v_occurred,v_anchor_start,v_anchor_end) period;
            if v_start <> v_anchor_start + v_week * interval '604800 seconds' then
                raise exception 'week % offset % resolved to % rather than %',
                    v_week, v_inner, v_start,
                    v_anchor_start + v_week * interval '604800 seconds';
            end if;
            if v_end - v_start <> interval '604800 seconds' then
                raise exception 'week % offset % is % wide', v_week, v_inner,
                    v_end - v_start;
            end if;
            if v_occurred < v_start or v_occurred >= v_end then
                raise exception 'week % offset % fell outside its own period',
                    v_week, v_inner;
            end if;
            v_checked := v_checked + 1;
        end loop;
    end loop;
    if v_checked <> 57 then
        raise exception 'the weekly anchor property sweep checked % cases', v_checked;
    end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. Session-timezone invariance. The same occurrence resolves to byte-equal
--    boundaries under UTC, a DST-observing zone, a southern-hemisphere zone
--    whose DST runs the other way, and a half-hour-offset zone. This is what
--    the pinned TimeZone=UTC in section 1 buys, and it is the reason the worker
--    (Railway, UTC) and psql (a laptop, anything) agree on which week a fee
--    belongs to.
-- ---------------------------------------------------------------------------
do $$
declare
    v_zone text;
    v_case record;
    v_start timestamptz;
    v_end timestamptz;
    v_reference_start timestamptz;
    v_reference_end timestamptz;
begin
    for v_case in
        select *
          from (values
              ('spring forward','2026-03-08 08:00:00+00'::timestamptz,
               '2026-03-05 10:00:00+00'::timestamptz,
               '2026-03-12 10:00:00+00'::timestamptz),
              ('fall back','2026-11-01 07:00:00+00'::timestamptz,
               '2026-10-29 10:00:00+00'::timestamptz,
               '2026-11-05 10:00:00+00'::timestamptz),
              ('twelve weeks before a DST anchor',
               '2026-01-01 00:00:00+00'::timestamptz,
               '2026-03-05 10:00:00+00'::timestamptz,
               '2026-03-12 10:00:00+00'::timestamptz)
          ) as t(label,occurred_at,anchor_start,anchor_end)
    loop
        v_reference_start := null;
        v_reference_end := null;
        foreach v_zone in array array[
            'UTC','America/New_York','Australia/Lord_Howe','Asia/Kolkata',
            'Pacific/Kiritimati'
        ]
        loop
            execute format('set local timezone = %L', v_zone);
            select period.period_start, period.period_end into v_start, v_end
              from public.billing_period_for_occurrence(
                  v_case.occurred_at,v_case.anchor_start,v_case.anchor_end) period;
            if v_reference_start is null then
                v_reference_start := v_start;
                v_reference_end := v_end;
            elsif v_start <> v_reference_start or v_end <> v_reference_end then
                raise exception
                    'session timezone % moved the % period to [%, %) from [%, %)',
                    v_zone, v_case.label, v_start, v_end,
                    v_reference_start, v_reference_end;
            end if;
            if v_end - v_start <> interval '604800 seconds' then
                raise exception 'session timezone % changed the period width to %',
                    v_zone, v_end - v_start;
            end if;
        end loop;
    end loop;
    -- Back to the file's non-UTC baseline, so section 6 runs under a session
    -- timezone that is not the one the function pins.
    set local timezone = 'America/New_York';
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Fail-closed on the live claim path: an account whose Stripe window is not
--    exactly seven days, and an account that never received a subscription
--    webhook at all, both park the ledger row in 'review' with the exact
--    operator-facing reason -- they are never sent, never expired, and never
--    silently attributed to a guessed week (202607200006:386-391).
-- ---------------------------------------------------------------------------
insert into auth.users(id,email) values
    ('bf000000-0000-4000-8000-000000000001','billing-anchor-owner@example.invalid')
on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('bf100000-0000-4000-8000-00000000000a','Billing anchor fixture A',
     'bf000000-0000-4000-8000-000000000001'),
    ('bf100000-0000-4000-8000-00000000000b','Billing anchor fixture B',
     'bf000000-0000-4000-8000-000000000001'),
    ('bf100000-0000-4000-8000-00000000000c','Billing anchor fixture C',
     'bf000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status,
    billing_started_at,current_period_start,current_period_end
) values
    -- a thirty-day window: the classic monthly-price misconfiguration
    ('bf100000-0000-4000-8000-00000000000a','bf000000-0000-4000-8000-000000000001',
     'cus_billing_anchor_a','active',now()-interval '40 days',
     now()-interval '86400 seconds',now()-interval '86400 seconds'+interval '2592000 seconds'),
    -- no window at all
    ('bf100000-0000-4000-8000-00000000000b','bf000000-0000-4000-8000-000000000001',
     'cus_billing_anchor_b','active',now()-interval '40 days',null,null),
    -- exactly seven days: the control, which must be claimable
    ('bf100000-0000-4000-8000-00000000000c','bf000000-0000-4000-8000-000000000001',
     'cus_billing_anchor_c','active',now()-interval '40 days',
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
select 'billing-anchor-key',
       'bf000000-0000-4000-8000-000000000001',
       seed.organization_id,
       seed.request_id,
       true,'priced',4,1,'proxy',now()
  from (values
      ('anchor-thirty-day-window','bf100000-0000-4000-8000-00000000000a'::uuid),
      ('anchor-missing-window','bf100000-0000-4000-8000-00000000000b'::uuid),
      ('anchor-valid-window','bf100000-0000-4000-8000-00000000000c'::uuid)
  ) as seed(request_id,organization_id)
on conflict (key_hash, request_id, authoritative) where request_id<>'' do nothing;

insert into public.billing_ledger(
    usage_log_id,organization_id,user_id,occurred_at,fee_microusd,next_attempt_at
)
select usage.id,
       usage.organization_id,
       'bf000000-0000-4000-8000-000000000001',
       usage.ts,
       310000,
       now() + interval '3650 days'
  from public.usage_log usage
 where usage.request_id in ('anchor-thirty-day-window','anchor-missing-window',
                            'anchor-valid-window')
on conflict (usage_log_id) do nothing;

do $$
declare
    v_case record;
    v_claim record;
    v_row public.billing_ledger%rowtype;
    v_id bigint;
begin
    for v_case in
        select *
          from (values
              ('anchor-thirty-day-window','a thirty-day Stripe window'),
              ('anchor-missing-window','an account with no Stripe window')
          ) as t(request_id,label)
    loop
        select ledger.id into v_id
          from public.billing_ledger ledger
          join public.usage_log usage on usage.id = ledger.usage_log_id
         where usage.request_id = v_case.request_id;
        update public.billing_ledger set next_attempt_at = now() where id = v_id;

        select * into v_claim
          from public.claim_billing_ledger_entries('anchor-owner',600,1,100000000);
        if found then
            raise exception '% produced a claimable row (%)', v_case.label, v_claim.id;
        end if;

        select * into v_row from public.billing_ledger where id = v_id;
        if v_row.status <> 'review' then
            raise exception '% left the row in % instead of review',
                v_case.label, v_row.status;
        end if;
        if v_row.last_error <> 'invalid Stripe weekly billing-period anchor' then
            raise exception '% recorded last_error %', v_case.label, v_row.last_error;
        end if;
        if v_row.lease_owner is not null or v_row.lease_expires_at is not null then
            raise exception '% left a lease on the quarantined row', v_case.label;
        end if;
        if v_row.attempts <> 0 then
            raise exception '% burned an attempt (%)', v_case.label, v_row.attempts;
        end if;
        if v_row.outbound_started_at is not null then
            raise exception '% marked an outbound Stripe send', v_case.label;
        end if;
    end loop;

    -- Control: the same fixture shape with a genuine seven-day window claims
    -- cleanly, so the two failures above are attributable to the anchor and
    -- not to the fixture.
    select ledger.id into v_id
      from public.billing_ledger ledger
      join public.usage_log usage on usage.id = ledger.usage_log_id
     where usage.request_id = 'anchor-valid-window';
    update public.billing_ledger set next_attempt_at = now() where id = v_id;

    select * into v_claim
      from public.claim_billing_ledger_entries('anchor-owner-control',600,1,100000000);
    if not found or v_claim.id <> v_id then
        raise exception 'the seven-day control window did not claim';
    end if;
    if v_claim.period_end - v_claim.period_start <> interval '604800 seconds' then
        raise exception 'the control claim returned a % period',
            v_claim.period_end - v_claim.period_start;
    end if;
    if v_claim.occurred_at < v_claim.period_start
       or v_claim.occurred_at >= v_claim.period_end then
        raise exception 'the control claim placed the fee outside its own week';
    end if;
end;
$$;

rollback;
