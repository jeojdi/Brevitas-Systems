-- Phase 4 of the billing correctness rollout: the halting conditions.
--
-- These are the preconditions for lifting the Phase 0 freeze
-- (202607280006_retire_per_row_fee_trigger.sql). Phase 0 made "no accidental
-- invoice" structural by removing the only writer into billing_ledger. That
-- freeze is safe but useless — it stops correct settlement as well as incorrect
-- settlement. It comes off only once something *else* is capable of stopping
-- money from moving. This migration is that something else.
--
-- Three circuit breakers. The first two are deliberately not derived from each
-- other and read only primary usage evidence (usage_log, warm_budget_ledger);
-- the third is the only one that reads the settlement ledger, and it exists
-- precisely because the first two cannot:
--
--   1. PER-ORG RELATIVE CEILING. A period's fee may never exceed a bounded
--      fraction of that same org's own verified savings for that same period.
--      The fraction is 0.25, matching the contracted rate introduced in
--      20260716_stripe_billing_rate_25pct.sql, where the retired per-row
--      trigger computed
--          least(brevitas_fee_usd, verified_savings_usd * 0.25).
--      Choosing anything larger would let the aggregate path bill more than the
--      per-row path it replaces; choosing anything smaller would silently
--      under-bill a correct settlement. So the ceiling is exactly the rate, and
--      the constant is bounded at 0.25 by a CHECK constraint (below) so that it
--      can be lowered in an incident but never raised without a new migration.
--
--      Savings are NETTED across the period before the fraction is applied, and
--      the net is floored at zero. This is the whole reason the per-row ledger
--      was retired: a week whose net savings are negative must bill $0, not the
--      sum of its positive rows. There is no deficit carry-forward.
--
--      The net has TWO terms, and both are recomputed here from primary
--      evidence — the ceiling is worthless if either operand is taken on the
--      writer's word. 202607280007 makes warm-ping spend a 100% deduction
--      applied BEFORE the 25% rate, but on that table both verified_savings_usd
--      and warm_spend_usd are writer-supplied columns arriving in the same
--      INSERT, and warm_spend_usd defaults to 0. A writer that simply omits it
--      (or whose day-overlap query returns 0 on a boundary week) states a net
--      that is too high, and a check that only recomputed the savings half would
--      wave it through: $1,000 verified against $400 of warm spend owes $150 but
--      would settle at $250, a 67% overcharge on savings we manufactured with
--      the customer's own money. So the warm deduction is recomputed here from
--      public.warm_budget_ledger, over the UTC days overlapping the period, and
--      subtracted before the fraction is applied.
--
--      Warm spend cannot net itself out of the savings sum: warm-ping receipts
--      carry verified_savings_usd = 0 (api/server.py excludes strategy
--      'cache_warm' from verification), and every writer clamps that column
--      non-negative, so sum(verified_savings_usd) is always >= 0. Warm spend is
--      the only negative term in this design, which is exactly why it has to be
--      read from its own ledger rather than inferred from usage_log.
--
--      The day-overlap window is deliberately generous: warm_budget_ledger is
--      day-keyed while Stripe periods are timestamp-anchored, so a boundary day
--      is deducted by BOTH adjacent periods, matching the rule stated in
--      202607280007. Over-deducting only ever lowers a ceiling.
--
--   2. ZERO-SPEND CONCENTRATION. Refuse to settle when the period's savings are
--      concentrated in rows whose RECORDED actual_cost_usd is zero or absent. A
--      row that saved money but recorded no spend has no marginal dollar
--      visible behind it, and billing a percentage of a dollar that was never
--      going to be spent is the worst failure mode available to this system.
--
--      SCOPE, precisely: this condition sees only what usage_log records, and
--      usage_log.actual_cost_usd is PUBLISHED LIST PRICE — brevitas/receipts.py
--      calculate_costs() resolves it through model_price(provider, model) from
--      the static MODEL_PRICES table, which has no organization dimension. So
--      this condition does NOT and CANNOT detect a PTU / committed-throughput /
--      Enterprise-agreement customer: their calls price at list like anyone
--      else's, giving a zero-spend share of 0 and passing both tests below.
--      That case is contract metadata, not arithmetic; it is handled by the
--      per-organization attestation in 202607280009
--      (public.organization_billing_arrangement, reached through
--      public.assert_billing_period_settlement_allowed). Do not re-describe
--      this condition as PTU protection.
--
--      Two tests, both scoped to the period:
--        (a) a positive fee requires strictly positive actual provider spend in
--            the period at all — no marginal dollar anywhere means no fee; and
--        (b) the share of net savings contributed by zero/absent-cost rows must
--            not exceed 0.50.
--
--      0.50 is derived, not picked. Let z be that share. The fee is 0.25 of all
--      savings, but only the (1 - z) fraction is demonstrably backed by spend,
--      so the effective rate charged against provably-paid-for savings is
--      0.25 / (1 - z). At z = 0.50 that is 50% — twice the headline rate, and
--      the point past which the invoice stops being explainable to the customer
--      from their own provider bill. (At z = 0.75 it reaches 100%: we would be
--      charging the entire provable saving as our fee.) The constant is bounded
--      at 0.50 by a CHECK constraint for the same reason as above.
--
--      Note that zero-cost rows are not inherently fraudulent: a fully replayed
--      call legitimately costs $0 and legitimately avoided a real dollar. The
--      halting condition is about *concentration*, not presence — an org whose
--      savings are almost entirely unaccompanied by spend is an org we cannot
--      show a marginal dollar for.
--
--   3. CUMULATIVE PERIOD CEILING. Conditions 1 and 2 each judge ONE proposed
--      fee. That is sufficient only if a period can be settled once, and it
--      cannot: 202607280007's correction flow stamps the predecessor's
--      superseded_at (which frees the live-period unique index) and then
--      APPENDS a successor row with a fresh identity. Stripe identifiers are
--      derived from the ledger id alone — api/billing_recovery.py builds
--      'brevitas-fee-{id}' and 'brevitas-meter-{id}' — so a successor is a
--      SECOND charge that sums onto the period meter, not a replacement for
--      the first. Judged in isolation, a period whose revision 1 already
--      reported at the ceiling passes again at the ceiling: 50% of net savings
--      collected on a period that owed 25% once, and every later revision adds
--      another 25%.
--
--      So condition 1's ceiling is re-applied to the cumulative amount rather
--      than being weakened. Conditions 1 and 2 keep reading only primary usage
--      evidence; this third test adds the only read of the settlement ledger,
--      which is what makes it a separate condition instead of an edit to 1.
--      This is not a new invariant — it restores one. The per-row claim path
--      it replaces already summed the period (`committed` in
--      202607200006_company_billing_authorization.sql) before authorizing a
--      send; the aggregate path dropped that sum on the false premise that a
--      period ledger has exactly one row per period.
--
--      Which rows count follows that `committed` computation, generalized from
--      "which statuses imply a send" to "was this row EVER sent": 'sending' and
--      'reported', plus ANY row whose outbound send was started
--      (outbound_started_at set), because Stripe may already have ingested an
--      ambiguous send. A row with no outbound_started_at that is not
--      'sending'/'reported' was provably never sent and is excluded. 'draft'
--      and 'pending' are therefore excluded, which is also what makes the test
--      safe to run on the candidate's own behalf: the guard runs before the
--      settlement row exists (or while it is still draft/pending, where
--      outbound_started_at has no default and is NULL), so it never counts
--      itself.
--      Superseded and 'void' rows are NOT excluded — being superseded or
--      retracted says nothing about whether Stripe was already paid. Note the
--      two are excluded by DIFFERENT mechanisms and only the first is free:
--      supersession is stamped in its own column (superseded_at), so a
--      superseded row keeps status='reported' and is summed regardless of the
--      predicate. Voiding IS a status, so a status-only predicate would silently
--      drop a retracted-but-already-paid row from the sum and reopen the exact
--      double settlement this condition exists to prevent (202607280007 admits
--      'void' from ANY state, its identity trigger deliberately does not freeze
--      `status`, and 'void' is exempted from both the live-period unique index
--      and the billing-owner requirement — so the period slot comes fully free).
--      The outbound_started_at branch is what closes that: it counts on
--      evidence of a send, not on the row's current status.
--
--      Consequence, stated plainly for the Phase 4 writer: a corrective
--      revision on an already-reported period will halt unless the period's
--      total collected stays under the ceiling. That is the intended answer
--      for the equal/upward case. It also halts the DOWNWARD correction that
--      202607280007 describes as "refunded through Stripe out of band" — a
--      lower recomputation is audit evidence and must stay in 'draft'; it must
--      never be promoted to pending, because this ledger has no way to express
--      a refund or a delta charge (fee_microusd is the period total, and
--      api/billing_recovery.py reconciles against it as the period total).
--
-- Why not a trigger on usage_log: a trigger would fire at receipt-ingestion
-- time, which (a) makes telemetry ingestion fail on a billing condition, (b)
-- re-introduces per-row evaluation of a predicate that is only meaningful over
-- a whole period, and (c) runs an unbounded aggregate on every insert. Phase 0
-- removed the per-row trigger on purpose; this migration does not add one back.
--
-- Why not purely CHECK constraints: every condition here is a cross-row
-- aggregate predicate — conditions 1 and 2 over usage_log and
-- warm_budget_ledger, condition 3 over the sibling revisions of the very row
-- being written — and a CHECK constraint cannot see other rows. The
-- CHECK constraints here therefore bound the *constants* (so the thresholds
-- cannot be relaxed by an UPDATE, only by a migration), and the aggregate
-- predicates live in a validation function that the settlement path must call
-- inside its own transaction, before writing any period settlement row.
--
-- Scope of the evidence: only rows that could ever be billed are counted —
-- authoritative (worker-observed; caller-reported proxy telemetry carries
-- authoritative = false BY DESIGN and is never billing input) and priced. The
-- window is half-open [period_start, period_end), matching
-- public.billing_period_for_occurrence (202607170004). Warm spend is the one
-- term that does not come from usage_log, because it does not live there; it is
-- summed from public.warm_budget_ledger (202607280001) over overlapping days.
--
-- Safety: this migration is additive. It creates one configuration table and
-- two functions, attaches nothing to any existing table, and grants nothing to
-- anon/authenticated. It cannot cause a fee to be charged; it can only cause a
-- settlement attempt to abort.

