-- FIX-11 / EVIDENCE-WARM-DAYS: warm_spend_days counted rows, not days.
--
-- WHAT WAS WRONG (reproduced by execution)
--
-- public.billing_period_settlement_evidence (202607280008, around line 352)
-- computes its warm_spend_days output as
--     select count(*) from public.warm_budget_ledger warm where <overlap>
-- but public.warm_budget_ledger's primary key is
-- (organization_id, provider, day) -- 202607280001 around line 80, widened to
-- three providers by 202607280003. One organization warming two providers on the
-- same UTC day therefore yields warm_spend_days = 2 for one day. Measured on a
-- real cluster: four rows over two distinct days reported warm_spend_days = 4.
--
-- BLAST RADIUS: diagnostics only. warm_spend_days is emitted in the
-- relative_ceiling DETAIL line and in the returned evidence jsonb
-- (202607280008 around lines 474, 477 and 614) and is consumed by no arithmetic
-- anywhere: the money terms are warm_spend_usd (a SUM over the same overlap
-- window, which is correct and is deliberately left byte-identical here) and
-- net_verified_savings_usd. So this is a low-severity operator-facing defect --
-- but it is an operator-facing defect on the one number an operator would use to
-- sanity-check whether a period's warm deduction covers the days it should, and
-- a multi-provider org silently inflates it by the number of providers.
--
-- THE FIX
--
-- count(distinct warm.day) over the identical overlap predicate. 202607280008 is
-- checksum-pinned in scripts/ci/verify-migrations.mjs and is (or will be) applied
-- to an environment with no migration ledger, so it is NOT edited in place: this
-- migration recreates the function.
--
-- Nothing else changes. Every other output column, the overlap predicate, the
-- eligibility predicate (authoritative and priced, half-open window), the
-- language/volatility/security markers, and the privilege posture are reproduced
-- verbatim from 202607280008 so that a diff of the two bodies shows exactly one
-- changed aggregate. In particular warm_spend_usd keeps summing EVERY row
-- (all providers, boundary days counted by both adjacent periods), because that
-- over-deduction is the deliberate, fee-lowering choice 202607280007 specifies.
--
-- CREATE OR REPLACE is sufficient and is used on purpose: the OUT list is
-- unchanged, so unlike 202607280008 (which widened it and had to drop first)
-- there is no need to drop the function, and not dropping it means the ACL and
-- the late-bound call from public.assert_billing_period_halting_conditions are
-- carried over untouched.
--
-- Safety: additive, idempotent, reads and writes no billing data, and cannot
-- change any fee -- the value it corrects is not an input to one.

begin;

