\set ON_ERROR_STOP on

-- Behavioural contract for the period settlement WRITER
-- (202607280013_period_settlement_writer.sql), which is the first thing in this
-- repository that can put a number on a customer's invoice.
--
-- It composes with, and is asserted against, everything the writer depends on:
--   202607280007  the ledger, its fee CHECK, its revision chain, its guards
--   202607280008  the three halting conditions and their halting_condition= tags
--   202607280009  the billing-arrangement attestation and the privilege posture
--                 that makes assert_billing_period_settlement_allowed the ONLY door
--   202607280010  the send-evidence latches, without which a reported period
--                 could be voided and re-billed at the full rate
--   202607280012  count(distinct warm.day) in the evidence function
--
-- Arithmetic used throughout, so every expected number is checkable by hand:
-- verified savings 100.00 USD, recomputed warm spend 10.00 USD, so net-after-warm
-- is 90.00 USD and the fee is 90 * 0.25 * 1e6 = 22,500,000 microUSD -- the same
-- boundary period_settlement_ledger's own fee CHECK enforces from the row's
-- recorded columns.
--
-- Every window is derived from now() rather than a fixed calendar date. Two of
-- the conditions under test (period_not_closed, and the promoter's 34-day Stripe
-- reporting window) are now()-relative, so fixed dates would silently stop
-- exercising them as the file aged. now() is transaction-stable, so the file is
-- still deterministic.
--
-- The trailing rollback discards every fixture, so this file is re-runnable at
-- any point in the replay and leaves no settlement behind in what
-- 202607280007 calls a seven-year financial record.

begin;

-- The whole file runs under a deliberately NON-UTC session timezone. Week
-- arithmetic in this system is 604800 FIXED seconds
-- (public.billing_period_for_occurrence floors extract(epoch ...)/604800), and
-- period_settlement_ledger_weekly_window compares against interval '7 days'. If
-- any of it were secretly wall-clock-dependent, the settlements below would
-- either fail the window CHECK or land on the wrong week here and only here.
set local timezone = 'America/New_York';

do $$
declare
    v_owner uuid := '5e770001-0000-4000-8000-00000000000a';
    v_unattested_org uuid := '5e770001-0000-4000-8000-000000000001';
    v_zero_spend_org uuid := '5e770001-0000-4000-8000-000000000002';
    v_concentration_org uuid := '5e770001-0000-4000-8000-000000000003';
    v_losing_org uuid := '5e770001-0000-4000-8000-000000000004';
    v_grid_org uuid := '5e770001-0000-4000-8000-000000000005';
    v_anchor_org uuid := '5e770001-0000-4000-8000-000000000006';
    v_enrollment_org uuid := '5e770001-0000-4000-8000-000000000007';
    v_inactive_org uuid := '5e770001-0000-4000-8000-000000000008';
    v_unenrolled_org uuid := '5e770001-0000-4000-8000-000000000009';
    v_ownerless_org uuid := '5e770001-0000-4000-8000-00000000000b';
    v_void_org uuid := '5e770001-0000-4000-8000-00000000000c';
    v_promote_org uuid := '5e770001-0000-4000-8000-00000000000d';
    v_promote2_org uuid := '5e770001-0000-4000-8000-000000000021';
    v_summary_org uuid := '5e770001-0000-4000-8000-00000000000e';
    v_sweep_org uuid := '5e770001-0000-4000-8000-00000000000f';
    v_lowered_org uuid := '5e770001-0000-4000-8000-000000000010';
    v_negative_org uuid := '5e770001-0000-4000-8000-000000000011';
    v_no_account_org uuid := '5e770001-0000-4000-8000-000000000012';

    -- The week that closed most recently, and the week that is still open.
    v_end constant timestamptz := date_trunc('hour', now()) - interval '1 hour';
    v_start constant timestamptz := v_end - interval '604800 seconds';
    v_next_end constant timestamptz := v_end + interval '604800 seconds';
    v_warm_day constant date := ((v_start at time zone 'UTC') + interval '1 day')::date;
    v_anchor constant timestamptz := v_start + interval '1 hour';

    v_result jsonb;
    v_second jsonb;
    v_row public.period_settlement_ledger%rowtype;
    v_settlement_id bigint;
    v_revised_id bigint;
    v_committed bigint;
    v_count bigint;
    v_tz text;
    v_tz_org uuid;
    v_tz_result jsonb;
    v_case text;
    v_expected text;
    v_summary jsonb;
    v_sweep_count integer;
