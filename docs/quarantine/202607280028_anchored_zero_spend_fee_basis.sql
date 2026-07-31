-- Phase 5 of the billing correctness rollout: THE ANCHORED FEE BASIS.
--
-- WHAT THIS FIXES, IN ONE SENTENCE: for an OpenAI-compatible tenant the only
-- organic savings Brevitas can produce are exact-cache replays, every one of
-- them carries actual_cost_usd = 0 BY CONSTRUCTION, and 202607280008's
-- zero-spend concentration condition therefore halts such a period forever --
-- so the product's own headline mechanism is structurally unbillable.
--
-- THE OBSERVED FACT. In the 2026-07-30 customer dress rehearsal
-- (docs/STRIPE_BUILD_REPORT.md, "CUSTOMER DRESS REHEARSAL") a real tenant on
-- real DeepSeek traffic produced 8 authoritative priced usage_log rows: 4 paid
-- cache misses and 4 exact_cache replays summing $0.0021019600 of verified
-- savings. The settlement halted with
--     halting_condition=zero_spend_concentration
--     zero_spend_net_savings_usd = 0.0021019600 = net_verified_savings_usd
--     share = 1.00000  (limit 0.50)
--     recomputed_fee_microusd = 525
-- This is structural, not a fixture artifact: a replay never touches an
-- upstream (brevitas/proxy.py:649-651 -> brevitas/receipts.py:381-386), and the
-- share's denominator is SAVINGS, so the paid misses in the same period cannot
-- dilute it. Every all-cache period sits at 1.00000 for ever.
--
-- WHY THE CONDITION WAS RIGHT ANYWAY. 202607280008 states its own reasoning
-- plainly: "a row that saved money but recorded no spend has no marginal dollar
-- visible behind it, and billing a percentage of a dollar that was never going
-- to be spent is the worst failure mode available to this system", and then
-- concedes "zero-cost rows are not inherently fraudulent: a fully replayed call
-- legitimately costs $0 and legitimately avoided a real dollar". The condition
-- could not tell those two apart because it had nothing to tell them apart
-- WITH. This migration supplies the missing evidence.
--
-- THE ANCHOR. A Brevitas exact/semantic cache replay is a replay OF SOMETHING.
-- The response it serves was produced, earlier, by a real request that really
-- went to the provider, really cost money, and really produced a provider
-- receipt. That ancestor IS the marginal dollar: it is the observed price of
-- the call the customer did not have to make again. So:
--
--     A zero-spend saving is BILLABLE iff it is ANCHORED -- a
--     Brevitas-attributable cache replay for which a real, receipted, PAID
--     request by the same organization for the same (provider, model) exists at
--     or before it in public.usage_log.
--
-- Every legitimate Brevitas cache hit has such an ancestor, because the cache
-- entry could not exist without one. A fabricated, buggy or forged savings row
-- does not: an attacker who wants to mint billable savings must first pay a
-- real provider for the same model, which is precisely the marginal dollar
-- 202607280008 demanded to see.
--
-- WHAT NARROWS, AND WHAT DOES NOT
--
--   * THE FEE BASIS narrows to PROVABLY ACTUAL SAVINGS:
--         anchored zero-spend savings + spend-backed savings.
--     Unanchored zero-spend savings are EXCLUDED from the basis entirely. They
--     are not "allowed but guarded"; they contribute $0 to every fee, always,
--     under every threshold setting. That is the whole decision: the previous
--     design would have billed them and relied on a ratio to notice; this one
--     cannot bill them at all.
--
--   * PROVIDER-NATIVE CACHE DISCOUNTS STAY UNBILLABLE. A DeepSeek
--     prompt_cache_hit_tokens row (or any provider-side or caller-side cache
--     discount) carries cache_attributable = false, which fails the anchor
--     predicate outright. Such a row is normally SPEND-BACKED, and
--     brevitas/receipts.py already gives it verified_savings_usd = 0 because
--     only the measured input-token delta is attributable when Brevitas did not
--     cause the discount -- so it contributes provider spend and no savings,
--     which is exactly right. Brevitas did not cause that discount and does not
--     bill for it. Note the deliberate asymmetry: cache_attributable is
--     REQUIRED to anchor a ZERO-SPEND row, and is NOT consulted for a
--     spend-backed row, because spend-backed savings (token compression,
--     routing) are billable on their own evidence and were never in question.
--
--   * THE CONCENTRATION GUARD IS RETAINED, over whatever unanchored zero-spend
--     savings remain. 202607280008's function is checksum-frozen and is NOT
--     touched here; instead its input narrows. public.billing_period_settlement
--     _evidence.zero_spend_net_savings_usd -- the guard's numerator, and the
--     only consumer of it -- now reports UNANCHORED zero-spend savings only.
--     The denominator stays the period's gross netted savings, so the ratio
--     keeps the meaning 0.50 was derived for in 202607280008 (at share z the
--     effective rate against provable savings is 0.25/(1-z); z = 0.50 is where
--     that reaches 50%). The original tripwire therefore still fires, unchanged,
--     for exactly the shape it was built to catch -- savings nobody paid for --
--     and stops firing for the shape it was never meant to catch.
--
-- WHY THE WRITER GATES ON THE GROSS FEE
--
-- public.settle_billing_period now computes TWO amounts:
--
--   v_gate_fee  -- what the OLD, un-narrowed basis would have charged.
--   v_fee       -- what we actually charge, from the anchored basis. Always
--                  <= v_gate_fee, because the basis is a subset of the gross
--                  (and is floored at the gross by the evidence function).
--
-- and it calls the halting-condition gate with v_gate_fee, not with v_fee.
-- That is deliberate and it is the conservative direction. 202607280008's
-- zero-spend tests are EVIDENCE tests -- "does this period have a marginal
-- dollar at all", "are its savings concentrated in rows nobody paid for" --
-- that are merely GATED on `fee > 0` because settling zero moves no money. If
-- the writer gated them on the narrowed fee, then an organization whose savings
-- are 100% unanchored would derive a $0 fee, skip both tests, and quietly
-- receive a $0 draft instead of a halt naming the condition. A $0 draft is a
-- silent verdict; a halt is an operator signal, and this system prefers the
-- signal. Gating on the larger amount keeps the tripwires answering a question
-- about the EVIDENCE rather than about how much of it we chose to bill, and it
-- can only ever refuse more than gating on v_fee would.
--
-- The fee that is finally written is additionally clamped to the gross ceiling,
-- so public.promote_billing_period_settlement -- which re-runs the guard for the
-- amount actually about to be billed -- can never reject a draft this writer
-- produced on the relative ceiling. (The evidence function already floors the
-- basis at the gross, so this clamp is belt-and-braces against a future
-- widening of the basis.)
--
-- WHAT period_settlement_ledger.verified_savings_usd MEANS AFTER THIS FILE
--
-- It is the BILLABLE BASIS -- anchored zero-spend savings plus spend-backed
-- savings -- not a naive sum of usage_log.verified_savings_usd. The fee CHECK
-- on that table (202607280007 around line 192) caps the fee at 25% of the
-- STORED value, so storing the narrowed basis is what makes the exclusion
-- structural rather than advisory: a row that tried to bill unanchored savings
-- would violate the CHECK. A reviewer reconciling a settlement must recompute
-- with public.billing_period_settlement_evidence().billable_savings_basis_usd,
-- not with `select sum(verified_savings_usd) from usage_log`. The gross is not
-- lost: the same function still returns it as net_verified_savings_usd over the
-- same window, and the row's usage_row_count / usage_log_watermark_id pin the
-- evidence set. The column comment is restated below to say so.
--
-- WHAT THIS MIGRATION DOES NOT DO
--
--   * It does not touch 202607280008 or 202607280013 on disk. Both are frozen
--     and both are applied or being applied to production today. The evidence
--     function and the writer are REPLACED here, in this file, as 202607280012
--     and 202607280013 already did before it.
--   * It does not widen cache_attributable, and it does not make caller-owned
--     caching billable (docs/SAVINGS_DROUGHT_DIAGNOSIS.md H4 -- recommended
--     against, and still recommended against).
--   * It adds no trigger, grants nothing to anon/authenticated, and widens no
--     table privilege. It can only ever lower a fee relative to the pre-existing
--     arithmetic, or convert a structural halt into a settlement whose every
--     dollar has a receipted ancestor.
--
-- ORDERING NOTE: this file DROPs and recreates
-- public.billing_period_settlement_evidence to widen its OUT list, exactly as
-- 202607280013 did. The same consequence applies and is deliberate: 202607280012
-- and 202607280013 both use CREATE OR REPLACE on that function and can no longer
-- be re-applied after this file, because PostgreSQL refuses to change the return
-- type of an existing function. An out-of-order replay aborts loudly instead of
-- silently narrowing the evidence function out from under settle_billing_period.
-- In manifest order the chain and the harness's back-to-back double-apply are
-- clean.

