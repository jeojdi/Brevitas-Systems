\set ON_ERROR_STOP on

-- Behavioural contract for the Phase 4 halting conditions:
--   202607280008_billing_halting_conditions.sql   (the three circuit breakers)
--   202607280009_billing_arrangement_attestation.sql (the attestation wrapper)
--   202607280012_settlement_evidence_warm_days.sql   (FIX-11, warm day count)
--
-- Both migrations self-verify against an org with NO usage at all, and both say
-- so in their own comments: the cumulative-ceiling and warm-deduction probes are
-- substring checks over pg_proc.prosrc, with a TODO(phase-4) to replace them with
-- behavioural tests, because a migration cannot leave an organizations /
-- auth.users / period_settlement_ledger fixture behind in a seven-year financial
-- record. This file is that behavioural test: every condition below is exercised
-- against NON-EMPTY evidence, and the trailing rollback discards every fixture so
-- the file is re-runnable at any point in the replay.
--
-- Evidence arithmetic used throughout, so the expected numbers are checkable by
-- hand: verified savings 100.00 USD, recomputed warm spend 10.00 USD, so
-- net-after-warm is 90.00 USD and the ceiling is 90 * 0.25 * 1e6 =
-- 22,500,000 microUSD -- the same boundary period_settlement_ledger's fee CHECK
-- enforces from the row's own columns.

begin;

do $$
declare
    -- One org per condition, so a condition can never pass because a sibling
    -- fixture happened to move the aggregate.
    v_ceiling_org uuid := 'ea170001-0000-4000-8000-000000000001';
    v_zero_spend_org uuid := 'ea170001-0000-4000-8000-000000000002';
    v_concentration_org uuid := 'ea170001-0000-4000-8000-000000000003';
    v_boundary_org uuid := 'ea170001-0000-4000-8000-000000000004';
    v_cumulative_org uuid := 'ea170001-0000-4000-8000-000000000005';
    v_warm_days_org uuid := 'ea170001-0000-4000-8000-000000000006';
    v_owner uuid := 'ea170001-0000-4000-8000-00000000000a';
    v_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_evidence record;
    v_result jsonb;
    v_message text;
    v_state text;
    v_raised boolean;
    v_reported_id bigint;

    -- Loop variable for the table-driven cases below.
    v_case text := '';
