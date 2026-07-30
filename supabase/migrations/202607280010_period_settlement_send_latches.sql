-- FIX-4 / PSL-LATCH: latch the send evidence on public.period_settlement_ledger.
--
-- WHAT WAS WRONG (reproduced by execution on PostgreSQL 17.10 before this file)
--
-- 202607280007 froze a settlement row's identity, window, evidence, and amount,
-- and latched supersession one-way. It deliberately did NOT freeze `status` (a
-- row must be able to move draft -> pending -> sending -> reported, and 'void'
-- is admitted from any state), and it did not latch the three columns that
-- record that Stripe was actually contacted:
--     outbound_started_at, reported_at, settled_at.
--
-- 202607280008's cumulative period ceiling counts a row as already committed to
-- Stripe when
--     status in ('sending','reported') OR outbound_started_at is not null
-- (202607280008 around line 561). That predicate is correct, and 202607280008
-- says in its own comments that the `outbound_started_at` branch is what stops a
-- retracted-but-already-paid row from dropping out of the sum. But the branch is
-- only load-bearing if the column cannot be erased, and it could be:
--
--   1. insert a settlement at the full 25% ceiling, promote it to 'reported'
--      with outbound_started_at/reported_at/settled_at stamped. The cumulative
--      ceiling then correctly refuses a second fee for the period.
--   2. update ... set status = 'void', outbound_started_at = null,
--                     reported_at = null, settled_at = null
--      was ACCEPTED by prevent_period_settlement_identity_change.
--   3. the ceiling now sums 0 committed micro-dollars for the period, because
--      the row is neither 'sending'/'reported' nor outbound-marked; 'void' is
--      additionally exempt from period_settlement_ledger_live_period_idx and
--      from period_settlement_ledger_billable_requires_owner, so the live period
--      slot comes fully free.
--   4. a second revision at the full rate passes every halting condition.
--
-- Measured consequence on a real cluster: 45,000,000 microUSD committed for one
-- (organization, period) against a 22,500,000 microUSD ceiling -- a 100%
-- overcharge. It is not deduplicated anywhere downstream: api/billing_recovery.py
-- derives 'brevitas-fee-{id}' / 'brevitas-meter-{id}' from the ledger id alone,
-- so each revision carries a DISTINCT Stripe idempotency key and Stripe sees two
-- unrelated charges.
--
-- Reachability at the time of writing: none. There is still no writer for this
-- table and no PostgREST role holds a privilege on it (202607280007 revokes all,
-- including from service_role). This migration lands BEFORE the Phase 4
-- settlement RPC that 202607280007 promises, which is the whole point: once a
-- writer exists the defect is a double charge, and the guard has to already be
-- there.
--
-- THE FIX
--
-- Recreate public.prevent_period_settlement_identity_change with every existing
-- check preserved verbatim, plus:
--
--   * a one-way latch on each of outbound_started_at, reported_at, settled_at:
--     once set, the value may neither be cleared nor re-stamped. This is exactly
--     the shape 202607280007 already uses for superseded_at/superseded_by, and
--     the same rule 202607170004 relies on for billing_ledger, where the release
--     RPC is documented to be a no-op once outbound_started_at is set: a send
--     that has begun may have been ingested, so its evidence is permanent.
--   * a marker-preserving exit rule: leaving 'sending'/'reported' for any other
--     status is refused unless outbound_started_at survives the update. Voiding
--     a reported row is still legal -- it just cannot take the evidence of the
--     send with it, so the cumulative ceiling keeps counting the money.
--
-- What is deliberately NOT changed:
--   * `status` is still not frozen. Promotion, retraction, and the sweeps must
--     stay expressible; what is forbidden is losing the send evidence on the way.
--   * a row that never started a send (draft/pending, where all three columns are
--     NULL and have no default) is completely unaffected, so a settlement can
--     still be retracted before it is sent and still self-excludes from the
--     cumulative sum.
--   * no column, constraint, index, trigger, or privilege is added or removed,
--     and 202607280007 is not edited: its checksum is frozen and it is (or will
--     be) applied to an environment with no migration ledger.
--
-- CONSEQUENCE FOR THE PHASE 4 WRITER (read before building the claim RPC):
-- a lease-release path for this table must NOT clear outbound_started_at. Model
-- it on public.release_billing_ledger_leases (202607170004), which refuses to
-- release a row whose marker is set precisely because the send may have landed.
-- An ambiguous send is resolved by reconciliation or by the operator sweep to
-- 'review', never by erasing the evidence.
--
-- Safety: additive and idempotent. It replaces one trigger function body,
-- touches no data, and cannot cause a fee to be charged -- it can only cause an
-- UPDATE to abort.

