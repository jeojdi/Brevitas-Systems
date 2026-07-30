-- Phase 4b of the billing correctness rollout: THE SETTLEMENT WRITER.
--
-- 202607280007 created public.period_settlement_ledger with no writer, on
-- purpose, and promised that Phase 4 would be "a new security-definer RPC over
-- an unchanged table" (202607280007 around line 76). 202607280008/0009 built the
-- guard that RPC has to call, and 202607280010 latched the send evidence that
-- guard counts on. This migration is that RPC. It adds four functions and one
-- enumeration helper, no trigger, and no table privilege.
--
-- THE CENTRAL PRIVILEGE IDEA: runtime may COMPUTE a settlement, only a human may
-- BILL one.
--
--   * public.settle_billing_period      -> writes 'draft' rows ONLY. Granted to
--     service_role, because a draft moves no money: 202607280007 (around line
--     154) defines promotion to 'pending' as "the deliberate manual act that
--     makes a period billable". A shadow sweep can therefore run for weeks with
--     BREVITAS_BILLING_ENABLED still false and nothing can be charged.
--   * public.promote_billing_period_settlement -> draft -> pending, the single
--     money-moving door. SECURITY DEFINER and granted to NOBODY: reachable only
--     from a direct SQL session, exactly like attesting an organization
--     (202607280009's TODO(billing-attestation-writer)). Automating it later is a
--     one-line, reviewed migration; it is deliberately not smuggled in here.
--   * public.billing_period_settlement_summary -> the read path for
--     /api/billing/status. Granted to service_role. The route CANNOT select the
--     table: 202607280007 (around line 232) revokes every privilege from
--     service_role, its own self-check asserts that, and so does
--     scripts/ci/migration-period-settlement-assertions.sql. A `grant select` to
--     satisfy a PostgREST read would turn both red and dismantle the Phase 4
--     privilege model. Hence an RPC.
--   * public.billing_periods_awaiting_settlement -> enumerates closed,
--     unsettled periods so a sweep needs no client-side week arithmetic.
--
-- WHAT MAKES A service_role GRANT ON THE WRITER SAFE
--
-- settle_billing_period has NO fee argument, NO status argument, and NO evidence
-- argument. Every money field is derived inside the function from
-- public.billing_period_settlement_evidence, and the fee is
--     least(period_settlement_fee_microusd(net_after_warm, net_verified),
--           the halting-condition ceiling)
-- so 202607280007's fee CHECK (around line 192) and 202607280008's relative
-- ceiling (around line 458) are satisfied BY CONSTRUCTION rather than by hope.
-- A caller can choose WHICH period to settle and nothing else.
--
-- THE EVIDENCE FUNCTION IS WIDENED BY ONE COLUMN
--
-- public.period_settlement_ledger.usage_log_watermark_id has existed since
-- 202607280007 (around line 143) with no producer. It is the late-arrival
-- detector in this writer's idempotence tuple: two calls whose evidence differs
-- only by a receipt that landed below the watermark must still be recognised as
-- a change. Rather than add a sibling helper that would duplicate the
-- eligibility predicate (`authoritative and pricing_status='priced'` over the
-- half-open window) -- the exact drift 202607280008 (around line 190) argues
-- against -- public.billing_period_settlement_evidence gains ONE trailing OUT
-- column, usage_log_watermark_id, computed over the identical eligible set.
--
-- CREATE OR REPLACE cannot widen an OUT list, so the function is dropped and
-- recreated. That is safe for the same reason 202607280008 gave when it did the
-- same thing (around line 298): assert_billing_period_halting_conditions is
-- plpgsql and resolves the call by name at execution time, and both existing
-- callers bind it with `select * into <record>` (202607280008 around line 444;
-- scripts/ci/migration-halting-conditions-assertions.sql), so a trailing column
-- binds nothing incorrectly. DROP loses the ACL and the comment, so both are
-- restated below. The 202607280012 body (count(distinct warm.day), and the money
-- term left summing every row) is reproduced verbatim: if 202607280012 is ever
-- revised, this file moves with it.
--
-- ORDERING NOTE, measured: because 202607280012 uses CREATE OR REPLACE, it
-- CANNOT be re-applied once this migration has widened the OUT list -- PostgreSQL
-- refuses with 'cannot change return type of existing function'. That is
-- fail-closed and deliberate: it makes an out-of-order replay abort loudly
-- instead of silently narrowing the evidence function out from under
-- settle_billing_period. In manifest order (…0012, 0013) both files, and the
-- harness's back-to-back double-apply of each, are clean. To roll back, DROP
-- first -- see the ROLLBACK PROCEDURE at the end of this file.
--
-- SETTLE ONCE, AFTER THE PERIOD CLOSES
--
-- period_end <= now() is mandatory. Incremental mid-period settlement is
-- forbidden because a revision is a SECOND Stripe charge -- the idempotency key
-- is derived from the ledger id alone (202607280008 around line 100) -- while
-- fee_microusd is defined as the period TOTAL that reconciliation compares
-- against. A delta revision would make reconciliation mismatch forever.
--
-- WHAT THE WRITER REFUSES TO DO, EVER
--
--   * It never produces any status but 'draft'.
--   * It never writes outbound_started_at / reported_at / settled_at, never
--     voids a row, and never moves a row out of 'sending'/'reported' -- so no
--     202607280010 latch can fire against it.
--   * If the period has already committed money to Stripe (202607280008's
--     `status in ('sending','reported') or outbound_started_at is not null`, so
--     including a marker-preserving 'void'), it refuses UNCONDITIONALLY, even
--     with p_allow_revision => true. Correcting a period Stripe may already hold
--     is an operator act with an out-of-band refund or charge.
--   * There is deliberately NO settlement-side release_unsent. 202607280011
--     added one for billing_ledger because HTTP 429 is documented
--     non-ingestion; the analogous function for this table is FORBIDDEN and
--     would be rejected by 202607280010 by design. A settlement 429 leaves the
--     row 'sending' and is resolved by reconciliation or an operator sweep to
--     'review', never by erasing evidence the cumulative ceiling reads.
--
-- WHO CALLS IT TODAY: nothing automatic. Stage one is an operator recipe:
--
--     select public.settle_billing_period(
--                '<organization uuid>'::uuid,
--                '<any instant inside the closed week>'::timestamptz,
--                'operator:<name>');
--
-- which is enough to start the shadow run by hand with zero ability to bill.
-- Stage two -- a shadow sweep in the Python worker driving
-- billing_periods_awaiting_settlement + settle_billing_period once an hour,
-- gated on its own env name and NOT on BREVITAS_BILLING_ENABLED -- is a
-- follow-on PR in api/, which this migration does not touch. Any automated
-- caller MUST branch on the returned outcome/code and MUST NOT treat a
-- successful RPC call as a successful settlement: halts and blocks are returned
-- as a body, not raised, precisely so one organization cannot abort a batch.
--
-- Safety: additive. It creates five functions, recreates one, attaches nothing
-- to any table, grants nothing to anon/authenticated, and widens no table
-- privilege. Applied to an empty ledger it writes no row; it can only write
-- 'draft' rows thereafter.

-- REVERSE: PITR-ONLY -- the widened evidence-function OUT list cannot be narrowed in place, and dropping the writer/promoter/summary orphans settlement evidence

begin;

do $writer_precondition$
begin
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280013 requires public.period_settlement_ledger from 202607280007',
            hint    = 'Apply 202607280007 first; the writer writes that table and nothing else.';
    end if;
    if to_regclass('public.billing_halting_conditions') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280013 requires public.billing_halting_conditions from 202607280008',
            hint    = 'The writer reads the fee ceiling from that table so a lowered ceiling '
                      'lowers the fee instead of halting the sweep. Apply 202607280008 first.';
    end if;
    if to_regprocedure(
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280013 requires public.assert_billing_period_settlement_allowed '
                      'from 202607280009',
            hint    = 'That wrapper is the ONLY settlement door: 202607280009 revokes EXECUTE on '
                      'the inner arithmetic guard from service_role so the attestation cannot be '
                      'skipped. Apply 202607280009 first.';
    end if;
    if to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280013 requires public.billing_period_settlement_evidence '
                      'from 202607280008 (as corrected by 202607280012)',
            hint    = 'This migration widens that function by one OUT column; it does not '
                      'introduce it.';
    end if;
    if to_regprocedure('public.period_settlement_fee_microusd(numeric,numeric)') is null then
        raise exception using
            errcode = '42883',
            message = '202607280013 requires public.period_settlement_fee_microusd from 202607280007',
            hint    = 'The writer must not duplicate the fee arithmetic that backs the table CHECK.';
    end if;
    if to_regprocedure(
           'public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280013 requires public.billing_period_for_occurrence from 202607170004',
            hint    = 'Every week boundary in this file comes from that function''s 604800-second '
                      'floor division. Local date arithmetic is not an acceptable substitute.';
    end if;

    -- 202607280010 must already be in place. A writer that exists before the
    -- send latches means a reported settlement can be voided with its evidence
    -- erased and the period re-billed at the full rate: the reproduced 100%
    -- overcharge FIX-4 exists to close.
    if not exists (
        select 1
          from pg_proc guard_function
          join pg_namespace guard_schema
            on guard_schema.oid = guard_function.pronamespace
         where guard_schema.nspname = 'public'
           and guard_function.proname = 'prevent_period_settlement_identity_change'
           and guard_function.prosrc like '%old.outbound_started_at is not null%'
           and guard_function.prosrc like '%old.reported_at is not null%'
           and guard_function.prosrc like '%old.settled_at is not null%'
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280013 refuses to install a settlement writer before the '
                      'send-evidence latches exist',
            hint    = 'apply 202607280010 first: a writer must not exist before the send latches, '
                      'or a reported period can be voided and re-billed at the full rate.';
    end if;

    -- Same reason 202607280008/0009/0012 refuse to install while the per-row fee
    -- trigger is attached: the per-row path bypasses every condition this writer
    -- calls, so a period writer beside it would double-bill.
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280013 refuses to install the period settlement writer while the '
                      'per-row fee trigger is attached',
            hint    = 'Two writers would bill the same savings twice, and the per-row path '
                      'reaches none of the halting conditions. Reapply 202607280006 first.';
    end if;
end;
$writer_precondition$;

-- ---------------------------------------------------------------------------
-- 1. Evidence, widened by one trailing column.
--
-- Everything except the new usage_log_watermark_id output is reproduced verbatim
-- from 202607280012 (which is itself 202607280008 with count(distinct warm.day)
-- in place of count(*)). The watermark is max(usage.id) over the SAME eligible
-- set, so it cannot disagree with eligible_rows about which rows were folded in.
-- NULL for an empty period, on purpose: a period with no rows has no watermark,
-- and 0 would be a lie about row id 0.
-- ---------------------------------------------------------------------------
drop function if exists public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz
);

create function public.billing_period_settlement_evidence(
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
    warm_spend_days bigint,
    usage_log_watermark_id bigint
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
        ),
        -- 202607280013: the highest usage_log id folded into this evidence, over
        -- the SAME eligible set as every column above. It is the late-arrival
        -- detector in the settlement writer's idempotence tuple, and it is the
        -- producer public.period_settlement_ledger.usage_log_watermark_id has
        -- been waiting for since 202607280007. It is NOT an input to any fee.
        max(usage.id)::bigint
      from public.usage_log usage
     where usage.organization_id = p_organization_id
       and usage.authoritative
       and usage.pricing_status = 'priced'
       and usage.ts >= p_period_start
       and usage.ts < p_period_end;
$$;

-- DROP discarded the ACL and the comment. Restate both.
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
    'counts DISTINCT overlapping days (202607280012). usage_log_watermark_id '
    '(202607280013) is max(usage_log.id) over the same eligible set, NULL for an '
    'empty period; it is the late-arrival detector in the settlement writer''s '
    'idempotence tuple and is an input to no fee.';

-- ---------------------------------------------------------------------------
-- 2. THE WRITER. Drafts only, fee derived, guard-gated, idempotent per period.
-- ---------------------------------------------------------------------------
create or replace function public.settle_billing_period(
    p_organization_id uuid,
    p_period_anchor timestamptz,
    p_computed_by text,
    p_allow_revision boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_account public.billing_accounts%rowtype;
    v_prev public.period_settlement_ledger%rowtype;
    v_prev_found boolean := false;
    v_prev_live boolean := false;
    v_start timestamptz;
    v_end timestamptz;
    v_owner uuid;
    v_evidence record;
    v_share numeric;
    v_verified numeric;
    v_warm numeric;
    v_net_after_warm numeric;
    v_ceiling_microusd bigint;
    v_fee bigint;
    v_committed bigint;
    v_gate jsonb;
    v_revision integer;
    v_revision_of bigint;
    v_new_id bigint;
    v_message text;
    v_detail text;
    v_condition text;
    v_computed_by text;
begin
    ------------------------------------------------------------------
    -- 0. Arguments. These are programmer errors, not settlement outcomes, so
    --    they RAISE (mirroring 202607280008's own argument checks) rather than
    --    returning a body a batch loop would quietly count as "blocked".
    ------------------------------------------------------------------
    if p_organization_id is null then
        raise exception using
            errcode = '22023',
            message = 'settle_billing_period requires an organization: the netting unit is the org';
    end if;
    if p_period_anchor is null then
        raise exception using
            errcode = '22023',
            message = 'settle_billing_period requires an instant inside the period to settle';
    end if;
    v_computed_by := btrim(coalesce(p_computed_by, ''));
    if v_computed_by = '' then
        raise exception using
            errcode = '22023',
            message = 'settle_billing_period requires a non-blank computed_by: a settlement is a '
                      'seven-year financial record and must name what produced it';
    end if;

    ------------------------------------------------------------------
    -- 1. Serialize on the organization. This is the SAME advisory key
    --    claim_billing_ledger_entries uses (202607170004 around line 225,
    --    202607200006 around line 367), so settlement serialises both against a
    --    concurrent settlement of this org and against the claim path.
    ------------------------------------------------------------------
    perform pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0));

    ------------------------------------------------------------------
    -- 2. Enrollment. Mirrors the predicate the retired per-row trigger used
    --    (202607200006 around line 200) so the aggregate path cannot bill an
    --    organization the per-row path would have skipped.
    ------------------------------------------------------------------
    select * into v_account
      from public.billing_accounts account
     where account.organization_id = p_organization_id;
    if not found then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'no_billing_account',
            'organization_id', p_organization_id, 'period_anchor', p_period_anchor
        );
    end if;
    if v_account.subscription_status not in ('active', 'trialing') then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'subscription_inactive',
            'organization_id', p_organization_id, 'period_anchor', p_period_anchor,
            'subscription_status', v_account.subscription_status
        );
    end if;
    if v_account.billing_started_at is null then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'enrollment_incomplete',
            'organization_id', p_organization_id, 'period_anchor', p_period_anchor
        );
    end if;

    ------------------------------------------------------------------
    -- 3. The window. Derived ONLY from the Stripe anchor, never from local date
    --    arithmetic: billing_period_for_occurrence floors
    --    extract(epoch ...) / 604800 (202607170004 around line 84), which is
    --    what makes the boundary DST-proof and locale-proof.
    ------------------------------------------------------------------
    begin
        select period.period_start, period.period_end
          into v_start, v_end
          from public.billing_period_for_occurrence(
              p_period_anchor,
              v_account.current_period_start,
              v_account.current_period_end
          ) period;
    exception
        when invalid_parameter_value then
            return jsonb_build_object(
                'ok', false, 'outcome', 'blocked', 'code', 'invalid_anchor',
                'organization_id', p_organization_id, 'period_anchor', p_period_anchor,
                'current_period_start', v_account.current_period_start,
                'current_period_end', v_account.current_period_end
            );
    end;

    -- Settle once, after the period closes. See the header: a mid-period
    -- settlement plus a later revision is two Stripe charges against a
    -- fee_microusd that is defined as the period total.
    if v_end > now() then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_not_closed',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end
        );
    end if;

    -- billing_period_settlement_evidence does NOT filter on billing_started_at,
    -- so a week that starts before enrollment would fold pre-enrollment savings
    -- into the first fee -- savings the retired per-row trigger explicitly
    -- excluded (202607200006 around line 205). Refusing under-bills at most one
    -- partial week, which is the safe direction. Clamping the evidence window
    -- instead would produce a row whose recorded verified_savings_usd no
    -- independent recomputation reproduces; that is a revenue decision, not an
    -- engineering one.
    if v_start < v_account.billing_started_at then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_precedes_enrollment',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'billing_started_at', v_account.billing_started_at
        );
    end if;

    ------------------------------------------------------------------
    -- 4. Grid guard. If the Stripe anchor ever moves by a non-multiple of
    --    604800 seconds (proration, plan change, pause/resume), history is
    --    re-sliced and the SAME usage can be settled twice under two
    --    overlapping windows. Nothing else in the schema catches that: the
    --    live-period unique index is on period_start equality, not overlap.
    ------------------------------------------------------------------
    if exists (
        select 1
          from public.period_settlement_ledger settlement
         where settlement.organization_id = p_organization_id
           and settlement.period_start <> v_start
           and settlement.period_start < v_end
           and settlement.period_end > v_start
    ) then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_grid_shifted',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end
        );
    end if;

    ------------------------------------------------------------------
    -- 5. Attribution, snapshotted at settlement and immutable thereafter
    --    (202607280007 around line 362). The Stripe customer is deliberately
    --    NOT snapshotted; it stays resolved at claim time.
    ------------------------------------------------------------------
    select organization.billing_owner_id into v_owner
      from public.organizations organization
     where organization.id = p_organization_id;
    if v_owner is null then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'billing_owner_unavailable',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end
        );
    end if;

    ------------------------------------------------------------------
    -- 6. The highest existing revision for this (org, period), locked. Taking
    --    the row lock here is what 202607280008's TOCTOU note (around line 556)
    --    relies on, and the max-revision row is the only legal predecessor: a
    --    voided or superseded revision still occupies its slot in
    --    period_settlement_ledger_revision_idx, so a "fresh" revision 1 after a
    --    retraction would be a unique violation.
    ------------------------------------------------------------------
    select * into v_prev
      from public.period_settlement_ledger settlement
     where settlement.organization_id = p_organization_id
       and settlement.period_start = v_start
     order by settlement.revision desc
     limit 1
       for update;
    v_prev_found := found;
    v_prev_live := v_prev_found
        and v_prev.superseded_at is null
        and v_prev.status <> 'void';

    ------------------------------------------------------------------
    -- 7. EVIDENCE SNAPSHOT. One statement, so every value written onto the row
    --    and the fee derived from them come from the SAME snapshot. Mixing
    --    snapshots is the one thing that could put a fee on a row whose
    --    recorded evidence does not justify it.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(p_organization_id, v_start, v_end);

    -- Rounded to the ledger's declared scale BEFORE the fee is derived, so the
    -- number the CHECK constraint re-derives from the stored columns is the
    -- exact number the fee came from.
    v_verified := round(v_evidence.net_verified_savings_usd, 10);
    v_warm := round(greatest(v_evidence.warm_spend_usd, 0), 10);
    v_net_after_warm := greatest(v_verified - v_warm, 0);

    select conditions.max_fee_share_of_verified_savings into v_share
      from public.billing_halting_conditions conditions
     where conditions.singleton;
    if not found then
        -- The same tag 202607280008 raises for this state. Nothing is written.
        return jsonb_build_object(
            'ok', false, 'outcome', 'halted', 'code', 'unconfigured',
            'halting_condition', 'unconfigured',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'message', 'halting_condition=unconfigured: billing_halting_conditions has no row'
        );
    end if;

    -- The ceiling as 202607280008 computes it, from the UNROUNDED evidence, so
    -- the fee below can never exceed what the gate will allow for this snapshot.
    v_ceiling_microusd := floor(
        greatest(
            v_evidence.net_verified_savings_usd - greatest(v_evidence.warm_spend_usd, 0),
            0
        ) * v_share * 1000000
    )::bigint;

    -- ONE arithmetic definition, shared with the table CHECK
    -- (period_settlement_fee_microusd, 202607280007 around line 274). The
    -- least() is a belt-and-braces backstop: the two terms are provably equal
    -- while max_fee_share_of_verified_savings is 0.25, and if it is LOWERED in
    -- an incident this settles at the lowered ceiling instead of halting.
    v_fee := least(
        public.period_settlement_fee_microusd(v_net_after_warm, v_verified),
        v_ceiling_microusd
    );

    ------------------------------------------------------------------
    -- 8. IDEMPOTENCE. This is what makes an hourly sweep free, and it is
    --    checked BEFORE the guard on purpose: an unchanged period must report
    --    'unchanged' even once its own row has been promoted and reported, at
    --    which point re-running the guard for the same fee would correctly halt
    --    on the cumulative ceiling.
    ------------------------------------------------------------------
    if v_prev_live
       and v_prev.verified_savings_usd is not distinct from v_verified
       and v_prev.warm_spend_usd is not distinct from v_warm
       and v_prev.usage_row_count is not distinct from v_evidence.eligible_rows
       and v_prev.usage_log_watermark_id is not distinct from v_evidence.usage_log_watermark_id
       and v_prev.fee_microusd is not distinct from v_fee then
        return jsonb_build_object(
            'ok', true, 'outcome', 'unchanged', 'code', 'unchanged',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'settlement_id', v_prev.id, 'revision', v_prev.revision,
            'settlement_status', v_prev.status,
            'fee_microusd', v_prev.fee_microusd
        );
    end if;

    ------------------------------------------------------------------
    -- 9. Already committed to Stripe? Refuse, always.
    --
    --    'pending' MUST be in this predicate. 202607280008's own ceiling
    --    (around line 561) counts only 'sending'/'reported' OR a row carrying
    --    outbound_started_at, because at the time it was written nothing could
    --    queue a settlement at all. This function is that missing writer, so
    --    the moment promote_billing_period_settlement() moves a draft to
    --    'pending' the money IS queued: the claim path will pick it up and
    --    derive a Stripe idempotency key from the row id. Two 'pending' rows
    --    for one (org, period) therefore bill twice and Stripe deduplicates
    --    nothing, because the keys differ.
    --
    --    Inheriting 280008's narrower predicate verbatim was a real
    --    double-charge: settle -> promote -> (late receipt) -> settle with
    --    p_allow_revision -> promote again yielded two queued rows totalling
    --    70,000,000 uUSD for a period whose correct total was 47,500,000.
    --    That is the PSL-LATCH class of defect reached through the promote
    --    door rather than the void door, and it must stay closed here even
    --    though 280008 is checksum-frozen and cannot be widened in place.
    --    202607280010's latches keep the markers readable through a retraction.
    ------------------------------------------------------------------
    select coalesce(sum(settlement.fee_microusd), 0)
      into v_committed
      from public.period_settlement_ledger settlement
     where settlement.organization_id = p_organization_id
       and settlement.period_start = v_start
       and settlement.period_end = v_end
       and (settlement.status in ('pending', 'sending', 'reported')
            or settlement.outbound_started_at is not null);

    if v_committed > 0 then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_already_committed',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'committed_period_microusd', v_committed,
            'recomputed_fee_microusd', v_fee,
            'settlement_id', case when v_prev_found then v_prev.id else null end
        );
    end if;

    ------------------------------------------------------------------
    -- 10. A prior settlement exists and the evidence moved. Appending a
    --     revision is an operator act: p_allow_revision stays false in every
    --     automated caller, forever.
    ------------------------------------------------------------------
    if v_prev_found and not coalesce(p_allow_revision, false) then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_already_settled',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'settlement_id', v_prev.id, 'revision', v_prev.revision,
            'settlement_status', v_prev.status,
            'settled_fee_microusd', v_prev.fee_microusd,
            'recomputed_fee_microusd', v_fee
        );
    end if;

    ------------------------------------------------------------------
    -- 11. THE GATE. public.assert_billing_period_settlement_allowed is the ONLY
    --     door -- 202607280009 revokes EXECUTE on the inner guard from
    --     service_role so the attestation cannot be skipped -- and it runs in
    --     THIS transaction, strictly before any INSERT or UPDATE, which is what
    --     202607280008's own comment requires. A halt therefore writes nothing:
    --     no row, no partial revision chain. It is RETURNED rather than raised,
    --     matching company_billing_authorize_actor's {ok:false, code:...} shape,
    --     so one organization cannot abort a sweep.
    ------------------------------------------------------------------
    begin
        v_gate := public.assert_billing_period_settlement_allowed(
            p_organization_id, v_start, v_end, v_fee
        );
    exception
        when sqlstate '55000' or sqlstate '22023' then
            get stacked diagnostics
                v_message = message_text,
                v_detail = pg_exception_detail;
            v_condition := coalesce(
                substring(v_message from 'halting_condition=([a-z_]+)'),
                'guard_error'
            );
            return jsonb_build_object(
                'ok', false, 'outcome', 'halted', 'code', v_condition,
                'halting_condition', v_condition,
                'organization_id', p_organization_id,
                'period_start', v_start, 'period_end', v_end,
                'recomputed_fee_microusd', v_fee,
                'message', v_message,
                'detail', v_detail
            );
    end;

    ------------------------------------------------------------------
    -- 12. The 3-step correction flow, in 202607280007's documented order
    --     (around line 107). Step 1 frees the live-period unique index BEFORE
    --     the successor exists; step 3 closes the chain afterwards, and is
    --     cross-checked by prevent_period_settlement_identity_change. A retracted
    --     ('void') predecessor is stamped the same way: the slot in the revision
    --     index is already spent, so the successor must be its next revision.
    ------------------------------------------------------------------
    v_revision := coalesce(v_prev.revision, 0) + 1;
    v_revision_of := case when v_prev_found then v_prev.id else null end;

    if v_prev_found and v_prev.superseded_at is null then
        -- Belt-and-braces behind step 9. Stamping superseded_at does NOT
        -- un-queue a row: the claim path selects on status, not on
        -- superseded_at, so superseding a 'pending'/'sending'/'reported'
        -- predecessor would leave it billable while its successor bills too.
        -- Step 9 already refuses that case; if the two ever disagree, fail
        -- loudly rather than quietly queueing a second charge.
        if v_prev.status not in ('draft', 'void') then
            raise exception
                'refusing to supersede a committed settlement % in status %',
                v_prev.id, v_prev.status
                using errcode = '55000';
        end if;
        update public.period_settlement_ledger
           set superseded_at = now()
         where id = v_prev.id;
    end if;

    insert into public.period_settlement_ledger (
        organization_id, period_start, period_end,
        verified_savings_usd, warm_spend_usd,
        usage_row_count, usage_log_watermark_id,
        fee_microusd, billing_owner_id,
        status, computed_by,
        revision, revision_of, last_error
    ) values (
        p_organization_id, v_start, v_end,
        -- SIGNED. A losing week must be representable; net_savings_usd is the
        -- generated column that floors it, and the writer must not pre-floor it.
        v_verified,
        v_warm,
        v_evidence.eligible_rows,
        v_evidence.usage_log_watermark_id,
        v_fee, v_owner,
        -- 'draft' ALWAYS. There is no code path in this function to any other
        -- status, which is what makes the service_role grant safe.
        'draft', left(v_computed_by, 200),
        v_revision, v_revision_of, ''
    ) returning id into v_new_id;

    if v_prev_found and v_prev.superseded_by is null then
        update public.period_settlement_ledger
           set superseded_by = v_new_id
         where id = v_prev.id;
    end if;

    return jsonb_build_object(
        'ok', true,
        'outcome', case when v_prev_found then 'revised' else 'settled' end,
        'code', case when v_prev_found then 'revised' else 'settled' end,
        'organization_id', p_organization_id,
        'period_start', v_start, 'period_end', v_end,
        'settlement_id', v_new_id, 'revision', v_revision,
        'revision_of', v_revision_of,
        'settlement_status', 'draft',
        'fee_microusd', v_fee,
        'fee_ceiling_microusd', v_ceiling_microusd,
        'committed_period_microusd', v_committed,
        'verified_savings_usd', v_verified,
        'warm_spend_usd', v_warm,
        'usage_row_count', v_evidence.eligible_rows,
        'usage_log_watermark_id', v_evidence.usage_log_watermark_id,
        'billing_arrangement', v_gate->>'billing_arrangement',
        'evidence', v_gate
    );