do $warm_days_precondition$
begin
    if to_regclass('public.warm_budget_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280012 requires public.warm_budget_ledger from 202607280001',
            hint    = 'Apply 202607280001 (and 202607280003) first.';
    end if;
    if to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280012 requires public.billing_period_settlement_evidence '
                      'from 202607280008',
            hint    = 'This migration recreates that function; it does not introduce it. '
                      'Apply 202607280008 first.';
    end if;
    -- Same reason 202607280008 and 202607280009 refuse to install while the
    -- per-row fee trigger is attached: the per-row path never reaches this
    -- evidence function at all, so hardening it would be misleading.
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280012 refuses to recreate the settlement evidence function while '
                      'the per-row fee trigger is attached',
            hint    = 'Reapply 202607280006 first.';
    end if;
end;
$warm_days_precondition$;

-- Period evidence, aggregated over billable-eligible rows only. Netted, not
-- floored per row: offsetting rows must be allowed to cancel.
create or replace function public.billing_period_settlement_evidence(
    p_organization_id uuid,
    p_period_start timestamptz,
    p_period_end timestamptz
)
returns table (
    eligible_rows bigint,
    zero_spend_rows bigint,
    net_verified_savings_usd numeric,
    zero_spend_net_savings_usd numeric,
    actual_spend_usd numeric,
    warm_spend_usd numeric,
    warm_spend_days bigint
)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    select
        count(*)::bigint,
        count(*) filter (
            where coalesce(usage.actual_cost_usd, 0) <= 0
        )::bigint,
        coalesce(sum(coalesce(usage.verified_savings_usd, 0)), 0)::numeric,
        coalesce(sum(
            case
                when coalesce(usage.actual_cost_usd, 0) <= 0
                    then coalesce(usage.verified_savings_usd, 0)
                else 0
            end
        ), 0)::numeric,
        coalesce(sum(greatest(coalesce(usage.actual_cost_usd, 0), 0)), 0)::numeric,
        -- Warm-ping spend does not live in usage_log: warm receipts are recorded
        -- with verified_savings_usd = 0, so the sums above cannot see this cost.
        -- It is summed from its own ledger, over every UTC day whose [day,
        -- day+1) span OVERLAPS the period. A boundary day is therefore deducted
        -- by both adjacent periods, exactly as 202607280007 specifies; that can
        -- only lower a ceiling, never raise one. All providers count.
        (
            select coalesce(sum(greatest(warm.spent_usd, 0)), 0)::numeric
              from public.warm_budget_ledger warm
             where warm.organization_id = p_organization_id
               and (warm.day::timestamp at time zone 'UTC') < p_period_end
               and ((warm.day + 1)::timestamp at time zone 'UTC') > p_period_start
        ),
        -- DISTINCT day, not row count (202607280012). warm_budget_ledger is keyed
        -- (organization_id, provider, day), so an org warming several providers
        -- on one day has several rows for that one day. This output is operator
        -- diagnostics -- it is emitted in the relative_ceiling detail line and in
        -- the returned evidence, and no arithmetic consumes it -- so counting
        -- rows inflated a number an operator uses to check that the warm
        -- deduction covers the days it should.
        (
            select count(distinct warm.day)::bigint
              from public.warm_budget_ledger warm
             where warm.organization_id = p_organization_id
               and (warm.day::timestamp at time zone 'UTC') < p_period_end
               and ((warm.day + 1)::timestamp at time zone 'UTC') > p_period_start
        )
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and usage.authoritative
       and usage.pricing_status = 'priced'
       and usage.ts >= p_period_start
       and usage.ts < p_period_end;
$$;

-- CREATE OR REPLACE preserves the ACL; restate it so a hand-recreated function
-- can never be left reachable from a browser session.
revoke all on function public.billing_period_settlement_evidence(uuid, timestamptz, timestamptz)
    from public, anon, authenticated;
grant execute on function public.billing_period_settlement_evidence(uuid, timestamptz, timestamptz)
    to service_role;

comment on function public.billing_period_settlement_evidence(uuid, timestamptz, timestamptz) is
    'Aggregate billing evidence for one (organization, period) over authoritative '
    'priced usage_log rows in [period_start, period_end). Savings are netted '
    'across the period and are NOT floored per row. warm_spend_usd is summed '
    'independently from warm_budget_ledger over the UTC days overlapping the '
    'period (boundary days are counted by both adjacent periods, which can only '
    'lower a fee); it is the second operand of the fee ceiling and is recomputed '
    'here precisely so that no settlement writer can supply it. warm_spend_days '
    'counts DISTINCT overlapping days (202607280012): the ledger is keyed '
    '(organization_id, provider, day), so a row count multiplied a day by the '
    'number of providers warmed on it. That column is operator diagnostics only '
    'and is not an input to any fee.';

do $warm_days_contract$
declare
    v_probe_org uuid;
    v_window_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_window_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_evidence record;
    v_result jsonb;
begin
    -- Structural: the corrected aggregate is present and the money term was not
    -- turned into a DISTINCT sum along with it.
    if not exists (
        select 1
          from pg_proc evidence_function
          join pg_namespace evidence_schema
            on evidence_schema.oid = evidence_function.pronamespace
         where evidence_schema.nspname = 'public'
           and evidence_function.proname = 'billing_period_settlement_evidence'
           and evidence_function.prosrc like '%count(distinct warm.day)%'
           and evidence_function.prosrc like '%sum(greatest(warm.spent_usd, 0))%'
           and evidence_function.prosrc like '%warm_budget_ledger%'
    ) then
        raise exception '202607280012 did not install the distinct-day warm count, or changed '
                        'the warm spend sum along with it';
    end if;

    -- Behavioural: three providers on one day plus one provider on a second day
    -- is FOUR ledger rows over TWO distinct days. Pre-fix this reported 4.
    insert into public.organizations (name)
    values ('202607280012 warm day contract probe')
    returning id into v_probe_org;

    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values
        (v_probe_org, 'anthropic', '2026-07-16', 1.5),
        (v_probe_org, 'openai',    '2026-07-16', 2.5),
        (v_probe_org, 'deepseek',  '2026-07-16', 3.0),
        (v_probe_org, 'anthropic', '2026-07-17', 4.0);

    select * into v_evidence
      from public.billing_period_settlement_evidence(
          v_probe_org, v_window_start, v_window_end
      );
    if v_evidence.warm_spend_days <> 2 then
        raise exception '202607280012 warm_spend_days counts ledger rows, not distinct days: %',
            v_evidence.warm_spend_days;
    end if;
    -- The money term is unchanged: every row still deducts, all providers count.
    if v_evidence.warm_spend_usd <> 11.0 then
        raise exception '202607280012 changed the warm spend deduction: %',
            v_evidence.warm_spend_usd;
    end if;
    -- ...and nothing else moved. This org has no usage rows at all.
    if v_evidence.eligible_rows <> 0
       or v_evidence.zero_spend_rows <> 0
       or v_evidence.net_verified_savings_usd <> 0
       or v_evidence.zero_spend_net_savings_usd <> 0
       or v_evidence.actual_spend_usd <> 0 then
        raise exception '202607280012 disturbed a usage evidence column';
    end if;

    -- The overlap predicate is untouched. For [07-15 10:00Z, 07-22 10:00Z):
    -- day 07-14 spans [07-14 00:00Z, 07-15 00:00Z), which does not reach
    -- period_start, so it is EXCLUDED; day 07-22 starts before period_end, so it
    -- IS included even though the period ends part-way through it. Distinct
    -- overlapping days therefore become {07-16, 07-17, 07-22} = 3.
    insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
    values
        (v_probe_org, 'anthropic', '2026-07-14', 9.0),
        (v_probe_org, 'anthropic', '2026-07-22', 9.0);
    select * into v_evidence
      from public.billing_period_settlement_evidence(
          v_probe_org, v_window_start, v_window_end
      );
    if v_evidence.warm_spend_days <> 3 then
        raise exception '202607280012 changed which days overlap the period: %',
            v_evidence.warm_spend_days;
    end if;
    if v_evidence.warm_spend_usd <> 20.0 then
        raise exception '202607280012 changed which days the warm deduction covers: %',
            v_evidence.warm_spend_usd;
    end if;

    -- The guard resolves this function late, so its evidence reports the
    -- corrected count too. A zero fee always passes, which is what lets an
    -- operator inspect a period.
    v_result := public.assert_billing_period_halting_conditions(
        v_probe_org, v_window_start, v_window_end, 0::bigint
    );
    if (v_result->>'warm_spend_days')::bigint <> 3 then
        raise exception '202607280012 halting-condition evidence still reports row counts: %',
            v_result;
    end if;
    -- No money moved with it: with no verified savings the ceiling is still zero.
    if (v_result->>'fee_ceiling_microusd')::bigint <> 0
       or (v_result->>'warm_spend_usd')::numeric <> 20.0 then
        raise exception '202607280012 changed the fee ceiling arithmetic: %', v_result;
    end if;

    delete from public.warm_budget_ledger where organization_id = v_probe_org;
    delete from public.organizations where id = v_probe_org;
end;
$warm_days_contract$;

commit;

-- ROLLBACK PROCEDURE (restores the inflated diagnostic; no money is affected
-- either way):
-- 1. Re-run the CREATE OR REPLACE FUNCTION
--    public.billing_period_settlement_evidence(uuid, timestamptz, timestamptz)
--    body from supabase/migrations/202607280008_billing_halting_conditions.sql,
--    which is identical except for `count(*)` in place of
--    `count(distinct warm.day)` in the final scalar subquery.
-- 2. Re-run its REVOKE/GRANT pair from that same file.
-- 3. Nothing in this migration writes data, so there is nothing else to undo.