begin;

do $settlement_ledger_precondition$
begin
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280010 requires public.period_settlement_ledger from 202607280007',
            hint    = 'Apply 202607280007 first; this migration only hardens its update guard.';
    end if;
    if to_regprocedure('public.prevent_period_settlement_identity_change()') is null then
        raise exception using
            errcode = '42883',
            message = '202607280010 requires public.prevent_period_settlement_identity_change '
                      'from 202607280007',
            hint    = 'This migration replaces that function body; it does not create the guard '
                      'from nothing, and it must not run against a table with no update trigger.';
    end if;
    if not exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.period_settlement_ledger'::regclass
           and trigger_state.tgname = 'prevent_period_settlement_identity_change'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280010 refuses to harden an update guard that is not attached',
            hint    = 'prevent_period_settlement_identity_change must be a BEFORE UPDATE trigger '
                      'on public.period_settlement_ledger. Reapply 202607280007.';
    end if;
end;
$settlement_ledger_precondition$;

-- Identity, window, evidence, and amount are frozen at insert. The only
-- backward-looking mutations permitted are stamping a row as superseded, once,
-- and moving `status` forward without discarding the evidence of a send.
-- Everything a correction wants to change is expressed as a new revision.
create or replace function public.prevent_period_settlement_identity_change()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    successor public.period_settlement_ledger%rowtype;
begin
    if new.id is distinct from old.id
       or new.organization_id is distinct from old.organization_id
       or new.period_start is distinct from old.period_start
       or new.period_end is distinct from old.period_end
       or new.revision is distinct from old.revision
       or new.revision_of is distinct from old.revision_of
       or new.verified_savings_usd is distinct from old.verified_savings_usd
       or new.warm_spend_usd is distinct from old.warm_spend_usd
       or new.usage_row_count is distinct from old.usage_row_count
       or new.usage_log_watermark_id is distinct from old.usage_log_watermark_id
       or new.fee_microusd is distinct from old.fee_microusd
       or new.computed_at is distinct from old.computed_at
       or new.created_at is distinct from old.created_at then
        raise exception 'period settlement identity, window, evidence, and amount are immutable; corrections append a revision';
    end if;
    -- Supersession is a one-way latch. Neither the stamp nor the successor
    -- pointer may be cleared or re-aimed once written, so a settled period can
    -- never be quietly reopened and re-billed.
    if old.superseded_at is not null
       and new.superseded_at is distinct from old.superseded_at then
        raise exception 'a superseded period settlement may not be un-superseded or re-stamped';
    end if;
    if old.superseded_by is not null
       and new.superseded_by is distinct from old.superseded_by then
        raise exception 'a superseded period settlement may not be re-pointed';
    end if;
    -- Send evidence is a one-way latch too (202607280010). The cumulative period
    -- ceiling in 202607280008 counts a row as already committed to Stripe on
    -- `status in ('sending','reported') or outbound_started_at is not null`, so
    -- clearing any of these three columns rewrites how much this period has
    -- already been charged. A send that has begun may have been ingested; that
    -- fact cannot be un-learned by an UPDATE.
    if old.outbound_started_at is not null
       and new.outbound_started_at is distinct from old.outbound_started_at then
        raise exception 'period settlement outbound send evidence is permanent; outbound_started_at may not be cleared or re-stamped';
    end if;
    if old.reported_at is not null
       and new.reported_at is distinct from old.reported_at then
        raise exception 'period settlement outbound send evidence is permanent; reported_at may not be cleared or re-stamped';
    end if;
    if old.settled_at is not null
       and new.settled_at is distinct from old.settled_at then
        raise exception 'period settlement outbound send evidence is permanent; settled_at may not be cleared or re-stamped';
    end if;
    -- `status` stays deliberately unfrozen -- promotion, retraction to 'void',
    -- and the operator sweeps must all remain expressible. What is refused is
    -- leaving 'sending'/'reported' in a state that no longer carries the
    -- outbound marker: 'void' is exempt from the live-period unique index and
    -- from the billing-owner CHECK, so such an exit would free the period slot
    -- AND drop the already-committed fee out of the cumulative sum.
    if old.status in ('sending', 'reported')
       and new.status is distinct from old.status
       and new.outbound_started_at is null then
        raise exception 'a period settlement that has begun sending may not leave that state without preserving its outbound send evidence';
    end if;
    -- Closing the chain: the successor must be this row's own next revision for
    -- the same company and period. Without this a correction could be pointed
    -- at an unrelated period's row and launder a different amount into the
    -- audit trail.
    if new.superseded_by is not null and old.superseded_by is null then
        select * into successor
          from public.period_settlement_ledger
         where id=new.superseded_by;
        if not found
           or successor.revision_of is distinct from old.id
           or successor.organization_id is distinct from old.organization_id
           or successor.period_start is distinct from old.period_start
           or successor.revision<=old.revision then
            raise exception 'a period settlement may only be superseded by its own later revision for the same company and period';
        end if;
    end if;
    if old.billing_owner_id is not null
       and new.billing_owner_id is distinct from old.billing_owner_id then
        raise exception 'period settlement billing-owner attribution is immutable once recorded';
    end if;
    return new;