end;
$$;

revoke all on function public.settle_billing_period(uuid, timestamptz, text, boolean)
    from public, anon, authenticated;
grant execute on function public.settle_billing_period(uuid, timestamptz, text, boolean)
    to service_role;

comment on function public.settle_billing_period(uuid, timestamptz, text, boolean) is
    'Computes and records ONE draft settlement for the closed Stripe week '
    'containing p_period_anchor. Takes no fee, status, or evidence argument: '
    'every money field is derived from public.billing_period_settlement_evidence '
    'and public.period_settlement_fee_microusd, capped by the halting-condition '
    'ceiling, and gated by public.assert_billing_period_settlement_allowed in the '
    'same transaction before any write. Produces status = ''draft'' only, so it '
    'moves no money and is safe to grant to service_role; promotion is '
    'public.promote_billing_period_settlement, which is granted to nobody. '
    'Idempotent per (organization, period): an unchanged recomputation returns '
    'outcome=unchanged and writes nothing. Refuses unconditionally once the '
    'period has committed money to Stripe. Returns {ok, outcome, code, ...}; '
    'halts and blocks are RETURNED, not raised, so a batch is not aborted by one '
    'organization -- a caller MUST branch on outcome/code and must never treat a '
    'successful call as a successful settlement.';

-- ---------------------------------------------------------------------------
-- 3. THE PROMOTER. draft -> pending: the single money-moving door.
--    Granted to NOBODY. Reachable only from a direct SQL session, exactly like
--    attesting an organization (202607280009). Automating weekly promotion is a
--    follow-on, reviewed migration that adds one GRANT -- not a change here.
-- ---------------------------------------------------------------------------
create or replace function public.promote_billing_period_settlement(
    p_settlement_id bigint,
    p_actor text,
    p_note text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_row public.period_settlement_ledger%rowtype;
    v_actor text;
    v_note text;
    v_gate jsonb;
    v_message text;
    v_detail text;
    v_condition text;
begin
    if p_settlement_id is null then
        raise exception using
            errcode = '22023',
            message = 'promote_billing_period_settlement requires a settlement id';
    end if;
    v_actor := btrim(coalesce(p_actor, ''));
    v_note := btrim(coalesce(p_note, ''));
    -- Same note floor manual billing resolution uses (202607170004 around line
    -- 447): promotion is the act that bills a customer and must carry a reason.
    if v_actor = '' or char_length(v_note) < 12 then
        raise exception using
            errcode = '22023',
            message = 'promote_billing_period_settlement requires a named actor and a note of at '
                      'least 12 characters: this is the act that makes a period billable';
    end if;

    select * into v_row
      from public.period_settlement_ledger settlement
     where settlement.id = p_settlement_id;
    if not found then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'settlement_not_found',
            'settlement_id', p_settlement_id
        );
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_row.organization_id::text, 0));

    select * into v_row
      from public.period_settlement_ledger settlement
     where settlement.id = p_settlement_id
       for update;

    if v_row.status <> 'draft' then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'not_a_draft',
            'settlement_id', v_row.id, 'settlement_status', v_row.status
        );
    end if;
    if v_row.superseded_at is not null then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'settlement_superseded',
            'settlement_id', v_row.id
        );
    end if;
    -- Close the ONE gap in the cumulative ceiling this function re-runs below.
    -- 202607280008's predicate (around line 567) is checksum-frozen and counts
    -- only 'sending'/'reported' OR a row carrying outbound_started_at, because
    -- when it was written nothing could queue a settlement. It therefore sees
    -- committed=0 for a period that already has a 'pending' sibling — and a
    -- 'pending' row is already money: the claim path will send it and derive a
    -- Stripe idempotency key from its row id, so two pending rows bill twice
    -- and Stripe deduplicates nothing.
    --
    -- Deliberately scoped to 'pending' ONLY. Every other committed state is
    -- genuinely caught by the ceiling re-run, and that re-run is this
    -- function's designed second line of defense; short-circuiting it here
    -- would silently delete that coverage.
    perform 1
       from public.period_settlement_ledger other
      where other.organization_id = v_row.organization_id
        and other.period_start = v_row.period_start
        and other.period_end = v_row.period_end
        and other.id <> v_row.id
        and other.status = 'pending';
    if found then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_already_committed',
            'settlement_id', v_row.id,
            'organization_id', v_row.organization_id,
            'period_start', v_row.period_start, 'period_end', v_row.period_end
        );
    end if;
    if v_row.billing_owner_id is null then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'billing_owner_unavailable',
            'settlement_id', v_row.id
        );
    end if;
    -- A zero fee stays 'draft' forever, as its own record that the week settled
    -- at $0. Never fake a 'reported' for a period nothing was sent for.
    if v_row.fee_microusd <= 0 then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'nothing_to_report',
            'settlement_id', v_row.id, 'fee_microusd', v_row.fee_microusd
        );
    end if;
    -- The settlement claim path maps occurred_at := period_end (202607280007
    -- around line 71), and the pending sweep expires anything older than 34 days
    -- (202607170004 around line 186). Promoting past that only manufactures an
    -- 'expired' row.
    if v_row.period_end < now() - interval '34 days' then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'reporting_window_elapsed',
            'settlement_id', v_row.id,
            'period_start', v_row.period_start, 'period_end', v_row.period_end
        );
    end if;

    -- NON-NEGOTIABLE: re-run the guard for the amount actually about to be
    -- billed. Both the evidence and the attestation can have moved since the
    -- draft was computed, and this is the last moment either can stop the money.
    begin
        v_gate := public.assert_billing_period_settlement_allowed(
            v_row.organization_id, v_row.period_start, v_row.period_end, v_row.fee_microusd
        );
    exception
        when sqlstate '55000' or sqlstate '22023' then
            get stacked diagnostics
                v_message = message_text,
                v_detail = pg_exception_detail;
            v_condition := coalesce(
                substring(v_message from 'halting_condition=([a-z_]+)'),
                'guard_error'
            );
            return jsonb_build_object(
                'ok', false, 'outcome', 'halted', 'code', v_condition,
                'halting_condition', v_condition,
                'settlement_id', v_row.id,
                'organization_id', v_row.organization_id,
                'period_start', v_row.period_start, 'period_end', v_row.period_end,
                'fee_microusd', v_row.fee_microusd,
                'message', v_message, 'detail', v_detail
            );
    end;

    -- Touches no frozen column and no latched column: only status, the retry
    -- schedule, and the audit string.
    update public.period_settlement_ledger
       set status = 'pending',
           next_attempt_at = now(),
           last_error = left('promoted by ' || v_actor || ': ' || v_note, 500)
     where id = v_row.id;

    return jsonb_build_object(
        'ok', true, 'outcome', 'promoted', 'code', 'promoted',
        'settlement_id', v_row.id,
        'organization_id', v_row.organization_id,
        'period_start', v_row.period_start, 'period_end', v_row.period_end,
        'revision', v_row.revision,
        'fee_microusd', v_row.fee_microusd,
        'settlement_status', 'pending',
        'billing_arrangement', v_gate->>'billing_arrangement',
        'evidence', v_gate
    );
