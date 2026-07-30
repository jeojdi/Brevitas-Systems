\set ON_ERROR_STOP on

-- Behavioural contract for public.period_settlement_ledger
-- (202607280007_period_settlement_ledger.sql) as hardened by
-- 202607280010_period_settlement_send_latches.sql.
--
-- Why this file exists: 202607280007/0008/0009 are ~1,884 lines of SQL that
-- define what a customer is charged, and before this file they had ZERO
-- executable coverage at any tier. The migrations self-verify their arithmetic
-- and their catalog state, but they deliberately cannot exercise row behaviour:
-- the table carries an unconditional BEFORE DELETE guard, so a fixture written
-- by a migration could never be removed from a seven-year financial record.
-- Every fixture below is discarded by the trailing rollback, so this file is
-- re-runnable at any point in the replay and leaves no settlement behind.
--
-- The PSL-LATCH block (FIX-4) is the regression test for a reproduced 100%
-- overcharge: before 202607280010, a settlement reported at the full 25% ceiling
-- could be voided with its send evidence erased, which zeroed 202607280008's
-- cumulative period ceiling and let a second full-rate revision through --
-- 45,000,000 microUSD committed against a 22,500,000 microUSD ceiling, with
-- distinct Stripe idempotency keys so nothing downstream deduplicated it.

begin;

do $$
declare
    v_org uuid := 'f5e70001-0000-4000-8000-000000000001';
    v_other_org uuid := 'f5e70001-0000-4000-8000-000000000002';
    v_owner uuid := 'f5e70001-0000-4000-8000-000000000003';
    v_other_owner uuid := 'f5e70001-0000-4000-8000-000000000004';
    -- Exactly seven days, matching period_settlement_ledger_weekly_window.
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_next_start constant timestamptz := '2026-07-22 10:00:00+00';
    v_next_end constant timestamptz := '2026-07-29 10:00:00+00';
    v_reported_id bigint;
    v_successor_id bigint;
    v_draft_id bigint;
    v_sending_id bigint;
    v_zero_floor_id bigint;
    v_sequence_start bigint;
    v_committed bigint;
    v_net numeric;
    v_state text;
    v_constraint text;
    v_message text;
    v_role text;
    v_privilege text;
    v_raised boolean;