end;
$$;

-- CREATE OR REPLACE preserves the ACL, but restate it so a hand-recreated
-- function can never be left reachable from a browser session.
revoke all on function public.prevent_period_settlement_identity_change()
    from public, anon, authenticated;

comment on function public.prevent_period_settlement_identity_change() is
    'BEFORE UPDATE guard on public.period_settlement_ledger. Freezes identity, '
    'window, evidence, amount, and billing-owner attribution; latches '
    'supersession one-way; and (202607280010) latches the send evidence '
    'outbound_started_at/reported_at/settled_at one-way, refusing any exit from '
    'sending/reported that does not preserve the outbound marker. The cumulative '
    'period ceiling in 202607280008 counts committed money on that marker, so '
    'erasing it would re-open a reported period for a second full-rate charge.';
comment on column public.period_settlement_ledger.outbound_started_at is
    'Evidence that a Stripe send began for this settlement, and a one-way latch '
    '(202607280010): once set it may never be cleared or re-stamped, and the row '
    'may not leave sending/reported without it. 202607280008''s cumulative period '
    'ceiling counts this row as already committed on this column, so erasing it '
    'would reopen the period for a second charge. A Phase 4 lease-release path '
    'must refuse a row whose marker is set, exactly as '
    'public.release_billing_ledger_leases does for billing_ledger.';
comment on column public.period_settlement_ledger.reported_at is
    'When Stripe accepted the meter event for this settlement. One-way latch '
    '(202607280010).';
comment on column public.period_settlement_ledger.settled_at is
    'When this settlement was recorded as settled. One-way latch (202607280010).';