end;
$$;

-- Granted to NOBODY, including service_role. Promotion is an out-of-band human
-- act; a leaked service key must not be able to move a single micro-dollar
-- toward Stripe.
revoke all on function public.promote_billing_period_settlement(bigint, text, text)
    from public, anon, authenticated, service_role;

comment on function public.promote_billing_period_settlement(bigint, text, text) is
    'Promotes ONE draft settlement to ''pending'', which is the deliberate manual '
    'act that makes a period billable (202607280007). Deliberately granted to NO '
    'role -- not even service_role -- so it is reachable only from a direct SQL '
    'session, exactly like attesting an organization in '
    'public.organization_billing_arrangement. Requires a named actor and a '
    '12-character note, refuses a zero fee (nothing_to_report), refuses a period '
    'past Stripe''s 34-day reporting window, and RE-RUNS '
    'public.assert_billing_period_settlement_allowed for the exact amount about '
    'to be billed, because evidence and attestation can both have moved since the '
    'draft was computed. Automating weekly promotion means adding one reviewed '
    'GRANT plus a sweep; do not smuggle it in with the writer.';

-- ---------------------------------------------------------------------------
-- 4. THE READER for /api/billing/status.
--
--    The route cannot select period_settlement_ledger: 202607280007 revokes
--    every privilege from service_role and two CI files assert it. This is the
--    whole read path, and it degrades instead of 500-ing when the guard is
--    unavailable, because a billing screen that errors is better than a billing
--    screen that shows a confident wrong number, and both are worse than
--    "Unavailable".
--
--    estimated_fee_microusd is an evidence PROJECTION, not a ledger read. FIX-5
--    says to exclude draft/void from the estimate, and applied literally to the
--    LIVE period that returns 0 forever -- settlement only happens after a
--    period closes, so the current period has no non-draft row, which is the
--    identical confident-wrong $0 FIX-5 exists to kill. The projection uses the
--    same arithmetic the writer will use, so it converges exactly to the settled
--    fee when the week closes. The ledger-derived number is reported separately
--    as settled_fee_microusd, which is where FIX-5's rule actually belongs.
-- ---------------------------------------------------------------------------
create or replace function public.billing_period_settlement_summary(
    p_organization_id uuid,
    p_period_start timestamptz
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_account public.billing_accounts%rowtype;
    v_start timestamptz;
    v_end timestamptz;
    v_gate jsonb;
    v_estimate bigint;
    v_ceiling bigint;
    v_arrangement text;
    v_settled bigint;
    v_reported bigint;
    v_committed bigint;
    v_review bigint;
    v_attention bigint;
    v_status text;
    v_id bigint;
    v_revision integer;
begin
    if p_organization_id is null or p_period_start is null then
        return jsonb_build_object('ok', false, 'code', 'invalid_arguments');
    end if;

    select * into v_account
      from public.billing_accounts account
     where account.organization_id = p_organization_id;
    if not found then
        return jsonb_build_object(
            'ok', false, 'code', 'no_billing_account',
            'organization_id', p_organization_id
        );
    end if;

    v_start := v_account.current_period_start;
    v_end := v_account.current_period_end;
    -- The same fail-closed anchor test the route applies in milliseconds, and
    -- the reason MU8 renders "Unavailable" instead of a number.
    if v_start is null or v_end is null
       or v_end - v_start <> interval '7 days'
       or date_trunc('milliseconds', p_period_start) is distinct from
          date_trunc('milliseconds', v_start) then
        return jsonb_build_object(
            'ok', false, 'code', 'period_anchor_mismatch',
            'organization_id', p_organization_id,
            'requested_period_start', p_period_start,
            'current_period_start', v_start,
            'current_period_end', v_end
        );
    end if;

    -- A zero fee always passes attestation and every ceiling, so this is a pure
    -- evidence read that also reports the arrangement. A missing thresholds row
    -- must degrade the endpoint, not break it.
    begin
        v_gate := public.assert_billing_period_settlement_allowed(
            p_organization_id, v_start, v_end, 0::bigint
        );
    exception
        when others then
            return jsonb_build_object(
                'ok', false, 'code', 'guard_unavailable',
                'organization_id', p_organization_id,
                'period_start', v_start, 'period_end', v_end
            );
    end;

    v_ceiling := coalesce((v_gate->>'fee_ceiling_microusd')::bigint, 0);
    v_arrangement := v_gate->>'billing_arrangement';
    v_estimate := least(
        public.period_settlement_fee_microusd(
            coalesce((v_gate->>'net_after_warm_savings_usd')::numeric, 0),
            coalesce((v_gate->>'net_verified_savings_usd')::numeric, 0)
        ),
        v_ceiling
    );

    select
        coalesce(sum(settlement.fee_microusd) filter (
            where settlement.status = 'reported'), 0),
        coalesce(max(settlement.fee_microusd) filter (
            where settlement.superseded_at is null
              and settlement.status not in ('draft', 'void')), 0),
        -- Committed money uses THE WRITER'S OWN refusal predicate (step 9 of
        -- settle_billing_period, above): the moment a draft is promoted to
        -- 'pending' the money IS queued, so a promoted-but-unsent period must
        -- report its fee as committed to the customer, not zero. 202607280008's
        -- narrower 'sending'/'reported' bucket exists only because nothing could
        -- queue a settlement when it was written.
        coalesce(sum(settlement.fee_microusd) filter (
            where settlement.status in ('pending', 'sending', 'reported')
               or settlement.outbound_started_at is not null), 0),
        count(*) filter (where settlement.status = 'review'),
        count(*) filter (where settlement.status in ('capped', 'expired', 'dead')),
        coalesce(max(settlement.status) filter (
            where settlement.superseded_at is null
              and settlement.status <> 'void'), 'accruing'),
        max(settlement.id) filter (
            where settlement.superseded_at is null and settlement.status <> 'void'),
        max(settlement.revision) filter (
            where settlement.superseded_at is null and settlement.status <> 'void')
      into v_reported, v_settled, v_committed, v_review, v_attention,
           v_status, v_id, v_revision
      from public.period_settlement_ledger settlement
     where settlement.organization_id = p_organization_id
       and settlement.period_start = v_start;

    return jsonb_build_object(
        'ok', true,
        'organization_id', p_organization_id,
        'period_start', v_start,
        'period_end', v_end,
        -- 'accruing' is the NORMAL state of the current period: nothing settles
        -- until the week closes.
        'settlement_status', v_status,
        'settlement_id', v_id,
        'revision', v_revision,
        'estimated_fee_microusd', v_estimate,
        'settled_fee_microusd', v_settled,
        'reported_fee_microusd', v_reported,
        'committed_fee_microusd', v_committed,
        'needs_review_count', v_review,
        'attention_count', v_attention,
        'billing_arrangement', v_arrangement,
        'billable', v_arrangement = 'marginal_per_call',
        'evidence', jsonb_build_object(
            'eligible_rows', v_gate->'eligible_rows',
            'net_verified_savings_usd', v_gate->'net_verified_savings_usd',
            'net_after_warm_savings_usd', v_gate->'net_after_warm_savings_usd',
            'warm_spend_usd', v_gate->'warm_spend_usd',
            'warm_spend_days', v_gate->'warm_spend_days',
            'actual_spend_usd', v_gate->'actual_spend_usd',
            'zero_spend_share', v_gate->'zero_spend_share',
            'fee_ceiling_microusd', v_ceiling
        )
    );
