-- Phase 3 of the billing correctness rollout: the period settlement ledger.
--
-- Phase 0 (202607280006) severed automatic invoicing by dropping the per-row
-- trigger queue_brevitas_fee_after_usage. A per-row ledger cannot express
-- netting: a week whose net savings are negative still bills every positive row
-- in it. This migration adds the structure that can express netting. It adds
-- STRUCTURE ONLY.
--
-- *** SETTLEMENT REMAINS MANUAL. ***
-- No trigger is attached to anything by this migration, no row is written by
-- it, and no RPC reads or writes this table. Nothing here can queue, claim, or
-- send a fee. Automatic settlement stays severed until Phase 4 reinstates the
-- halting conditions AND a reconciled provider invoice exists. Re-enabling is
-- deliberately a separate, reviewable change; do not shortcut it by attaching a
-- trigger to this table.
--
-- Settled design (implemented here, not up for rediscussion):
--
-- 1. One immutable row per (organization_id, period). Corrections APPEND a
--    revision (revision_of / superseded_by) instead of mutating a settled row,
--    so the evidence a customer was shown is never rewritten under them.
-- 2. The window is the EXISTING Stripe seven-day billing period. No new period
--    concept is invented: period_start/period_end must be derived by the writer
--    from public.billing_period_for_occurrence(occurred_at,
--    billing_accounts.current_period_start, billing_accounts.current_period_end),
--    which is the same anchor claim_billing_ledger_entries already uses. The
--    exact-seven-days check below is the structural half of that contract; the
--    anchor alignment itself depends on mutable account state and so must be
--    enforced by the writer.
-- 3. Netting happens WITHIN the period and is floored at zero. A losing week
--    bills $0 and carries NO deficit forward: there is deliberately no
--    carry-forward column, and net_savings_usd is a generated column whose
--    greatest(..., 0) cannot be bypassed by a caller.
-- 4. A negative fee is unrepresentable. Stripe rejects negative meter values,
--    and a negative row landing in `review` beside reporting positives would
--    overcharge with no auto-correction. fee_microusd is bigint >= 0 and is
--    additionally capped by CHECK (see 5) — not by application code.
-- 5. Fee = 25% of net positive savings, AND capped at 25% of the period's
--    verified savings. This mirrors the safe_fee := least(...) guard in
--    public.queue_brevitas_fee (202607200006_company_billing_authorization.sql,
--    around line 221). With a non-negative warm deduction the net term is
--    always the smaller one, so the verified-savings term is a defensive
--    backstop against a mis-stated net rather than the binding limit — exactly
--    the role it plays in the per-row guard. The constraint is `<=`, not `=`,
--    so a corrective revision may bill LESS than the formula; it can never bill
--    more. Undercharging is always the safe direction.
-- 6. Warm pings deduct 100% of the period's warm spend BEFORE the 25% is
--    applied. There is no attribution key in the schema tying a warm ping to
--    the savings it produced, so the full spend is deducted rather than a
--    fraction credited: over-deducting only lowers our fee, over-crediting
--    would overcharge the customer.
--
-- Intended (not yet implemented) evidence sources for the writer:
--   verified_savings_usd := sum(usage_log.verified_savings_usd) over rows with
--       organization_id = this org, ts >= period_start, ts < period_end,
--       authoritative = true, pricing_status = 'priced'. The `authoritative`
--       predicate is load-bearing: proxy-reported rows are non-authoritative BY
--       DESIGN and are never billing input. This sum is SIGNED — a period may
--       legitimately net negative, which is the entire point of the table.
--   warm_spend_usd := sum(warm_budget_ledger.spent_usd) for this org over every
--       UTC day that OVERLAPS [period_start, period_end). warm_budget_ledger is
--       day-keyed while Stripe periods are timestamp-anchored, so a boundary day
--       is counted by BOTH adjacent periods. That double-deduction is chosen
--       deliberately: it can only reduce a fee, never inflate one.
--
-- How api/billing_recovery.py connects LATER (do not wire it now):
--   claim_one() calls RPC 'claim_billing_ledger_entries' and BillingEntry
--   requires id, user_id, occurred_at, fee_microusd, stripe_customer_id,
--   attempts, reclaimed, outbound_started_at, period_start, period_end,
--   expected_period_microusd. Every one of those is satisfiable from this table
--   without a schema change: user_id from billing_owner_id, occurred_at from
--   period_end, stripe_customer_id resolved at claim time from billing_accounts
--   exactly as today, and expected_period_microusd from the row's own
--   fee_microusd (a period ledger has one row per period, so the per-period sum
--   the per-row claim had to compute collapses to the row itself). The lease,
--   attempt, and outbound columns below mirror billing_ledger so that Phase 4
--   is a new security-definer RPC over an unchanged table.
--
--   IDENTITY SPACE: BillingEntry derives Stripe identifiers from the ledger id
--   alone ('brevitas-fee-{id}', 'brevitas-meter-{id}'). If this table's ids
--   overlapped billing_ledger's, a settlement row could collide with a per-row
--   ledger id and be silently deduplicated by Stripe. The identity below
--   therefore starts at 1000000000, disjoint from billing_ledger (which has no
--   rows at all today). The assertion at the end enforces the disjointness.
--
-- Safety at time of writing: billing_ledger is intentionally unfed (its only
-- writer was dropped in 202607280006) and this table is created empty. No
-- historical usage or billing evidence is read, rewritten, or deleted here.

