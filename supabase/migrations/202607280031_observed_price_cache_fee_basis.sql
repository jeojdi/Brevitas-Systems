-- Phase 5 of the billing correctness rollout, ATTEMPT 2:
-- THE OBSERVED-PRICE CACHE FEE BASIS.
--
-- WHAT THIS FIXES, IN ONE SENTENCE: for an OpenAI-compatible tenant the only
-- organic savings Brevitas produces are exact-cache replays, every one of them
-- carries actual_cost_usd = 0 BY CONSTRUCTION (brevitas/proxy.py:649-651 ->
-- brevitas/receipts.py:381-386), and 202607280008's zero_spend_concentration
-- therefore halts such a period FOREVER -- so the product's own headline
-- mechanism is structurally unbillable. Observed verbatim on caestus for
-- organization f1305475-bc00-4ea0-bfc7-502cd2025191:
--     halting_condition=zero_spend_concentration
--     zero_spend_net_savings_usd = 0.0021019600 = net_verified_savings_usd
--     share = 1.00000 (limit 0.50), recomputed_fee_microusd = 525
--
-- THIS FILE IS NOT 202607280028. That file is QUARANTINED in docs/quarantine/
-- and must not be resurrected: its adversarial review returned do-not-ship with
-- one blocker and three highs, ALL REPRODUCED BY EXECUTION. Read
-- docs/quarantine/README.md before touching anything here. The four defects,
-- and where each is closed below:
--
--   1. THE FORWARD LINK IT BILLED ON DID NOT EXIST. usage_log
--      .savings_anchor_request_id appears in no migration, so its predicate
--      degraded to "this org once made ANY paid call for this provider+model at
--      any earlier time" -- an existence gate over a set, not a link to an
--      individual.                                     -> CLOSED by section 1+4.
--   2. NO MATERIALITY FLOOR. A single $0.0000000001 ancestor anchored $500 of
--      replays into a $125 fee.                        -> CLOSED by GUARD 1.
--   3. RETROACTIVE RE-ANCHORING. A paid row inserted AFTER settlement but
--      backdated into the period raised an already-settled fee from 2,500,000 to
--      3,500,000 microUSD with the watermark and row count UNCHANGED, defeating
--      the reproducibility 202607280013's watermark exists to guarantee.
--                                                      -> CLOSED by section 4's
--      watermark bound, which is now an INPUT as well as an output.
--   4. DILUTION. Narrowing the guard's numerator while leaving the gross
--      denominator let a large anchored component hide fabricated rows: a shape
--      that halts at share 1.00000 settled at 0.00200.
--      -> NOT CLOSED. An earlier revision of this header claimed it was, and
--      that claim was wrong. Reproduced against this file on PostgreSQL 17.10:
--      400 unanchored zero-spend rows worth $40,000 halt correctly on their own
--      (share 1.00000), but the SAME rows beside 5,000 legitimately anchored
--      replays drop the share to 0.44444 and the period settles. GUARD 3 bounds
--      the anchored component but does not touch the denominator the FROZEN
--      202607280008 guard divides by (net_verified_savings_usd, the gross
--      list-price sum), so legitimate savings still dilute the ratio.
--
--      WHAT THIS DOES AND DOES NOT MEAN. It is NOT an overcharge: the fee basis
--      is `spend_backed + anchored_capped` (see the SELECT at the end of
--      billing_period_settlement_evidence), and unanchored savings appear in no
--      fee expression anywhere in this file. In the reproduction the 750,000
--      uUSD fee came entirely from the anchored replays -- 3.000 x $1.00 of real
--      spend x 0.25 -- and the $40,000 of unanchored savings contributed zero.
--      Unanchored savings bill $0 by construction, which is a STRONGER
--      protection than the share guard ever provided.
--
--      What is genuinely lost is the ALARM. The frozen share guard used to be
--      the thing that stopped unverifiable savings from being billed; that job
--      now belongs to the basis, and the guard has been left measuring a ratio
--      that legitimate activity can quietly suppress. An operator loses the
--      signal that an account looks wrong. The fix is a NEW condition that
--      watches unanchored savings in ABSOLUTE terms against recorded provider
--      spend, so no amount of legitimate savings can dilute it -- 202607280008
--      is frozen and applied to production, so its share guard cannot be
--      re-aimed in place. Until that ships, treat the concentration condition as
--      informational rather than protective.
--
-- ============================================================================
-- THE DECISION (the owner's; this file implements it and does not relitigate it)
-- ============================================================================
--
-- THE PAID ANCESTOR IS THE PRICE SOURCE, NOT A GATE. A replay is billed at
--
--     25% of  tokens_saved x (the customer's OWN observed price per token)
--
-- so the fee cannot exceed what they demonstrably would have paid. 202607280028
-- treated the ancestor as a boolean; that is what let $1e-10 of paid traffic
-- unlock $125 of billing. Here the ancestor's existence establishes ELIGIBILITY
-- for exactly one replay row (the id equijoin), and the organization's own
-- receipted history for that model establishes the PRICE.
--
-- PER-ROW BILLABLE AMOUNT, for an eligible zero-spend replay r:
--
--     anchored_savings(r) = greatest(least(
--         r.verified_savings_usd,                       -- (A) list-price counterfactual
--         r.tokens_saved * observed_unit_cost(org, provider, model)
--     ), 0)                                             -- (B) observed-price reconstruction
--
-- PROOF THE FEE CANNOT EXCEED 25% OF WHAT THEY WOULD ACTUALLY HAVE PAID.
-- Let W(r) be what the customer would have paid the provider for the avoided
-- call. brevitas/receipts.py:364-405 with a replay's all-zero TokenReceipt gives
-- actual = 0 and baseline = (prompt_avoided * p_in + completion_avoided * p_out)
-- / 1e6, and api/server.py:4772 assigns measured (= baseline - 0) to verified.
-- So r.verified_savings_usd = W(r) EXACTLY. The least() gives
-- anchored_savings(r) <= W(r); the period basis is a sum of such terms floored at
-- the period gross by least() and capped by the spend ratio, all monotone
-- non-increasing; and the fee is
-- public.period_settlement_fee_microusd(net_after_warm, basis) = floor(least(...)
-- * 0.25 * 1e6) (202607280007 around line 274). Therefore
--     fee <= 0.25 * sum W(r).                                              QED.
--
-- WHICH TERM BINDS, AND IN WHICH DIRECTION. If the customer's real effective
-- price per token is BELOW our list table (aggregator discount, PTU, committed
-- throughput, a cache-heavy token mix), (B) is smaller and binds: the fee falls.
-- If their realised mix is output-heavy, (B) can exceed the list INPUT rate, but
-- (A) still caps the total at the full list saving including the output leg.
--
-- A HARD LIMIT THAT COULD NOT BE DESIGNED AROUND, STATED PLAINLY. usage_log has
-- no baseline_output_tokens column (20260710_cloud_usage.sql:36-43; 202607170012
-- adds only cache_write_5m/1h and cache_attributable). A replay stores
-- output_tokens = 0 and tokens_saved = prompt tokens only, so the AVOIDED
-- COMPLETION token count is unrecoverable from the row and term (B) can
-- reconstruct the INPUT leg only. The least() against (A) makes that safe: it
-- costs US revenue, never the customer money. That is the under-billing the
-- owner authorised. Fixing it properly needs a new usage_log column plus a write
-- in api/store.py::_usage_row, and is NOT attempted here.
--
-- WHAT "OBSERVED" HONESTLY MEANS TODAY. usage_log.actual_cost_usd is PUBLISHED
-- LIST PRICE resolved through the static MODEL_PRICES table
-- (brevitas/receipts.py:280-361), which has no organization dimension --
-- 202607280008 says so explicitly at its lines 64-75. So sum(cost)/sum(tokens)
-- is genuinely this organization's own TOKEN MIX at our LIST price per leg. It
-- bounds the fee correctly and becomes fully observed the moment a
-- provider-reported cost lands in that column, with no change to the arithmetic
-- below. A discounted/PTU contract is still handled separately and correctly by
-- the per-organization attestation in 202607280009.
--
-- ============================================================================
-- THE FORWARD LINK: TWO DB OBJECTS ARE MISSING, NOT ONE
-- ============================================================================
--
-- The link ALREADY EXISTS IN CODE and is inert only because the schema does not
-- persist it. The cache entry -- not the proxy at replay time, not the API --
-- is the side that knows the ancestor's identity:
--
--   1. MINT.     brevitas/proxy.py:775/778 mints `proxy:<...>` on a cache MISS;
--                that id is the paid row's usage_log.request_id.
--   2. STORE.    brevitas/proxy.py:546-579 `_cache_store(..., origin_request_id=)`
--                (called at proxy.py:1339 and :1519) carries it into the cache,
--                degrading to unanchored rather than losing the write.
--   3. PERSIST.  semantic_cache.py:116/222/230-233 (SQLite) and :855-861
--                (Supabase RPC `p_origin_request_id`).
--   4. RETRIEVE. semantic_cache.py:476/496/515/543, :730/762, :788.
--   5. CARRY.    brevitas/proxy.py:681/694 emits `savings_anchor_request_id`
--                AFTER **labels, so a tracking header cannot become an anchor.
--   6. TRAVEL.   api/server.py:4362 accepts it as a CLAIM (max_length 128).
--   7. DECIDE.   api/server.py:4431-4473 `_decide_savings_anchor` -- five
--                necessary conditions; a claim is never accepted.
--   8. RE-FLOOR. api/store.py:715-736 `_savings_anchor` on EVERY write path.
--   9. PERSIST.  public.usage_log.savings_anchor_request_id -- declared only in
--                api/store.py:165 and EXISTING IN NO MIGRATION. This file.
--
-- 202607280028 would have shipped (9) ALONE. THAT IS NOT SUFFICIENT AND IS A
-- FIFTH DEFECT THE QUARANTINE README DID NOT RECORD: with
-- semantic_cache.origin_request_id still absent, every hosted CacheHit carries
-- '' , so `_report_cache_hit` sends an empty anchor, `_decide_savings_anchor`
-- returns '', and EVERY replay is unanchored forever -- a mechanism that reads
-- as live and bills nothing. This file therefore ships BOTH halves.
--
-- A SECOND OPERATIONAL HAZARD, MEASURED, NOT HYPOTHETICAL: 202607170002 ends
-- with `drop function if exists public.semantic_cache_lookup(vector, text,
-- float, text, text)` followed by its own CREATE. Re-applying that file -- a
-- rollback drill, a hand repair, scripts/ci/migration-cache-rollback.sql +
-- reapply in the migration harness -- therefore SILENTLY RE-NARROWS the lookup
-- back to the version with no origin_request_id, at which point every hosted
-- CacheHit carries '' again and anchoring stops with no error anywhere.
-- Verified by execution against a fresh chain. If 202607170002 is ever
-- re-applied to a live project, RE-APPLY THIS FILE AFTER IT and restart the
-- process. Nothing in the schema can detect that state, because an unanchored
-- replay is a legal, fail-closed row.
--
-- OPERATIONALLY REQUIRED AND EASY TO FORGET: both
-- SemanticCache._anchor_supported (semantic_cache.py:698-711) and
-- store._anchor_column_warned (api/store.py:262-270) are PROCESS-LIFETIME
-- latches. The API/proxy process MUST be restarted after this migration or
-- anchoring stays off until it is.
--
-- ROWS WRITTEN BEFORE THIS SHIPS carry savings_anchor_request_id = '' (the ALTER
-- backfills the default). Under this design an empty anchor is UNANCHORED, full
-- stop. There is NO fallback to "same org + provider + model + any earlier paid
-- row" -- that path is DELETED, not weakened. An unanchored zero-spend row
-- contributes exactly $0 to the basis and is counted into the concentration
-- guard's numerator, so a period made entirely of pre-ship replays still halts
-- and still settles $0, verbatim the behaviour observed today. Every degradation
-- in the whole chain is fail-closed: a missing cache column, a tripped
-- _anchor_supported latch, a PGRST204 retry, a caller-supplied claim over
-- POST /v1/usage, a batch import -- all yield '' and all bill zero.
--
-- ============================================================================
-- THE THREE NEW GUARDS (all CHECK-bounded columns, tunable only downward)
-- ============================================================================
--
-- GUARD 1 -- MATERIALITY FLOOR, default $0.30 TOTAL per (provider, model) per
--   window. Evaluated once per (organization, provider, model), NOT per row: a
--   single DeepSeek request is ~$0.0005, so a per-request floor of any
--   defensible size would exclude 100% of real traffic. Below the floor every
--   replay for that (org, provider, model) is UNANCHORED -- out of the basis
--   entirely, not "billed at a reduced rate". Under this, $0.0000000001 of paid
--   traffic anchors NOTHING (1e-10 < 0.30), which is quarantine defect 2.
--
-- GUARD 2 -- LOOKBACK, default 30 days, applied to BOTH the link and the price
--   set. 202607280028 made the lookback deliberately UNBOUNDED, reasoning that
--   "a cache entry legitimately outlives a billing period". That is wrong on its
--   own facts: 202607170002_cache_security.sql:31-33 DELETES any entry with
--   expires_at > created_at + 24 hours, and the semantic_cache_positive_bounded
--   _ttl CHECK plus semantic_cache.py's clamp enforce it going forward. A
--   genuine ancestor is ALWAYS within 24 hours of its replay. 30 days is
--   therefore pure slack for the PRICE SET (a bursty tenant may have no paid
--   call for a model inside one 7-day Stripe period yet legitimately replay from
--   it) and is never load-bearing for the link. It also bounds the anchor scan
--   to one index range instead of the whole table.
--
-- GUARD 3 -- SPEND-RATIO CAP, default 3.000, applied at the PERIOD level:
--       anchored := least(sum(anchored_savings), ratio * actual_spend_usd)
--   where actual_spend_usd is the evidence function's EXISTING term, recomputed
--   from primary evidence and never supplied by a writer. It exists because
--   NOTHING ELSE in this design relates savings to spend: the link is per-row
--   and the floor is a one-time threshold. Replay-hammering -- one paid $0.35
--   call fills a cache entry, a loop replays it 1,000,000 times -- passes BOTH
--   the link and the floor, and without this cap yields a $8,500 basis and a
--   $2,125 fee against $0.35 of provider spend. With it the basis cannot exceed
--   3.000 * $0.35 = $1.05.
--
--   WHY 3.000, two independent derivations that agree:
--     (a) AS A HIT-RATE CEILING. With N requests at uniform cost c and hit rate
--         h, spend = (1-h)Nc and cache savings = hNc, so savings/spend =
--         h/(1-h). A ratio R asserts a maximum plausible hit rate of R/(1+R).
--         R = 3.000 is a 75% ceiling. R = 1.0 would be 50% -- too tight, it
--         would re-halt a genuinely excellent cache and recreate the drought
--         this work exists to end. Sustained above 75% across a whole billing
--         period, the shape is far more likely a benchmark loop or a retry storm
--         than production traffic, and an operator should look before we invoice.
--     (b) AS A FEE-VS-PROVIDER-BILL CEILING. Our rate is 0.25, so the cap bounds
--         the cache-derived fee at 0.25 * R * actual_spend. At R = 3.000 the fee
--         can never exceed 75% of the customer's own provider spend for that
--         period. R = 4.0 is the absolute explainability line where the fee could
--         equal their entire provider bill -- the same reasoning 202607280008
--         used to derive 0.50 ("the point past which the invoice stops being
--         explainable to the customer from their own provider bill"). 3.000 sits
--         strictly under it, and the CHECK bound at 3.000 means reaching 4.0
--         requires a reviewed migration.
--   This is a REASONED choice, not a measured one -- there is no hit-rate data in
--   this repository to validate it against, and the one observed period (caestus,
--   4 misses / 4 hits) sits at ratio ~1.0. Re-derive it from real data; it is a
--   column precisely so that costs a one-line UPDATE downward.
--
-- GUARD 0 (RETAINED, NOT NEW) -- ZERO-SPEND CONCENTRATION. The frozen
--   public.assert_billing_period_halting_conditions (202607280008:383-623) is
--   NOT edited. Its INPUT narrows: billing_period_settlement_evidence
--   .zero_spend_net_savings_usd -- its only decision-making consumer, at
--   202607280008:491 and :521 -- now reports UNANCHORED zero-spend savings only.
--   zero_spend_rows is NOT narrowed: it is operator diagnostics in halt detail
--   lines and must stay the true count. The DENOMINATOR stays the period's gross
--   netted savings, so the ratio keeps the meaning 0.50 was derived for, and a
--   period whose savings have no price source behind them AT ALL still sits at
--   share 1.00000 and still halts, exactly as today.
--
-- ============================================================================
-- NON-RETROACTIVITY: THE WATERMARK BECOMES AN INPUT
-- ============================================================================
--
-- The signature of quarantine defect 3 is its last clause: usage_row_count AND
-- usage_log_watermark_id both unchanged, fee changed. Root cause: the quarantined
-- anchor lookup bounded the ancestor by TIME ONLY (`ancestor.ts <= p_ts`,
-- docs/quarantine/202607280028...sql:307), and ts is WRITER-SUPPLIED
-- (api/store.py:755, `values.get("ts") or _now()`), so it is trivially
-- backdatable. The anchor set was the one input to the fee that nothing pinned.
--
-- THE MECHANISM: public.billing_period_settlement_evidence gains a TRAILING
-- p_usage_log_watermark_id bigint DEFAULT NULL, and EVERY row-producing subquery
-- inside it -- the eligible set, the link equijoin, and the price/materiality
-- set -- carries `and <row>.id <= v_watermark`. The returned
-- usage_log_watermark_id is that same value, so the number a settlement records
-- is the number that bounded every input it was derived from.
--
-- WHY THIS IS AIRTIGHT FOR THE STATED DEFECT: usage_log.id is
-- `bigint generated always as identity primary key` (20260710_cloud_usage.sql:19)
-- -- assigned by the server at INSERT, and impossible for any writer to supply,
-- backdate, or update. A row inserted after settlement necessarily receives an id
-- strictly greater than every id that existed at settlement time, REGARDLESS of
-- the ts it claims, so `id <= watermark` excludes it from the eligible set, the
-- link, and the price set SIMULTANEOUSLY. Hence
--     same (organization, period, watermark)  ==>  same evidence  ==>  same fee
-- and re-deriving a settled basis months later is one call:
--     select * from public.billing_period_settlement_evidence(
--         org, period_start, period_end, <the settled row's usage_log_watermark_id>);
-- The pin itself is immutable: 202607280007:329's identity trigger includes
-- usage_log_watermark_id, and deletion is blocked outright.
--
-- Revisions still work and are still honest: settle_billing_period passes NULL,
-- derives a NEW higher watermark, legitimately sees late arrivals, and APPENDS a
-- new ledger row with its own recorded watermark and its own reproducible basis.
--
-- THE ONE RESIDUAL RACE, STATED HONESTLY AND NOT FIXED HERE: identity values are
-- assigned at INSERT but transactions COMMIT out of order, so a concurrent
-- transaction holding id = M-1 can commit after the settlement's read snapshot,
-- producing a row below the watermark that was invisible when the evidence was
-- computed. This is NOT introduced here -- 202607280013's watermark already has
-- it, for the same reason -- and it is bounded to rows already in flight at
-- settlement time. Closing it properly needs an xmin/pg_current_snapshot() bound
-- recorded alongside the watermark, which touches a frozen column set. The
-- practical mitigation is what 202607280013 already enforces: settle only CLOSED
-- periods (period_not_closed), so in-flight writes have long since drained.
--
-- ============================================================================
-- WHAT THIS FILE REPLACES, AND THE ORDERING CONSEQUENCE
-- ============================================================================
--
-- Nothing at or below 202607280030 is edited. 202607280008 (the guard and the
-- thresholds table body), 202607280012 and 202607280013 stay frozen on disk;
-- their OBJECTS are replaced here, exactly as 0012 and 0013 replaced their own
-- predecessors. After this file, 202607280008/0012/0013 can no longer be
-- re-applied, because CREATE OR REPLACE cannot change a return type. That is
-- FAIL-CLOSED and deliberate: an out-of-order replay aborts loudly instead of
-- silently narrowing the evidence function out from under the writer. In
-- manifest order, including the harness's back-to-back double-apply, the chain
-- is clean. To roll back, DROP first -- see the ROLLBACK PROCEDURE at the end.
--
-- NOT CHANGED BY THIS FILE: assert_billing_period_halting_conditions,
-- assert_billing_period_settlement_allowed, promote_billing_period_settlement,
-- period_settlement_fee_microusd, period_settlement_ledger and every CHECK on
-- it, the 202607280029 claim path, the 202607280010 latches. No trigger is
-- added. Nothing is granted to anon or authenticated.
--
-- Safety: additive. Two nullable-free defaulted columns with backfills that are
-- the empty string, three defaulted config columns, three partial indexes, one
-- new helper function, one new RPC overload, and four recreated functions. It
-- can only LOWER a fee relative to 202607280013 (basis <= gross, always), and on
-- a database with no anchored rows it changes no settled number at all.

-- REVERSE: PITR-ONLY -- the widened evidence-function OUT list and the widened semantic_cache_lookup OUT list cannot be narrowed in place, and re-narrowing the basis re-halts every anchored period

begin;

set local lock_timeout = '15s';

-- ---------------------------------------------------------------------------
-- 0. PRECONDITIONS.
-- ---------------------------------------------------------------------------
do $observed_price_precondition$
declare
    v_required_column text;
begin
    if to_regclass('public.usage_log') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280031 requires public.usage_log',
            hint    = 'Apply 20260710_cloud_usage.sql first.';
    end if;
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280031 requires public.period_settlement_ledger from 202607280007';
    end if;
    if to_regclass('public.billing_halting_conditions') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280031 requires public.billing_halting_conditions from 202607280008',
            hint    = 'The three new thresholds are columns on that table, so that tightening '
                      'them is an UPDATE and loosening them is a reviewed migration.';
    end if;
    if to_regclass('public.warm_budget_ledger') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280031 requires public.warm_budget_ledger from 202607280001',
            hint    = 'The warm deduction is reproduced verbatim in the evidence function; '
                      'silently dropping it would overcharge.';
    end if;
    if to_regclass('public.semantic_cache') is null then
        raise exception using
            errcode = '42P01',
            message = '202607280031 requires public.semantic_cache from 202607170002',
            hint    = 'semantic_cache.origin_request_id is HALF the forward link. Shipping '
                      'usage_log.savings_anchor_request_id alone leaves the mechanism inert.';
    end if;

    -- Either signature satisfies this: the three-argument form on a first apply,
    -- the four-argument form on a re-apply of THIS file. The harness applies
    -- every migration back to back to prove idempotence, so accepting only the
    -- narrow form would make the second apply fail with a missing-prerequisite
    -- message that is a lie.
    if to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz)'
       ) is null
       and to_regprocedure(
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280031 requires public.billing_period_settlement_evidence '
                      'from 202607280008 (as corrected by 202607280012/0013)',
            hint    = 'This migration widens that function; it does not introduce it.';
    end if;
    if to_regprocedure('public.settle_billing_period(uuid,timestamptz,text,boolean)') is null then
        raise exception using
            errcode = '42883',
            message = '202607280031 requires public.settle_billing_period from 202607280013';
    end if;
    if to_regprocedure(
           'public.assert_billing_period_settlement_allowed(uuid,timestamptz,timestamptz,bigint)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280031 requires public.assert_billing_period_settlement_allowed '
                      'from 202607280009',
            hint    = 'That wrapper is the ONLY settlement door; the attestation cannot be '
                      'skipped.';
    end if;
    if to_regprocedure('public.period_settlement_fee_microusd(numeric,numeric)') is null then
        raise exception using
            errcode = '42883',
            message = '202607280031 requires public.period_settlement_fee_microusd from 202607280007',
            hint    = 'The writer must not duplicate the fee arithmetic that backs the table CHECK.';
    end if;
    if to_regprocedure(
           'public.billing_period_for_occurrence(timestamptz,timestamptz,timestamptz)'
       ) is null then
        raise exception using
            errcode = '42883',
            message = '202607280031 requires public.billing_period_for_occurrence from 202607170004';
    end if;

    -- The whole basis is recomputed from receipt-accounting columns. A missing
    -- one would silently degrade a term to NULL and change a fee.
    foreach v_required_column in array array[
        'id', 'organization_id', 'ts', 'request_id', 'provider', 'model',
        'authoritative', 'pricing_status', 'receipt_source', 'strategy',
        'quality_status', 'cache_attributable', 'actual_cost_usd',
        'verified_savings_usd', 'tokens_saved', 'fresh_input_tokens',
        'cached_input_tokens', 'cache_write_tokens', 'output_tokens'
    ] loop
        if not exists (
            select 1
              from information_schema.columns
             where table_schema = 'public'
               and table_name = 'usage_log'
               and column_name = v_required_column
        ) then
            raise exception using
                errcode = '42703',
                message = format('202607280031 requires public.usage_log.%s', v_required_column),
                hint    = 'The observed-price basis is recomputed from the receipt-accounting '
                          'column set (20260710_cloud_usage + 202607170012). Apply those first.';
        end if;
    end loop;

    -- Same refusal 202607280008:223-240 and 202607280012/0013 repeat. The
    -- per-row path reaches none of these conditions, so a second writer beside
    -- it would double-bill.
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception using
            errcode = '55000',
            message = '202607280031 refuses to install the observed-price fee basis while the '
                      'per-row fee trigger is attached',
            hint    = 'Two writers would bill the same savings twice, and the per-row path '
                      'reaches none of the halting conditions. Reapply 202607280006 first.';
    end if;
end;
$observed_price_precondition$;

-- ---------------------------------------------------------------------------
-- 1. THE FORWARD-LINK SCHEMA -- BOTH HALVES. This is what 202607280028 omitted.
-- ---------------------------------------------------------------------------

alter table public.usage_log
    add column if not exists savings_anchor_request_id text not null default '';

comment on column public.usage_log.savings_anchor_request_id is
    'request_id of the PAID usage_log row whose provider response this row '
    'replays. Non-empty only on an authoritative, Brevitas-attributable cache '
    'replay; server-DECIDED in api/server.py::_decide_savings_anchor and '
    're-floored in api/store.py::_savings_anchor -- it is never accepted as a '
    'caller claim. Empty means UNANCHORED, which is the fail-closed value: '
    'excluded from the fee basis entirely and counted into the zero-spend '
    'concentration guard''s numerator (202607280031).';

-- The link probe: given a replay, find its named ancestor. Note request_id is
-- unique only per (key_hash, request_id, authoritative) since 202607280026, so
-- this index is a probe, not a uniqueness claim -- see the exists() in the
-- evidence function.
create index if not exists usage_log_anchor_ancestor_idx
    on public.usage_log (organization_id, request_id)
    where authoritative and pricing_status = 'priced'
      and receipt_source = 'proxy' and actual_cost_usd > 0;

-- The price/materiality aggregate: paid rows per (org, provider, model, ts).
create index if not exists usage_log_observed_price_idx
    on public.usage_log (organization_id, provider, model, ts)
    where authoritative and pricing_status = 'priced'
      and receipt_source = 'proxy' and actual_cost_usd > 0;

-- The replay side.
create index if not exists usage_log_anchored_replay_idx
    on public.usage_log (organization_id, ts)
    where savings_anchor_request_id <> '';

alter table public.semantic_cache
    add column if not exists origin_request_id text not null default '';

comment on column public.semantic_cache.origin_request_id is
    'Metering id (brevitas.proxy._request_id, always in the proxy: namespace) of '
    'the PAID request whose response this entry stores. Read back onto '
    'CacheHit.origin_request_id and carried onto the replay receipt as '
    'usage_log.savings_anchor_request_id. Empty = unanchored = unbillable. '
    'Without this column every hosted CacheHit carries '''' and the entire '
    'anchoring mechanism reads as live while billing nothing (202607280031).';

-- A NEW 11-argument overload. The 10-argument version from 202607170002:156
-- STAYS, so semantic_cache.py's TypeError fallback path (semantic_cache.py:862)
-- still resolves and an older process degrades to unanchored rather than
-- failing the write. Body is 202607170002:167-218 plus the one column.
create or replace function public.semantic_cache_store_bounded(
    p_exact_hash text,
    p_context_hash text,
    p_model_id text,
    p_embedding vector(384),
    p_response_ciphertext text,
    p_tenant_namespace text,
    p_prompt_tokens integer,
    p_completion_tokens integer,
    p_ttl_seconds integer,
    p_max_entries integer,
    p_origin_request_id text
) returns void as $$
declare
    v_now timestamptz;
    v_ttl_seconds integer;
begin
    if p_max_entries < 1 or p_max_entries > 1000000 then
        raise exception 'semantic cache entry bound is invalid';
    end if;
    if octet_length(p_response_ciphertext) < 1
       or octet_length(p_response_ciphertext) > 16777216 then
        raise exception 'semantic cache ciphertext exceeds its absolute bound';
    end if;

    -- Transaction-scoped and shared by the trigger. The lock is acquired before
    -- every state read/write so concurrent PostgREST replicas cannot each observe
    -- a below-cap snapshot and overshoot after committing.
    perform pg_advisory_xact_lock(
        hashtextextended('brevitas.semantic_cache.write_bound.v1', 0)
    );
    v_now := clock_timestamp();
    v_ttl_seconds := least(86400, greatest(1, coalesce(p_ttl_seconds, 3600)));
    delete from public.semantic_cache where expires_at <= v_now;

    insert into public.semantic_cache (
        exact_hash, context_hash, model_id, embedding, response_json,
        response_ciphertext, tenant_namespace, prompt_tokens, completion_tokens,
        created_at, expires_at, origin_request_id
    ) values (
        p_exact_hash, p_context_hash, p_model_id, p_embedding, null,
        p_response_ciphertext, p_tenant_namespace,
        least(2000000000, greatest(0, coalesce(p_prompt_tokens, 0))),
        least(2000000000, greatest(0, coalesce(p_completion_tokens, 0))),
        v_now, v_now + make_interval(secs => v_ttl_seconds),
        left(coalesce(p_origin_request_id, ''), 128)
    )
    on conflict (exact_hash) do update set
        context_hash = excluded.context_hash,
        model_id = excluded.model_id,
        embedding = excluded.embedding,
        response_json = null,
        response_ciphertext = excluded.response_ciphertext,
        tenant_namespace = excluded.tenant_namespace,
        prompt_tokens = excluded.prompt_tokens,
        completion_tokens = excluded.completion_tokens,
        created_at = excluded.created_at,
        expires_at = excluded.expires_at,
        origin_request_id = excluded.origin_request_id,
        hit_count = 0;

    delete from public.semantic_cache
     where exact_hash in (
        select exact_hash from public.semantic_cache
         order by created_at desc, exact_hash desc
         offset p_max_entries
     );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public, extensions;

revoke all on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    integer, integer, text
) from public, anon, authenticated;
grant execute on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer,
    integer, integer, text
) to service_role;

comment on function public.semantic_cache_store_bounded(
    text, text, text, vector, text, text, integer, integer, integer, integer, text
) is
    'Anchor-carrying overload of the bounded cache write (202607280031). '
    'Identical to the 202607170002 ten-argument version -- same advisory lock, '
    'same purge, same eviction, same bounds -- plus origin_request_id, the id of '
    'the PAID request whose response is being stored. The ten-argument version '
    'is deliberately retained so semantic_cache.py''s TypeError fallback still '
    'resolves and an un-restarted process degrades to unanchored (and therefore '
    'unbillable) rather than losing the write.';

-- The OUT list widens, so DROP + CREATE: PostgreSQL cannot change OUT columns
-- with CREATE OR REPLACE, the same reason 202607170002:234-236 gave. DROP
-- discards the ACL, so both the revoke and the grant are restated.
drop function if exists public.semantic_cache_lookup(vector, text, float, text, text);

create function public.semantic_cache_lookup(
    p_embedding vector(384),
    p_context_hash text,
    p_threshold float,
    p_tenant_namespace text,
    p_model_id text
) returns table (
    exact_hash text,
    response_ciphertext text,
    prompt_tokens integer,
    completion_tokens integer,
    similarity float,
    origin_request_id text
) as $$
    select cache.exact_hash, cache.response_ciphertext, cache.prompt_tokens,
           cache.completion_tokens,
           (1 - (cache.embedding <=> p_embedding))::float as similarity,
           cache.origin_request_id
      from public.semantic_cache cache
     where cache.context_hash = p_context_hash
       and cache.tenant_namespace = p_tenant_namespace
       and cache.model_id = p_model_id
       and cache.expires_at > now()
       and cache.embedding is not null
       and cache.response_ciphertext <> ''
       and (1 - (cache.embedding <=> p_embedding)) >= p_threshold
     order by cache.embedding <=> p_embedding
     limit 1;
$$ language sql security invoker set search_path = pg_catalog, public, extensions;

revoke all on function public.semantic_cache_lookup(vector, text, float, text, text)
    from public, anon, authenticated;
grant execute on function public.semantic_cache_lookup(vector, text, float, text, text)
    to service_role;

comment on function public.semantic_cache_lookup(vector, text, float, text, text) is
    'Semantic cache read, widened by origin_request_id (202607280031) so a hosted '
    'CacheHit can carry the id of the paid request it replays. Without that '
    'column the replay receipt''s savings_anchor_request_id is always empty and '
    'every replay is permanently unbillable.';

-- ---------------------------------------------------------------------------
-- 2. CONFIG COLUMNS on public.billing_halting_conditions.
--
--    Same posture 202607280008:244-294 established: CHECK-bounded so an operator
--    can TIGHTEN in an incident but can only LOOSEN via a reviewed migration,
--    every bound equal to its default (the 0.25/0.50 pattern), no new grant, RLS
--    already on with zero policies and select-only to service_role. ADD COLUMN
--    with a NOT NULL default backfills the existing singleton row, so no
--    separate seeding statement is needed or wanted.
-- ---------------------------------------------------------------------------
alter table public.billing_halting_conditions
    add column if not exists cache_anchor_materiality_floor_usd numeric(12,4) not null default 0.3000;
alter table public.billing_halting_conditions
    add column if not exists cache_anchor_lookback_days integer not null default 30;
alter table public.billing_halting_conditions
    add column if not exists max_cache_savings_per_spend_ratio numeric(6,3) not null default 3.000;

-- Tightening the floor means RAISING it. The upper bound of 1000.00 is a
-- fat-finger stop, not a policy: a floor typo'd to 300000 silently unbills every
-- organization forever, which is a different failure from an overcharge but
-- still worth bounding.
alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_cache_floor_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_cache_floor_check
    check (cache_anchor_materiality_floor_usd >= 0.30
       and cache_anchor_materiality_floor_usd <= 1000.00);

-- Tightening the lookback means SHORTENING it.
alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_cache_lookback_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_cache_lookback_check
    check (cache_anchor_lookback_days between 1 and 30);

-- Tightening the ratio means LOWERING it.
alter table public.billing_halting_conditions
    drop constraint if exists billing_halting_conditions_cache_spend_ratio_check;
alter table public.billing_halting_conditions
    add constraint billing_halting_conditions_cache_spend_ratio_check
    check (max_cache_savings_per_spend_ratio >= 0
       and max_cache_savings_per_spend_ratio <= 3.000);

comment on column public.billing_halting_conditions.cache_anchor_materiality_floor_usd is
    'GUARD 1. Minimum TOTAL priced provider spend (USD) an organization must have '
    'recorded for one (provider, model) inside the lookback window before any '
    'cache replay of that model may enter the fee basis. Per (provider, model) '
    'per window, NOT per request: a single DeepSeek request is ~$0.0005, so a '
    'per-request floor of any defensible size would exclude 100% of real '
    'traffic. Below the floor every replay for that tuple is UNANCHORED and '
    'contributes $0 -- it is not billed at a reduced rate. Bounded [0.30, '
    '1000.00]: raising it is tightening.';
comment on column public.billing_halting_conditions.cache_anchor_lookback_days is
    'GUARD 2. How far back an anchoring paid request may be, applied to BOTH the '
    'per-row link and the observed-price set. The cache itself cannot outlive 24 '
    'hours (202607170002:31-33 deletes anything with expires_at > created_at + 24 '
    'hours), so this is pure slack for the PRICE SET -- a bursty tenant may have '
    'no paid call for a model inside one 7-day Stripe period yet legitimately '
    'replay from it -- and is never load-bearing for the link. Bounded [1, 30]: '
    'shortening it is tightening.';
comment on column public.billing_halting_conditions.max_cache_savings_per_spend_ratio is
    'GUARD 3. Maximum ratio of billable cache savings to the SAME period''s '
    'recomputed actual provider spend. Nothing else in the basis relates savings '
    'to spend, so without this one paid call plus a replay loop manufactures '
    'unbounded savings. 3.000 encodes a 75%% cache hit-rate ceiling (R/(1+R)) and '
    'caps the cache-derived fee at 75%% of the customer''s own provider bill for '
    'the period; 4.0 is the explainability line where the fee could equal that '
    'bill entirely. A reasoned choice, not a measured one -- re-derive it from '
    'real hit-rate data. Bounded [0, 3.000]: lowering it is tightening.';

-- ---------------------------------------------------------------------------
-- 3. THE OBSERVED UNIT COST, as a named function.
--
--    It exists as a function rather than an inlined CTE so an operator can
--    explain any settlement line by line without re-deriving the writer's
--    arithmetic:
--        select * from public.billing_observed_model_price(
--            org, period_start, period_end, 30, <the settled watermark>);
--
--    WHICH FOUR TOKEN FIELDS, AND WHY NO OTHERS: fresh_input, cached_input,
--    cache_write and output are EXACTLY the four legs brevitas/receipts.py:382-389
--    prices. cache_write_5m/1h are a SUBDIVISION of cache_write_tokens
--    (receipts.py:17-21, 377-381) and adding them would double-count the same
--    tokens; provider_input_tokens_avoided and baseline_tokens are
--    counterfactual, not billed.
--
--    WHAT sum(cost)/sum(tokens) HONESTLY IS: the average dollars-per-token this
--    organization actually paid for this model over its own receipted traffic in
--    the window, at whatever mix of fresh/cached/write/output tokens it actually
--    ran. It is a DOLLAR-WEIGHTED AVERAGE, not a per-leg decomposition:
--    actual_cost_usd is a single scalar per row and the four leg prices cannot be
--    algebraically inverted from it (four unknowns, one equation). No
--    decomposition is invented here.
--
--    MID-WINDOW PRICE CHANGE needs no special case and gets none: the ratio is a
--    token-weighted mean over the whole window, so a cut on day 3 drags it down
--    in proportion to the tokens that ran after it and a rise drags it up -- and
--    a rise cannot raise the fee above the list-price bound, because of the
--    least() in the evidence function. A settlement's price is a fact about that
--    settlement's own window.
--
--    EVERY CLAUSE OF THE ELIGIBILITY PREDICATE EARNS ITS PLACE:
--      authoritative           caller-reported telemetry is never billing input
--                              and is never evidence of a payment (server.py:4441)
--      pricing_status='priced' an unpriced row has actual_cost_usd NULL and would
--                              poison the ratio
--      receipt_source='proxy'  it produced a provider receipt through us
--      actual_cost_usd > 0     real money moved -- and this is also what makes a
--                              zero-spend replay incapable of pricing itself
--      id <= watermark         the anti-retroactivity bound
--      ts >= start - lookback  GUARD 2
--      ts <  period_end        a settlement never prices itself from the future
-- ---------------------------------------------------------------------------
create or replace function public.billing_observed_model_price(
    p_organization_id uuid,
    p_period_start timestamptz,
    p_period_end timestamptz,
    p_lookback_days integer,
    p_usage_log_watermark_id bigint
)
returns table (
    provider text,
    model text,
    paid_cost_usd numeric,
    paid_tokens bigint,
    observed_unit_cost_usd numeric
)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    select
        paid.provider,
        paid.model,
        sum(paid.actual_cost_usd)::numeric,
        sum(paid.fresh_input_tokens + paid.cached_input_tokens
            + paid.cache_write_tokens + paid.output_tokens)::bigint,
        (sum(paid.actual_cost_usd)
           / nullif(sum(paid.fresh_input_tokens + paid.cached_input_tokens
                        + paid.cache_write_tokens + paid.output_tokens), 0))::numeric
      from public.usage_log paid
     where paid.organization_id = p_organization_id
       and paid.authoritative
       and paid.pricing_status = 'priced'
       and paid.receipt_source = 'proxy'
       and paid.actual_cost_usd > 0
       and paid.ts >= p_period_start - make_interval(days => greatest(coalesce(p_lookback_days, 0), 0))
       and paid.ts <  p_period_end
       and paid.id <= p_usage_log_watermark_id
     group by paid.provider, paid.model;
$$;

revoke all on function public.billing_observed_model_price(
    uuid, timestamptz, timestamptz, integer, bigint
) from public, anon, authenticated;
grant execute on function public.billing_observed_model_price(
    uuid, timestamptz, timestamptz, integer, bigint
) to service_role;

comment on function public.billing_observed_model_price(
    uuid, timestamptz, timestamptz, integer, bigint
) is
    'The customer''s OWN observed price per token, per (provider, model), over '
    'their authoritative priced proxy receipts with actual_cost_usd > 0 inside '
    '[period_start - lookback, period_end) and at or below the settlement '
    'watermark. paid_cost_usd is also the GUARD 1 materiality total. The four '
    'summed token legs are exactly the four brevitas/receipts.py prices; '
    'cache_write_5m/1h are a subdivision of cache_write_tokens and are '
    'deliberately excluded to avoid double counting. observed_unit_cost_usd is a '
    'DOLLAR-WEIGHTED average -- this organization''s own token mix at our list '
    'price per leg, not a contracted rate -- and is NULL when no tokens were '
    'billed, which the caller must treat as UNANCHORED rather than as a free '
    'pass. A null watermark yields no rows, on purpose.';

-- ---------------------------------------------------------------------------
-- 4. THE EVIDENCE FUNCTION, replaced.
--
--    IN list gains a TRAILING p_usage_log_watermark_id bigint DEFAULT NULL, so
--    the existing three-argument call in the CHECKSUM-FROZEN guard
--    (202607280008:444-447) still resolves unchanged and is not touched.
--
--    OUT list gains six columns AFTER the existing eight, so `select * into
--    <record>` in every caller binds nothing incorrectly.
--
--    zero_spend_net_savings_usd (position 4, the frozen guard's numerator) now
--    reports UNANCHORED zero-spend savings only. zero_spend_rows (position 2) is
--    NOT narrowed: operator diagnostics need the true count.
--
--    THE LINK IS AN EQUIJOIN ON THE ID, and it uses exists(), never a scalar
--    subquery: request_id is unique only per (key_hash, request_id,
--    authoritative) since 202607280026, and scoping by organization_id +
--    authoritative + receipt_source='proxy' + actual_cost_usd>0 makes it
--    single-valued in practice but not provably so from the schema. exists()
--    means a hypothetical duplicate cannot fan out the savings sum.
-- ---------------------------------------------------------------------------
drop function if exists public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz
);
-- ...and the widened form too, so a back-to-back re-apply of THIS file replaces
-- its own function instead of failing with 'already exists with same argument
-- types'. The harness double-applies every migration on purpose.
drop function if exists public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz, bigint
);

create function public.billing_period_settlement_evidence(
    p_organization_id uuid,
    p_period_start timestamptz,
    p_period_end timestamptz,
    p_usage_log_watermark_id bigint default null
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
    observed_priced_models bigint,
    cache_savings_spend_ratio numeric,
    billable_savings_basis_usd numeric
)
language sql
stable
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    with limits as (
        -- Scalar subqueries rather than a join, so a MISSING thresholds row
        -- yields exactly one row of fail-closed sentinels (an unreachable floor,
        -- no lookback, no ratio headroom) instead of collapsing the eligible set
        -- and lying about eligible_rows. Every settlement caller raises
        -- halting_condition=unconfigured long before reaching this anyway.
        select
            coalesce(
                (select conditions.cache_anchor_materiality_floor_usd
                   from public.billing_halting_conditions conditions
                  where conditions.singleton),
                1000000000000::numeric
            ) as floor_usd,
            coalesce(
                (select conditions.cache_anchor_lookback_days
                   from public.billing_halting_conditions conditions
                  where conditions.singleton),
                0
            ) as lookback_days,
            coalesce(
                (select conditions.max_cache_savings_per_spend_ratio
                   from public.billing_halting_conditions conditions
                  where conditions.singleton),
                0::numeric
            ) as spend_ratio
    ),
    watermark as (
        -- The settlement's pin. Supplied => this call reproduces that
        -- settlement exactly. NULL => derive a fresh one over the SAME eligible
        -- predicate every column below uses, so the watermark can never disagree
        -- with eligible_rows about which rows were folded in. NULL for an empty
        -- period, on purpose: a period with no rows has no watermark, and 0
        -- would be a lie about row id 0.
        select coalesce(
            p_usage_log_watermark_id,
            (select max(fresh.id)
               from public.usage_log fresh
              where fresh.organization_id = p_organization_id
                and fresh.authoritative
                and fresh.pricing_status = 'priced'
                and fresh.ts >= p_period_start
                and fresh.ts <  p_period_end)
        ) as mark
    ),
    price as (
        -- GUARD 1 is applied HERE, once per (provider, model), so a tuple below
        -- the materiality floor simply has no price and every replay of it falls
        -- out of the basis.
        select observed.provider as pv,
               observed.model as md,
               observed.observed_unit_cost_usd as unit_cost
          from limits
          cross join watermark
          cross join lateral public.billing_observed_model_price(
              p_organization_id, p_period_start, p_period_end,
              limits.lookback_days, watermark.mark
          ) observed
         where observed.paid_cost_usd >= limits.floor_usd
           and observed.observed_unit_cost_usd is not null
    ),
    eligible as (
        select
            usage.actual_cost_usd as row_cost,
            coalesce(usage.verified_savings_usd, 0) as row_savings,
            (coalesce(usage.actual_cost_usd, 0) <= 0) as is_zero_spend,
            (
                    coalesce(usage.actual_cost_usd, 0) <= 0
                and usage.cache_attributable
                and usage.receipt_source = 'proxy'
                and usage.quality_status = 'verified'
                and (usage.strategy like 'exact_cache%' or usage.strategy like 'semantic_cache%')
                and usage.savings_anchor_request_id <> ''
                and usage.savings_anchor_request_id <> usage.request_id
                and price.unit_cost is not null
                and exists (
                    select 1
                      from public.usage_log ancestor
                     where ancestor.organization_id = usage.organization_id
                       and ancestor.request_id = usage.savings_anchor_request_id
                       and ancestor.authoritative
                       and ancestor.pricing_status = 'priced'
                       and ancestor.receipt_source = 'proxy'
                       and ancestor.actual_cost_usd > 0
                       and ancestor.id <= watermark.mark
                       and ancestor.ts <= usage.ts
                       and ancestor.ts >= usage.ts
                                          - make_interval(days => limits.lookback_days)
                )
            ) as is_anchored,
            greatest(
                least(
                    coalesce(usage.verified_savings_usd, 0),
                    usage.tokens_saved * price.unit_cost
                ),
                0
            ) as row_anchored_savings
          from public.usage_log usage
          cross join limits
          cross join watermark
          left join price
            on price.pv = usage.provider
           and price.md = usage.model
         where usage.organization_id = p_organization_id
           and usage.authoritative
           and usage.pricing_status = 'priced'
           and usage.ts >= p_period_start
           and usage.ts <  p_period_end
           and usage.id <= watermark.mark
    ),
    totals as (
        select
            count(*)::bigint as rows_all,
            count(*) filter (where eligible.is_zero_spend)::bigint as rows_zero,
            count(*) filter (where eligible.is_anchored)::bigint as rows_anchored,
            count(*) filter (
                where eligible.is_zero_spend and not eligible.is_anchored
            )::bigint as rows_unanchored,
            coalesce(sum(eligible.row_savings), 0)::numeric as net_verified,
            -- The frozen guard's numerator, narrowed to UNANCHORED zero-spend
            -- savings. Its denominator is untouched.
            coalesce(sum(
                case when eligible.is_zero_spend and not eligible.is_anchored
                    then eligible.row_savings else 0 end
            ), 0)::numeric as unanchored_savings,
            coalesce(sum(
                case when eligible.is_zero_spend then 0 else eligible.row_savings end
            ), 0)::numeric as spend_backed_savings,
            coalesce(sum(
                case when eligible.is_anchored then eligible.row_anchored_savings else 0 end
            ), 0)::numeric as anchored_savings_raw,
            coalesce(sum(greatest(coalesce(eligible.row_cost, 0), 0)), 0)::numeric as spend
          from eligible
    ),
    capped as (
        -- GUARD 3. actual provider spend is the evidence function's own existing
        -- term, recomputed from primary evidence and never supplied by a writer.
        select
            totals.*,
            least(
                totals.anchored_savings_raw,
                (select limits.spend_ratio from limits) * totals.spend
            ) as anchored_capped
          from totals
    )
    select
        capped.rows_all,
        capped.rows_zero,
        capped.net_verified,
        capped.unanchored_savings,
        capped.spend,
        -- Warm-ping spend does not live in usage_log: warm receipts are recorded
        -- with verified_savings_usd = 0, so the sums above cannot see this cost.
        -- Summed from its own ledger over every UTC day whose [day, day+1) span
        -- OVERLAPS the period. A boundary day is deducted by both adjacent
        -- periods, exactly as 202607280007 specifies; that can only lower a
        -- ceiling. All providers count. Reproduced verbatim from 202607280012/13.
        (
            select coalesce(sum(greatest(warm.spent_usd, 0)), 0)::numeric
              from public.warm_budget_ledger warm
             where warm.organization_id = p_organization_id
               and (warm.day::timestamp at time zone 'UTC') < p_period_end
               and ((warm.day + 1)::timestamp at time zone 'UTC') > p_period_start
        ),
        -- DISTINCT day, not row count (202607280012). Operator diagnostics; no
        -- arithmetic consumes it.
        (
            select count(distinct warm.day)::bigint
              from public.warm_budget_ledger warm
             where warm.organization_id = p_organization_id
               and (warm.day::timestamp at time zone 'UTC') < p_period_end
               and ((warm.day + 1)::timestamp at time zone 'UTC') > p_period_start
        ),
        (select watermark.mark from watermark),
        capped.rows_anchored,
        capped.anchored_capped,
        capped.rows_unanchored,
        (select count(*)::bigint from price),
        -- PRE-cap ratio, operator diagnostics only. NULL when there is no spend
        -- to divide by, never 0 -- a shape with savings and no spend is exactly
        -- what halting_condition=zero_spend is for, and reporting 0 would read
        -- as "healthy".
        case when capped.spend > 0
            then round(capped.anchored_savings_raw / capped.spend, 6)
            else null
        end,
        least(capped.spend_backed_savings + capped.anchored_capped, capped.net_verified)
      from capped;
$$;

-- DROP discarded the ACL and the comment. Restate both.
revoke all on function public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz, bigint
) from public, anon, authenticated;
grant execute on function public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz, bigint
) to service_role;

comment on function public.billing_period_settlement_evidence(
    uuid, timestamptz, timestamptz, bigint
) is
    'Aggregate billing evidence for one (organization, period) over authoritative '
    'priced usage_log rows in [period_start, period_end) at or below the '
    'settlement watermark. Savings are netted across the period and are NOT '
    'floored per row. p_usage_log_watermark_id (202607280031) is OPTIONAL and '
    'trailing so the frozen guard''s three-argument call still resolves: pass the '
    'watermark a settlement recorded and this function reproduces that '
    'settlement''s evidence EXACTLY, no matter what has been written since -- pass '
    'NULL and it derives a fresh watermark and sees late arrivals. '
    'billable_savings_basis_usd is the fee basis: spend-backed savings plus '
    'ANCHORED cache savings priced at the organization''s own observed cost per '
    'token, capped by max_cache_savings_per_spend_ratio and floored at the '
    'period gross. zero_spend_net_savings_usd reports UNANCHORED zero-spend '
    'savings only -- it is the frozen concentration guard''s numerator -- while '
    'zero_spend_rows stays the TRUE count for operator diagnostics. '
    'warm_spend_usd is summed independently from warm_budget_ledger over the UTC '
    'days overlapping the period (boundary days counted by both adjacent '
    'periods, which can only lower a fee) and is recomputed here precisely so no '
    'settlement writer can supply it; warm_spend_days counts DISTINCT overlapping '
    'days (202607280012). cache_savings_spend_ratio is the PRE-cap diagnostic '
    'ratio, NULL when the period has no spend.';

-- ---------------------------------------------------------------------------
-- 5. THE WRITER, replaced.
--
--    Reproduced from 202607280013 with exactly three changes, all in step 7 and
--    step 11. Steps 0-6 and 8-12 are 202607280013 behaviour verbatim.
--
--      (a) The row records the BASIS (billable_savings_basis_usd), not the gross.
--      (b) TWO fees are derived: v_gate_fee from the UN-NARROWED gross, and
--          v_fee from the basis. The row carries v_fee.
--      (c) Step 11 gates on v_gate_fee.
--
--    WHY GATE ON THE GROSS -- this is 202607280028's one piece of reasoning that
--    survives review unchanged, and it is the CONSERVATIVE direction. An
--    organization whose savings are 100% unanchored derives a $0 basis and hence
--    a $0 fee, and every halting condition in 202607280008 is enforced only for
--    a POSITIVE fee ("settling zero is always allowed: it moves no money").
--    Gating on $0 would therefore SKIP both zero-spend tests and hand that
--    organization a silent $0 draft instead of a halt that names the condition.
--    The halt is the operator-visible signal that something is structurally
--    wrong; losing it would make the drought this migration exists to end
--    invisible. Since basis <= gross always, promote's re-run of the frozen
--    guard on the ACTUAL fee can never reject a draft this writer produced.
--
--    EXPLICITLY UNCHANGED, and asserted by the self-check below: the 25% rate
--    read from billing_halting_conditions; the warm deduction taken from the
--    SMALLER basis (the lower fee); the cumulative ceiling in the frozen guard;
--    the PSL latches (this writer still writes only 'draft', never touches
--    outbound_started_at / reported_at / settled_at, never voids); and step 9's
--    promote-door predicate `status in ('pending','sending','reported') or
--    outbound_started_at is not null` with 'pending' PRESENT.
--
--    Recorded verified_savings_usd stays SIGNED -- a losing week must be
--    representable and net_savings_usd is the generated column that floors it.
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
    -- 0. Arguments. Programmer errors, not settlement outcomes, so they RAISE
    --    rather than returning a body a batch loop would count as "blocked".
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
    -- 1. Serialize on the organization, on the SAME advisory key the claim path
    --    uses, so settlement serialises against both.
    ------------------------------------------------------------------
    perform pg_advisory_xact_lock(hashtextextended(p_organization_id::text, 0));

    ------------------------------------------------------------------
    -- 2. Enrollment. Mirrors the retired per-row trigger's predicate so the
    --    aggregate path cannot bill an organization the per-row path skipped.
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
    -- 4. Grid guard. A Stripe anchor that moves by a non-multiple of 604800
    --    seconds re-slices history and the SAME usage settles twice.
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
    -- 5. Attribution, snapshotted at settlement and immutable thereafter.
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
    --    and every fee derived from them come from the SAME snapshot.
    --
    --    p_usage_log_watermark_id is NULL here ON PURPOSE: a settlement derives
    --    a FRESH watermark, and the value it records is the value that bounded
    --    the eligible set, the anchor link and the price set inside this call.
    --    Re-passing it to billing_period_settlement_evidence() later reproduces
    --    this basis exactly. A revision (p_allow_revision) likewise derives a
    --    NEW, higher watermark and legitimately sees late arrivals -- as a NEW
    --    ledger row, never as a silent change to a settled number.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(p_organization_id, v_start, v_end);

    -- Rounded to the ledger's declared scale BEFORE any fee is derived, so the
    -- number the CHECK constraint re-derives from the stored columns is the
    -- exact number the fee came from.
    v_gross_verified := round(v_evidence.net_verified_savings_usd, 10);
    v_verified := round(v_evidence.billable_savings_basis_usd, 10);
    v_warm := round(greatest(v_evidence.warm_spend_usd, 0), 10);
    v_net_after_warm := greatest(v_verified - v_warm, 0);
    v_gross_net_after_warm := greatest(v_gross_verified - v_warm, 0);

    select conditions.max_fee_share_of_verified_savings into v_share
      from public.billing_halting_conditions conditions
     where conditions.singleton;
    if not found then
        return jsonb_build_object(
            'ok', false, 'outcome', 'halted', 'code', 'unconfigured',
            'halting_condition', 'unconfigured',
            'organization_id', p_organization_id,
            'period_start', v_start, 'period_end', v_end,
            'message', 'halting_condition=unconfigured: billing_halting_conditions has no row'
        );
    end if;

    -- The gross ceiling is the one the FROZEN guard will compute, from the
    -- UNROUNDED evidence, because that guard reads net_verified_savings_usd.
    v_gross_ceiling_microusd := floor(
        greatest(
            v_evidence.net_verified_savings_usd - greatest(v_evidence.warm_spend_usd, 0),
            0
        ) * v_share * 1000000
    )::bigint;
    -- The basis ceiling is what the ledger's own fee CHECK will re-derive from
    -- the recorded verified_savings_usd / warm_spend_usd columns.
    v_ceiling_microusd := floor(v_net_after_warm * v_share * 1000000)::bigint;

    -- ONE arithmetic definition, shared with the table CHECK
    -- (period_settlement_fee_microusd, 202607280007 around line 274). The
    -- least()s are backstops: the terms are provably equal while
    -- max_fee_share_of_verified_savings is 0.25, and if it is LOWERED in an
    -- incident this settles at the lowered ceiling instead of halting.
    v_gate_fee := least(
        public.period_settlement_fee_microusd(v_gross_net_after_warm, v_gross_verified),
        v_gross_ceiling_microusd
    );
    v_fee := least(
        public.period_settlement_fee_microusd(v_net_after_warm, v_verified),
        v_ceiling_microusd,
        v_gross_ceiling_microusd
    );

    ------------------------------------------------------------------
    -- 8. IDEMPOTENCE, checked BEFORE the guard on purpose: an unchanged period
    --    must report 'unchanged' even once its own row has been promoted and
    --    reported, at which point re-running the guard would correctly halt on
    --    the cumulative ceiling.
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
    -- 9. Already committed to Stripe? Refuse, always. 'pending' MUST stay in
    --    this predicate: the moment promote_billing_period_settlement moves a
    --    draft to 'pending' the money IS queued, the claim path will pick it up,
    --    and Stripe deduplicates nothing because the idempotency keys differ.
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
    --     revision is an operator act.
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
    -- 11. THE GATE, on the UN-NARROWED gross fee. See the header note above
    --     for why: a $0 basis must still HALT with a named condition rather
    --     than becoming a silent $0 draft.
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
                'billable_savings_basis_usd', v_verified,
                'gross_verified_savings_usd', v_gross_verified,
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
        -- SIGNED, and now the BILLABLE BASIS rather than the gross.
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
        'gate_fee_microusd', v_gate_fee,
        'fee_ceiling_microusd', least(v_ceiling_microusd, v_gross_ceiling_microusd),
        'committed_period_microusd', v_committed,
        'verified_savings_usd', v_verified,
        'gross_verified_savings_usd', v_gross_verified,
        'billable_savings_basis_usd', v_verified,
        'anchored_zero_spend_rows', v_evidence.anchored_zero_spend_rows,
        'anchored_zero_spend_savings_usd', v_evidence.anchored_zero_spend_savings_usd,
        'unanchored_zero_spend_rows', v_evidence.unanchored_zero_spend_rows,
        'cache_savings_spend_ratio', v_evidence.cache_savings_spend_ratio,
        'observed_priced_models', v_evidence.observed_priced_models,
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
    'same transaction before any write. Since 202607280031 the recorded '
    'verified_savings_usd is the BILLABLE BASIS -- spend-backed savings plus '
    'anchored cache savings priced at the organization''s own observed cost per '
    'token -- while the halting gate is still evaluated on the UN-NARROWED gross, '
    'so a period with no billable basis HALTS with a named condition instead of '
    'becoming a silent $0 draft. Produces status = ''draft'' only, so it moves no '
    'money and is safe to grant to service_role; promotion is '
    'public.promote_billing_period_settlement, which is granted to nobody. '
    'Idempotent per (organization, period). Refuses unconditionally once the '
    'period has committed money to Stripe. Returns {ok, outcome, code, ...}; '
    'halts and blocks are RETURNED, not raised, so a batch is not aborted by one '
    'organization -- a caller MUST branch on outcome/code and must never treat a '
    'successful call as a successful settlement.';

comment on column public.period_settlement_ledger.verified_savings_usd is
    'The BILLABLE BASIS for this settlement, signed (202607280031). It is NOT a '
    'naive sum of usage_log.verified_savings_usd: unanchored zero-spend savings '
    'are excluded entirely, anchored cache savings are repriced at the '
    'organization''s own observed cost per token, and the cache component is '
    'capped relative to the period''s actual provider spend. Recompute it with '
    'public.billing_period_settlement_evidence(organization_id, period_start, '
    'period_end, usage_log_watermark_id).billable_savings_basis_usd -- passing '
    'this row''s own watermark reproduces it exactly, no matter what has been '
    'written since.';

-- ---------------------------------------------------------------------------
-- 6. THE READ PATH for /api/billing/status, projected over the SAME basis.
--
--    The route cannot select period_settlement_ledger (202607280007 revokes
--    every privilege from service_role and two CI files assert it), so this RPC
--    is the whole read path. estimated_fee_microusd must converge EXACTLY to the
--    fee the writer will record when the week closes, which means it has to
--    project the basis, not the gross -- otherwise every customer sees a number
--    that drops on settlement day.
--
--    The frozen guard's jsonb cannot carry the new terms (202607280008 is
--    checksum-frozen), so the evidence function is called directly for them and
--    the guard is still called for the arrangement and the gross ceiling.
--    Removes no key.
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
    v_evidence record;
    v_share numeric;
    v_basis numeric;
    v_warm numeric;
    v_net_after_warm numeric;
    v_estimate bigint;
    v_gross_ceiling bigint;
    v_basis_ceiling bigint;
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

    begin
        select * into v_evidence
          from public.billing_period_settlement_evidence(p_organization_id, v_start, v_end);
        select conditions.max_fee_share_of_verified_savings into strict v_share
          from public.billing_halting_conditions conditions
         where conditions.singleton;
    exception
        when others then
            return jsonb_build_object(
                'ok', false, 'code', 'guard_unavailable',
                'organization_id', p_organization_id,
                'period_start', v_start, 'period_end', v_end
            );
    end;

    v_gross_ceiling := coalesce((v_gate->>'fee_ceiling_microusd')::bigint, 0);
    v_arrangement := v_gate->>'billing_arrangement';

    -- The writer's own arithmetic, verbatim, so the two converge exactly.
    v_basis := round(v_evidence.billable_savings_basis_usd, 10);
    v_warm := round(greatest(v_evidence.warm_spend_usd, 0), 10);
    v_net_after_warm := greatest(v_basis - v_warm, 0);
    v_basis_ceiling := floor(v_net_after_warm * v_share * 1000000)::bigint;
    -- The BINDING ceiling is the smaller of the two: the gross one is what the
    -- frozen guard enforces, the basis one is what the ledger CHECK enforces.
    v_ceiling := least(v_gross_ceiling, v_basis_ceiling);
    v_estimate := least(
        public.period_settlement_fee_microusd(v_net_after_warm, v_basis),
        v_ceiling
    );

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
            -- 202607280031. The basis is what the customer is actually billed
            -- on; the gross above is what the halting gate is evaluated on.
            'billable_savings_basis_usd', v_evidence.billable_savings_basis_usd,
            'anchored_zero_spend_savings_usd', v_evidence.anchored_zero_spend_savings_usd,
            'unanchored_zero_spend_savings_usd', v_evidence.zero_spend_net_savings_usd,
            'cache_savings_spend_ratio', v_evidence.cache_savings_spend_ratio
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
    'public.period_settlement_ledger. estimated_fee_microusd is an evidence '
    'PROJECTION for the live week over the SAME billable basis the writer will '
    'record (202607280031), so it converges exactly to the settled fee when the '
    'week closes rather than dropping on settlement day; fee_ceiling_microusd is '
    'the BINDING ceiling, least(gross ceiling enforced by the frozen guard, basis '
    'ceiling enforced by the ledger CHECK). settled_fee_microusd is the ledger '
    'number (live row, excluding draft and void). Returns {ok:false, code} for '
    'no_billing_account / period_anchor_mismatch / guard_unavailable; the caller '
    'must render null, never zero.';

-- ---------------------------------------------------------------------------
-- 7. SELF-CHECK. Structural, privilege, and argument-level only: every
--    behavioural claim (a materially-paid linked replay bills at 25% of
--    tokens_saved x observed price; the floor, the lookback, the ratio cap and
--    the retroactivity bound each refuse SEPARATELY; a provider-native discount
--    contributes nothing; savings with no price source still trip the
--    concentration guard) is executed against real fixtures in
--    scripts/ci/migration-anchored-savings-v2-assertions.sql, because those
--    need organizations / auth.users / billing_accounts / attestation rows and
--    period_settlement_ledger carries an UNCONDITIONAL BEFORE DELETE guard.
-- ---------------------------------------------------------------------------
do $observed_price_contract$
declare
    v_expected_out constant text[] := array[
        'eligible_rows', 'zero_spend_rows', 'net_verified_savings_usd',
        'zero_spend_net_savings_usd', 'actual_spend_usd', 'warm_spend_usd',
        'warm_spend_days', 'usage_log_watermark_id', 'anchored_zero_spend_rows',
        'anchored_zero_spend_savings_usd', 'unanchored_zero_spend_rows',
        'observed_priced_models', 'cache_savings_spend_ratio',
        'billable_savings_basis_usd'
    ];
    v_actual_out text[];
    v_role text;
    v_rejected boolean;
    v_evidence record;
begin
    ------------------------------------------------------------------
    -- The widened evidence OUT list, in order, by name.
    ------------------------------------------------------------------
    select array_agg(procedure.proargnames[argument_index] order by argument_index)
      into v_actual_out
      from pg_proc procedure,
           generate_subscripts(procedure.proargnames, 1) as argument_index
     where procedure.oid = to_regprocedure(
               'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)')
       and procedure.proargmodes[argument_index] = 't';
    if v_actual_out is distinct from v_expected_out then
        raise exception '202607280031 evidence OUT list is % (expected %)',
            v_actual_out, v_expected_out;
    end if;
    -- to_regprocedure() resolves by EXACT argument types and is deliberately not
    -- default-aware, so it cannot answer this question. The trailing default is
    -- what keeps the frozen 202607280008 guard's three-argument call resolving,
    -- and the only honest test is to make that call. It is made for real at the
    -- end of this block; here we assert the default exists at all.
    if not exists (
        select 1 from pg_proc procedure
         where procedure.oid = to_regprocedure(
               'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)')
           and procedure.pronargdefaults = 1
    ) then
        raise exception '202607280031 dropped the trailing default on '
                        'p_usage_log_watermark_id; the frozen 202607280008 guard''s '
                        'three-argument call would then fail to resolve and every settlement '
                        'would raise instead of halting';
    end if;

    ------------------------------------------------------------------
    -- Both halves of the forward link exist, and both default to ''.
    ------------------------------------------------------------------
    if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'usage_log'
           and column_name = 'savings_anchor_request_id'
           and is_nullable = 'NO' and column_default = '''''::text'
    ) then
        raise exception '202607280031 did not install usage_log.savings_anchor_request_id '
                        'as NOT NULL DEFAULT ''''';
    end if;
    if not exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'semantic_cache'
           and column_name = 'origin_request_id'
           and is_nullable = 'NO' and column_default = '''''::text'
    ) then
        raise exception '202607280031 shipped only HALF the forward link: without '
                        'semantic_cache.origin_request_id every hosted CacheHit carries '''' '
                        'and every replay is unanchored forever';
    end if;

    ------------------------------------------------------------------
    -- Both cache RPC overloads resolve; the lookup returns the anchor.
    ------------------------------------------------------------------
    if to_regprocedure('public.semantic_cache_store_bounded'
           || '(text,text,text,vector,text,text,integer,integer,integer,integer)') is null then
        raise exception '202607280031 removed the ten-argument bounded cache write; '
                        'semantic_cache.py''s TypeError fallback would then fail the write '
                        'instead of degrading to unanchored';
    end if;
    if to_regprocedure('public.semantic_cache_store_bounded'
           || '(text,text,text,vector,text,text,integer,integer,integer,integer,text)') is null then
        raise exception '202607280031 did not install the anchor-carrying bounded cache write';
    end if;
    if not exists (
        select 1
          from pg_proc procedure
         where procedure.oid = to_regprocedure(
                   'public.semantic_cache_lookup(vector,text,double precision,text,text)')
           and 'origin_request_id' = any(procedure.proargnames)
    ) then
        raise exception '202607280031 did not widen semantic_cache_lookup by origin_request_id';
    end if;

    ------------------------------------------------------------------
    -- The writer bills the basis and gates on the gross, and its promote-door
    -- predicate is intact.
    ------------------------------------------------------------------
    if not exists (
        select 1 from pg_proc procedure
         where procedure.oid = to_regprocedure(
                   'public.settle_billing_period(uuid,timestamptz,text,boolean)')
           and procedure.prosrc like '%billable_savings_basis_usd%'
           and procedure.prosrc like '%v_gate_fee%'
    ) then
        raise exception '202607280031 did not install the basis/gate split in the writer';
    end if;
    if not exists (
        select 1 from pg_proc procedure
         where procedure.oid = to_regprocedure(
                   'public.settle_billing_period(uuid,timestamptz,text,boolean)')
           and procedure.prosrc like '%''pending'', ''sending'', ''reported''%'
    ) then
        raise exception '202607280031 dropped the promote-door predicate from the writer; '
                        'without ''pending'' a promoted period can be settled again and '
                        'billed twice';
    end if;

    ------------------------------------------------------------------
    -- The frozen guard is untouched and still reads the narrowed numerator.
    ------------------------------------------------------------------
    if not exists (
        select 1 from pg_proc procedure
         where procedure.oid = to_regprocedure(
                   'public.assert_billing_period_halting_conditions'
                   || '(uuid,timestamptz,timestamptz,bigint)')
           and procedure.prosrc like '%zero_spend_net_savings_usd%'
           and procedure.prosrc like '%or settlement.outbound_started_at is not null%'
           and procedure.prosrc like '%cumulative_ceiling%'
    ) then
        raise exception '202607280031 disturbed the frozen 202607280008 halting guard';
    end if;

    ------------------------------------------------------------------
    -- The three thresholds are seeded at their defaults, and each can only be
    -- moved in the TIGHTENING direction.
    ------------------------------------------------------------------
    if not exists (
        select 1 from public.billing_halting_conditions
         where singleton
           and cache_anchor_materiality_floor_usd = 0.3000
           and cache_anchor_lookback_days = 30
           and max_cache_savings_per_spend_ratio = 3.000
    ) then
        raise exception '202607280031 did not seed the cache-anchor thresholds at '
                        '0.3000 / 30 / 3.000';
    end if;

    v_rejected := false;
    begin
        update public.billing_halting_conditions
           set cache_anchor_materiality_floor_usd = 0.2900 where singleton;
    exception when check_violation then v_rejected := true;
    end;
    if not v_rejected then
        raise exception '202607280031 allows the materiality floor to be LOWERED without a '
                        'migration';
    end if;

    v_rejected := false;
    begin
        update public.billing_halting_conditions
           set cache_anchor_lookback_days = 31 where singleton;
    exception when check_violation then v_rejected := true;
    end;
    if not v_rejected then
        raise exception '202607280031 allows the anchor lookback to be LENGTHENED without a '
                        'migration';
    end if;

    v_rejected := false;
    begin
        update public.billing_halting_conditions
           set max_cache_savings_per_spend_ratio = 3.001 where singleton;
    exception when check_violation then v_rejected := true;
    end;
    if not v_rejected then
        raise exception '202607280031 allows the spend-ratio cap to be RAISED without a '
                        'migration';
    end if;

    ------------------------------------------------------------------
    -- Privilege posture. Nothing new is reachable from a browser role, and the
    -- runtime role keeps exactly what it had.
    ------------------------------------------------------------------
    for v_role in select unnest(array['anon', 'authenticated']) loop
        if has_function_privilege(v_role,
               'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)',
               'EXECUTE')
           or has_function_privilege(v_role,
               'public.billing_observed_model_price(uuid,timestamptz,timestamptz,integer,bigint)',
               'EXECUTE')
           or has_function_privilege(v_role,
               'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.semantic_cache_lookup(vector,text,double precision,text,text)', 'EXECUTE')
           or has_function_privilege(v_role,
               'public.semantic_cache_store_bounded'
               || '(text,text,text,vector,text,text,integer,integer,integer,integer,text)',
               'EXECUTE') then
            raise exception '202607280031 exposed a billing or cache function to browser role %',
                v_role;
        end if;
    end loop;
    if not has_function_privilege('service_role',
           'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE')
       or not has_function_privilege('service_role',
           'public.billing_observed_model_price(uuid,timestamptz,timestamptz,integer,bigint)',
           'EXECUTE')
       or not has_function_privilege('service_role',
           'public.settle_billing_period(uuid,timestamptz,text,boolean)', 'EXECUTE')
       or not has_function_privilege('service_role',
           'public.billing_period_settlement_summary(uuid,timestamptz)', 'EXECUTE')
       or not has_function_privilege('service_role',
           'public.semantic_cache_lookup(vector,text,double precision,text,text)', 'EXECUTE')
       or not has_function_privilege('service_role',
           'public.semantic_cache_store_bounded'
           || '(text,text,text,vector,text,text,integer,integer,integer,integer,text)',
           'EXECUTE') then
        raise exception '202607280031 dropped a service_role grant a DROP+CREATE discarded';
    end if;
    -- The attestation-free inner guard must still be unreachable at runtime.
    if has_function_privilege('service_role',
           'public.assert_billing_period_halting_conditions(uuid,timestamptz,timestamptz,bigint)',
           'EXECUTE') then
        raise exception '202607280031 left the attestation-free inner guard reachable at runtime';
    end if;
    -- No table privilege was widened, and no trigger was attached.
    if has_table_privilege('service_role', 'public.billing_halting_conditions', 'update')
       or has_table_privilege('service_role', 'public.billing_halting_conditions', 'insert') then
        raise exception '202607280031 let the runtime role rewrite its own halting thresholds';
    end if;
    if exists (
        select 1 from pg_trigger trigger_state
         where trigger_state.tgrelid = 'public.usage_log'::regclass
           and trigger_state.tgname = 'queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception '202607280031 attached a per-row fee trigger';
    end if;

    ------------------------------------------------------------------
    -- Argument-level behaviour that holds on an EMPTY database. An organization
    -- with no rows at all must produce a zero, non-null basis and a NULL
    -- watermark -- not a NULL basis that would propagate into a fee.
    ------------------------------------------------------------------
    select * into v_evidence
      from public.billing_period_settlement_evidence(
          '00000000-0000-0000-0000-000000000031'::uuid,
          now() - interval '14 days', now() - interval '7 days'
      );
    if v_evidence.eligible_rows <> 0
       or v_evidence.billable_savings_basis_usd <> 0
       or v_evidence.anchored_zero_spend_rows <> 0
       or v_evidence.unanchored_zero_spend_rows <> 0
       or v_evidence.observed_priced_models <> 0
       or v_evidence.usage_log_watermark_id is not null
       or v_evidence.cache_savings_spend_ratio is not null then
        raise exception '202607280031 evidence for an empty organization is not zeroed: %',
            row_to_json(v_evidence);
    end if;
    -- An explicitly supplied watermark of 0 excludes every row that can exist,
    -- which is the fail-closed direction the retroactivity bound relies on.
    select * into v_evidence
      from public.billing_period_settlement_evidence(
          '00000000-0000-0000-0000-000000000031'::uuid,
          now() - interval '14 days', now() - interval '7 days', 0::bigint
      );
    if v_evidence.eligible_rows <> 0 or v_evidence.usage_log_watermark_id <> 0 then
        raise exception '202607280031 does not honour an explicitly supplied watermark';
    end if;
end;
$observed_price_contract$;

commit;

-- ROLLBACK PROCEDURE
--
-- Rolling this back RE-HALTS every period whose savings are cache replays, which
-- is the drought this migration exists to end. Any already-written 'draft' rows
-- stay in place and keep the basis they were settled on; they are seven-year
-- financial records and a draft bills nothing.
--
-- 1. DROP FUNCTION IF EXISTS public.billing_period_settlement_summary(uuid,timestamptz);
--    then re-run the CREATE OR REPLACE body from
--    supabase/migrations/202607280013_period_settlement_writer.sql section 4,
--    followed by that file's REVOKE/GRANT pair.
-- 2. Re-run the CREATE OR REPLACE FUNCTION
--    public.settle_billing_period(uuid,timestamptz,text,boolean) body from
--    202607280013 section 2 verbatim (its checksum is frozen in
--    scripts/ci/verify-migrations.mjs, so the exact text is recoverable),
--    followed by its REVOKE/GRANT pair.
-- 3. DROP FUNCTION IF EXISTS public.billing_period_settlement_evidence(
--        uuid,timestamptz,timestamptz,bigint);
--    then re-run the CREATE FUNCTION body from 202607280013 section 1 and its
--    REVOKE/GRANT pair. It MUST be dropped first: CREATE OR REPLACE cannot
--    narrow a return type.
-- 4. DROP FUNCTION IF EXISTS public.billing_observed_model_price(
--        uuid,timestamptz,timestamptz,integer,bigint);
-- 5. DROP FUNCTION IF EXISTS public.semantic_cache_store_bounded(
--        text,text,text,vector,text,text,integer,integer,integer,integer,text);
--    The ten-argument version was never touched, so the Python fallback path
--    keeps working and the cache degrades to unanchored writes.
-- 6. DROP FUNCTION IF EXISTS public.semantic_cache_lookup(vector,text,float,text,text);
--    then re-run the CREATE OR REPLACE body from
--    supabase/migrations/202607170002_cache_security.sql (around line 237) and
--    its REVOKE/GRANT pair.
-- 7. The columns and indexes are deliberately NOT dropped. usage_log
--    .savings_anchor_request_id and semantic_cache.origin_request_id are
--    evidence, they cost one empty string per row, and dropping them destroys
--    the only record of which replay answered which paid request. The three
--    threshold columns are likewise inert once nothing reads them. If they must
--    go, they go in a separate reviewed migration with its own review.