end;
$$;

revoke all on function public.billing_period_settlement_summary(uuid, timestamptz)
    from public, anon, authenticated;
grant execute on function public.billing_period_settlement_summary(uuid, timestamptz)
    to service_role;

comment on function public.billing_period_settlement_summary(uuid, timestamptz) is
    'Read path for /api/billing/status. Exists because the route CANNOT select '
    'public.period_settlement_ledger -- 202607280007 revokes every privilege '
    'from service_role and scripts/ci asserts it, so a `grant select` to make a '
    'PostgREST read work would dismantle the Phase 4 privilege model. '
    'estimated_fee_microusd is an evidence PROJECTION for the live week, not a '
    'ledger read: settlement only happens after a period closes, so a ledger '
    'read would be $0 for every customer forever, which is the exact wrong-money '
    'display FIX-5 exists to prevent. settled_fee_microusd is the ledger number '
    '(live row, excluding draft and void). Returns {ok:false, code} for '
    'no_billing_account / period_anchor_mismatch / guard_unavailable; the caller '
    'must render null, never zero.';

-- ---------------------------------------------------------------------------
-- 5. Enumeration for the shadow sweep, so no caller does week arithmetic.
--
--    n stops at 4 because n = 4 puts period_end at worst now() - 28 days, still
--    inside the 34-day Stripe reporting window the promoter enforces. Offsets
--    are written n * interval '604800 seconds', never n * interval '7 days':
--    for timestamptz the latter is DST-aware wall-clock arithmetic and would
--    walk off the Stripe grid across a transition.
-- ---------------------------------------------------------------------------
create or replace function public.billing_periods_awaiting_settlement(
    p_limit integer default 50
)
returns table (
    organization_id uuid,
    period_start timestamptz,
    period_end timestamptz
)
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_limit integer;
begin
    v_limit := least(greatest(coalesce(p_limit, 50), 1), 500);
    return query
    with eligible as materialized (
        select account.organization_id as org_id,
               account.billing_started_at as started_at,
               account.current_period_start as anchor_start,
               account.current_period_end as anchor_end
          from public.billing_accounts account
         where account.subscription_status in ('active', 'trialing')
           and account.billing_started_at is not null
           and account.current_period_start is not null
           and account.current_period_end is not null
           and account.current_period_end - account.current_period_start = interval '7 days'
    ),
    windows as (
        select eligible.org_id,
               eligible.started_at,
               period.period_start as window_start,
               period.period_end as window_end
          from eligible
          cross join generate_series(1, 4) as offsets(n)
          cross join lateral public.billing_period_for_occurrence(
              eligible.anchor_start - offsets.n * interval '604800 seconds',
              eligible.anchor_start,
              eligible.anchor_end
          ) period
    )
    select windows.org_id, windows.window_start, windows.window_end
      from windows
     where windows.window_start >= windows.started_at
       and windows.window_end <= now()
       and not exists (
           select 1
             from public.period_settlement_ledger settlement
            where settlement.organization_id = windows.org_id
              and settlement.period_start = windows.window_start
              and settlement.superseded_at is null
              and settlement.status <> 'void'
       )
     order by windows.org_id, windows.window_start
     limit v_limit;