-- REVERSE: PITR-ONLY -- the widened evidence-function OUT list cannot be narrowed in place, and re-narrowing the fee basis would re-halt every all-cache period

begin;

-- A stuck apply must fail, not hold writes off the usage table while the anchor
-- index builds.
set local lock_timeout = '15s';

do $anchored_basis_precondition$
begin
    if to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280028 requires public.usage_log from 20260710_cloud_usage';
    end if;
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280028 requires public.period_settlement_ledger from 202607280007';
    end if;
    if to_regclass('public.billing_halting_conditions') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280028 requires public.billing_halting_conditions from 202607280008';
    end if;
    if to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280028 requires public.billing_period_settlement_evidence '
                      '(202607280008, widened by 202607280012 and 202607280013)';
    end if;
    if to_regprocedure(
           'public.settle_billing_period(uuid,timestamptz,text,boolean)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280028 requires public.settle_billing_period from 202607280013',
            hint    = 'This file replaces that writer body; it must exist first so an '
                      'out-of-order apply cannot install the narrowed basis without the '
                      'guard/promote pair 202607280013 introduced.';
    end if;
    -- The anchor predicate reads these three columns. If any is missing the
    -- narrowing would silently classify every zero-spend row as unanchored and
    -- take every organic fee to $0 -- fail loudly instead.
    if (select count(*)
          from information_schema.columns column_state
         where column_state.table_schema = 'public'
           and column_state.table_name = 'usage_log'
           and column_state.column_name in
               ('cache_attributable', 'strategy', 'quality_status',
                'receipt_source', 'provider', 'model', 'authoritative',
                'pricing_status', 'actual_cost_usd')) <> 9 then
        raise exception using
            errcode = '42703',
            message = '202607280028 requires the full receipt-accounting column set on '
                      'public.usage_log (202607170001, 202607170012)';
    end if;
end;
$anchored_basis_precondition$;

-- ---------------------------------------------------------------------------
-- 1. THE ANCHOR LOOKUP. One definition, shared by the evidence function and by
--    any operator who has to explain a settlement line by line.
-- ---------------------------------------------------------------------------
--
-- Returns the public.usage_log.id of the paid ancestor that anchors a
-- zero-spend saving, or NULL when the row does not qualify as a
-- Brevitas-attributable cache replay OR has no such ancestor.
--
-- THE FOUR CONDITIONS ON THE ROW ITSELF, and why each one is load-bearing:
--
--   cache_attributable            Brevitas, not the client and not the provider,
--                                 caused the discount (202607170012's column
--                                 comment). This is the single test that keeps
--                                 provider-native cache discounts (DeepSeek
--                                 prompt_cache_hit_tokens, Anthropic native
--                                 cache reads, a caller's own cache) out of the
--                                 fee basis for ever.
--   receipt_source = 'proxy'      Minted by the hosted proxy's own cache-hit
--                                 path (brevitas/proxy.py::_report_cache_hit),
--                                 not asserted by a caller. Caller-reported
--                                 telemetry is already authoritative=false and
--                                 never reaches this function, but a receipt
--                                 source is the positive evidence, not the
--                                 absence of a flag.
--   quality_status = 'verified'   The replay was proven byte-preserving. An
--                                 unverified or degraded reply did not save the
--                                 customer the call it claims to have saved.
--   strategy is a cache replay    exact_cache% / semantic_cache% -- the shapes
--                                 that serve a stored response instead of
--                                 calling the provider, i.e. the only shapes for
--                                 which "there was an earlier paid original" is
--                                 a meaningful claim.
--
-- THE ANCESTOR must be, for the SAME organization (the netting unit, per
-- 202607280008) and the SAME (provider, model):
--
--   authoritative                 worker-observed; caller-reported telemetry is
--                                 never billing input and is never evidence of
--                                 a real payment either.
--   pricing_status = 'priced'     its cost was actually resolved.
--   receipt_source = 'proxy'      it produced a provider receipt through us.
--   actual_cost_usd > 0           real money left the customer's account. This
--                                 is also what makes self-anchoring impossible:
--                                 the caller of this function is by definition a
--                                 row whose actual_cost_usd is <= 0 or absent.
--   ts <= the replay's ts         EARLIER. A cache entry cannot be a replay of a
--                                 request that had not happened yet.
--
-- THE LOOKBACK IS DELIBERATELY UNBOUNDED IN THE PAST. A cache entry legitimately
-- outlives a billing period, so requiring the ancestor inside the settled window
-- would make every week after the first unbillable and would recreate the exact
-- drought this migration exists to end. The ancestor is a FLOOR on evidence --
-- "this organization has really paid this provider for this model, through us"
-- -- not a claim about concurrency. It is deliberately NOT scoped to the
-- replay's key_hash either: keys rotate, the organization is the netting unit,
-- and a per-key scope would make a key rotation silently unbill a tenant.
--
-- Ordering is newest-ancestor-first so the returned id is the most recent
-- observed price for that (provider, model) -- the most useful one to put in
-- front of an operator. Any qualifying ancestor makes the row anchored; which
-- one is returned changes no fee.
create or replace function public.billing_zero_spend_savings_anchor_id(
    p_organization_id uuid,
    p_ts timestamptz,
    p_provider text,
    p_model text,
    p_cache_attributable boolean,
    p_strategy text,
    p_quality_status text,
    p_receipt_source text
)
returns bigint
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    select ancestor.id
      from public.usage_log ancestor
     -- Row-qualification first. These quals reference no column, so the planner
     -- evaluates them once as a gating filter: a row that is not a
     -- Brevitas-attributable verified cache replay costs no scan at all.
     where p_organization_id is not null
       and p_ts is not null
       and coalesce(p_cache_attributable, false)
       and coalesce(p_receipt_source, '') = 'proxy'
       and coalesce(p_quality_status, '') = 'verified'
       and (coalesce(p_strategy, '') like 'exact_cache%'
            or coalesce(p_strategy, '') like 'semantic_cache%')
     -- The receipted, paid ancestor.
       and ancestor.organization_id = p_organization_id
       and ancestor.provider = coalesce(p_provider, '')
       and ancestor.model = coalesce(p_model, '')
       and ancestor.ts <= p_ts
       and ancestor.authoritative
       and ancestor.pricing_status = 'priced'
       and ancestor.receipt_source = 'proxy'
       and ancestor.actual_cost_usd > 0
     order by ancestor.ts desc, ancestor.id desc
     limit 1;
$$;

revoke all on function public.billing_zero_spend_savings_anchor_id(
    uuid, timestamptz, text, text, boolean, text, text, text
) from public, anon, authenticated;
grant execute on function public.billing_zero_spend_savings_anchor_id(
    uuid, timestamptz, text, text, boolean, text, text, text
) to service_role;

comment on function public.billing_zero_spend_savings_anchor_id(
    uuid, timestamptz, text, text, boolean, text, text, text
) is
    'usage_log.id of the receipted PAID request that anchors a zero-spend '
    'Brevitas cache replay, or NULL when the row is not a Brevitas-attributable '
    'verified cache replay or has no such ancestor. A zero-spend saving is '
    'billable ONLY when this returns non-NULL (202607280028). The ancestor must '
    'be the same organization and the same (provider, model), authoritative, '
    'priced, receipt_source=proxy, actual_cost_usd > 0 and at or before the '
    'replay. Lookback is unbounded in the past on purpose: a cache entry '
    'outlives a billing period. Never call this for a spend-backed row -- its '
    'savings are billable on their own evidence and cache_attributable is not '
    'consulted for them.';

-- Supports the ancestor probe. Without it the evidence function degrades to one
-- sequential scan per zero-spend row over the whole usage table.
create index if not exists usage_log_paid_receipt_anchor_idx
    on public.usage_log (organization_id, provider, model, ts desc)
    where authoritative
      and pricing_status = 'priced'
      and receipt_source = 'proxy'
      and actual_cost_usd > 0;

comment on index public.usage_log_paid_receipt_anchor_idx is
    'Receipted paid requests, keyed for the zero-spend anchor probe in '
    'public.billing_zero_spend_savings_anchor_id (202607280028).';

-- ---------------------------------------------------------------------------
-- 2. THE EVIDENCE FUNCTION, widened by four columns and narrowed in one.
-- ---------------------------------------------------------------------------
--
-- NARROWED: zero_spend_net_savings_usd now reports UNANCHORED zero-spend
-- savings only. That column has exactly one consumer that makes a decision with
-- it -- the concentration test in
-- public.assert_billing_period_halting_conditions (202607280008 around line
-- 521) -- and narrowing the input is how the frozen guard is taught the
-- difference between a replay and a fabrication without editing a checksum-
-- frozen file. zero_spend_rows is NOT narrowed: it stays the count of every
-- zero/absent-cost eligible row, because it is operator diagnostics that appear
-- in halt detail lines and an operator needs the true count.
--
-- WIDENED, all four purely additive so existing `select * into <record>`
-- callers bind nothing incorrectly:
--   anchored_zero_spend_rows        how many replays were provably anchored
--   anchored_zero_spend_savings_usd their savings -- the term that was
--                                   previously unbillable and now is
--   unanchored_zero_spend_rows      the residue the concentration guard judges
--   billable_savings_basis_usd      THE FEE BASIS: anchored zero-spend savings
--                                   + spend-backed savings, floored at the
--                                   period's gross netted savings.
--
-- The floor at the gross matters: exclusions are subtractive in the normal case,
-- but a zero-spend row carrying NEGATIVE verified savings would be excluded too
-- and would push the basis ABOVE the gross. least() makes "we never bill on more
-- than the period actually saved" an invariant of the evidence rather than a
-- property a reader has to prove.
--
-- Everything else is reproduced verbatim from 202607280013 (which reproduced
-- 202607280012, which reproduced 202607280008). If any of those is ever revised,
-- this file moves with it.
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
    usage_log_watermark_id bigint,
    anchored_zero_spend_rows bigint,
    anchored_zero_spend_savings_usd numeric,
    unanchored_zero_spend_rows bigint,
    billable_savings_basis_usd numeric
)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    with eligible as (
        select
            usage.id,
            coalesce(usage.verified_savings_usd, 0) as savings_usd,
            coalesce(usage.actual_cost_usd, 0) as cost_usd,
            (
                coalesce(usage.actual_cost_usd, 0) <= 0
                and public.billing_zero_spend_savings_anchor_id(
                        usage.organization_id, usage.ts,
                        usage.provider, usage.model,
                        usage.cache_attributable, usage.strategy,
                        usage.quality_status, usage.receipt_source
                    ) is not null
            ) as anchored
          from public.usage_log usage
         where usage.organization_id = p_organization_id
           and usage.authoritative
           and usage.pricing_status = 'priced'
           and usage.ts >= p_period_start
           and usage.ts < p_period_end
    ),
    totals as (
        select
            count(*)::bigint as eligible_rows,
            count(*) filter (where usage.cost_usd <= 0)::bigint as zero_spend_rows,
            coalesce(sum(usage.savings_usd), 0)::numeric as net_verified_savings_usd,
            coalesce(sum(usage.savings_usd) filter (
                where usage.cost_usd <= 0 and not usage.anchored
            ), 0)::numeric as unanchored_zero_spend_savings_usd,
            coalesce(sum(greatest(usage.cost_usd, 0)), 0)::numeric as actual_spend_usd,
            -- Same eligible set as every column beside it, so the watermark
            -- cannot disagree with eligible_rows about which rows were folded in.
            max(usage.id)::bigint as usage_log_watermark_id,
            count(*) filter (
                where usage.cost_usd <= 0 and usage.anchored
            )::bigint as anchored_zero_spend_rows,
            coalesce(sum(usage.savings_usd) filter (
                where usage.cost_usd <= 0 and usage.anchored
            ), 0)::numeric as anchored_zero_spend_savings_usd,
            count(*) filter (
                where usage.cost_usd <= 0 and not usage.anchored
            )::bigint as unanchored_zero_spend_rows,
            -- THE FEE BASIS. Spend-backed savings are billable on their own
            -- evidence; zero-spend savings are billable only when anchored.
            coalesce(sum(usage.savings_usd) filter (
                where usage.cost_usd > 0 or usage.anchored
            ), 0)::numeric as billable_savings_basis_usd
          from eligible usage
    )
    select
        totals.eligible_rows,
        totals.zero_spend_rows,
        totals.net_verified_savings_usd,
        -- 202607280028: UNANCHORED only. The frozen concentration guard divides
        -- this by the gross netted savings above, so the ratio still means
        -- "share of this period's savings that nobody paid for".
        totals.unanchored_zero_spend_savings_usd,
        totals.actual_spend_usd,
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
        -- DISTINCT day, not row count (202607280012).
        (
            select count(distinct warm.day)::bigint
              from public.warm_budget_ledger warm
             where warm.organization_id = p_organization_id
               and (warm.day::timestamp at time zone 'UTC') < p_period_end
               and ((warm.day + 1)::timestamp at time zone 'UTC') > p_period_start
        ),
        totals.usage_log_watermark_id,
        totals.anchored_zero_spend_rows,
        totals.anchored_zero_spend_savings_usd,
        totals.unanchored_zero_spend_rows,
        -- Never bill on more than the period actually saved. See the header.
        least(
            totals.billable_savings_basis_usd,
            totals.net_verified_savings_usd
        )::numeric
      from totals;
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
    'empty period. 202607280028: billable_savings_basis_usd is THE FEE BASIS -- '
    'anchored zero-spend savings plus spend-backed savings, floored at '
    'net_verified_savings_usd -- and zero_spend_net_savings_usd now reports '
    'UNANCHORED zero-spend savings only, so the frozen concentration guard '
    'judges only savings nobody paid for. zero_spend_rows remains the count of '
    'ALL zero/absent-cost eligible rows.';

-- ---------------------------------------------------------------------------
-- 3. THE WRITER, re-derived from the anchored basis.
-- ---------------------------------------------------------------------------
--
-- Reproduced from 202607280013 with exactly three changes, all in step 7 and
-- all documented inline: the recorded savings become the anchored basis, the
-- guard is called with the gross-basis fee, and the returned body carries the
-- anchoring evidence so an operator can see what was excluded and why. Every
-- other step -- the advisory lock, enrollment, the period grid guard, the
-- idempotence tuple, the PSL-LATCH `period_already_committed` refusal that
-- includes 'pending', the three-step revision flow, 'draft' as the only status
-- it can ever write -- is byte-for-byte the behaviour 202607280013 shipped and
-- the settlement-writer suite proves.
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
    v_gross_verified numeric;
    v_warm numeric;
    v_net_after_warm numeric;
    v_gross_net_after_warm numeric;
    v_ceiling_microusd bigint;
    v_gross_ceiling_microusd bigint;
    v_fee bigint;
    v_gate_fee bigint;
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
    -- 0. Arguments. Programmer errors, not settlement outcomes.
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
    -- 1. Serialize on the organization.
    ------------------------------------------------------------------
    perform pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0));

    ------------------------------------------------------------------
    -- 2. Enrollment.
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
    -- 3. The window, derived ONLY from the Stripe anchor.
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

    if v_end > now() then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_not_closed',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end
        );
    end if;

    if v_start < v_account.billing_started_at then
        return jsonb_build_object(
            'ok', false, 'outcome', 'blocked', 'code', 'period_precedes_enrollment',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'billing_started_at', v_account.billing_started_at
        );
    end if;

    ------------------------------------------------------------------
    -- 4. Grid guard.
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
    -- 5. Attribution, snapshotted at settlement.
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
    -- 6. The highest existing revision for this (org, period), locked.
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
    --    and the fee derived from them come from the SAME snapshot.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(p_organization_id, v_start, v_end);

    -- 202607280028. TWO quantities, and the difference between them is the whole
    -- point of this migration:
    --
    --   v_gross_verified -- every eligible row's verified savings, the quantity
    --     202607280013 recorded and billed on, and the quantity 202607280008's
    --     frozen ceiling and concentration denominator are still computed from.
    --   v_verified       -- the ANCHORED BASIS: spend-backed savings plus
    --     zero-spend savings whose replay has a receipted PAID ancestor. This is
    --     what goes on the row and what the fee is charged against. Unanchored
    --     zero-spend savings are excluded here, once, structurally: the fee
    --     CHECK on period_settlement_ledger caps the fee at 25% of the STORED
    --     value, so there is no path by which they can be billed.
    --
    -- Both are rounded to the ledger's declared scale BEFORE any fee is derived,
    -- so the number the CHECK constraint re-derives from the stored columns is
    -- the exact number the fee came from.
    v_gross_verified := round(v_evidence.net_verified_savings_usd, 10);
    v_verified := round(v_evidence.billable_savings_basis_usd, 10);
    v_warm := round(greatest(v_evidence.warm_spend_usd, 0), 10);
    -- Warm-ping spend is deducted from the BASIS, not from the gross: it is a
    -- cost we incurred with the customer's money and it must reduce what we
    -- charge, and deducting it from the smaller quantity is the lower fee.
    v_net_after_warm := greatest(v_verified - v_warm, 0);
    v_gross_net_after_warm := greatest(v_gross_verified - v_warm, 0);

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

    -- The ceiling exactly as 202607280008 computes it, from the UNROUNDED gross
    -- evidence. This is the number the frozen guard will compare against, both
    -- here and again at promote time.
    v_gross_ceiling_microusd := floor(
        greatest(
            v_evidence.net_verified_savings_usd - greatest(v_evidence.warm_spend_usd, 0),
            0
        ) * v_share * 1000000
    )::bigint;
    -- The same arithmetic over the anchored basis: the ceiling that actually
    -- binds this settlement.
    v_ceiling_microusd := floor(v_net_after_warm * v_share * 1000000)::bigint;

    -- What the un-narrowed basis would have charged. This is the amount the
    -- halting conditions are evaluated against -- see "WHY THE WRITER GATES ON
    -- THE GROSS FEE" in this migration's header. It is never written anywhere
    -- and never sent to Stripe.
    v_gate_fee := least(
        public.period_settlement_fee_microusd(v_gross_net_after_warm, v_gross_verified),
        v_gross_ceiling_microusd
    );

    -- ONE arithmetic definition, shared with the table CHECK
    -- (period_settlement_fee_microusd, 202607280007 around line 274). The
    -- gross-ceiling term is belt-and-braces: the evidence function already
    -- floors the basis at the gross, so v_ceiling_microusd <=
    -- v_gross_ceiling_microusd already holds. Keeping it here guarantees that
    -- promote_billing_period_settlement -- which re-runs the frozen guard for
    -- the amount actually about to be billed -- can never reject a draft this
    -- writer produced on the relative ceiling.
    v_fee := least(
        public.period_settlement_fee_microusd(v_net_after_warm, v_verified),
        v_ceiling_microusd,
        v_gross_ceiling_microusd
    );

    ------------------------------------------------------------------
    -- 8. IDEMPOTENCE, checked BEFORE the guard on purpose.
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
    -- 9. Already committed to Stripe? Refuse, always. 'pending' MUST be in this
    --    predicate: it is the PSL-LATCH promote-door defect.
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
    -- 10. A prior settlement exists and the evidence moved.
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
    -- 11. THE GATE, on v_gate_fee. See the header.
    ------------------------------------------------------------------
    begin
        v_gate := public.assert_billing_period_settlement_allowed(
            p_organization_id, v_start, v_end, v_gate_fee
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
                'gate_fee_microusd', v_gate_fee,
                'message', v_message,
                'detail', v_detail
            );
    end;

    ------------------------------------------------------------------
    -- 12. The 3-step correction flow, in 202607280007's documented order.
    ------------------------------------------------------------------
    v_revision := coalesce(v_prev.revision, 0) + 1;
    v_revision_of := case when v_prev_found then v_prev.id else null end;

    if v_prev_found and v_prev.superseded_at is null then
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
        -- 202607280028: THE ANCHORED BASIS, still SIGNED. A losing week must be
        -- representable; net_savings_usd is the generated column that floors it,
        -- and the writer must not pre-floor it.
        v_verified,
        v_warm,
        v_evidence.eligible_rows,
        v_evidence.usage_log_watermark_id,
        v_fee, v_owner,
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
        -- 202607280028 anchoring evidence: what was billed on, what was
        -- excluded, and the un-narrowed total the gate was evaluated against.
        'gross_verified_savings_usd', v_gross_verified,
        'billable_savings_basis_usd', v_verified,
        'anchored_zero_spend_rows', v_evidence.anchored_zero_spend_rows,
        'anchored_zero_spend_savings_usd', v_evidence.anchored_zero_spend_savings_usd,
        'unanchored_zero_spend_rows', v_evidence.unanchored_zero_spend_rows,
        'unanchored_zero_spend_savings_usd', v_evidence.zero_spend_net_savings_usd,
        'gate_fee_microusd', v_gate_fee,
        'gross_fee_ceiling_microusd', v_gross_ceiling_microusd,
        'billing_arrangement', v_gate->>'billing_arrangement',
        'evidence', v_gate
    );
