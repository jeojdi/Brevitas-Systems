-- wyfz function realignment, 2026-07-30.
--
-- WHY THIS FILE EXISTS. The 202607280005-280024 chain reached wyfz out of
-- manifest order: 202607280021's legal-acceptance trigger was live on
-- auth.users before 202607280010/280012 were attempted, and both of those
-- files carry embedded self-tests that insert a bare auth.users probe row
-- (no created_at). The live trigger copies created_at into a NOT NULL
-- column, so the probe -- and with it the whole file transaction -- fails.
-- Verified 2026-07-30T19:03:41Z (apply log) and by md5(pg_get_functiondef):
-- wyfz differs from the canonical chain end-state in EXACTLY two functions;
-- settle_billing_period, halting conditions and release_billing_ledger_unsent
-- are already byte-identical to canonical.
--
-- WHAT THIS FILE IS. The two bodies below are pg_get_functiondef output from
-- a local database that applied the full canonical manifest in order --
-- byte-identical semantics to CI's end state, with the embedded self-test
-- probes omitted (their guarantees are proven in CI on canonical order; the
-- probes are the only part that cannot run under the live 280021 trigger).
--
-- Canonical md5s this file must produce (verification select at the end):
--   prevent_period_settlement_identity_change : fc6a55f1f773925355d3274f55f6a7c0
--   billing_period_settlement_evidence        : ddcb0f6d601e7a29370da6920e63e24e
--
-- The evidence function's OUT list differs between the wyfz and canonical
-- versions, so CREATE OR REPLACE would 42P13; it is dropped and recreated in
-- the same transaction. plpgsql callers resolve it at execution time, so the
-- gap is invisible outside this transaction.

begin;

CREATE OR REPLACE FUNCTION public.prevent_period_settlement_identity_change()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
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
$function$;


drop function public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz);

CREATE OR REPLACE FUNCTION public.billing_period_settlement_evidence(p_organization_id uuid, p_period_start timestamp with time zone, p_period_end timestamp with time zone)
 RETURNS TABLE(eligible_rows bigint, zero_spend_rows bigint, net_verified_savings_usd numeric, zero_spend_net_savings_usd numeric, actual_spend_usd numeric, warm_spend_usd numeric, warm_spend_days bigint, usage_log_watermark_id bigint)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'public', 'pg_temp'
AS $function$
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
$function$;


commit;

-- post-apply verification (read-only)
select md5(pg_get_functiondef('public.prevent_period_settlement_identity_change()'::regprocedure))
         = 'fc6a55f1f773925355d3274f55f6a7c0' as identity_trigger_canonical,
       md5(pg_get_functiondef('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'::regprocedure))
         = 'ddcb0f6d601e7a29370da6920e63e24e' as evidence_canonical;