begin
    ------------------------------------------------------------------
    -- 0. Structure: the table, both guards, and the id space.
    ------------------------------------------------------------------
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception 'period settlement ledger is missing (202607280007 not applied)';
    end if;
    if to_regprocedure('public.period_settlement_fee_microusd(numeric,numeric)') is null then
        raise exception 'the shared settlement fee formula is missing';
    end if;
    if (
        select count(*)
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.period_settlement_ledger'::regclass
           and not trigger_state.tgisinternal
    ) <> 2 then
        raise exception 'period settlement must carry exactly its two immutability triggers';
    end if;
    -- No writer may have been attached: settlement stays manual until Phase 4.
    if exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid = trigger_state.tgfoid
         where not trigger_state.tgisinternal
           and trigger_state.tgrelid <> 'public.period_settlement_ledger'::regclass
           and coalesce(trigger_function.prosrc, '') like '%period_settlement_ledger%'
    ) then
        raise exception 'an external trigger writes period settlements; settlement must stay manual';
    end if;

    -- Stripe identifiers are derived from the ledger id alone
    -- ('brevitas-fee-{id}', api/billing_recovery.py), so the two ledgers' id
    -- spaces must never overlap.
    select sequence_state.seqstart into v_sequence_start
      from pg_sequence sequence_state
     where sequence_state.seqrelid
           = pg_get_serial_sequence('public.period_settlement_ledger', 'id')::regclass;
    if coalesce(v_sequence_start, 0) < 1000000000 then
        raise exception 'settlement ids must start beyond the billing_ledger id space: %',
            v_sequence_start;
    end if;
    if exists (select 1 from public.billing_ledger where id >= v_sequence_start) then
        raise exception 'settlement and per-row ledger ids would collide in Stripe identifiers';
    end if;

    ------------------------------------------------------------------
    -- 1. Privilege posture: no PostgREST role may reach this table at all,
    --    including service_role. This is what makes PSL-LATCH unreachable
    --    today, and it is the property the Phase 4 RPC must preserve.
    ------------------------------------------------------------------
    foreach v_role in array array['anon', 'authenticated', 'service_role'] loop
        foreach v_privilege in array array[
            'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'
        ] loop
            if has_table_privilege(v_role, 'public.period_settlement_ledger', v_privilege) then
                raise exception 'a PostgREST role reaches period settlements before Phase 4: % %',
                    v_role, v_privilege;
            end if;
        end loop;
    end loop;
    if exists (
        select 1
          from aclexplode(coalesce(
              (select relation.relacl from pg_class relation
                where relation.oid = 'public.period_settlement_ledger'::regclass),
              acldefault('r', (select relation.relowner from pg_class relation
                                where relation.oid = 'public.period_settlement_ledger'::regclass))
          )) privilege
         where privilege.grantee = 0
    ) then
        raise exception 'PUBLIC has a direct period settlement privilege';
    end if;
    if not exists (
        select 1 from pg_class relation
         where relation.oid = 'public.period_settlement_ledger'::regclass
           and relation.relrowsecurity
    ) then
        raise exception 'period settlement must have row level security enabled';
    end if;
    if exists (
        select 1 from pg_policy policy_state
         where policy_state.polrelid = 'public.period_settlement_ledger'::regclass
    ) then
        raise exception 'period settlement must expose no row level policy';
    end if;

    ------------------------------------------------------------------
    -- 2. Fixtures.
    ------------------------------------------------------------------
    insert into auth.users (id, email) values
        (v_owner, 'period-settlement-assertion@example.invalid'),
        (v_other_owner, 'period-settlement-assertion-other@example.invalid');
    insert into public.organizations (id, name) values
        (v_org, 'Period settlement assertion fixture'),
        (v_other_org, 'Period settlement assertion fixture (second org)');

    ------------------------------------------------------------------
    -- 3. The 25% CHECK cap is a boundary, enforced by the table and not by
    --    application code. verified=100, warm=10 -> cap exactly 22,500,000.
    ------------------------------------------------------------------
    if public.period_settlement_fee_microusd(90, 100) <> 22500000 then
        raise exception 'the shared fee formula disagrees with the 25%% cap boundary';
    end if;

    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd,
            billing_owner_id, status
        ) values (
            v_org, v_start, v_end, 100, 10, 22500001, v_owner, 'draft'
        );
    exception
        when check_violation then
            v_raised := true;
            get stacked diagnostics
                v_state = returned_sqlstate,
                v_constraint = constraint_name;
            if v_state <> '23514'
               or v_constraint <> 'period_settlement_ledger_fee_capped_non_negative' then
                raise exception 'one microdollar over the cap raised % on %', v_state, v_constraint;
            end if;
    end;
    if not v_raised then
        raise exception 'period settlement accepted a fee one microdollar above the 25%% cap';
    end if;

    -- ...and the boundary itself is accepted.
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status
    ) values (
        v_org, v_start, v_end, 100, 10, 22500000, v_owner, 'draft'
    ) returning id into v_draft_id;
    if v_draft_id < 1000000000 then
        raise exception 'a settlement row was minted inside the billing_ledger id space: %',
            v_draft_id;
    end if;

    -- A negative fee is unrepresentable at all.
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd
        ) values (v_other_org, v_start, v_end, 100, 10, -1);
    exception
        when check_violation then v_raised := true;
    end;
    if not v_raised then
        raise exception 'period settlement accepted a negative fee';
    end if;

    ------------------------------------------------------------------
    -- 4. The zero floor is generated, not advisory: a losing week nets 0, not
    --    -5, and can carry no fee at all.
    ------------------------------------------------------------------
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd
    ) values (
        v_other_org, v_start, v_end, 5, 10, 0
    ) returning id, net_savings_usd into v_zero_floor_id, v_net;
    if v_net <> 0 then
        raise exception 'a losing period did not net to zero: %', v_net;
    end if;
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd
        ) values (v_other_org, v_next_start, v_next_end, 5, 10, 1);
    exception
        when check_violation then v_raised := true;
    end;
    if not v_raised then
        raise exception 'a losing period accepted a positive fee';
    end if;
    -- The generated column cannot be supplied by a caller either.
    v_raised := false;
    begin
        execute 'update public.period_settlement_ledger set net_savings_usd = 999 where id = $1'
            using v_zero_floor_id;
    exception
        when others then v_raised := true;
    end;
    if not v_raised then
        raise exception 'the netting zero floor can be bypassed by a caller';
    end if;

    ------------------------------------------------------------------
    -- 5. The window is exactly the Stripe seven-day period. Six days is not a
    --    billing week, and a settlement over the wrong window would charge the
    --    wrong evidence.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd
        ) values (
            v_other_org, '2026-08-01 10:00:00+00', '2026-08-07 10:00:00+00', 100, 10, 0
        );
    exception
        when check_violation then
            v_raised := true;
            get stacked diagnostics v_constraint = constraint_name;
            if v_constraint <> 'period_settlement_ledger_weekly_window' then
                raise exception 'a six-day window was rejected by the wrong constraint: %',
                    v_constraint;
            end if;
    end;
    if not v_raised then
        raise exception 'period settlement accepted a six-day billing window';
    end if;
    -- Eight days is equally wrong; the check is equality, not a minimum.
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd
        ) values (
            v_other_org, '2026-08-01 10:00:00+00', '2026-08-09 10:00:00+00', 100, 10, 0
        );
    exception
        when check_violation then v_raised := true;
    end;
    if not v_raised then
        raise exception 'period settlement accepted an eight-day billing window';
    end if;

    ------------------------------------------------------------------
    -- 6. Nothing leaves draft/void without a billing owner to attribute the
    --    charge to.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd, status
        ) values (v_other_org, v_next_start, v_next_end, 100, 10, 22500000, 'pending');
    exception
        when check_violation then
            v_raised := true;
            get stacked diagnostics v_constraint = constraint_name;
            if v_constraint <> 'period_settlement_ledger_billable_requires_owner' then
                raise exception 'an unattributed billable settlement was rejected by %',
                    v_constraint;
            end if;
    end;
    if not v_raised then
        raise exception 'period settlement made a period billable with no billing owner';
    end if;

    ------------------------------------------------------------------
    -- 7. Exactly one LIVE settlement per (organization, period): a successor
    --    cannot be appended until the predecessor is stamped superseded. This is
    --    the serialization the cumulative ceiling's TOCTOU note relies on.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end, revision, revision_of,
            verified_savings_usd, warm_spend_usd, fee_microusd,
            billing_owner_id, status
        ) values (
            v_org, v_start, v_end, 2, v_draft_id, 100, 10, 22500000, v_owner, 'draft'
        );
    exception
        when unique_violation then
            v_raised := true;
            get stacked diagnostics
                v_state = returned_sqlstate,
                v_constraint = constraint_name;
            if v_state <> '23505'
               or v_constraint <> 'period_settlement_ledger_live_period_idx' then
                raise exception 'a second live settlement was rejected by % / %',
                    v_state, v_constraint;
            end if;
    end;
    if not v_raised then
        raise exception 'period settlement allowed two live rows for one organization and period';
    end if;

    ------------------------------------------------------------------
    -- 8. Delete is refused unconditionally: these are seven-year financial
    --    records. The guard is a statement trigger, so it fires even for a
    --    predicate that matches nothing.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        delete from public.period_settlement_ledger where id = v_draft_id;
    exception
        when sqlstate 'P0001' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('seven years' in v_message) = 0 then
                raise exception 'the settlement delete guard raised an unexpected message: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'a period settlement row was deletable';
    end if;
    v_raised := false;
    begin
        delete from public.period_settlement_ledger where id = -1;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'the settlement delete guard is per-row, not per-statement';
    end if;

    ------------------------------------------------------------------
    -- 9. Identity, window, evidence, and amount are frozen. A correction is a
    --    new revision, never an UPDATE.
    ------------------------------------------------------------------
    foreach v_message in array array[
        'set fee_microusd = 1',
        'set verified_savings_usd = 1000',
        'set warm_spend_usd = 0',
        'set organization_id = ' || quote_literal(v_other_org) || '::uuid',
        'set period_start = period_start + interval ''1 hour'', '
            || 'period_end = period_end + interval ''1 hour''',
        'set revision = 2',
        'set usage_row_count = 7',
        'set usage_log_watermark_id = 7',
        'set computed_at = now() + interval ''1 day''',
        'set created_at = now() + interval ''1 day'''
    ] loop
        v_raised := false;
        begin
            execute format(
                'update public.period_settlement_ledger %s where id = $1', v_message
            ) using v_draft_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception 'period settlement permitted a frozen-field UPDATE: %', v_message;
        end if;
    end loop;

    -- Billing-owner attribution is immutable once recorded.
    v_raised := false;
    begin
        update public.period_settlement_ledger
           set billing_owner_id = v_other_owner
         where id = v_draft_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'period settlement permitted billing-owner reattribution';
    end if;

    ------------------------------------------------------------------
    -- 10. The correction flow works in the documented order, and supersession
    --     is a one-way latch that may only point at this row's own successor.
    ------------------------------------------------------------------
    update public.period_settlement_ledger
       set superseded_at = now()
     where id = v_draft_id;
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end, revision, revision_of,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status
    ) values (
        v_org, v_start, v_end, 2, v_draft_id, 100, 10, 22000000, v_owner, 'draft'
    ) returning id into v_successor_id;

    -- A predecessor may not be pointed at an unrelated row.
    v_raised := false;
    begin
        update public.period_settlement_ledger
           set superseded_by = v_zero_floor_id
         where id = v_draft_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'a settlement was supersedable by an unrelated period''s row';
    end if;

    update public.period_settlement_ledger
       set superseded_by = v_successor_id
     where id = v_draft_id;

    -- ...and neither the stamp nor the pointer may then be cleared.
    v_raised := false;
    begin
        update public.period_settlement_ledger
           set superseded_at = null, superseded_by = null
         where id = v_draft_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'a superseded settlement was un-supersedable';
    end if;

    ------------------------------------------------------------------
    -- 11. FIX-4 / PSL-LATCH regression (202607280010).
    --
    -- The reproduction: a settlement reported at the full ceiling, then voided
    -- with its send evidence erased, zeroed 202607280008's cumulative ceiling
    -- and let a second full-rate revision through. All four marker-erasing
    -- UPDATEs must now raise, and the cumulative ceiling must survive the
    -- attempt.
    ------------------------------------------------------------------
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status,
        outbound_started_at, reported_at, settled_at
    ) values (
        v_other_org, v_next_start, v_next_end,
        100, 10, 22500000, v_other_owner, 'reported',
        now(), now(), now()
    ) returning id into v_reported_id;

    foreach v_message in array array[
        'set outbound_started_at = null',
        'set reported_at = null',
        'set settled_at = null',
        'set status = ''void'', outbound_started_at = null, reported_at = null, '
            || 'settled_at = null'
    ] loop
        v_raised := false;
        begin
            execute format(
                'update public.period_settlement_ledger %s where id = $1', v_message
            ) using v_reported_id;
        exception
            when sqlstate 'P0001' then v_raised := true;
        end;
        if not v_raised then
            raise exception 'PSL-LATCH: send evidence was erasable (%); a reported period '
                            'could be re-billed at the full rate', v_message;
        end if;
    end loop;

    -- Re-stamping is refused too: moving the marker forward would move the
    -- 23-hour ambiguity sweep's clock, and moving it back is simply a lie.
    v_raised := false;
    begin
        update public.period_settlement_ledger
           set outbound_started_at = v_next_start
         where id = v_reported_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'PSL-LATCH: outbound_started_at was re-stampable';
    end if;

    -- The evidence survived, so the money is still counted as committed by the
    -- exact predicate 202607280008 uses.
    select coalesce(sum(settlement.fee_microusd), 0)
      into v_committed
      from public.period_settlement_ledger settlement
     where settlement.organization_id = v_other_org
       and settlement.period_start = v_next_start
       and settlement.period_end = v_next_end
       and (settlement.status in ('sending', 'reported')
            or settlement.outbound_started_at is not null);
    if v_committed <> 22500000 then
        raise exception 'PSL-LATCH: the committed period fee did not survive the void attempt: %',
            v_committed;
    end if;

    -- Leaving 'sending'/'reported' is refused for a row that carries no marker
    -- either, so a writer cannot park an unmarked row in 'sending' and retract
    -- it for free.
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status
    ) values (
        v_other_org, '2026-07-29 10:00:00+00', '2026-08-05 10:00:00+00',
        100, 10, 22500000, v_other_owner, 'sending'
    ) returning id into v_sending_id;
    v_raised := false;
    begin
        update public.period_settlement_ledger set status = 'void' where id = v_sending_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'PSL-LATCH: an unmarked sending settlement was voidable';
    end if;

    -- The legitimate lifecycle is untouched: sending -> reported stamping the
    -- marker, then reported -> void KEEPING it (a retraction is allowed, it just
    -- cannot take the evidence of the send with it).
    update public.period_settlement_ledger
       set status = 'reported', outbound_started_at = now(), reported_at = now()
     where id = v_sending_id;
    update public.period_settlement_ledger
       set status = 'void', last_error = 'retracted after a completed send'
     where id = v_sending_id;
    if not exists (
        select 1 from public.period_settlement_ledger
         where id = v_sending_id and status = 'void' and outbound_started_at is not null
    ) then
        raise exception 'PSL-LATCH: a reported settlement could not be retracted at all';
    end if;

    -- A never-sent draft is completely unaffected, which is what preserves the
    -- candidate revision's self-exclusion from the cumulative sum.
    update public.period_settlement_ledger
       set status = 'void', last_error = 'retracted before any send'
     where id = v_zero_floor_id;
    if not exists (
        select 1 from public.period_settlement_ledger
         where id = v_zero_floor_id and status = 'void' and outbound_started_at is null
    ) then
        raise exception 'PSL-LATCH: a never-sent draft could not be retracted';
    end if;

    -- Promotion out of draft still works and still stamps its own markers.
    update public.period_settlement_ledger
       set status = 'pending', next_attempt_at = now()
     where id = v_successor_id;
    update public.period_settlement_ledger
       set status = 'sending', outbound_started_at = now(),
           attempts = attempts + 1, lease_owner = 'assertion-owner',
           lease_expires_at = now() + interval '5 minutes'
     where id = v_successor_id;
    if not exists (
        select 1 from public.period_settlement_ledger
         where id = v_successor_id and status = 'sending' and outbound_started_at is not null
    ) then
        raise exception 'PSL-LATCH: the normal draft -> pending -> sending promotion broke';
    end if;
end;
$$;

rollback;