end;
$$;

revoke all on function public.billing_periods_awaiting_settlement(integer)
    from public, anon, authenticated;
grant execute on function public.billing_periods_awaiting_settlement(integer)
    to service_role;

comment on function public.billing_periods_awaiting_settlement(integer) is
    'Closed Stripe weeks (up to four back) with no live settlement, for the '
    'shadow settlement sweep. Every window comes from '
    'public.billing_period_for_occurrence so no caller does date arithmetic, and '
    'offsets are 604800-second multiples rather than ''7 days'' so the grid '
    'survives a DST transition. Four back keeps every candidate inside the '
    '34-day Stripe reporting window the promoter enforces.';

-- ---------------------------------------------------------------------------
-- Self-verifying contract assertions.
--
-- The behavioural half needs organizations / auth.users / billing_accounts /
-- usage_log / period_settlement_ledger fixtures, and
-- public.period_settlement_ledger carries an UNCONDITIONAL BEFORE DELETE guard,
-- so a fixture written here could never be cleaned up with a DELETE. It is
-- therefore built inside an inner block that is deliberately unwound by raising
-- a private SQLSTATE, exactly as 202607280010 does, so the probe observes real
-- behaviour and leaves nothing behind in a seven-year financial record. (The
-- identity sequence advances; nothing reads its position -- 202607280007 asserts
-- the sequence's declared START, not where it is.)
--
-- Every window below is relative to now() rather than a fixed calendar date, so
-- the probe cannot rot: period_not_closed and the promoter's 34-day reporting
-- window are both now()-relative conditions.
-- ---------------------------------------------------------------------------
do $settlement_writer_contract$
declare
    v_org uuid;
    v_owner uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_warm_day date;
    v_result jsonb;
    v_second jsonb;
    v_settlement_id bigint;
    v_row public.period_settlement_ledger%rowtype;
    v_committed bigint;
    v_count bigint;
    v_role text;
    v_privilege text;
begin
    ------------------------------------------------------------------
    -- Structural: privilege posture. Runtime may compute, only a human may bill.
    ------------------------------------------------------------------
    if not has_function_privilege('service_role',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE') then
        raise exception '202607280013 did not leave the settlement writer callable by service_role';
    end if;
    if has_function_privilege('service_role',
           'public.promote_billing_period_settlement(bigint,text,text)', 'EXECUTE') then
        raise exception '202607280013 let a runtime role promote a settlement into billability; '
                        'promotion must stay an out-of-band human act';
    end if;
    if not has_function_privilege('service_role',
           'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE') then
        raise exception '202607280013 did not leave the status summary callable by service_role';
    end if;
    if not has_function_privilege('service_role',
           'public.billing_periods_awaiting_settlement(integer)', 'EXECUTE') then
        raise exception '202607280013 did not leave the sweep enumeration callable by service_role';
    end if;
    for v_role in select unnest(array['anon', 'authenticated']) loop
        if has_function_privilege(v_role,
               'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.promote_billing_period_settlement(bigint,text,text)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.billing_periods_awaiting_settlement(integer)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)',
               'EXECUTE') then
            raise exception '202607280013 exposed a settlement function to browser role %', v_role;
        end if;
    end loop;

    -- Not one table privilege was widened. The status route reads the RPC.
    for v_role in select unnest(array['anon', 'authenticated', 'service_role']) loop
        for v_privilege in select unnest(array['select', 'insert', 'update', 'delete',
                                               'truncate', 'references', 'trigger']) loop
            if has_table_privilege(v_role, 'public.period_settlement_ledger', v_privilege) then
                raise exception '202607280013 granted % on period_settlement_ledger to %; every '
                                'write must stay behind a definer RPC',
                    v_privilege, v_role;
            end if;
        end loop;
    end loop;

    -- The inner attestation-free guard must still be unreachable at runtime, or
    -- a writer could be re-pointed at it and skip the attestation.
    if has_function_privilege('service_role',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception '202607280013 left the attestation-free inner guard reachable at runtime';
    end if;

    -- No trigger anywhere may reach the writer or the table.
    if (
        select count(*)
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.period_settlement_ledger'::regclass
           and not trigger_state.tgisinternal
    ) <> 2 then
        raise exception '202607280013 changed the trigger set on period settlements';
    end if;
    if exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid = trigger_state.tgfoid
         where not trigger_state.tgisinternal
           and trigger_state.tgrelid <> 'public.period_settlement_ledger'::regclass
           and (coalesce(trigger_function.prosrc, '') like '%period_settlement_ledger%'
                or coalesce(trigger_function.prosrc, '') like '%settle_billing_period%')
    ) then
        raise exception '202607280013 an external trigger writes period settlements; settlement '
                        'must never fire from row ingestion';
    end if;

    -- The evidence function kept its shape and gained exactly one column.
    if (
        select count(*)
          from pg_proc evidence_function
         where evidence_function.oid = to_regprocedure(
                   'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
           and evidence_function.prosrc like '%count(distinct warm.day)%'
           and evidence_function.prosrc like '%sum(greatest(warm.spent_usd, 0))%'
           and evidence_function.prosrc like '%max(usage.id)%'
           and evidence_function.prosrc like '%usage.authoritative%'
           and evidence_function.prosrc like '%usage.pricing_status = ''priced''%'
    ) <> 1 then
        raise exception '202607280013 did not preserve the 202607280012 evidence body while '
                        'adding the usage_log watermark';
    end if;

    ------------------------------------------------------------------
    -- Behavioural. Fixture: verified 100, list-price spend 40, warm 10, so
    -- net-after-warm is 90 and the fee is 90 * 0.25 * 1e6 = 22,500,000 microUSD.
    ------------------------------------------------------------------
    begin
        v_end := date_trunc('hour', now()) - interval '1 hour';
        v_start := v_end - interval '604800 seconds';
        v_warm_day := ((v_start at time zone 'UTC') + interval '1 day')::date;

        v_owner := gen_random_uuid();
        insert into auth.users (id, email)
        values (v_owner, '202607280013-settlement-writer-probe@example.invalid');
        insert into public.organizations (name, billing_owner_id)
        values ('202607280013 settlement writer contract probe', v_owner)
        returning id into v_org;
        -- The anchor is the NEXT period, so the probe window is the week that
        -- just closed. billing_period_for_occurrence reconstructs it by floor
        -- division; nothing here does local date arithmetic.
        insert into public.billing_accounts (
            organization_id, user_id, subscription_status, billing_started_at,
            current_period_start, current_period_end, stripe_customer_id
        ) values (
            v_org, v_owner, 'active', v_start - interval '1 day',
            v_end, v_end + interval '604800 seconds', 'cus_202607280013probe'
        );
        insert into public.usage_log (
            key_hash, owner_id, organization_id, ts, request_id, project,
            verified_savings_usd, actual_cost_usd, baseline_cost_usd,
            authoritative, pricing_status, receipt_source
        ) values (
            'settlement-writer-probe', 'settlement-writer-probe', v_org,
            v_start + interval '1 hour', 'settlement-writer-probe-1', 'probe',
            100, 40, 140, true, 'priced', 'proxy'
        );
        insert into public.warm_budget_ledger (organization_id, provider, day, spent_usd)
        values (v_org, 'anthropic', v_warm_day, 10);

        -- 1. An unattested organization cannot be settled for a positive fee,
        --    and the writer names the condition rather than swallowing it.
        v_result := public.settle_billing_period(v_org, v_start + interval '1 hour', 'probe');
        if v_result->>'outcome' <> 'halted'
           or v_result->>'halting_condition' <> 'unattested_billing_arrangement' then
            raise exception '202607280013 settled an unattested organization: %', v_result;
        end if;
        if exists (select 1 from public.period_settlement_ledger where organization_id = v_org) then
            raise exception '202607280013 wrote a settlement row on a halt';
        end if;

        insert into public.organization_billing_arrangement
            (organization_id, arrangement, attested_by, attested_evidence)
        values (v_org, 'marginal_per_call', '202607280013 contract', 'probe fixture');

        -- 2. An open period is never settled: a revision is a second Stripe
        --    charge, so a mid-period settlement would break reconciliation.
        v_result := public.settle_billing_period(v_org, now(), 'probe');
        if v_result->>'code' <> 'period_not_closed' then
            raise exception '202607280013 settled a period that has not closed: %', v_result;
        end if;

        -- 3. The clean settlement: exactly one draft row at the 25% fee.
        v_result := public.settle_billing_period(v_org, v_start + interval '1 hour', 'probe');
        if v_result->>'outcome' <> 'settled'
           or (v_result->>'fee_microusd')::bigint <> 22500000 then
            raise exception '202607280013 did not settle 25%% of net savings: %', v_result;
        end if;
        v_settlement_id := (v_result->>'settlement_id')::bigint;
        select count(*) into v_count
          from public.period_settlement_ledger
         where organization_id = v_org;
        if v_count <> 1 then
            raise exception '202607280013 wrote % settlement rows for one period', v_count;
        end if;
        select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
        if v_row.status <> 'draft' then
            raise exception '202607280013 wrote a settlement in status %; the writer must only '
                            'ever produce drafts', v_row.status;
        end if;
        if v_row.period_start <> v_start or v_row.period_end <> v_end then
            raise exception '202607280013 settled the wrong window: [%,%)',
                v_row.period_start, v_row.period_end;
        end if;
        if v_row.verified_savings_usd <> 100 or v_row.warm_spend_usd <> 10
           or v_row.net_savings_usd <> 90 or v_row.usage_row_count <> 1
           or v_row.usage_log_watermark_id is null
           or v_row.billing_owner_id <> v_owner or v_row.revision <> 1
           or v_row.revision_of is not null then
            raise exception '202607280013 recorded the wrong evidence: %', v_row;
        end if;

        -- 4. Idempotence: the second identical call writes NOTHING.
        v_second := public.settle_billing_period(v_org, v_start + interval '2 hours', 'probe');
        if v_second->>'outcome' <> 'unchanged'
           or (v_second->>'settlement_id')::bigint <> v_settlement_id then
            raise exception '202607280013 is not idempotent per period: %', v_second;
        end if;
        select count(*) into v_count
          from public.period_settlement_ledger
         where organization_id = v_org;
        if v_count <> 1 then
            raise exception '202607280013 double-settled a period: % rows', v_count;
        end if;

        -- 5. A changed period refuses to revise itself without an operator.
        insert into public.usage_log (
            key_hash, owner_id, organization_id, ts, request_id, project,
            verified_savings_usd, actual_cost_usd, baseline_cost_usd,
            authoritative, pricing_status, receipt_source
        ) values (
            'settlement-writer-probe', 'settlement-writer-probe', v_org,
            v_start + interval '2 hours', 'settlement-writer-probe-2', 'probe',
            20, 8, 28, true, 'priced', 'proxy'
        );
        v_result := public.settle_billing_period(v_org, v_start + interval '1 hour', 'probe');
        if v_result->>'code' <> 'period_already_settled'
           or (v_result->>'recomputed_fee_microusd')::bigint <> 27500000 then
            raise exception '202607280013 auto-revised a settled period: %', v_result;
        end if;

        -- 6. The operator revision appends, in the documented 3-step order.
        v_result := public.settle_billing_period(
            v_org, v_start + interval '1 hour', 'operator:probe', true
        );
        if v_result->>'outcome' <> 'revised'
           or (v_result->>'revision')::integer <> 2
           or (v_result->>'fee_microusd')::bigint <> 27500000 then
            raise exception '202607280013 did not append a correct revision: %', v_result;
        end if;
        select * into v_row from public.period_settlement_ledger where id = v_settlement_id;
        if v_row.superseded_at is null
           or v_row.superseded_by <> (v_result->>'settlement_id')::bigint then
            raise exception '202607280013 left the revision chain open on the predecessor';
        end if;

        -- 7. Once the period has committed money to Stripe, the writer refuses
        --    unconditionally -- including through a 202607280010-style
        --    marker-preserving void, which is the void-and-rebill defense.
        update public.period_settlement_ledger
           set status = 'sending', outbound_started_at = now(),
               lease_owner = 'probe', lease_expires_at = now() + interval '5 minutes'
         where id = (v_result->>'settlement_id')::bigint;
        update public.period_settlement_ledger
           set status = 'reported', reported_at = now(), settled_at = now()
         where id = (v_result->>'settlement_id')::bigint;
        update public.period_settlement_ledger
           set status = 'void', last_error = 'operator retraction'
         where id = (v_result->>'settlement_id')::bigint;

        select coalesce(sum(fee_microusd), 0) into v_committed
          from public.period_settlement_ledger
         where organization_id = v_org
           and period_start = v_start
           and (status in ('sending', 'reported') or outbound_started_at is not null);
        if v_committed <> 27500000 then
            raise exception '202607280013 lost the committed period fee through a void: %',
                v_committed;
        end if;

        v_second := public.settle_billing_period(
            v_org, v_start + interval '1 hour', 'operator:probe', true
        );
        if v_second->>'code' <> 'period_already_committed' then
            raise exception '202607280013 re-billed a period Stripe may already hold: %', v_second;
        end if;
        select count(*) into v_count
          from public.period_settlement_ledger
         where organization_id = v_org;
        if v_count <> 2 then
            raise exception '202607280013 wrote a row after refusing a committed period';
        end if;

        raise exception using
            errcode = 'ZZ013',
            message = '202607280013 settlement-writer probe complete; unwinding its fixture';
    exception
        when sqlstate 'ZZ013' then null;
    end;

    if exists (select 1 from public.period_settlement_ledger where organization_id = v_org) then
        raise exception '202607280013 left a settlement probe row behind';
    end if;
    if exists (select 1 from public.organizations where id = v_org) then
        raise exception '202607280013 left an organization probe row behind';
    end if;
end;
$settlement_writer_contract$;

commit;

-- ROLLBACK PROCEDURE
--
-- Dropping the writer leaves any already-written 'draft' rows in place, which is
-- correct: they are seven-year financial records and a draft bills nothing.
--
-- 1. DROP FUNCTION IF EXISTS public.billing_periods_awaiting_settlement(integer);
-- 2. DROP FUNCTION IF EXISTS public.billing_period_settlement_summary(uuid,timestamptz);
--    (do this only together with reverting src/app/api/billing/status/route.ts to
--    its SETTLEMENT_PENDING nulls, or the endpoint 500s.)
-- 3. DROP FUNCTION IF EXISTS public.promote_billing_period_settlement(bigint,text,text);
-- 4. DROP FUNCTION IF EXISTS public.settle_billing_period(uuid,timestamptz,text,boolean);
-- 5. Restore the narrower evidence function: DROP FUNCTION IF EXISTS
--    public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz); then
--    re-run the CREATE OR REPLACE FUNCTION body from
--    supabase/migrations/202607280012_settlement_evidence_warm_days.sql verbatim
--    (its checksum is frozen in scripts/ci/verify-migrations.mjs, so the exact
--    text is recoverable), followed by that file's REVOKE/GRANT pair. Any
--    settlement row already carrying usage_log_watermark_id keeps it; the column
--    has existed since 202607280007 and simply loses its producer again.