-- Self-verifying contract assertions.
--
-- The behavioural half needs a settlement row, and public.period_settlement_ledger
-- carries an unconditional BEFORE DELETE guard: a fixture written here could not
-- be cleaned up with a DELETE. It is therefore built inside an inner block that
-- is deliberately unwound by raising a private SQLSTATE, so the probe observes
-- real trigger behaviour and leaves no row behind in a seven-year financial
-- record. (The identity sequence advances; nothing reads its current value --
-- 202607280007 asserts the sequence's declared START, not its position.)
do $send_latch_contract$
declare
    v_probe_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_probe_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_probe_org uuid;
    v_probe_owner uuid;
    v_reported_id bigint;
    v_sending_id bigint;
    v_draft_id bigint;
    v_committed bigint;
    v_raised boolean;
begin
    -- Structural: the guard must still be the attached BEFORE UPDATE trigger and
    -- it must name all three latched columns, so that a future hand-edit that
    -- drops one of them is caught here rather than in production.
    if not exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid = trigger_state.tgfoid
         where trigger_state.tgrelid = 'public.period_settlement_ledger'::regclass
           and trigger_state.tgname = 'prevent_period_settlement_identity_change'
           and not trigger_state.tgisinternal
           and trigger_function.proname = 'prevent_period_settlement_identity_change'
           and trigger_function.prosrc like '%old.outbound_started_at is not null%'
           and trigger_function.prosrc like '%old.reported_at is not null%'
           and trigger_function.prosrc like '%old.settled_at is not null%'
           and trigger_function.prosrc like '%new.outbound_started_at is null%'
    ) then
        raise exception '202607280010 did not install the send-evidence latches on the '
                        'period settlement update guard';
    end if;

    -- 202607280007 asserts exactly two non-internal triggers on the table.
    -- Nothing here may add a third, and nothing may have removed one.
    if (
        select count(*)
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.period_settlement_ledger'::regclass
           and not trigger_state.tgisinternal
    ) <> 2 then
        raise exception '202607280010 changed the trigger set on period settlements';
    end if;

    -- The cumulative ceiling this latch protects must still count on the marker
    -- rather than on status alone; without that branch the latch guards nothing.
    if not exists (
        select 1
          from pg_proc guard_function
          join pg_namespace guard_schema
            on guard_schema.oid = guard_function.pronamespace
         where guard_schema.nspname = 'public'
           and guard_function.proname = 'assert_billing_period_halting_conditions'
           and guard_function.prosrc like '%or settlement.outbound_started_at is not null%'
    ) then
        raise exception '202607280010 latches send evidence the cumulative ceiling no longer '
                        'reads; 202607280008 must count committed fees on the outbound marker';
    end if;

    begin
        v_probe_owner := gen_random_uuid();
        insert into auth.users (id, email)
        values (v_probe_owner, '202607280010-send-latch-probe@example.invalid');
        insert into public.organizations (name)
        values ('202607280010 send latch contract probe')
        returning id into v_probe_org;

        -- verified 100, warm 10 -> the CHECK cap and 202607280008's ceiling are
        -- both exactly 22,500,000 microUSD.
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd,
            billing_owner_id, status,
            outbound_started_at, reported_at, settled_at
        ) values (
            v_probe_org, v_probe_start, v_probe_end,
            100, 10, 22500000, v_probe_owner, 'reported',
            now(), now(), now()
        ) returning id into v_reported_id;

        -- 1. outbound_started_at may not be cleared.
        v_raised := false;
        begin
            update public.period_settlement_ledger
               set outbound_started_at = null
             where id = v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed outbound_started_at to be cleared';
        end if;

        -- ...nor re-stamped to a different instant.
        v_raised := false;
        begin
            update public.period_settlement_ledger
               set outbound_started_at = v_probe_start
             where id = v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed outbound_started_at to be re-stamped';
        end if;

        -- 2. reported_at may not be cleared.
        v_raised := false;
        begin
            update public.period_settlement_ledger
               set reported_at = null
             where id = v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed reported_at to be cleared';
        end if;

        -- 3. settled_at may not be cleared.
        v_raised := false;
        begin
            update public.period_settlement_ledger
               set settled_at = null
             where id = v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed settled_at to be cleared';
        end if;

        -- 4. the whole reproduction UPDATE, verbatim.
        v_raised := false;
        begin
            update public.period_settlement_ledger
               set status = 'void',
                   outbound_started_at = null,
                   reported_at = null,
                   settled_at = null
             where id = v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed a reported settlement to be voided with its '
                            'send evidence erased; the period could be re-billed at full rate';
        end if;

        -- The evidence survived every attempt, so the cumulative ceiling still
        -- sees the money as committed.
        select coalesce(sum(settlement.fee_microusd), 0)
          into v_committed
          from public.period_settlement_ledger settlement
         where settlement.organization_id = v_probe_org
           and settlement.period_start = v_probe_start
           and settlement.period_end = v_probe_end
           and (settlement.status in ('sending', 'reported')
                or settlement.outbound_started_at is not null);
        if v_committed <> 22500000 then
            raise exception '202607280010 lost the committed period fee after the void attempt: %',
                v_committed;
        end if;

        -- 5. leaving sending/reported without the marker is refused even when
        --    the row never had one to begin with -- otherwise a writer could
        --    park a row in 'sending' unmarked and retract it for free.
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd,
            billing_owner_id, status
        ) values (
            v_probe_org, v_probe_start + interval '7 days',
            v_probe_end + interval '7 days',
            100, 10, 22500000, v_probe_owner, 'sending'
        ) returning id into v_sending_id;

        v_raised := false;
        begin
            update public.period_settlement_ledger
               set status = 'void'
             where id = v_sending_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception '202607280010 allowed an unmarked sending settlement to be voided';
        end if;

        -- 6. ...and the legitimate transitions still work: 'sending' -> 'reported'
        --    while stamping the marker, and 'reported' -> 'void' with the
        --    evidence preserved.
        update public.period_settlement_ledger
           set status = 'reported',
               outbound_started_at = now(),
               reported_at = now()
         where id = v_sending_id;
        update public.period_settlement_ledger
           set status = 'void'
         where id = v_sending_id;
        if not exists (
            select 1 from public.period_settlement_ledger
             where id = v_sending_id
               and status = 'void'
               and outbound_started_at is not null
        ) then
            raise exception '202607280010 broke the legitimate retraction of a reported '
                            'settlement that keeps its send evidence';
        end if;

        -- 7. a draft that never started a send is untouched by any of this: it
        --    can still be retracted freely, and it stays out of the committed
        --    sum, so self-exclusion is preserved for the candidate revision.
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd
        ) values (
            v_probe_org, v_probe_start + interval '14 days',
            v_probe_end + interval '14 days',
            100, 10, 22500000
        ) returning id into v_draft_id;
        update public.period_settlement_ledger
           set status = 'void', last_error = 'retracted before any send'
         where id = v_draft_id;
        if not exists (
            select 1 from public.period_settlement_ledger
             where id = v_draft_id
               and status = 'void'
               and outbound_started_at is null
        ) then
            raise exception '202607280010 broke retraction of a never-sent draft settlement';
        end if;

        raise exception using
            errcode = 'ZZ010',
            message = '202607280010 send-latch probe complete; unwinding its fixture';
    exception
        when sqlstate 'ZZ010' then null;
    end;

    if exists (
        select 1 from public.period_settlement_ledger
         where organization_id = v_probe_org
    ) then
        raise exception '202607280010 left a settlement probe row behind';
    end if;
end;
$send_latch_contract$;

commit;

-- ROLLBACK PROCEDURE (restores the 202607280007 guard body, reopening PSL-LATCH
-- -- do this only to unblock an incident, and re-apply this file immediately):
-- 1. Re-run the CREATE OR REPLACE FUNCTION
--    public.prevent_period_settlement_identity_change() body from
--    supabase/migrations/202607280007_period_settlement_ledger.sql (lines
--    311-368 at the commit that introduced it). The trigger binding is by name
--    and needs no change.
-- 2. Re-run: REVOKE ALL ON FUNCTION
--    public.prevent_period_settlement_identity_change()
--    FROM public, anon, authenticated;
-- 3. Nothing else in this migration writes data, so there is nothing to undo.