begin
    ------------------------------------------------------------------
    -- 0. Structure and privilege posture.
    --
    -- 202607280009 makes the wrapper the ONLY reachable door: EXECUTE on the
    -- inner arithmetic guard is revoked from service_role precisely so that no
    -- runtime code can skip the attestation. If that ever regressed, every
    -- condition below would still pass while the system silently became
    -- billable for unattested orgs.
    ------------------------------------------------------------------
    if to_regprocedure(
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)'
       ) is null
       or to_regprocedure(
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)'
       ) is null
       -- 202607280031 widened this by a trailing p_usage_log_watermark_id
       -- bigint DEFAULT NULL. to_regprocedure resolves by EXACT argument types
       -- and is not default-aware, so the three-argument spelling no longer
       -- names anything -- while the frozen 202607280008 guard's own
       -- three-argument CALL still resolves, which is what the default is for.
       or to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)'
       ) is null
       or to_regprocedure('public.organization_billing_arrangement_state(uuid)') is null then
        raise exception 'a halting-condition function is missing (202607280008/0009 not applied)';
    end if;

    if has_function_privilege('service_role',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception 'the attestation-free inner guard is reachable at runtime; settlement '
                        'could skip halting_condition=unattested_billing_arrangement';
    end if;
    if not has_function_privilege('service_role',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception 'the settlement guard is not callable by service_role';
    end if;
    if has_function_privilege('anon',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('anon',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception 'billing evidence is reachable from a browser role';
    end if;
    -- Attestation is an out-of-band human act: no runtime role may write one.
    if has_table_privilege('service_role', 'public.organization_billing_arrangement', 'INSERT')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'UPDATE')
       or has_table_privilege('service_role', 'public.organization_billing_arrangement', 'DELETE') then
        raise exception 'a runtime role can attest an organization into billability';
    end if;
    -- The thresholds must still be the contracted ones.
    if not exists (
        select 1 from public.billing_halting_conditions
         where singleton
           and max_fee_share_of_verified_savings = 0.25
           and max_zero_spend_savings_share = 0.50
    ) then
        raise exception 'the halting-condition thresholds are not the contracted 0.25 / 0.50';
    end if;

    ------------------------------------------------------------------
    -- 1. Fixtures. Every org gets NON-EMPTY evidence.
    ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_owner, 'halting-conditions-assertion@example.invalid');
    insert into public.organizations (id, name) values
        (v_ceiling_org, 'Halting: relative ceiling'),
        (v_zero_spend_org, 'Halting: zero spend'),
        (v_concentration_org, 'Halting: zero-spend concentration'),
        (v_boundary_org, 'Halting: concentration boundary'),
        (v_cumulative_org, 'Halting: cumulative ceiling'),
        (v_warm_days_org, 'Halting: warm day count');

    -- relative-ceiling org: verified 100, list-price spend 40, warm 10.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'halting-ceiling', 'halting-owner', v_ceiling_org, v_start + interval '1 hour',
        'halting-ceiling-1', 'halting', 100, 40, 140, true, 'priced', 'proxy'
    );
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_ceiling_org, 'anthropic', '2026-07-16', 10);

    -- Rows that must NOT count: non-authoritative, unpriced, and out-of-window.
    -- Any of these leaking in would raise a ceiling with unbillable evidence.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('halting-ceiling', 'halting-owner', v_ceiling_org, v_start + interval '2 hours',
         'halting-ceiling-nonauth', 'halting', 1000, 40, 1040, false, 'priced', 'proxy'),
        ('halting-ceiling', 'halting-owner', v_ceiling_org, v_start + interval '3 hours',
         'halting-ceiling-unpriced', 'halting', 1000, 40, 1040, true, 'unpriced', 'proxy'),
        ('halting-ceiling', 'halting-owner', v_ceiling_org, v_start - interval '1 second',
         'halting-ceiling-before', 'halting', 1000, 40, 1040, true, 'priced', 'proxy'),
        ('halting-ceiling', 'halting-owner', v_ceiling_org, v_end,
         'halting-ceiling-at-end', 'halting', 1000, 40, 1040, true, 'priced', 'proxy');

    -- zero-spend org: real verified savings, no recorded provider spend at all.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'halting-zero-spend', 'halting-owner', v_zero_spend_org, v_start + interval '1 hour',
        'halting-zero-spend-1', 'halting', 100, 0, 100, true, 'priced', 'proxy'
    );

    -- concentration org: 100 USD saved with no spend behind it, 10 USD with.
    -- share = 100 / 110 = 0.90909 > 0.50.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('halting-concentration', 'halting-owner', v_concentration_org,
         v_start + interval '1 hour', 'halting-concentration-free', 'halting',
         100, 0, 100, true, 'priced', 'proxy'),
        ('halting-concentration', 'halting-owner', v_concentration_org,
         v_start + interval '2 hours', 'halting-concentration-paid', 'halting',
         10, 5, 15, true, 'priced', 'proxy');

    -- boundary org: share = 50 / 100 = exactly 0.50, which is NOT above the limit.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('halting-boundary', 'halting-owner', v_boundary_org,
         v_start + interval '1 hour', 'halting-boundary-free', 'halting',
         50, 0, 50, true, 'priced', 'proxy'),
        ('halting-boundary', 'halting-owner', v_boundary_org,
         v_start + interval '2 hours', 'halting-boundary-paid', 'halting',
         50, 20, 70, true, 'priced', 'proxy');

    -- cumulative org: same evidence as the ceiling org, plus a settlement that
    -- already reported the whole ceiling to Stripe.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'halting-cumulative', 'halting-owner', v_cumulative_org, v_start + interval '1 hour',
        'halting-cumulative-1', 'halting', 100, 40, 140, true, 'priced', 'proxy'
    );
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_cumulative_org, 'anthropic', '2026-07-16', 10);

    ------------------------------------------------------------------
    -- 2. The evidence function reads what it claims to read.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_ceiling_org, v_start, v_end);
    if v_evidence.eligible_rows <> 1 then
        raise exception 'settlement evidence counted ineligible usage rows: % eligible',
            v_evidence.eligible_rows;
    end if;
    if v_evidence.net_verified_savings_usd <> 100 then
        raise exception 'settlement evidence netted the wrong verified savings: %',
            v_evidence.net_verified_savings_usd;
    end if;
    if v_evidence.actual_spend_usd <> 40 then
        raise exception 'settlement evidence summed the wrong provider spend: %',
            v_evidence.actual_spend_usd;
    end if;
    if v_evidence.warm_spend_usd <> 10 then
        raise exception 'settlement evidence did not recompute the warm deduction: %',
            v_evidence.warm_spend_usd;
    end if;
    if v_evidence.zero_spend_rows <> 0 or v_evidence.zero_spend_net_savings_usd <> 0 then
        raise exception 'settlement evidence mis-classified a priced row as zero-spend';
    end if;

    ------------------------------------------------------------------
    -- 3. FIX-11 / EVIDENCE-WARM-DAYS: warm_spend_days counts DISTINCT days.
    --    The ledger is keyed (organization_id, provider, day), so before
    --    202607280012 two providers on one day reported 2.
    ------------------------------------------------------------------
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values
        (v_warm_days_org, 'anthropic', '2026-07-16', 1),
        (v_warm_days_org, 'openai', '2026-07-16', 2);
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_warm_days_org, v_start, v_end);
    if v_evidence.warm_spend_days <> 1 then
        raise exception 'warm_spend_days counts (provider, day) rows, not days: %',
            v_evidence.warm_spend_days;
    end if;
    -- The money term still counts every provider: over-deducting lowers the fee.
    if v_evidence.warm_spend_usd <> 3 then
        raise exception 'the warm deduction stopped counting every provider: %',
            v_evidence.warm_spend_usd;
    end if;
    -- A third provider on a second day is a second day, not a fourth row.
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_warm_days_org, 'deepseek', '2026-07-17', 4);
    select * into v_evidence
      from public.billing_period_settlement_evidence(v_warm_days_org, v_start, v_end);
    if v_evidence.warm_spend_days <> 2 or v_evidence.warm_spend_usd <> 7 then
        raise exception 'warm evidence over two days is wrong: days=% usd=%',
            v_evidence.warm_spend_days, v_evidence.warm_spend_usd;
    end if;

    ------------------------------------------------------------------
    -- 4. halting_condition=negative_fee -> 22023, not a clamp.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_ceiling_org, v_start, v_end, (-1)::bigint);
    exception
        when sqlstate '22023' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=negative_fee' in v_message) = 0 then
                raise exception 'a negative fee was rejected without naming its condition: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the halting conditions accepted a negative settlement fee';
    end if;
    -- The wrapper must report it the same way, before the attestation branch.
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_ceiling_org, v_start, v_end, (-1)::bigint);
    exception
        when sqlstate '22023' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=negative_fee' in v_message) = 0 then
                raise exception 'the settlement guard masked a negative fee as an attestation '
                                'problem: %', v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the settlement guard accepted a negative fee';
    end if;

    ------------------------------------------------------------------
    -- 5. halting_condition=relative_ceiling. The boundary is exact.
    ------------------------------------------------------------------
    v_result := public.assert_billing_period_halting_conditions(
        v_ceiling_org, v_start, v_end, 22500000::bigint);
    if (v_result->>'fee_ceiling_microusd')::bigint <> 22500000 then
        raise exception 'the ceiling is not 25%% of net-after-warm savings: %', v_result;
    end if;
    if (v_result->>'net_after_warm_savings_usd')::numeric <> 90 then
        raise exception 'the ceiling did not deduct the recomputed warm spend: %', v_result;
    end if;
    if (v_result->>'fee_ceiling_remaining_microusd')::bigint <> 22500000
       or (v_result->>'committed_period_microusd')::bigint <> 0 then
        raise exception 'an unsettled period reports committed money: %', v_result;
    end if;

    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_ceiling_org, v_start, v_end, 22500001::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=relative_ceiling' in v_message) = 0 then
                raise exception 'one microdollar over the ceiling halted for the wrong reason: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the relative ceiling accepted a fee one microdollar above it';
    end if;

    -- The warm deduction is load-bearing, not cosmetic: without it the ceiling
    -- would be 25,000,000 and 22,500,001 would have passed.
    if public.period_settlement_fee_microusd(100, 100) <> 25000000 then
        raise exception 'the shared fee formula changed; the warm-deduction proof above is void';
    end if;

    ------------------------------------------------------------------
    -- 6. halting_condition=zero_spend. Savings with no marginal dollar behind
    --    them anywhere in the period are not billable at any amount.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_zero_spend_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=zero_spend:' in v_message) = 0 then
                raise exception 'a period with no provider spend halted for the wrong reason: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the halting conditions billed a period with no provider spend';
    end if;
    -- Settling zero is always allowed: it moves no money, so blocking it would
    -- only make a period un-inspectable.
    v_result := public.assert_billing_period_halting_conditions(
        v_zero_spend_org, v_start, v_end, 0::bigint);
    if (v_result->>'actual_spend_usd')::numeric <> 0
       or (v_result->>'zero_spend_share')::numeric <> 1 then
        raise exception 'zero-fee inspection of a zero-spend period is wrong: %', v_result;
    end if;

    ------------------------------------------------------------------
    -- 7. halting_condition=zero_spend_concentration, and its boundary.
    ------------------------------------------------------------------
    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_concentration_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=zero_spend_concentration' in v_message) = 0 then
                raise exception 'a concentrated period halted for the wrong reason: %', v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the halting conditions billed a period whose savings are 91%% unpaid-for';
    end if;

    -- Exactly at the limit is NOT above it: the test is a strict inequality, so
    -- 0.50 must pass. (A drift to >= here would silently halt legitimate orgs.)
    v_result := public.assert_billing_period_halting_conditions(
        v_boundary_org, v_start, v_end, 1::bigint);
    if (v_result->>'zero_spend_share')::numeric <> 0.5 then
        raise exception 'the concentration boundary fixture is not exactly 0.50: %', v_result;
    end if;

    ------------------------------------------------------------------
    -- 8. halting_condition=cumulative_ceiling. This is the condition PSL-LATCH
    --    (FIX-4) attacked: a revision on an already-reported period must halt,
    --    and it must keep halting after an attempt to void the reported row.
    ------------------------------------------------------------------
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status, outbound_started_at, reported_at, settled_at
    ) values (
        v_cumulative_org, v_start, v_end,
        100, 10, 22500000, v_owner, 'reported', now(), now(), now()
    ) returning id into v_reported_id;

    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_cumulative_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=cumulative_ceiling' in v_message) = 0 then
                raise exception 'a second charge on a reported period halted for the wrong '
                                'reason: %', v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the cumulative ceiling allowed a second charge on a reported period';
    end if;

    -- Zero still inspects, and reports the committed total honestly.
    v_result := public.assert_billing_period_halting_conditions(
        v_cumulative_org, v_start, v_end, 0::bigint);
    if (v_result->>'committed_period_microusd')::bigint <> 22500000
       or (v_result->>'fee_ceiling_remaining_microusd')::bigint <> 0 then
        raise exception 'the cumulative ceiling mis-reports the committed period total: %',
            v_result;
    end if;

    -- FIX-4 regression, from the guard's side: the void-and-rebill UPDATE must
    -- raise, so the committed sum cannot be zeroed and the second charge stays
    -- refused. Pre-202607280010 this UPDATE succeeded and the assertion below
    -- reported 0 committed.
    v_raised := false;
    begin
        update public.period_settlement_ledger
           set status = 'void', outbound_started_at = null,
               reported_at = null, settled_at = null
         where id = v_reported_id;
    exception
        when sqlstate 'P0001' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'PSL-LATCH: a reported settlement was voided with its send evidence '
                        'erased; the cumulative ceiling can be reset';
    end if;
    v_result := public.assert_billing_period_halting_conditions(
        v_cumulative_org, v_start, v_end, 0::bigint);
    if (v_result->>'committed_period_microusd')::bigint <> 22500000 then
        raise exception 'PSL-LATCH: the cumulative ceiling lost the committed fee after a void '
                        'attempt: %', v_result;
    end if;
    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_cumulative_org, v_start, v_end, 22500000::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=cumulative_ceiling' in v_message) = 0 then
                raise exception 'the post-void second charge halted for the wrong reason: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'PSL-LATCH: a second full-rate charge was accepted after a void attempt';
    end if;

    -- A retraction that KEEPS the evidence is legal and still counts the money.
    update public.period_settlement_ledger
       set status = 'void', last_error = 'retracted after a completed send'
     where id = v_reported_id;
    v_result := public.assert_billing_period_halting_conditions(
        v_cumulative_org, v_start, v_end, 0::bigint);
    if (v_result->>'committed_period_microusd')::bigint <> 22500000 then
        raise exception 'voiding a reported settlement dropped its fee from the committed sum: %',
            v_result;
    end if;

    ------------------------------------------------------------------
    -- 9. halting_condition=unattested_billing_arrangement (202607280009).
    --    An org that passes every arithmetic condition is STILL unbillable
    --    until a human attests it pays a marginal per-call price.
    ------------------------------------------------------------------
    if public.organization_billing_arrangement_state(v_ceiling_org) <> 'unattested' then
        raise exception 'an un-attested organization does not default to unattested';
    end if;
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_ceiling_org, v_start, v_end, 22500000::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics
                v_state = returned_sqlstate,
                v_message = message_text;
            if position('halting_condition=unattested_billing_arrangement' in v_message) = 0 then
                raise exception 'an unattested org halted for the wrong reason (%): %',
                    v_state, v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'the settlement guard billed an unattested organization';
    end if;

    -- committed_capacity and unknown are attested-and-still-unbillable.
    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by, attested_evidence)
    values (v_ceiling_org, 'committed_capacity', 'halting-conditions assertion',
            'fixture: PTU agreement');
    foreach v_case in array array['committed_capacity', 'unknown'] loop
        update public.organization_billing_arrangement
           set arrangement = v_case
         where organization_id = v_ceiling_org;
        v_raised := false;
        begin
            perform public.assert_billing_period_settlement_allowed(
                v_ceiling_org, v_start, v_end, 22500000::bigint);
        exception
            when sqlstate '55000' then
                v_raised := true;
                get stacked diagnostics v_message = message_text;
                if position('halting_condition=unattested_billing_arrangement'
                            in v_message) = 0 then
                    raise exception '% halted for the wrong reason: %', v_case, v_message;
                end if;
        end;
        if not v_raised then
            raise exception 'the settlement guard billed a % organization', v_case;
        end if;
    end loop;

    -- Attested marginal_per_call: the whole stack now passes at the boundary and
    -- refuses one microdollar more, and it reports the arrangement it saw.
    update public.organization_billing_arrangement
       set arrangement = 'marginal_per_call'
     where organization_id = v_ceiling_org;
    v_result := public.assert_billing_period_settlement_allowed(
        v_ceiling_org, v_start, v_end, 22500000::bigint);
    if v_result->>'billing_arrangement' <> 'marginal_per_call'
       or (v_result->>'fee_ceiling_microusd')::bigint <> 22500000
       or (v_result->>'warm_spend_usd')::numeric <> 10
       or (v_result->>'warm_spend_days')::bigint <> 1
       or (v_result->>'eligible_rows')::bigint <> 1 then
        raise exception 'the attested settlement guard returned wrong evidence: %', v_result;
    end if;
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_ceiling_org, v_start, v_end, 22500001::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=relative_ceiling' in v_message) = 0 then
                raise exception 'the attested guard did not delegate to the arithmetic '
                                'conditions: %', v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'attestation alone lifted the relative ceiling';
    end if;

    ------------------------------------------------------------------
    -- 10. Malformed arguments stay argument errors on both doors.
    ------------------------------------------------------------------
    foreach v_case in array array['inverted', 'closed', 'null-fee'] loop
        v_raised := false;
        begin
            if v_case = 'inverted' then
                perform public.assert_billing_period_settlement_allowed(
                    v_ceiling_org, v_end, v_start, 1::bigint);
            elsif v_case = 'closed' then
                perform public.assert_billing_period_settlement_allowed(
                    v_ceiling_org, v_start, v_start, 1::bigint);
            else
                perform public.assert_billing_period_settlement_allowed(
                    v_ceiling_org, v_start, v_end, null::bigint);
            end if;
        exception
            when sqlstate '22023' then v_raised := true;
        end;
        if not v_raised then
            raise exception 'the settlement guard accepted a % period/fee argument',
                v_case;
        end if;
    end loop;
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            null::uuid, v_start, v_end, 1::bigint);
    exception
        when sqlstate '22023' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'the settlement guard accepted a null organization';
    end if;

    ------------------------------------------------------------------
    -- 11. halting_condition=unconfigured: losing the threshold row must halt
    --     settlement, not default the ceilings to something permissive.
    --
    --     Done last, and only in this rolled-back transaction, because it
    --     removes the singleton every later check depends on.
    --
    --     The one remaining tag, halting_condition=no_savings (202607280008
    --     around line 518), is deliberately NOT covered: it is documented
    --     unreachable in that file's own comment, because condition 1 rejects any
    --     positive fee when there are no net savings before that branch can run.
    --     Covering it would mean asserting a state the guard cannot enter.
    ------------------------------------------------------------------
    delete from public.billing_halting_conditions where singleton;
    v_raised := false;
    begin
        perform public.assert_billing_period_settlement_allowed(
            v_ceiling_org, v_start, v_end, 1::bigint);
    exception
        when sqlstate '55000' then
            v_raised := true;
            get stacked diagnostics v_message = message_text;
            if position('halting_condition=unconfigured' in v_message) = 0 then
                raise exception 'a missing threshold row halted for the wrong reason: %',
                    v_message;
            end if;
    end;
    if not v_raised then
        raise exception 'settlement proceeded with no halting-condition thresholds configured';
    end if;
    -- ...and a zero fee must halt too here: without thresholds there is no
    -- ceiling to inspect against, so the guard cannot tell the truth.
    v_raised := false;
    begin
        perform public.assert_billing_period_halting_conditions(
            v_ceiling_org, v_start, v_end, 0::bigint);
    exception
        when sqlstate '55000' then v_raised := true;
    end;
    if not v_raised then
        raise exception 'the halting conditions returned evidence with no thresholds configured';
    end if;
end;
$$;

rollback;