begin
    ------------------------------------------------------------------
    -- 0. Structure and privilege posture.
    --
    -- The entire safety argument for granting the writer to service_role is
    -- that a draft moves no money and that promotion is unreachable at runtime.
    -- If either half regressed, every behavioural assertion below would still
    -- pass while a leaked service key became able to bill.
    ------------------------------------------------------------------
    if to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)') is null
       or to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)') is null
       or to_regprocedure('public.billing_period_settlement_summary(uuid,timestamptz)') is null
       or to_regprocedure('public.billing_periods_awaiting_settlement(integer)') is null then
        raise exception 'the period settlement writer is missing (202607280013 not applied)';
    end if;

    if not has_function_privilege('service_role',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE') then
        raise exception 'the settlement writer is not callable by service_role; the shadow sweep '
                        'cannot run';
    end if;
    if has_function_privilege('service_role',
           'public.promote_billing_period_settlement(bigint,text,text)', 'EXECUTE') then
        raise exception 'a runtime role can promote a settlement to pending; a leaked service key '
                        'could move money toward Stripe';
    end if;
    if has_function_privilege('anon',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE')
       or has_function_privilege('authenticated',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE')
       or has_function_privilege('anon',
           'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE')
       or has_function_privilege('authenticated',
           'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE') then
        raise exception 'a browser role can reach the settlement writer or its read path';
    end if;
    -- The writer holds no table privilege for anyone: every write is SECURITY
    -- DEFINER. A `grant select` added to make the status route work by PostgREST
    -- would break 202607280007's own self-check and
    -- scripts/ci/migration-period-settlement-assertions.sql.
    if exists (
        select 1
          from unnest(array['anon', 'authenticated', 'service_role']) as role_name
          cross join unnest(array['select', 'insert', 'update', 'delete',
                                  'truncate', 'references', 'trigger']) as privilege
         where has_table_privilege(role_name, 'public.period_settlement_ledger', privilege)
    ) then
        raise exception 'the settlement writer widened a table privilege on '
                        'period_settlement_ledger; writes must stay behind a definer RPC';
    end if;
    -- 202607280009 made the wrapper the sole door. The writer must not have
    -- re-opened the attestation-free inner guard.
    if has_function_privilege('service_role',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception 'the attestation-free inner guard is reachable at runtime; settlement '
                        'could skip halting_condition=unattested_billing_arrangement';
    end if;
    -- The writer must actually call the guard, not reimplement it.
    if not exists (
        select 1 from pg_proc writer
         where writer.oid = to_regprocedure(
                   'public.settle_billing_period(uuid,timestamptz,text,boolean)')
           and writer.prosrc like '%assert_billing_period_settlement_allowed%'
           and writer.prosrc like '%period_settlement_fee_microusd%'
           and writer.prosrc like '%billing_period_settlement_evidence%'
           and writer.prosrc like '%billing_period_for_occurrence%'
           and writer.prosrc like '%pg_advisory_xact_lock%'
    ) then
        raise exception 'the settlement writer no longer routes through the settlement guard, the '
                        'shared fee formula, the evidence function, the Stripe anchor, and the '
                        'per-organization advisory lock';
    end if;
    -- FIX-10's release_billing_ledger_unsent has NO settlement-side analogue, by
    -- design: clearing outbound_started_at on this table is what 202607280010
    -- exists to forbid.
    if to_regprocedure('public.release_period_settlement_unsent(bigint,text)') is not null then
        raise exception 'a settlement-side unsent release exists; erasing outbound_started_at on '
                        'period_settlement_ledger reopens a reported period for a second charge';
    end if;
    -- Nothing may auto-settle from row ingestion.
    if exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid = trigger_state.tgfoid
         where not trigger_state.tgisinternal
           and coalesce(trigger_function.prosrc, '') like '%settle_billing_period%'
    ) then
        raise exception 'a trigger calls the settlement writer; settlement is a period aggregate, '
                        'never a row-ingestion predicate';
    end if;
    -- The evidence function now produces the watermark 202607280007 declared.
    if not exists (
        select 1
          from pg_proc evidence_function
         where evidence_function.oid = to_regprocedure(
                   'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
           and evidence_function.prosrc like '%max(usage.id)%'
           and evidence_function.prosrc like '%count(distinct warm.day)%'
    ) then
        raise exception 'the evidence function lost either the 202607280012 distinct-day count or '
                        'the 202607280013 usage_log watermark';
    end if;

    ------------------------------------------------------------------
    -- 1. Fixtures. One organization per condition, so no condition can pass
    --    because a sibling fixture moved an aggregate.
    ------------------------------------------------------------------
    insert into auth.users (id, email)
    values (v_owner, 'settlement-writer-assertion@example.invalid');

    insert into public.organizations (id, name, billing_owner_id) values
        (v_unattested_org,    'Writer: unattested',            v_owner),
        (v_zero_spend_org,    'Writer: zero spend',            v_owner),
        (v_concentration_org, 'Writer: concentration',         v_owner),
        (v_losing_org,        'Writer: losing week',           v_owner),
        (v_grid_org,          'Writer: shifted grid',          v_owner),
        (v_anchor_org,        'Writer: non-weekly anchor',     v_owner),
        (v_enrollment_org,    'Writer: partial first week',    v_owner),
        (v_inactive_org,      'Writer: inactive subscription', v_owner),
        (v_unenrolled_org,    'Writer: enrollment incomplete', v_owner),
        (v_ownerless_org,     'Writer: no billing owner',      null),
        (v_void_org,          'Writer: void and rebill',       v_owner),
        (v_promote_org,       'Writer: promotion',             v_owner),
        (v_promote2_org,      'Writer: promote-door rebill',   v_owner),
        (v_summary_org,       'Writer: status summary',        v_owner),
        (v_sweep_org,         'Writer: sweep enumeration',     v_owner),
        (v_lowered_org,       'Writer: lowered ceiling',       v_owner),
        (v_negative_org,      'Writer: negative savings',      v_owner),
        (v_no_account_org,    'Writer: no billing account',    v_owner);

    -- Every account except the two deliberately-broken ones is enrolled, active,
    -- and anchored on the week AFTER the probe window, so
    -- billing_period_for_occurrence reconstructs [v_start, v_end) by floor
    -- division rather than by anything doing local date arithmetic.
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    )
    select org_id, v_owner, 'active', v_start - interval '1 day', v_end, v_next_end
      from unnest(array[
          v_unattested_org, v_zero_spend_org, v_concentration_org, v_losing_org,
          v_grid_org, v_void_org, v_promote_org, v_promote2_org, v_sweep_org,
          v_lowered_org, v_negative_org, v_ownerless_org
      ]) as org_id;

    -- Not exactly 604800 seconds: six days. The window cannot be reconstructed,
    -- so the writer must refuse rather than invent one.
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    ) values (
        v_anchor_org, v_owner, 'active', v_start - interval '1 day',
        v_end, v_end + interval '6 days'
    );
    -- Enrolled halfway through the probe week: the evidence function does not
    -- filter on billing_started_at, so settling would fold pre-enrollment
    -- savings into the first fee.
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    ) values (
        v_enrollment_org, v_owner, 'active', v_start + interval '3 days',
        v_end, v_next_end
    );
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    ) values (
        v_inactive_org, v_owner, 'canceled', v_start - interval '1 day',
        v_end, v_next_end
    );
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    ) values (
        v_unenrolled_org, v_owner, 'active', null, v_end, v_next_end
    );
    -- The status summary reads the LIVE week, so this account's current period
    -- is the one that is still open.
    insert into public.billing_accounts (
        organization_id, user_id, subscription_status, billing_started_at,
        current_period_start, current_period_end
    ) values (
        v_summary_org, v_owner, 'active', v_start - interval '1 day', v_end, v_next_end
    );

    -- verified 100, list-price spend 40 -> the canonical 22,500,000 microUSD fee
    -- after the 10.00 warm deduction.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    )
    select 'settlement-writer', 'settlement-writer', org_id, v_anchor,
           'settlement-writer-' || org_id::text, 'writer',
           100, 40, 140, true, 'priced', 'proxy'
      from unnest(array[
          v_unattested_org, v_grid_org, v_void_org, v_promote_org, v_promote2_org,
          v_sweep_org, v_lowered_org, v_enrollment_org
      ]) as org_id;
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    select org_id, 'anthropic', v_warm_day, 10
      from unnest(array[
          v_unattested_org, v_grid_org, v_void_org, v_promote_org, v_promote2_org,
          v_sweep_org, v_lowered_org, v_enrollment_org
      ]) as org_id;

    -- Rows that must never count. Any of these leaking in raises a ceiling with
    -- evidence that is unbillable by design (proxy-reported telemetry is
    -- authoritative=false BY DESIGN and is never billing input).
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('settlement-writer', 'settlement-writer', v_void_org, v_anchor,
         'writer-nonauthoritative', 'writer', 5000, 40, 5040, false, 'priced', 'proxy'),
        ('settlement-writer', 'settlement-writer', v_void_org, v_anchor,
         'writer-unpriced', 'writer', 5000, 40, 5040, true, 'unpriced', 'proxy'),
        ('settlement-writer', 'settlement-writer', v_void_org, v_start - interval '1 second',
         'writer-before-window', 'writer', 5000, 40, 5040, true, 'priced', 'proxy'),
        ('settlement-writer', 'settlement-writer', v_void_org, v_end,
         'writer-at-window-end', 'writer', 5000, 40, 5040, true, 'priced', 'proxy');

    -- zero-spend org: real savings, no recorded provider spend at all.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'settlement-writer', 'settlement-writer', v_zero_spend_org, v_anchor,
        'writer-zero-spend', 'writer', 100, 0, 100, true, 'priced', 'proxy'
    );
    -- concentration org: 100 saved with no spend behind it, 10 with. The share
    -- is 100/110 = 0.90909, above the 0.50 limit.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('settlement-writer', 'settlement-writer', v_concentration_org, v_anchor,
         'writer-concentration-free', 'writer', 100, 0, 100, true, 'priced', 'proxy'),
        ('settlement-writer', 'settlement-writer', v_concentration_org,
         v_anchor + interval '1 hour',
         'writer-concentration-paid', 'writer', 10, 5, 15, true, 'priced', 'proxy');
    -- losing org: warm spend swallowed the savings. A losing week bills $0 and
    -- carries no deficit forward.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'settlement-writer', 'settlement-writer', v_losing_org, v_anchor,
        'writer-losing', 'writer', 5, 2, 7, true, 'priced', 'proxy'
    );
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_losing_org, 'anthropic', v_warm_day, 10);
    -- negative org: a genuinely negative netted period WITH warm spend, the
    -- combination that would break the fee CHECK if the writer pre-floored the
    -- recorded savings or derived the fee from a different snapshot.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values
        ('settlement-writer', 'settlement-writer', v_negative_org, v_anchor,
         'writer-negative-a', 'writer', -80, 3, -77, true, 'priced', 'proxy'),
        ('settlement-writer', 'settlement-writer', v_negative_org,
         v_anchor + interval '1 hour',
         'writer-negative-b', 'writer', 30, 4, 34, true, 'priced', 'proxy');
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_negative_org, 'anthropic', v_warm_day, 10);
    -- summary org: evidence inside the LIVE week.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'settlement-writer', 'settlement-writer', v_summary_org, v_end + interval '1 minute',
        'writer-summary', 'writer', 100, 40, 140, true, 'priced', 'proxy'
    );
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values (v_summary_org, 'anthropic', ((v_end at time zone 'UTC') + interval '1 day')::date, 10);

    -- Only marginal_per_call is billable. v_unattested_org is deliberately left
    -- out, and v_losing_org and v_negative_org are left out too so the $0 path
    -- is proven to be reachable WITHOUT an attestation.
    insert into public.organization_billing_arrangement
        (organization_id, arrangement, attested_by, attested_evidence)
    select org_id, 'marginal_per_call', 'settlement-writer assertion', 'fixture'
      from unnest(array[
          v_zero_spend_org, v_concentration_org, v_grid_org, v_anchor_org,
          v_enrollment_org, v_inactive_org, v_unenrolled_org, v_ownerless_org,
          v_void_org, v_promote_org, v_promote2_org, v_summary_org, v_sweep_org,
          v_lowered_org, v_no_account_org
      ]) as org_id;

    ------------------------------------------------------------------
    -- 2. Argument validation RAISES. These are programmer errors: a batch loop
    --    must not be able to mistake them for "this period is not settleable".
    ------------------------------------------------------------------
    begin
        perform public.settle_billing_period(null, v_anchor, 'assertion');
        raise exception 'the writer accepted a null organization';
    exception
        when sqlstate '22023' then null;
    end;
    begin
        perform public.settle_billing_period(v_void_org, null, 'assertion');
        raise exception 'the writer accepted a null period anchor';
    exception
        when sqlstate '22023' then null;
    end;
    begin
        perform public.settle_billing_period(v_void_org, v_anchor, '   ');
        raise exception 'the writer accepted a blank computed_by; a seven-year financial record '
                        'must name what produced it';
    exception
        when sqlstate '22023' then null;
    end;

    ------------------------------------------------------------------
    -- 3. Enrollment, window, and grid refusals. Each is a BLOCK with its own
    --    code, and none of them may write a row.
    ------------------------------------------------------------------
    -- A settlement row that overlaps the candidate window on a DIFFERENT
    -- period_start: a Stripe anchor that moved by a non-multiple of 604800
    -- seconds re-slices history, and the same usage could be settled twice.
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd, status
    ) values (
        v_grid_org, v_start - interval '1 day', v_end - interval '1 day',
        0, 0, 0, 'draft'
    );

    for v_case, v_expected in
        select * from (values
            (v_no_account_org::text, 'no_billing_account'),
            (v_inactive_org::text,   'subscription_inactive'),
            (v_unenrolled_org::text, 'enrollment_incomplete'),
            (v_anchor_org::text,     'invalid_anchor'),
            (v_enrollment_org::text, 'period_precedes_enrollment'),
            (v_grid_org::text,       'period_grid_shifted'),
            (v_ownerless_org::text,  'billing_owner_unavailable')
        ) as cases(org_id, code)
    loop
        v_result := public.settle_billing_period(v_case::uuid, v_anchor, 'assertion');
        if v_result->>'code' <> v_expected then
            raise exception 'the writer answered % for the % fixture: %',
                v_result->>'code', v_expected, v_result;
        end if;
        if (v_result->>'ok')::boolean then
            raise exception 'the writer reported ok=true while blocking with %', v_expected;
        end if;
        if exists (
            select 1 from public.period_settlement_ledger
             where organization_id = v_case::uuid
               and computed_by <> ''
        ) then
            raise exception 'the writer wrote a settlement row while blocking with %', v_expected;
        end if;
    end loop;

    -- A period that has not closed is never settled, even for an otherwise
    -- perfect organization: a revision is a SECOND Stripe charge, and
    -- fee_microusd is defined as the period total reconciliation compares
    -- against, so a mid-period settlement plus a correction mismatches forever.
    v_result := public.settle_billing_period(v_void_org, now(), 'assertion');
    if v_result->>'code' <> 'period_not_closed' then
        raise exception 'the writer settled a period that has not closed: %', v_result;
    end if;

    ------------------------------------------------------------------
    -- 4. Halting conditions. Each must block the write and name ITSELF, so an
    --    operator can tell the conditions apart from a worker log line.
    ------------------------------------------------------------------
    for v_case, v_expected in
        select * from (values
            (v_unattested_org::text,    'unattested_billing_arrangement'),
            (v_zero_spend_org::text,    'zero_spend'),
            (v_concentration_org::text, 'zero_spend_concentration')
        ) as cases(org_id, condition)
    loop
        v_result := public.settle_billing_period(v_case::uuid, v_anchor, 'assertion');
        if v_result->>'outcome' <> 'halted' then
            raise exception 'the writer did not halt on %: %', v_expected, v_result;
        end if;
        if v_result->>'halting_condition' <> v_expected
           or v_result->>'code' <> v_expected then
            raise exception 'the writer halted for the wrong reason (expected %): %',
                v_expected, v_result;
        end if;
        -- The tag must come from the guard's own message, not from a hard-coded
        -- string in the writer: that coupling is what keeps the two in step.
        if position('halting_condition=' || v_expected in coalesce(v_result->>'message', '')) = 0 then
            raise exception 'the halting condition tag was not parsed from the guard message: %',
                v_result;
        end if;
        if exists (select 1 from public.period_settlement_ledger
                    where organization_id = v_case::uuid) then
            raise exception 'a halt wrote a settlement row for %', v_expected;
        end if;
    end loop;

    ------------------------------------------------------------------
    -- 5. A losing week still gets a row: fee 0, status draft, no deficit
    --    carried forward -- and it does NOT need an attestation, because
    --    settling zero moves no money.
    ------------------------------------------------------------------
    v_result := public.settle_billing_period(v_losing_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'settled' or (v_result->>'fee_microusd')::bigint <> 0 then
        raise exception 'a losing week did not settle at zero: %', v_result;
    end if;
    select * into v_row
      from public.period_settlement_ledger
     where id = (v_result->>'settlement_id')::bigint;
    if v_row.status <> 'draft' or v_row.verified_savings_usd <> 5
       or v_row.warm_spend_usd <> 10 or v_row.net_savings_usd <> 0
       or v_row.fee_microusd <> 0 then
        raise exception 'the losing-week record is wrong: %', v_row;
    end if;

    -- A genuinely negative netted period, WITH warm spend. This is the
    -- combination where a writer that pre-floored verified_savings_usd, or
    -- derived the fee from a second evidence read, would violate
    -- period_settlement_ledger_fee_capped_non_negative.
    v_result := public.settle_billing_period(v_negative_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'settled' or (v_result->>'fee_microusd')::bigint <> 0 then
        raise exception 'a negative period did not settle at zero: %', v_result;
    end if;
    select * into v_row
      from public.period_settlement_ledger
     where id = (v_result->>'settlement_id')::bigint;
    if v_row.verified_savings_usd <> -50 or v_row.warm_spend_usd <> 10
       or v_row.net_savings_usd <> 0 or v_row.usage_row_count <> 2 then
        raise exception 'the negative-period record was floored or mis-summed: %', v_row;
    end if;

    ------------------------------------------------------------------
    -- 6. THE CLEAN SETTLEMENT, and the idempotence proof.
    ------------------------------------------------------------------
    v_result := public.settle_billing_period(v_void_org, v_anchor, 'assertion:clean');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the clean settlement did not settle: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;
    if (v_result->>'fee_microusd')::bigint <> 22500000 then
        raise exception 'the clean settlement is not 25%% of net-after-warm savings: %', v_result;
    end if;

    select count(*) into v_count
      from public.period_settlement_ledger where organization_id = v_void_org;
    if v_count <> 1 then
        raise exception 'a clean settlement wrote % rows for one period', v_count;
    end if;

    select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
    if v_row.status <> 'draft' then
        raise exception 'the writer produced status %; runtime may compute a settlement but only '
                        'a human may bill one', v_row.status;
    end if;
    if v_row.period_start <> v_start or v_row.period_end <> v_end
       or v_row.period_end - v_row.period_start <> interval '604800 seconds' then
        raise exception 'the writer settled the wrong window under a non-UTC session timezone: '
                        '[%,%)', v_row.period_start, v_row.period_end;
    end if;
    -- Only the eligible row counted: the non-authoritative, unpriced,
    -- before-window and at-window-end rows carried 5000 USD each and would have
    -- produced a wildly different fee.
    if v_row.verified_savings_usd <> 100 or v_row.warm_spend_usd <> 10
       or v_row.net_savings_usd <> 90 or v_row.usage_row_count <> 1 then
        raise exception 'the writer folded ineligible usage into a settlement: %', v_row;
    end if;
    if v_row.usage_log_watermark_id is null then
        raise exception 'the writer did not record a usage_log watermark';
    end if;
    if v_row.usage_log_watermark_id <> (
        select max(usage.id) from public.usage_log usage
         where usage.organization_id = v_void_org
           and usage.authoritative and usage.pricing_status = 'priced'
           and usage.ts >= v_start and usage.ts < v_end
    ) then
        raise exception 'the usage_log watermark disagrees with the eligible set';
    end if;
    if v_row.billing_owner_id <> v_owner then
        raise exception 'the writer did not snapshot billing-owner attribution';
    end if;
    if v_row.revision <> 1 or v_row.revision_of is not null
       or v_row.superseded_at is not null or v_row.outbound_started_at is not null
       or v_row.reported_at is not null or v_row.settled_at is not null then
        raise exception 'the writer touched a chain or send-evidence column: %', v_row;
    end if;
    if v_row.computed_by <> 'assertion:clean' then
        raise exception 'the writer did not record computed_by: %', v_row.computed_by;
    end if;

    -- IDEMPOTENCE. A second call for ANY instant inside the same week is a
    -- no-op: no second row, no second charge, and the same settlement id back.
    v_second := public.settle_billing_period(
        v_void_org, v_end - interval '1 second', 'assertion:sweep'
    );
    if v_second->>'outcome' <> 'unchanged' or v_second->>'code' <> 'unchanged' then
        raise exception 'the writer is not idempotent per (organization, period): %', v_second;
    end if;
    if (v_second->>'settlement_id')::bigint <> v_settlement_id then
        raise exception 'the idempotent call reported a different settlement';
    end if;
    if not (v_second->>'ok')::boolean then
        raise exception 'an idempotent no-op reported failure: %', v_second;
    end if;
    select count(*), coalesce(sum(fee_microusd), 0) into v_count, v_committed
      from public.period_settlement_ledger where organization_id = v_void_org;
    if v_count <> 1 or v_committed <> 22500000 then
        raise exception 'the second call double-charged the period: % rows totalling %',
            v_count, v_committed;
    end if;
    -- ...and the no-op really wrote nothing: computed_by still names the first call.
    if (select computed_by from public.period_settlement_ledger where id = v_settlement_id)
       <> 'assertion:clean' then
        raise exception 'the idempotent call mutated the existing settlement';
    end if;

    ------------------------------------------------------------------
    -- 7. A changed period will not revise itself, and the operator revision
    --    appends in 202607280007's documented three-step order.
    ------------------------------------------------------------------
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'settlement-writer', 'settlement-writer', v_void_org, v_anchor + interval '2 hours',
        'writer-late-receipt', 'writer', 20, 8, 28, true, 'priced', 'proxy'
    );

    v_result := public.settle_billing_period(v_void_org, v_anchor, 'assertion:sweep');
    if v_result->>'code' <> 'period_already_settled' then
        raise exception 'an automated caller revised a settled period: %', v_result;
    end if;
    -- verified 120, warm 10 -> net 110 -> 27,500,000 microUSD.
    if (v_result->>'recomputed_fee_microusd')::bigint <> 27500000 then
        raise exception 'the refusal did not report the recomputed fee: %', v_result;
    end if;
    select count(*) into v_count
      from public.period_settlement_ledger where organization_id = v_void_org;
    if v_count <> 1 then
        raise exception 'the refusal to auto-revise still wrote a row';
    end if;

    v_result := public.settle_billing_period(
        v_void_org, v_anchor, 'operator:assertion', true
    );
    if v_result->>'outcome' <> 'revised' or (v_result->>'revision')::integer <> 2
       or (v_result->>'fee_microusd')::bigint <> 27500000 then
        raise exception 'the operator revision is wrong: %', v_result;
    end if;
    v_revised_id := (v_result->>'settlement_id')::bigint;

    select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
    if v_row.superseded_at is null or v_row.superseded_by <> v_revised_id then
        raise exception 'the predecessor was not stamped and closed: %', v_row;
    end if;
    select * into v_row from public.period_settlement_ledger where id = v_revised_id;
    if v_row.revision <> 2 or v_row.revision_of <> v_settlement_id
       or v_row.status <> 'draft' or v_row.superseded_at is not null then
        raise exception 'the successor is not a linear next revision: %', v_row;
    end if;
    -- Exactly one live settlement for the period, which is what
    -- period_settlement_ledger_live_period_idx guarantees structurally.
    select count(*) into v_count
      from public.period_settlement_ledger
     where organization_id = v_void_org and period_start = v_start
       and superseded_at is null and status <> 'void';
    if v_count <> 1 then
        raise exception 'the revision left % live settlements for one period', v_count;
    end if;

    ------------------------------------------------------------------
    -- 8. VOID-AND-REBILL (FIX-4 / PSL-LATCH, composed with the writer). This is
    --    the single most important assertion in this file: a period Stripe may
    --    already hold can never be settled again, even after a retraction that
    --    frees the live-period slot.
    ------------------------------------------------------------------
    update public.period_settlement_ledger
       set status = 'pending', next_attempt_at = now() where id = v_revised_id;
    update public.period_settlement_ledger
       set status = 'sending', outbound_started_at = now(), attempts = attempts + 1,
           lease_owner = 'assertion-owner', lease_expires_at = now() + interval '5 minutes'
     where id = v_revised_id;
    update public.period_settlement_ledger
       set status = 'reported', reported_at = now(), settled_at = now()
     where id = v_revised_id;

    -- The 202607280010 reproduction UPDATE must still be refused outright.
    begin
        update public.period_settlement_ledger
           set status = 'void', outbound_started_at = null,
               reported_at = null, settled_at = null
         where id = v_revised_id;
        raise exception 'a reported settlement was voided with its send evidence erased; the '
                        'period could be re-billed at full rate';
    exception
        when sqlstate 'P0001' then null;
    end;

    -- The legal retraction keeps the marker, so the money stays committed...
    update public.period_settlement_ledger
       set status = 'void', last_error = 'operator retraction after reporting'
     where id = v_revised_id;
    select coalesce(sum(fee_microusd), 0) into v_committed
      from public.period_settlement_ledger
     where organization_id = v_void_org and period_start = v_start
       and (status in ('sending', 'reported') or outbound_started_at is not null);
    if v_committed <> 27500000 then
        raise exception 'the committed period fee was lost through a void: %', v_committed;
    end if;

    -- ...and the writer refuses UNCONDITIONALLY, even for an operator with
    -- p_allow_revision => true. Correcting a period Stripe may hold is an
    -- out-of-band refund or charge, not a second settlement.
    v_second := public.settle_billing_period(
        v_void_org, v_anchor, 'operator:assertion', true
    );
    if v_second->>'code' <> 'period_already_committed' then
        raise exception 'a voided-but-committed period was re-settled: %', v_second;
    end if;
    if (v_second->>'committed_period_microusd')::bigint <> 27500000 then
        raise exception 'the refusal did not report the committed total: %', v_second;
    end if;
    select count(*), coalesce(sum(fee_microusd), 0) into v_count, v_committed
      from public.period_settlement_ledger where organization_id = v_void_org;
    if v_count <> 2 or v_committed <> 50000000 then
        raise exception 'the refused re-settlement wrote a row: % rows totalling %',
            v_count, v_committed;
    end if;
    -- And the plain (non-operator) sweep answers the same way, so an automated
    -- caller cannot walk into it either.
    v_second := public.settle_billing_period(v_void_org, v_anchor, 'assertion:sweep');
    if v_second->>'code' <> 'period_already_committed' then
        raise exception 'the sweep re-settled a committed period: %', v_second;
    end if;

    ------------------------------------------------------------------
    -- 9. The relative ceiling can never be TRIPPED by the writer, because the
    --    fee is derived rather than supplied. Lowering the threshold must lower
    --    the fee, not halt the sweep -- which is the whole point of the
    --    least(formula, ceiling) backstop.
    ------------------------------------------------------------------
    update public.billing_halting_conditions set max_fee_share_of_verified_savings = 0.10000;
    v_result := public.settle_billing_period(v_lowered_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'a lowered fee ceiling halted the writer instead of lowering the fee: %',
            v_result;
    end if;
    -- net-after-warm 90 at 0.10 -> 9,000,000 microUSD, not 22,500,000.
    if (v_result->>'fee_microusd')::bigint <> 9000000 then
        raise exception 'the writer ignored the lowered ceiling: %', v_result;
    end if;
    update public.billing_halting_conditions set max_fee_share_of_verified_savings = 0.25000;

    ------------------------------------------------------------------
    -- 10. THE PROMOTER: the single money-moving door.
    ------------------------------------------------------------------
    v_result := public.settle_billing_period(v_promote_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the promotion fixture did not settle: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;

    -- A named actor and a real note are mandatory.
    begin
        perform public.promote_billing_period_settlement(v_settlement_id, '', 'a real long note');
        raise exception 'the promoter accepted an anonymous actor';
    exception
        when sqlstate '22023' then null;
    end;
    begin
        perform public.promote_billing_period_settlement(v_settlement_id, 'operator', 'too short');
        raise exception 'the promoter accepted a note shorter than 12 characters';
    exception
        when sqlstate '22023' then null;
    end;

    -- A zero fee is never promoted: it stays draft forever as its own record.
    v_result := public.promote_billing_period_settlement(
        (select id from public.period_settlement_ledger
          where organization_id = v_losing_org limit 1),
        'operator', 'losing week should not be reported'
    );
    if v_result->>'code' <> 'nothing_to_report' then
        raise exception 'the promoter would have reported a $0 period: %', v_result;
    end if;

    -- The happy path: draft -> pending, nothing else touched.
    v_result := public.promote_billing_period_settlement(
        v_settlement_id, 'operator:assertion', 'reconciled the provider invoice'
    );
    if v_result->>'outcome' <> 'promoted' then
        raise exception 'the promoter refused a clean draft: %', v_result;
    end if;
    select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
    if v_row.status <> 'pending' or v_row.fee_microusd <> 22500000
       or v_row.outbound_started_at is not null or v_row.reported_at is not null then
        raise exception 'the promoter mutated more than the status: %', v_row;
    end if;
    if position('reconciled the provider invoice' in v_row.last_error) = 0 then
        raise exception 'the promoter did not record its note';
    end if;

    -- Promoting twice is refused: the row is no longer a draft.
    v_result := public.promote_billing_period_settlement(
        v_settlement_id, 'operator:assertion', 'attempting a second promotion'
    );
    if v_result->>'code' <> 'not_a_draft' then
        raise exception 'the promoter promoted the same settlement twice: %', v_result;
    end if;

    -- The promoter re-runs the guard, so it is the SECOND line of defense
    -- against the cumulative ceiling. Report the promoted revision, then
    -- hand-craft a further draft revision (which the writer itself would have
    -- refused with period_already_committed) and prove promotion halts.
    update public.period_settlement_ledger
       set status = 'sending', outbound_started_at = now(),
           lease_owner = 'assertion-owner', lease_expires_at = now() + interval '5 minutes'
     where id = v_settlement_id;
    update public.period_settlement_ledger
       set status = 'reported', reported_at = now(), settled_at = now()
     where id = v_settlement_id;
    update public.period_settlement_ledger
       set superseded_at = now() where id = v_settlement_id;
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, usage_row_count,
        fee_microusd, billing_owner_id, status, computed_by,
        revision, revision_of
    ) values (
        v_promote_org, v_start, v_end, 100, 10, 1,
        22500000, v_owner, 'draft', 'assertion hand-crafted revision', 2, v_settlement_id
    ) returning id into v_revised_id;

    v_result := public.promote_billing_period_settlement(
        v_revised_id, 'operator:assertion', 'attempting to double-bill the period'
    );
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'cumulative_ceiling' then
        raise exception 'the promoter would have billed a period twice: %', v_result;
    end if;
    if (select status from public.period_settlement_ledger where id = v_revised_id) <> 'draft' then
        raise exception 'a halted promotion still moved the settlement out of draft';
    end if;

    -- A period outside Stripe's 34-day reporting window can only ever become an
    -- 'expired' row, so promoting it is refused up front.
    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd, fee_microusd,
        billing_owner_id, status, computed_by, revision
    ) values (
        v_sweep_org, v_start - interval '40 days', v_end - interval '40 days',
        100, 10, 22500000, v_owner, 'draft', 'assertion stale draft', 1
    ) returning id into v_revised_id;
    v_result := public.promote_billing_period_settlement(
        v_revised_id, 'operator:assertion', 'promoting a very stale draft'
    );
    if v_result->>'code' <> 'reporting_window_elapsed' then
        raise exception 'the promoter accepted a draft past the Stripe reporting window: %',
            v_result;
    end if;

    ------------------------------------------------------------------
    -- 11. THE READ PATH for /api/billing/status.
    ------------------------------------------------------------------
    v_summary := public.billing_period_settlement_summary(v_summary_org, v_end);
    if not (v_summary->>'ok')::boolean then
        raise exception 'the status summary refused a healthy account: %', v_summary;
    end if;
    -- The live week has no settlement yet, which is its NORMAL state -- and the
    -- estimate must still be a real projection, not the confident $0 a ledger
    -- read would produce.
    if v_summary->>'settlement_status' <> 'accruing' then
        raise exception 'the live period is not reported as accruing: %', v_summary;
    end if;
    if (v_summary->>'estimated_fee_microusd')::bigint <> 22500000 then
        raise exception 'the live-period estimate is not the evidence projection: %', v_summary;
    end if;
    if (v_summary->>'settled_fee_microusd')::bigint <> 0
       or (v_summary->>'reported_fee_microusd')::bigint <> 0
       or (v_summary->>'committed_fee_microusd')::bigint <> 0 then
        raise exception 'the status summary invented settled money for an unsettled week: %',
            v_summary;
    end if;
    if not (v_summary->>'billable')::boolean
       or v_summary->>'billing_arrangement' <> 'marginal_per_call' then
        raise exception 'the status summary lost the attestation state: %', v_summary;
    end if;
    if (v_summary->'evidence'->>'eligible_rows')::bigint <> 1
       or (v_summary->'evidence'->>'warm_spend_usd')::numeric <> 10
       or (v_summary->'evidence'->>'fee_ceiling_microusd')::bigint <> 22500000 then
        raise exception 'the status summary evidence is wrong: %', v_summary;
    end if;

    -- Per-status buckets, against rows seeded on the live week: 'reported' sums,
    -- 'draft' and 'void' never reach settled_fee, and review/attention count.
    -- period_settlement_ledger_revision_chain forces revision > 1 to name its
    -- predecessor, and _revision_idx forces the revisions to be distinct, so the
    -- fixture is a real linear chain with exactly one live row at its head.
    -- The 'pending' row carries NO outbound marker: it is exactly the state
    -- promote_billing_period_settlement produces, and it must reach
    -- committed_fee_microusd through its status alone.
    v_revised_id := null;
    for v_case, v_expected in
        select * from (values
            ('reported', '22500000'), ('review', '1000000'), ('capped', '2000000'),
            ('expired', '3000000'), ('pending', '5000000'), ('sending', '4000000')
        ) as rows(status_name, fee)
    loop
        insert into public.period_settlement_ledger (
            organization_id, period_start, period_end,
            verified_savings_usd, warm_spend_usd, fee_microusd,
            billing_owner_id, status, computed_by,
            revision, revision_of, superseded_at, outbound_started_at, reported_at
        ) values (
            v_summary_org, v_end, v_next_end, 100, 10, v_expected::bigint, v_owner,
            v_case, 'assertion ' || v_case,
            coalesce((select max(settlement.revision)
                        from public.period_settlement_ledger settlement
                       where settlement.organization_id = v_summary_org
                         and settlement.period_start = v_end), 0) + 1,
            v_revised_id,
            -- Only the last row stays live; the chain behind it is superseded.
            case when v_case = 'sending' then null else now() end,
            case when v_case in ('reported', 'review', 'sending') then now() else null end,
            case when v_case = 'reported' then now() else null end
        ) returning id into v_revised_id;
    end loop;

    v_summary := public.billing_period_settlement_summary(v_summary_org, v_end);
    if (v_summary->>'reported_fee_microusd')::bigint <> 22500000 then
        raise exception 'reported money must count only status=reported: %', v_summary;
    end if;
    -- The live row is the un-superseded 'sending' one.
    if v_summary->>'settlement_status' <> 'sending'
       or (v_summary->>'settled_fee_microusd')::bigint <> 4000000 then
        raise exception 'the status summary picked the wrong live settlement: %', v_summary;
    end if;
    -- The WRITER'S committed predicate: pending/sending/reported OR
    -- outbound-marked. A promoted-but-unsent ('pending', no marker) period is
    -- queued money and must be non-zero here: 22.5 (reported) + 1 (review,
    -- outbound-marked) + 4 (sending) + 5 (pending) = 32.5M uUSD. Were 'pending'
    -- dropped from the summary's bucket this reads 27500000 and fails.
    if (v_summary->>'committed_fee_microusd')::bigint <> 32500000 then
        raise exception 'the status summary under-reported committed money: %', v_summary;
    end if;
    if (v_summary->>'needs_review_count')::bigint <> 1
       or (v_summary->>'attention_count')::bigint <> 2 then
        raise exception 'the status summary mis-counted attention states: %', v_summary;
    end if;

    -- Fail closed, never zero: the caller must be able to render null.
    v_summary := public.billing_period_settlement_summary(v_no_account_org, v_end);
    if (v_summary->>'ok')::boolean or v_summary->>'code' <> 'no_billing_account' then
        raise exception 'the status summary did not fail closed for a missing account: %',
            v_summary;
    end if;
    v_summary := public.billing_period_settlement_summary(
        v_summary_org, v_end + interval '1 millisecond'
    );
    if (v_summary->>'ok')::boolean or v_summary->>'code' <> 'period_anchor_mismatch' then
        raise exception 'the status summary accepted an anchor one millisecond off: %', v_summary;
    end if;
    v_summary := public.billing_period_settlement_summary(v_anchor_org, v_end);
    if (v_summary->>'ok')::boolean or v_summary->>'code' <> 'period_anchor_mismatch' then
        raise exception 'the status summary accepted a non-weekly billing period: %', v_summary;
    end if;

    ------------------------------------------------------------------
    -- 12. Sweep enumeration. It must offer the closed, unsettled week and stop
    --     offering it the moment a live settlement exists.
    ------------------------------------------------------------------
    select count(*) into v_sweep_count
      from public.billing_periods_awaiting_settlement(200) candidate
     where candidate.organization_id = v_sweep_org
       and candidate.period_start = v_start
       and candidate.period_end = v_end;
    if v_sweep_count <> 1 then
        raise exception 'the sweep did not offer the closed unsettled week (% matches)',
            v_sweep_count;
    end if;
    -- No open period is ever offered, and nothing before enrollment.
    if exists (
        select 1 from public.billing_periods_awaiting_settlement(200) candidate
         where candidate.period_end > now()
    ) then
        raise exception 'the sweep offered a period that has not closed';
    end if;
    if exists (
        select 1 from public.billing_periods_awaiting_settlement(200) candidate
         where candidate.organization_id = v_enrollment_org
    ) then
        raise exception 'the sweep offered a period that precedes enrollment';
    end if;
    if exists (
        select 1 from public.billing_periods_awaiting_settlement(200) candidate
         where candidate.organization_id in (v_inactive_org, v_unenrolled_org, v_anchor_org)
    ) then
        raise exception 'the sweep offered a period for an unbillable account';
    end if;
    -- Every offered window is exactly 604800 seconds, under a non-UTC session
    -- timezone: the offsets must be fixed seconds, never wall-clock '7 days'.
    if exists (
        select 1 from public.billing_periods_awaiting_settlement(200) candidate
         where candidate.period_end - candidate.period_start <> interval '604800 seconds'
    ) then
        raise exception 'the sweep produced a window that is not exactly 604800 seconds';
    end if;

    v_result := public.settle_billing_period(v_sweep_org, v_anchor, 'assertion:sweep');
    if v_result->>'outcome' <> 'settled' then
        raise exception 'the sweep candidate could not be settled: %', v_result;
    end if;
    if exists (
        select 1 from public.billing_periods_awaiting_settlement(200) candidate
         where candidate.organization_id = v_sweep_org
           and candidate.period_start = v_start
    ) then
        raise exception 'the sweep keeps offering an already-settled period; an hourly sweep '
                        'would never converge';
    end if;

    ------------------------------------------------------------------
    -- 13. An unconfigured halting-condition table halts the writer with the
    --     tag 202607280008 uses, and writes nothing. Last, because it removes
    --     the thresholds every check above depends on.
    ------------------------------------------------------------------
    delete from public.billing_halting_conditions where singleton;
    v_result := public.settle_billing_period(v_concentration_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'halted'
       or v_result->>'halting_condition' <> 'unconfigured' then
        raise exception 'a missing halting-condition row did not halt the writer: %', v_result;
    end if;
    if exists (select 1 from public.period_settlement_ledger
                where organization_id = v_concentration_org) then
        raise exception 'the writer settled with no configured thresholds';
    end if;
    insert into public.billing_halting_conditions (singleton) values (true);

    ------------------------------------------------------------------
    -- THE PROMOTE DOOR: a queued ('pending') settlement is already money.
    --
    -- 202607280008's cumulative ceiling (around line 561) counts only
    -- 'sending'/'reported' OR outbound_started_at, because when it was written
    -- nothing could queue a settlement. Inheriting that predicate verbatim in
    -- settle_billing_period let this sequence bill one period twice:
    --   settle -> promote -> (late eligible receipt) -> settle(p_allow_revision)
    --   -> promote
    -- yielded TWO 'pending' rows for one (org, period) totalling 70,000,000
    -- uUSD where the correct total was 47,500,000. Each row has a distinct id,
    -- so api/billing_recovery.py derives a DISTINCT Stripe idempotency key from
    -- it and Stripe deduplicates nothing -- it sums both meter events.
    --
    -- This is the PSL-LATCH class of defect reached through the promote door
    -- instead of the void door. Do not relax either check below.
    ------------------------------------------------------------------
    v_result := public.settle_billing_period(v_promote2_org, v_anchor, 'assertion');
    if v_result->>'outcome' <> 'settled'
       or (v_result->>'fee_microusd')::bigint <> 22500000 then
        raise exception 'the promote-door fixture did not settle: %', v_result;
    end if;
    v_settlement_id := (v_result->>'settlement_id')::bigint;

    v_result := public.promote_billing_period_settlement(
        v_settlement_id, 'operator:assertion', 'reconciled the provider invoice'
    );
    if v_result->>'outcome' <> 'promoted' then
        raise exception 'the promote-door fixture did not promote: %', v_result;
    end if;

    -- A late eligible receipt doubles the verified savings for the same period.
    insert into public.usage_log (
        key_hash, owner_id, organization_id, ts, request_id, project,
        verified_savings_usd, actual_cost_usd, baseline_cost_usd,
        authoritative, pricing_status, receipt_source
    ) values (
        'settlement-writer', 'settlement-writer', v_promote2_org,
        v_start + interval '2 days', 'promote-door-late-receipt', 'writer',
        100, 40, 140, true, 'priced', 'proxy'
    );

    -- The writer must REFUSE. Before the fix this returned outcome='revised'
    -- with fee_microusd=47500000 and committed_period_microusd=0.
    v_result := public.settle_billing_period(
        v_promote2_org, v_anchor, 'assertion-late', p_allow_revision => true
    );
    if v_result->>'outcome' <> 'blocked'
       or v_result->>'code' <> 'period_already_committed' then
        raise exception
            'the writer revised a period that was already queued: %', v_result;
    end if;

    -- And exactly one row may be queued or sent for the period.
    if (select count(*) from public.period_settlement_ledger
         where organization_id = v_promote2_org
           and status in ('pending', 'sending', 'reported')) <> 1 then
        raise exception 'the promote door left more than one queued settlement: %',
            (select jsonb_agg(jsonb_build_object('id', id, 'status', status,
                                                 'fee', fee_microusd))
               from public.period_settlement_ledger
              where organization_id = v_promote2_org);
    end if;
end;
$$;

rollback;