begin;

create table if not exists public.period_settlement_ledger (
    -- Disjoint from billing_ledger's id space: Stripe idempotency keys are
    -- derived from the id alone. See IDENTITY SPACE above.
    id bigint generated always as identity (start with 1000000000) primary key,
    organization_id uuid not null
        references public.organizations(id) on delete restrict,

    -- The existing Stripe seven-day window, half-open [start, end).
    period_start timestamptz not null,
    period_end timestamptz not null,

    -- Revision chain. A correction is a NEW row pointing back at what it
    -- replaces; the replaced row is stamped superseded_at/superseded_by and is
    -- otherwise left exactly as the customer saw it.
    --
    -- CORRECTION FLOW (one transaction, in this order):
    --   1. update the predecessor: superseded_at = now()   <- frees the live slot
    --   2. insert the successor:   revision = predecessor.revision + 1,
    --                              revision_of = predecessor.id
    --   3. update the predecessor: superseded_by = successor.id
    -- superseded_at, not superseded_by, is what marks a row non-live. The two
    -- cannot be one step: the live-row index below requires the predecessor be
    -- stamped BEFORE the successor is inserted, while the superseded_by foreign
    -- key requires the successor to already exist. Splitting the stamp resolves
    -- that ordering cycle without deferring either guarantee. Step 3 is
    -- cross-checked by prevent_period_settlement_identity_change, so a row can
    -- only ever be superseded by its own next revision.
    revision integer not null default 1 check (revision >= 1),
    revision_of bigint
        references public.period_settlement_ledger(id) on delete restrict,
    superseded_by bigint
        references public.period_settlement_ledger(id) on delete restrict,
    superseded_at timestamptz,

    -- Period evidence. Wider than the numeric(18,10) house money type because
    -- these are aggregates of many numeric(18,10) receipts; numeric is exact at
    -- any declared precision, so widening only removes an overflow failure mode
    -- and changes no compared value.
    -- Signed on purpose: a losing week must be representable.
    verified_savings_usd numeric(24,10) not null default 0,
    warm_spend_usd numeric(24,10) not null default 0
        check (warm_spend_usd >= 0),
    -- Netting and the zero floor are structural, not advisory. No carry-forward
    -- column exists, so a losing week cannot create a deficit for the next one.
    net_savings_usd numeric(24,10)
        generated always as (greatest(verified_savings_usd - warm_spend_usd, 0))
        stored,
    usage_row_count bigint not null default 0 check (usage_row_count >= 0),
    -- Highest usage_log.id folded into this revision. A late-arriving receipt
    -- below the watermark is handled by appending a revision, never by
    -- recomputing this row in place.
    usage_log_watermark_id bigint,

    -- Money. Micro-dollars, floored, never negative, never above the cap.
    fee_microusd bigint not null default 0,
    -- Billing-owner attribution snapshot, mirroring billing_ledger.user_id.
    -- The Stripe customer is NOT snapshotted: it stays resolved at claim time
    -- from billing_accounts, as claim_billing_ledger_entries already does.
    billing_owner_id uuid references auth.users(id) on delete restrict,

    -- Lifecycle. 'draft' is the only state this migration permits anything to
    -- be created in, and nothing in this migration creates anything. The
    -- remaining states exist so the Phase 4 claim RPC needs no schema change;
    -- promoting draft -> pending is the deliberate manual act that makes a
    -- period billable, and it must not happen before Phase 4.
    status text not null default 'draft'
        check (status in ('draft', 'pending', 'sending', 'reported',
                          'review', 'capped', 'expired', 'dead', 'void')),
    attempts integer not null default 0 check (attempts >= 0),
    max_attempts integer not null default 5
        check (max_attempts between 1 and 10),
    lease_owner text,
    lease_expires_at timestamptz,
    outbound_started_at timestamptz,
    last_attempt_at timestamptz,
    next_attempt_at timestamptz not null default now(),
    reported_at timestamptz,
    settled_at timestamptz,
    last_error text not null default '',

    -- Who/when computed this revision, for operator audit.
    computed_by text not null default '',
    computed_at timestamptz not null default now(),
    created_at timestamptz not null default now(),

    constraint period_settlement_ledger_weekly_window
        check (period_end - period_start = interval '7 days'),
    constraint period_settlement_ledger_revision_chain
        check ((revision = 1) = (revision_of is null)),
    constraint period_settlement_ledger_no_self_reference
        check (revision_of is distinct from id
               and superseded_by is distinct from id),
    -- A row may be stamped superseded before its successor exists (step 1 of
    -- the correction flow), but a successor pointer without the stamp would
    -- leave two live rows for one period.
    constraint period_settlement_ledger_supersede_stamp
        check (superseded_by is null or superseded_at is not null),
    -- The anti-overcharge invariant. Mirrors safe_fee in queue_brevitas_fee:
    -- 25% of net positive savings, also capped at 25% of verified savings,
    -- floored to whole micro-dollars, and never negative.
    constraint period_settlement_ledger_fee_capped_non_negative
        check (fee_microusd >= 0
               and fee_microusd <= floor(least(
                   greatest(verified_savings_usd - warm_spend_usd, 0) * 0.25,
                   greatest(verified_savings_usd, 0) * 0.25
               ) * 1000000)),
    -- Nothing leaves 'draft'/'void' without an owner to attribute the charge to.
    constraint period_settlement_ledger_billable_requires_owner
        check (status in ('draft', 'void') or billing_owner_id is not null)
);