begin;

-- The period settlement ledger (202607280007) is the caller these conditions
-- exist to guard. Depend on its presence, never on its column names: this
-- migration reads its evidence from usage_log, on purpose, so that the two
-- checks are genuinely independent. 202607280007 carries its own CHECK that a
-- row's fee is 25% of the net savings *recorded on that row*; the condition
-- below recomputes that same ceiling from the *underlying evidence* — BOTH
-- terms of it: verified savings from usage_log and the warm-spend deduction
-- from warm_budget_ledger. A settlement row whose recorded savings, or whose
-- recorded warm spend, do not match the evidence it claims to summarise passes
-- the first and fails the second, which is the entire point of having two.
-- Recomputing only one of the two operands would leave the other trusted, and
-- an untrusted-input check that trusts half its inputs is not a second breaker.
do $settlement_ledger_precondition$
begin
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280008 requires public.period_settlement_ledger from 202607280007',
            hint    = 'Apply 202607280007 first; if the ledger was renamed, update this '
                      'probe rather than removing the check.';
    end if;
    -- The warm deduction half of the ceiling is unenforceable without this
    -- table, and silently dropping the deduction would overcharge. Fail loudly.
    if to_regclass('public.warm_budget_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280008 requires public.warm_budget_ledger from 202607280001',
            hint    = 'The relative ceiling deducts recomputed warm-ping spend. Without '
                      'that ledger the deduction would silently be zero, which overcharges. '
                      'Apply 202607280001 first.';
    end if;
end;
$settlement_ledger_precondition$;

-- Phase 0 assumed no per-row writer exists. The halting conditions are written
-- for the aggregate path only; if the per-row trigger were reattached it would
-- queue fees that never pass through this function at all.
do $freeze_still_held$
begin
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280008 refuses to install halting conditions while the '
                      'per-row fee trigger is attached',
            hint    = 'The per-row path bypasses these conditions entirely. '
                      'Reapply 202607280006 first.';
    end if;
end;
$freeze_still_held$;

-- One row, forever. Thresholds live in the schema rather than in application
-- configuration so that relaxing them is a reviewed migration, not a deploy.
create table if not exists public.billing_halting_conditions (
    singleton boolean primary key default true,
    max_fee_share_of_verified_savings numeric(6,5) not null default 0.25000,
    max_zero_spend_savings_share numeric(6,5) not null default 0.50000,
    updated_at timestamptz not null default now()
);

-- Stated separately so that a pre-existing table (create-if-not-exists is a
-- no-op) still acquires the bounds.
alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_singleton_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_singleton_check
    check (singleton);

alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_fee_share_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_fee_share_check
    check (max_fee_share_of_verified_savings > 0
       and max_fee_share_of_verified_savings <= 0.25);

alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_zero_spend_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_zero_spend_check
    check (max_zero_spend_savings_share >= 0
       and max_zero_spend_savings_share <= 0.50);

insert into public.billing_halting_conditions (singleton)
values (true)
on conflict (singleton) do nothing;

alter table public.billing_halting_conditions enable row level security;
-- Service-owned. No browser policy is intentionally created.
revoke all on table public.billing_halting_conditions
    from public, anon, authenticated, service_role;
grant select on table public.billing_halting_conditions to service_role;

comment on table public.billing_halting_conditions is
    'Phase 4 halting-condition thresholds. Single row. CHECK-bounded so the '
    'ceilings can be tightened operationally but never loosened without a '
    'migration. Read by public.assert_billing_period_halting_conditions.';
comment on column public.billing_halting_conditions.max_fee_share_of_verified_savings is
    'Halting condition 1: a period fee may not exceed this fraction of the same '
    'org''s net verified savings for the same period. 0.25 matches the '
    'contracted rate (20260716_stripe_billing_rate_25pct).';
comment on column public.billing_halting_conditions.max_zero_spend_savings_share is
    'Halting condition 2: maximum share of a period''s net savings that may come '
    'from rows with zero/absent actual_cost_usd. 0.50 caps the effective rate '
    'against provably-paid-for savings at 0.25/(1-0.50) = 50%.';

-- Period evidence, aggregated over billable-eligible rows only. Netted, not
-- floored per row: offsetting rows must be allowed to cancel.
-- The return type gains a column, and CREATE OR REPLACE cannot widen an OUT
-- list, so drop first. Safe: assert_billing_period_halting_conditions resolves
-- this by name at execution time (plpgsql, late-bound), and no other object
-- depends on it.
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
        (
            select count(*)::bigint
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
    'here precisely so that no settlement writer can supply it.';

-- The guard the settlement path must call. Raises 55000 when either condition
-- halts; returns the evidence it decided on when it does not.
create or replace function public.assert_billing_period_halting_conditions(
    p_organization_id uuid,
    p_period_start timestamptz,
    p_period_end timestamptz,
    p_fee_microusd bigint
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_limits public.billing_halting_conditions%rowtype;
    v_evidence record;
    v_billable_savings_usd numeric;
    -- Distinct from v_billable_savings_usd on purpose. The fee ceiling is taken
    -- against savings NET of recomputed warm spend; the zero-spend
    -- concentration share below keeps its original denominator (gross netted
    -- savings), because warm spend is a cost we incurred, not a saving whose
    -- marginal dollar is in question. Conflating them would silently retune a
    -- threshold that was derived for a different quantity.
    v_net_after_warm_usd numeric;
    v_ceiling_microusd bigint;
    v_zero_spend_share numeric;
    v_committed_microusd bigint;
begin
    if p_organization_id is null then
        raise exception using
            errcode = '22023',
            message = 'halting conditions require an organization: the netting unit is the org';
    end if;
    if p_period_start is null or p_period_end is null
       or p_period_end <= p_period_start then
        raise exception using
            errcode = '22023',
            message = 'halting conditions require a half-open period with end after start';
    end if;
    if p_fee_microusd is null then
        raise exception using
            errcode = '22023',
            message = 'halting conditions require an explicit fee to evaluate';
    end if;
    -- Negative fees are rejected by design: a credit landing beside reporting
    -- positives overcharges with no auto-correction, and Stripe rejects them.
    if p_fee_microusd < 0 then
        raise exception using
            errcode = '22023',
            message = 'halting_condition=negative_fee: a settlement fee may not be negative',
            detail  = format('fee_microusd=%s', p_fee_microusd);
    end if;

    select * into v_limits
      from public.billing_halting_conditions
     where singleton;
    if not found then
        raise exception using
            errcode = '55000',
            message = 'halting_condition=unconfigured: billing_halting_conditions has no row';
    end if;

    select * into v_evidence
      from public.billing_period_settlement_evidence(
          p_organization_id, p_period_start, p_period_end
      );

    -- Condition 1 — per-org relative ceiling. A losing period bills $0, and a
    -- period whose warm-ping spend swallowed its savings is a losing period.
    -- Both operands of the net come from evidence, never from the caller: the
    -- caller supplies only the fee it wants to charge.
    v_billable_savings_usd := greatest(v_evidence.net_verified_savings_usd, 0);
    v_net_after_warm_usd := greatest(
        v_evidence.net_verified_savings_usd - greatest(v_evidence.warm_spend_usd, 0),
        0
    );
    v_ceiling_microusd := floor(
        v_net_after_warm_usd * v_limits.max_fee_share_of_verified_savings * 1000000
    )::bigint;

    if p_fee_microusd > v_ceiling_microusd then
        raise exception using
            errcode = '55000',
            message = format(
                'halting_condition=relative_ceiling: fee %s microUSD exceeds %s microUSD '
                '(%s of this org''s own net verified savings for this period, after '
                'deducting recomputed warm-ping spend)',
                p_fee_microusd, v_ceiling_microusd,
                v_limits.max_fee_share_of_verified_savings
            ),
            detail = format(
                'organization=%s period=[%s,%s) eligible_rows=%s net_verified_savings_usd=%s '
                'warm_spend_usd=%s warm_spend_days=%s net_after_warm_savings_usd=%s',
                p_organization_id, p_period_start, p_period_end,
                v_evidence.eligible_rows, v_evidence.net_verified_savings_usd,
                v_evidence.warm_spend_usd, v_evidence.warm_spend_days,
                v_net_after_warm_usd
            ),
            hint = 'Settle at or below the ceiling, or settle zero. If the settlement row '
                   'states a smaller warm_spend_usd than the ledger, the row is wrong, '
                   'not the ceiling.';
    end if;

    -- Condition 2 — zero-spend concentration. Settling zero is always allowed:
    -- it moves no money, so neither test can protect anything by blocking it.
    -- The share is computed unconditionally so that the returned evidence tells
    -- the truth even on the zero-fee path a caller uses to inspect a period.
    -- It is undefined (null, not zero) when there are no net savings to divide.
    if v_billable_savings_usd > 0 then
        v_zero_spend_share := v_evidence.zero_spend_net_savings_usd / v_billable_savings_usd;
    end if;

    if p_fee_microusd > 0 then
        if v_evidence.actual_spend_usd <= 0 then
            raise exception using
                errcode = '55000',
                message = 'halting_condition=zero_spend: the period has no actual provider '
                          'spend, so the savings have no marginal dollar behind them',
                detail = format(
                    'organization=%s period=[%s,%s) eligible_rows=%s zero_spend_rows=%s '
                    'net_verified_savings_usd=%s',
                    p_organization_id, p_period_start, p_period_end,
                    v_evidence.eligible_rows, v_evidence.zero_spend_rows,
                    v_evidence.net_verified_savings_usd
                ),
                hint = 'No priced provider spend was recorded for this period at all. '
                       'This test measures recorded LIST-PRICE spend and cannot detect a '
                       'PTU/committed-throughput agreement, which prices normally here; '
                       'halting_condition=unattested_billing_arrangement (202607280009) '
                       'is the test that covers that. Do not settle this period.';
        end if;

        if v_billable_savings_usd <= 0 then
            -- Unreachable: condition 1 already rejected any positive fee here.
            raise exception using
                errcode = '55000',
                message = 'halting_condition=no_savings: positive fee with no net savings';
        end if;

        if v_zero_spend_share > v_limits.max_zero_spend_savings_share then
            raise exception using
                errcode = '55000',
                message = format(
                    'halting_condition=zero_spend_concentration: %s of this period''s net '
                    'savings come from rows with zero or absent actual_cost_usd '
                    '(limit %s)',
                    round(v_zero_spend_share, 5),
                    v_limits.max_zero_spend_savings_share
                ),
                detail = format(
                    'organization=%s period=[%s,%s) eligible_rows=%s zero_spend_rows=%s '
                    'zero_spend_net_savings_usd=%s net_verified_savings_usd=%s '
                    'actual_spend_usd=%s',
                    p_organization_id, p_period_start, p_period_end,
                    v_evidence.eligible_rows, v_evidence.zero_spend_rows,
                    v_evidence.zero_spend_net_savings_usd,
                    v_evidence.net_verified_savings_usd,
                    v_evidence.actual_spend_usd
                ),
                hint = 'Savings with no marginal dollar behind them are not billable. '
                       'Investigate the org''s provider agreement before settling.';
        end if;
    end if;

    -- Condition 3 — cumulative period ceiling. The only read of the settlement
    -- ledger in this function; see the CUMULATIVE PERIOD CEILING note at the
    -- top of this migration for why conditions 1 and 2 cannot cover this.
    --
    -- Computed unconditionally so the returned evidence tells the truth on the
    -- zero-fee path a caller uses to inspect a period, and enforced only for a
    -- positive fee: settling zero moves no money, so blocking it protects
    -- nothing (and a ceiling lowered after the fact would otherwise make an
    -- already-settled period un-inspectable).
    --
    -- TOCTOU: this must run in the settlement transaction, as the function
    -- comment requires. Two concurrent settlements of one period are already
    -- serialized by period_settlement_ledger_live_period_idx — a second live
    -- row is rejected outright, and appending a successor requires first
    -- UPDATEing the predecessor's superseded_at, which takes that row's lock.
    select coalesce(sum(settlement.fee_microusd), 0)
      into v_committed_microusd
      from public.period_settlement_ledger settlement
     where settlement.organization_id = p_organization_id
       and settlement.period_start = p_period_start
       and settlement.period_end = p_period_end
       and (settlement.status in ('sending', 'reported')
            or settlement.outbound_started_at is not null);

    if p_fee_microusd > 0
       and v_committed_microusd + p_fee_microusd > v_ceiling_microusd then
        raise exception using
            errcode = '55000',
            message = format(
                'halting_condition=cumulative_ceiling: this period has already '
                'committed %s microUSD to Stripe; adding %s would reach %s, above '
                'the %s microUSD ceiling (%s of this org''s own net verified '
                'savings for this period)',
                v_committed_microusd, p_fee_microusd,
                v_committed_microusd + p_fee_microusd, v_ceiling_microusd,
                v_limits.max_fee_share_of_verified_savings
            ),
            detail = format(
                'organization=%s period=[%s,%s) eligible_rows=%s '
                'net_verified_savings_usd=%s committed_period_microusd=%s',
                p_organization_id, p_period_start, p_period_end,
                v_evidence.eligible_rows, v_evidence.net_verified_savings_usd,
                v_committed_microusd
            ),
            hint = 'A corrective revision is a NEW Stripe charge, not a '
                   'replacement: the idempotency key is derived from the ledger '
                   'id. Leave the recomputation in ''draft'' as audit evidence '
                   'and refund or charge the difference out of band.';
    end if;

    return jsonb_build_object(
        'organization_id', p_organization_id,
        'period_start', p_period_start,
        'period_end', p_period_end,
        'fee_microusd', p_fee_microusd,
        'fee_ceiling_microusd', v_ceiling_microusd,
        -- Already committed to Stripe for this (organization, period) by an
        -- earlier revision, and what is left under the ceiling after it.
        'committed_period_microusd', v_committed_microusd,
        'fee_ceiling_remaining_microusd',
            greatest(v_ceiling_microusd - v_committed_microusd, 0),
        'eligible_rows', v_evidence.eligible_rows,
        'zero_spend_rows', v_evidence.zero_spend_rows,
        'net_verified_savings_usd', v_evidence.net_verified_savings_usd,
        -- Recomputed from warm_budget_ledger, NOT read from any settlement row.
        -- A writer reconciling its own warm_spend_usd against this value is the
        -- intended use; a mismatch means the settlement row is wrong.
        'warm_spend_usd', v_evidence.warm_spend_usd,
        'warm_spend_days', v_evidence.warm_spend_days,
        'net_after_warm_savings_usd', v_net_after_warm_usd,
        'zero_spend_net_savings_usd', v_evidence.zero_spend_net_savings_usd,
        'zero_spend_share', round(v_zero_spend_share, 5),
        'actual_spend_usd', v_evidence.actual_spend_usd,
        'max_fee_share_of_verified_savings', v_limits.max_fee_share_of_verified_savings,
        'max_zero_spend_savings_share', v_limits.max_zero_spend_savings_share
    );
end;
$$;

revoke all on function public.assert_billing_period_halting_conditions(uuid, timestamptz, timestamptz, bigint)
    from public, anon, authenticated;
grant execute on function public.assert_billing_period_halting_conditions(uuid, timestamptz, timestamptz, bigint)
    to service_role;

comment on function public.assert_billing_period_halting_conditions(uuid, timestamptz, timestamptz, bigint) is
    'Phase 4 halting conditions. The period settlement path MUST call this in '
    'its own transaction before writing a settlement row, passing the fee in '
    'microUSD (floor(fee_usd * 1000000)). Raises SQLSTATE 55000 with a '
    'halting_condition=<name> message when settlement must not proceed; returns '
    'the evidence it decided on otherwise. Not attached to any trigger: it is a '
    'period-aggregate predicate, not a row-ingestion predicate. Calling it in '
    'the settlement transaction is load-bearing, not stylistic: the cumulative '
    'ceiling reads period_settlement_ledger, so evaluating it in an earlier '
    'transaction reintroduces the double-settlement it exists to stop.';

do $halting_conditions_contract$
declare
    v_limits public.billing_halting_conditions%rowtype;
    v_unbilled_org uuid := gen_random_uuid();
    v_window_start constant timestamptz := '2026-07-15 10:00:00+00';
    v_window_end constant timestamptz := '2026-07-22 10:00:00+00';
    v_evidence record;
    v_result jsonb;
    v_row_count bigint;
begin
    select * into v_limits from public.billing_halting_conditions where singleton;
    if not found then
        raise exception '202607280008 did not seed the halting-condition thresholds';
    end if;
    if v_limits.max_fee_share_of_verified_savings <> 0.25 then
        raise exception '202607280008 fee ceiling must match the contracted 25%% rate';
    end if;
    if v_limits.max_zero_spend_savings_share <> 0.50 then
        raise exception '202607280008 zero-spend concentration limit must be 0.50';
    end if;

    select count(*) into v_row_count from public.billing_halting_conditions;
    if v_row_count <> 1 then
        raise exception '202607280008 expects exactly one halting-condition row, found %',
            v_row_count;
    end if;

    -- The thresholds must be tightenable but not loosenable.
    begin
        update public.billing_halting_conditions
           set max_fee_share_of_verified_savings = 0.90;
        raise exception '202607280008 allowed the fee ceiling to be raised above 0.25';
    exception
        when check_violation then null;
    end;

    begin
        update public.billing_halting_conditions
           set max_zero_spend_savings_share = 0.99;
        raise exception '202607280008 allowed the zero-spend limit to be raised above 0.50';
    exception
        when check_violation then null;
    end;

    begin
        insert into public.billing_halting_conditions (singleton) values (false);
        raise exception '202607280008 allowed a second halting-condition row';
    exception
        when check_violation then null;
    end;

    -- Evidence over an org with no rows must be zero, not null.
    select * into v_evidence
      from public.billing_period_settlement_evidence(
          v_unbilled_org, v_window_start, v_window_end
      );
    if v_evidence.eligible_rows <> 0
       or v_evidence.net_verified_savings_usd <> 0
       or v_evidence.zero_spend_net_savings_usd <> 0
       or v_evidence.actual_spend_usd <> 0
       or v_evidence.warm_spend_usd <> 0
       or v_evidence.warm_spend_days <> 0 then
        raise exception '202607280008 evidence for an org with no usage is not zeroed';
    end if;

    -- Condition 1: with no verified savings the ceiling is zero, so any positive
    -- fee must halt and a zero fee must pass.
    begin
        perform public.assert_billing_period_halting_conditions(
            v_unbilled_org, v_window_start, v_window_end, 1::bigint
        );
        raise exception '202607280008 settled a positive fee against zero verified savings';
    exception
        when sqlstate '55000' then null;
    end;

    v_result := public.assert_billing_period_halting_conditions(
        v_unbilled_org, v_window_start, v_window_end, 0::bigint
    );
    if (v_result->>'fee_ceiling_microusd')::bigint <> 0
       or (v_result->>'eligible_rows')::bigint <> 0
       or jsonb_typeof(v_result->'zero_spend_share') <> 'null'
       or (v_result->>'committed_period_microusd')::bigint <> 0
       or (v_result->>'fee_ceiling_remaining_microusd')::bigint <> 0
       or (v_result->>'warm_spend_usd')::numeric <> 0
       or (v_result->>'net_after_warm_savings_usd')::numeric <> 0 then
        raise exception '202607280008 zero-fee evidence is wrong: %', v_result;
    end if;

    -- Negative fees are rejected outright rather than clamped.
    begin
        perform public.assert_billing_period_halting_conditions(
            v_unbilled_org, v_window_start, v_window_end, (-1)::bigint
        );
        raise exception '202607280008 accepted a negative settlement fee';
    exception
        when sqlstate '22023' then null;
    end;

    -- Malformed periods are rejected: a closed or inverted window would silently
    -- evaluate the conditions against no evidence at all.
    begin
        perform public.assert_billing_period_halting_conditions(
            v_unbilled_org, v_window_end, v_window_start, 0::bigint
        );
        raise exception '202607280008 accepted an inverted settlement period';
    exception
        when sqlstate '22023' then null;
    end;

    -- The cumulative ceiling must still be in the guard. This is a structural
    -- check, not a behavioural one: exercising it needs a settled ledger row,
    -- which needs an organizations and an auth.users fixture that a migration
    -- must not leave behind in a seven-year financial record. What it does
    -- catch is the regression that motivated it — a guard that evaluates a fee
    -- in isolation and lets a corrective revision re-bill a reported period.
    -- TODO(phase-4): once the settlement RPC exists, move this to a real test
    -- that reports a revision at the ceiling and asserts the successor halts.
    if not exists (
        select 1
          from pg_proc guard_function
          join pg_namespace guard_schema
            on guard_schema.oid = guard_function.pronamespace
         where guard_schema.nspname = 'public'
           and guard_function.proname = 'assert_billing_period_halting_conditions'
           and guard_function.prosrc like '%period_settlement_ledger%'
           and guard_function.prosrc like '%cumulative_ceiling%'
    ) then
        raise exception '202607280008 halting conditions no longer sum already-committed '
                        'period fees; a corrective revision would re-bill the period';
    end if;

    -- ...and it must count a row as committed on evidence that it was SENT, not
    -- on its current status. 202607280007 admits 'void' from any state and does
    -- not freeze `status` in its identity trigger, so a status-only predicate
    -- lets an operator retract an already-reported row (`set status='void'`) and
    -- drop its fee out of the sum — after which 'void' is also exempt from the
    -- live-period unique index and the billing-owner CHECK, so the period slot
    -- comes free and the successor settles at the full ceiling a second time.
    -- The unconditional `or ... outbound_started_at is not null` branch is what
    -- prevents that; draft/pending rows have no default for that column and stay
    -- excluded, preserving self-exclusion.
    -- Substring check, so it is sensitive to reformatting the predicate — that
    -- is deliberate: reformatting it is the moment to re-derive this property.
    -- TODO(phase-4): replace with a behavioural test that reports a revision at
    -- the ceiling, voids it, and asserts the successor still halts.
    if not exists (
        select 1
          from pg_proc guard_function
          join pg_namespace guard_schema
            on guard_schema.oid = guard_function.pronamespace
         where guard_schema.nspname = 'public'
           and guard_function.proname = 'assert_billing_period_halting_conditions'
           and guard_function.prosrc like '%or settlement.outbound_started_at is not null%'
    ) then
        raise exception '202607280008 cumulative ceiling counts committed fees by status '
                        'alone; voiding a reported row would free the period to be '
                        'settled at the ceiling a second time';
    end if;

    -- The warm deduction must be recomputed here, not trusted from the
    -- settlement row. Same shape of check, and for the same reason, as the
    -- cumulative-ceiling probe above: exercising it behaviourally needs an
    -- organizations fixture plus a warm_budget_ledger row, which a migration
    -- must not leave behind. What it catches is the regression it was written
    -- for — an evidence function that recomputes only the savings half of the
    -- net, leaving warm_spend_usd on the writer's word (it defaults to 0 on
    -- 202607280007, so omitting it overcharges by up to the full warm spend).
    -- TODO(phase-4): once the settlement RPC exists, move this to a real test
    -- that settles $1,000 verified against $400 warm and asserts the ceiling is
    -- $150, not $250.
    if not exists (
        select 1
          from pg_proc evidence_function
          join pg_namespace evidence_schema
            on evidence_schema.oid = evidence_function.pronamespace
         where evidence_schema.nspname = 'public'
           and evidence_function.proname = 'billing_period_settlement_evidence'
           and evidence_function.prosrc like '%warm_budget_ledger%'
    ) then
        raise exception '202607280008 settlement evidence no longer recomputes warm-ping '
                        'spend; the fee ceiling would trust the writer for that term';
    end if;
    if not exists (
        select 1
          from pg_proc guard_function
          join pg_namespace guard_schema
            on guard_schema.oid = guard_function.pronamespace
         where guard_schema.nspname = 'public'
           and guard_function.proname = 'assert_billing_period_halting_conditions'
           and guard_function.prosrc like '%warm_spend_usd%'
    ) then
        raise exception '202607280008 the fee ceiling no longer deducts recomputed '
                        'warm-ping spend before applying the rate';
    end if;

    -- The guard must not have been wired into ingestion.
    if exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid = trigger_state.tgfoid
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and not trigger_state.tgisinternal
           and trigger_function.prosrc like '%halting_conditions%'
    ) then
        raise exception '202607280008 halting conditions must not fire from a usage_log trigger';
    end if;

    -- Neither function may be reachable by a browser session.
    if has_function_privilege('anon',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or has_function_privilege('anon',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)',
           'EXECUTE')
       or has_function_privilege('authenticated',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)',
           'EXECUTE') then
        raise exception '202607280008 exposed billing evidence to a browser role';
    end if;
end;
$halting_conditions_contract$;

commit;