end;
$$;

-- CREATE OR REPLACE preserved the ACL and the comment 202607280013 set; restate
-- the privilege posture anyway so this file is self-contained evidence of it.
revoke all on function public.settle_billing_period(uuid, timestamptz, text, boolean)
    from public, anon, authenticated;
grant execute on function public.settle_billing_period(uuid, timestamptz, text, boolean)
    to service_role;

comment on function public.settle_billing_period(uuid, timestamptz, text, boolean) is
    'Compute and record ONE closed billing period as a draft settlement. Writes '
    '''draft'' rows only; every money field is derived inside the function, so a '
    'caller chooses WHICH period and nothing else. 202607280028: the recorded '
    'savings and the fee are the ANCHORED BASIS -- spend-backed savings plus '
    'zero-spend cache-replay savings with a receipted paid ancestor -- while the '
    'halting-condition gate is still evaluated against the un-narrowed gross '
    'fee, so the fraud tripwires answer a question about the evidence rather '
    'than about how much of it we chose to bill.';

comment on column public.period_settlement_ledger.verified_savings_usd is
    'Signed BILLABLE BASIS for the period (202607280028): spend-backed savings '
    'plus anchored zero-spend cache-replay savings, over authoritative, priced '
    'usage_log rows in [period_start, period_end), floored at the period''s '
    'gross netted savings. Unanchored zero-spend savings are excluded and the '
    'fee CHECK on this table caps the fee at 25% of THIS value, which is what '
    'makes the exclusion structural. Recompute with '
    'public.billing_period_settlement_evidence().billable_savings_basis_usd, '
    'NOT with a naive sum over usage_log; that function still returns the gross '
    'as net_verified_savings_usd. May be negative; a negative period nets to '
    'zero fee and carries no deficit forward.';

-- ---------------------------------------------------------------------------
-- 4. THE CUSTOMER-FACING PROJECTION, moved onto the same basis.
-- ---------------------------------------------------------------------------
--
-- public.billing_period_settlement_summary is the read path for
-- /api/billing/status, and 202607280013 makes it one specific promise: "The
-- projection uses the same arithmetic the writer will use, so it converges
-- exactly to the settled fee when the week closes." Narrowing the writer's
-- basis without narrowing the projection would break exactly that promise, in
-- the customer-hostile direction: an organization with unanchored zero-spend
-- savings would be shown a running estimate HIGHER than the fee that will ever
-- be charged, and every reconciliation of the screen against the invoice would
-- mismatch by the excluded amount.
--
-- So this reproduces 202607280013's function with one change: the estimate is
-- derived from public.billing_period_settlement_evidence directly -- the
-- anchored basis, its warm deduction and the ceiling that binds it -- instead of
-- from the gross numbers the guard happens to return. The guard call is kept
-- exactly as it was, because it is also how this function learns the billing
-- arrangement and how it degrades to 'guard_unavailable'.
--
-- Three evidence keys are added so the screen can explain the difference rather
-- than merely show a smaller number. No key is removed.
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
    v_evidence record;
    v_share numeric;
    v_basis numeric;
    v_warm numeric;
    v_basis_ceiling bigint;
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

    -- 202607280028: the projection is the WRITER'S arithmetic, over the WRITER'S
    -- basis. Same rounding, same warm deduction, same double ceiling, so this
    -- converges exactly to settle_billing_period's fee when the week closes.
    select * into v_evidence
      from public.billing_period_settlement_evidence(p_organization_id, v_start, v_end);
    v_share := coalesce(
        (v_gate->>'max_fee_share_of_verified_savings')::numeric, 0.25);
    v_basis := round(coalesce(v_evidence.billable_savings_basis_usd, 0), 10);
    v_warm := round(greatest(coalesce(v_evidence.warm_spend_usd, 0), 0), 10);
    v_basis_ceiling := floor(
        greatest(v_basis - v_warm, 0) * v_share * 1000000
    )::bigint;
    v_estimate := least(
        public.period_settlement_fee_microusd(greatest(v_basis - v_warm, 0), v_basis),
        v_basis_ceiling,
        v_ceiling
    );
    -- Report the ceiling that actually binds, not the looser gross one: a
    -- customer screen that shows a cap higher than anything we can charge is
    -- the same wrong-number failure in a different field.
    v_ceiling := least(v_ceiling, v_basis_ceiling);

    select
        coalesce(sum(settlement.fee_microusd) filter (
            where settlement.status = 'reported'), 0),
        coalesce(max(settlement.fee_microusd) filter (
            where settlement.superseded_at is null
              and settlement.status not in ('draft', 'void')), 0),
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
            'fee_ceiling_microusd', v_ceiling,
            -- 202607280028. What we can actually bill on, and what we cannot.
            'billable_savings_basis_usd', v_basis,
            'anchored_zero_spend_savings_usd',
                v_evidence.anchored_zero_spend_savings_usd,
            'unanchored_zero_spend_savings_usd',
                v_evidence.zero_spend_net_savings_usd
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
    'from service_role and scripts/ci asserts it. estimated_fee_microusd is an '
    'evidence PROJECTION for the live week, not a ledger read, and since '
    '202607280028 it is projected over the ANCHORED BASIS with the writer''s own '
    'arithmetic, so it converges exactly to the fee settle_billing_period will '
    'record when the week closes. evidence.billable_savings_basis_usd and '
    'evidence.unanchored_zero_spend_savings_usd are what let a screen explain '
    'the gap between a period''s savings and its billable savings. '
    'settled_fee_microusd is the ledger number (live row, excluding draft and '
    'void). Returns {ok:false, code} for no_billing_account / '
    'period_anchor_mismatch / guard_unavailable; the caller must render null, '
    'never zero.';

-- ---------------------------------------------------------------------------
-- 5. SELF-CHECK. Everything this file promises, asserted against the catalog
--    and against arithmetic, inside the same transaction that made it true.
-- ---------------------------------------------------------------------------
do $anchored_basis_selfcheck$
declare
    v_out_columns text;
    v_writer_source text;
begin
    -- The widened OUT list, in order.
    select string_agg(argument.name, ',' order by argument.ordinality)
      into v_out_columns
      from pg_proc procedure
      cross join lateral unnest(procedure.proargnames)
           with ordinality as argument(name, ordinality)
     where procedure.oid = to_regprocedure(
               'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)')
       and argument.ordinality > 3;
    if v_out_columns is distinct from
       'eligible_rows,zero_spend_rows,net_verified_savings_usd,'
       'zero_spend_net_savings_usd,actual_spend_usd,warm_spend_usd,'
       'warm_spend_days,usage_log_watermark_id,anchored_zero_spend_rows,'
       'anchored_zero_spend_savings_usd,unanchored_zero_spend_rows,'
       'billable_savings_basis_usd' then
        raise exception '202607280028 did not install the widened evidence OUT list: %',
            v_out_columns;
    end if;

    -- The writer really reads the basis, and really gates on the gross fee.
    select procedure.prosrc into v_writer_source
      from pg_proc procedure
     where procedure.oid = to_regprocedure(
               'public.settle_billing_period(uuid,timestamptz,text,boolean)');
    if v_writer_source not like '%billable_savings_basis_usd%'
       or v_writer_source not like '%v_gate_fee%' then
        raise exception '202607280028 left settle_billing_period on the un-narrowed basis';
    end if;

    -- The frozen guard is untouched: it must still read
    -- zero_spend_net_savings_usd, which is the column this file narrowed.
    if (select procedure.prosrc
          from pg_proc procedure
         where procedure.oid = to_regprocedure(
             'public.assert_billing_period_halting_conditions'
             '(uuid,timestamptz,timestamptz,bigint)'))
       not like '%zero_spend_net_savings_usd%' then
        raise exception '202607280028 expects the frozen 202607280008 guard to be present '
                        'and to consume zero_spend_net_savings_usd';
    end if;

    -- Privileges: neither new nor recreated function may be reachable from a
    -- browser role, and the anchor lookup must not be public.
    if has_function_privilege('anon',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)', 'execute')
       or has_function_privilege('authenticated',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)', 'execute')
       or has_function_privilege('anon',
           'public.billing_zero_spend_savings_anchor_id'
           '(uuid,timestamptz,text,text,boolean,text,text,text)', 'execute')
       or has_function_privilege('authenticated',
           'public.billing_zero_spend_savings_anchor_id'
           '(uuid,timestamptz,text,text,boolean,text,text,text)', 'execute')
       or has_function_privilege('anon',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'execute')
       or has_function_privilege('authenticated',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'execute') then
        raise exception '202607280028 left a billing function reachable from a browser role';
    end if;
    if not has_function_privilege('service_role',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)', 'execute')
       or not has_function_privilege('service_role',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'execute') then
        raise exception '202607280028 revoked a grant the settlement path needs';
    end if;

    -- The anchor index the probe depends on.
    if to_regclass('public.usage_log_paid_receipt_anchor_idx') is null then
        raise exception '202607280028 did not create the paid-receipt anchor index';
    end if;

    -- The anchor predicate refuses every non-replay shape without touching the
    -- table. These are pure argument checks, so they hold on an empty database.
    if public.billing_zero_spend_savings_anchor_id(
           gen_random_uuid(), now(), 'deepseek', 'deepseek-chat',
           false, 'exact_cache', 'verified', 'proxy') is not null then
        raise exception '202607280028 anchored a row Brevitas did not cause';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           gen_random_uuid(), now(), 'deepseek', 'deepseek-chat',
           true, 'compress', 'verified', 'proxy') is not null then
        raise exception '202607280028 anchored a row that is not a cache replay';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           gen_random_uuid(), now(), 'deepseek', 'deepseek-chat',
           true, 'exact_cache', 'degraded', 'proxy') is not null then
        raise exception '202607280028 anchored an unverified replay';
    end if;
    if public.billing_zero_spend_savings_anchor_id(
           gen_random_uuid(), now(), 'deepseek', 'deepseek-chat',
           true, 'exact_cache', 'verified', 'sdk') is not null then
        raise exception '202607280028 anchored a caller-asserted replay';
    end if;
end;
$anchored_basis_selfcheck$;

commit;