-- Exactly one live settlement per organization+period. Superseded rows and
-- retracted ('void') drafts are excluded so the chain can grow behind it.
create unique index if not exists period_settlement_ledger_live_period_idx
    on public.period_settlement_ledger (organization_id, period_start)
    where superseded_at is null and status <> 'void';
create unique index if not exists period_settlement_ledger_revision_idx
    on public.period_settlement_ledger (organization_id, period_start, revision);
-- Keep the chain linear: at most one successor per row and one predecessor per
-- successor. Nulls are distinct, so unrevised rows are unconstrained.
create unique index if not exists period_settlement_ledger_revision_of_idx
    on public.period_settlement_ledger (revision_of)
    where revision_of is not null;
create unique index if not exists period_settlement_ledger_superseded_by_idx
    on public.period_settlement_ledger (superseded_by)
    where superseded_by is not null;
-- Shaped for the Phase 4 claim RPC; no reader exists yet.
create index if not exists period_settlement_ledger_claim_idx
    on public.period_settlement_ledger (next_attempt_at, lease_expires_at, id)
    where status in ('pending', 'sending');
create index if not exists period_settlement_ledger_attention_idx
    on public.period_settlement_ledger (status, period_end, id);
create index if not exists period_settlement_ledger_company_period_idx
    on public.period_settlement_ledger (organization_id, period_end);

-- Service-owned and, for now, unreachable. RLS is on with zero policies and
-- every PostgREST role — including service_role — holds no table privilege, so
-- no HTTP path can read or write a settlement until Phase 4 introduces
-- security-definer RPCs. Manual settlement happens in a direct SQL session.
alter table public.period_settlement_ledger enable row level security;
revoke all on table public.period_settlement_ledger
    from public, anon, authenticated, service_role;

comment on table public.period_settlement_ledger is
    'Immutable per-organization, per-Stripe-period settlement with netting '
    'floored at zero and an append-only revision chain. Seven-year financial '
    'record. Settlement is MANUAL: no trigger writes here and no RPC reads '
    'here until Phase 4. Never store prompts or responses.';
comment on column public.period_settlement_ledger.verified_savings_usd is
    'Signed sum of usage_log.verified_savings_usd over authoritative, priced '
    'rows in [period_start, period_end). May be negative; a negative period '
    'nets to zero fee and carries no deficit forward.';
comment on column public.period_settlement_ledger.warm_spend_usd is
    'Full period warm-ping spend, deducted at 100% before the 25% rate. No '
    'attribution key exists, so spend is over-deducted rather than '
    'over-credited; day-keyed warm spend on a period boundary is counted by '
    'both adjacent periods on purpose.';
comment on column public.period_settlement_ledger.net_savings_usd is
    'Generated: greatest(verified_savings_usd - warm_spend_usd, 0). The zero '
    'floor is structural and cannot be bypassed by a caller.';
comment on column public.period_settlement_ledger.fee_microusd is
    'Whole micro-dollars, >= 0, and CHECK-capped at 25% of both net positive '
    'savings and verified savings. A negative fee is unrepresentable: Stripe '
    'rejects it and it would overcharge if it reached review beside positives.';
comment on column public.period_settlement_ledger.status is
    'Created only as draft. Promotion to pending is the manual act that makes a '
    'period billable and must not occur before the Phase 4 halting conditions '
    'and a reconciled provider invoice exist.';
comment on column public.period_settlement_ledger.superseded_at is
    'Liveness marker and a one-way latch: a row with this set is no longer the '
    'live settlement for its period and can never be un-superseded. Set in step '
    '1 of the correction flow, before the successor exists.';
comment on column public.period_settlement_ledger.revision_of is
    'The settlement this row corrects. Corrections append; they never mutate. A '
    'revision that lowers an already-reported fee cannot be expressed as a '
    'negative charge — it is audit evidence here and the difference is refunded '
    'through Stripe out of band.';

-- The fee formula as a pure, table-free function, mirroring safe_fee in
-- public.queue_brevitas_fee. It reads nothing and writes nothing; it exists so
-- the Phase 4 writer and any reviewer share one arithmetic definition with the
-- CHECK constraint above. If either is ever changed, change both.
create or replace function public.period_settlement_fee_microusd(
    p_net_savings_usd numeric,
    p_verified_savings_usd numeric
) returns bigint
language plpgsql
immutable
set search_path = pg_catalog, public, pg_temp
as $$
declare
    safe_fee numeric;
begin
    safe_fee := least(
        greatest(coalesce(p_net_savings_usd, 0), 0) * 0.25,
        greatest(coalesce(p_verified_savings_usd, 0), 0) * 0.25
    );
    return floor(greatest(safe_fee, 0) * 1000000)::bigint;
end;
$$;
revoke all on function public.period_settlement_fee_microusd(numeric, numeric)
    from public, anon, authenticated;

create or replace function public.prevent_period_settlement_delete()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    raise exception 'period settlement rows are immutable financial records retained for seven years';
end;
$$;
revoke all on function public.prevent_period_settlement_delete()
    from public, anon, authenticated;

-- Identity, window, evidence, and amount are frozen at insert. The only
-- backward-looking mutation permitted is stamping a row as superseded, once.
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
revoke all on function public.prevent_period_settlement_identity_change()
    from public, anon, authenticated;

drop trigger if exists prevent_period_settlement_delete
    on public.period_settlement_ledger;
create trigger prevent_period_settlement_delete
before delete on public.period_settlement_ledger
for each statement execute function public.prevent_period_settlement_delete();

drop trigger if exists prevent_period_settlement_identity_change
    on public.period_settlement_ledger;
create trigger prevent_period_settlement_identity_change
before update on public.period_settlement_ledger
for each row execute function public.prevent_period_settlement_identity_change();

-- Self-verifying contract assertions. Deterministic: pure arithmetic and
-- catalog state only, no production data and no external credential.
do $$
declare
    v_sequence oid;
    v_sequence_start bigint;
begin
    -- 1. The fee formula: floored, never negative, and capped both ways.
    if public.period_settlement_fee_microusd(100, 100) <> 25000000 then
        raise exception '202607280007 fee formula is not 25%% of net savings';
    end if;
    if public.period_settlement_fee_microusd(100, 40) <> 10000000 then
        raise exception '202607280007 fee is not capped at 25%% of verified savings';
    end if;
    if public.period_settlement_fee_microusd(-5, -5) <> 0
       or public.period_settlement_fee_microusd(-5, 100) <> 0
       or public.period_settlement_fee_microusd(null, null) <> 0 then
        raise exception '202607280007 a losing period must settle to a zero fee';
    end if;
    if public.period_settlement_fee_microusd(0.0000001, 100) <> 0 then
        raise exception '202607280007 sub-micro-dollar fees must floor to zero';
    end if;

    -- 2. The zero floor is structural, not application-side.
    if not exists (
        select 1
          from pg_attribute column_state
         where column_state.attrelid='public.period_settlement_ledger'::regclass
           and column_state.attname='net_savings_usd'
           and column_state.attgenerated='s'
    ) then
        raise exception '202607280007 net savings must be a generated column';
    end if;

    -- 3. The anti-overcharge CHECK exists on the table.
    if not exists (
        select 1
          from pg_constraint constraint_state
         where constraint_state.conrelid='public.period_settlement_ledger'::regclass
           and constraint_state.contype='c'
           and constraint_state.conname='period_settlement_ledger_fee_capped_non_negative'
    ) then
        raise exception '202607280007 fee cap constraint is missing';
    end if;

    -- 4. Nothing auto-settles: the only triggers on the table are the two
    --    immutability guards, and no trigger anywhere else touches this table.
    if (
        select count(*)
          from pg_trigger trigger_state
         where trigger_state.tgrelid='public.period_settlement_ledger'::regclass
           and not trigger_state.tgisinternal
    ) <> 2 then
        raise exception '202607280007 period settlement carries an unexpected trigger';
    end if;
    if exists (
        select 1
          from pg_trigger trigger_state
          join pg_proc trigger_function
            on trigger_function.oid=trigger_state.tgfoid
         where not trigger_state.tgisinternal
           and trigger_state.tgrelid<>'public.period_settlement_ledger'::regclass
           and coalesce(trigger_function.prosrc,'') like '%period_settlement_ledger%'
    ) then
        raise exception '202607280007 an external trigger writes period settlements; settlement must stay manual';
    end if;

    -- 5. RLS on, zero policies, and no PostgREST role holds a table privilege.
    if not exists (
        select 1
          from pg_class relation
         where relation.oid='public.period_settlement_ledger'::regclass
           and relation.relrowsecurity
    ) then
        raise exception '202607280007 period settlement must have row level security enabled';
    end if;
    if exists (
        select 1
          from pg_policy policy_state
         where policy_state.polrelid='public.period_settlement_ledger'::regclass
    ) then
        raise exception '202607280007 period settlement must expose no row level policy';
    end if;
    if exists (
        select 1
          from unnest(array['anon','authenticated','service_role']) as role_name
         where has_table_privilege(
             role_name,'public.period_settlement_ledger','select,insert,update,delete')
    ) then
        raise exception '202607280007 no PostgREST role may reach period settlements before Phase 4';
    end if;

    -- 6. Stripe identifiers are derived from the ledger id alone, so the two
    --    ledgers' id spaces must never overlap.
    v_sequence := pg_get_serial_sequence('public.period_settlement_ledger','id')::regclass;
    select sequence_state.seqstart into v_sequence_start
      from pg_sequence sequence_state
     where sequence_state.seqrelid=v_sequence;
    if coalesce(v_sequence_start,0) < 1000000000 then
        raise exception '202607280007 settlement ids must start beyond the billing_ledger id space';
    end if;
    if exists (
        select 1 from public.billing_ledger where id>=v_sequence_start
    ) then
        raise exception '202607280007 settlement and per-row ledger ids would collide in Stripe identifiers';
    end if;
end;
$$;

commit;

-- ROLLBACK PROCEDURE (nothing consumes this table, so rollback is safe while
-- it is empty; once a settlement exists it is a retained financial record and
-- must be exported before any drop):
-- 1. Verify: select count(*) from public.period_settlement_ledger; must be 0.
-- 2. DROP TRIGGER IF EXISTS prevent_period_settlement_identity_change ON public.period_settlement_ledger;
-- 3. DROP TRIGGER IF EXISTS prevent_period_settlement_delete ON public.period_settlement_ledger;
-- 4. DROP TABLE IF EXISTS public.period_settlement_ledger;
-- 5. DROP FUNCTION IF EXISTS public.prevent_period_settlement_identity_change();
-- 6. DROP FUNCTION IF EXISTS public.prevent_period_settlement_delete();
-- 7. DROP FUNCTION IF EXISTS public.period_settlement_fee_microusd(numeric,numeric);
