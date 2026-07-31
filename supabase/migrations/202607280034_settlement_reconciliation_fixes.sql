-- REVERSE: DDL: re-apply 202607280029's definitions of claim_period_settlement_entries and complete_period_settlement_entry
--
-- Two defects on the outbound settlement path, both found by audit and both on
-- code that has NEVER run against real money -- production has 0
-- period_settlement_ledger rows. Neither has caused harm; both would have, on
-- the first real send.
--
-- ============================================================================
-- DEFECT 1 -- expected_period_microusd is the WRONG QUANTITY
-- ============================================================================
--
-- 202607280029's claim returns `v_claimed.fee_microusd` as
-- expected_period_microusd: the fee of the ONE row being claimed. But
-- api/billing_recovery.py compares it against Stripe's aggregated meter total
-- for the whole [period_start, period_end) window:
--
--     remote_total == Decimal(entry.expected_period_microusd)   -> ACCEPTED
--
-- Stripe's total is the sum of EVERY meter event in that window. So the moment a
-- period carries more than one committed settlement -- a revision, or the legacy
-- per-row fee sharing the window and the Stripe customer -- the equality
-- compares a sum against a single addend and can never hold. Reconciliation is
-- then permanently UNKNOWN, which is indistinguishable from "Stripe may or may
-- not have charged them" and is the state the whole claim/send machine exists to
-- avoid.
--
-- The right quantity is what the remote total should equal AFTER this send: every
-- already-committed fee for the period, plus this row's. 202607280029 already
-- computes exactly that sum as `v_committed` for its cap check -- it was simply
-- never returned. This migration computes it UNCONDITIONALLY (the original only
-- computed it on the non-reclaimed path, leaving it NULL for a row that already
-- carried an outbound marker) and returns v_committed + fee.
--
-- CORROBORATION, and the reason this is stated as fact rather than as a theory:
-- tests/test_billing_recovery.py's FakeSettlementStore ALREADY returns
-- `committed + (0 if reclaimed else fee)`. The fake encodes the intended
-- contract; the SQL never implemented it. The suite passed because it only ever
-- exercised the fake.
--
-- ============================================================================
-- DEFECT 2 -- a 429-released row can never be terminalized
-- ============================================================================
--
-- release_period_settlement_claim (202607280029:385-420) deliberately returns a
-- row to status 'pending' while KEEPING outbound_started_at, because
-- 202607280010's identity latches refuse any exit from 'sending'/'reported' that
-- erases send evidence. That is correct and is not changed here.
--
-- But complete_period_settlement_entry restricts its UPDATE to
-- `settlement.status = 'sending'`. So after a Stripe 429, the recovery worker's
-- attempt to park the row in 'review' matches no row and returns false. The row
-- is re-leased every cycle, burning an attempt and emitting a spurious
-- billing.lease_lost metric, until the 23h sweep escalates it -- alerts firing
-- during exactly the incident an operator is trying to read.
--
-- The fix accepts 'pending' TOO, but only when outbound_started_at is set. A row
-- carrying the outbound marker has provably begun sending, which is the real
-- invariant 'sending' was standing in for. A plain 'pending' row that never
-- started is still refused, so this cannot be used to complete work that never
-- left the building.
--
-- NOTHING ELSE IS TOUCHED. Both bodies are the 202607280029 originals with only
-- the edits above applied -- extracted mechanically rather than retyped, because
-- the claim carries three sweeps, a UTC timezone pin and an eleven-column result
-- that a hand copy would silently drop. An earlier draft of this migration did
-- exactly that and declared a THREE-argument signature, which would have created
-- an overload beside the real four-argument function rather than replacing it;
-- the precondition below is what caught it.

begin;

do $$
begin
    if to_regprocedure('public.claim_period_settlement_entries(text,integer,integer,bigint)') is null then
        raise exception '202607280034 requires 202607280029'
            using errcode = '55000';
    end if;
end
$$;

create or replace function public.claim_period_settlement_entries(
    p_owner text,
    p_lease_seconds integer,
    p_limit integer,
    p_cap_microusd bigint
)
returns table (
    id bigint,
    user_id uuid,
    occurred_at timestamptz,
    fee_microusd bigint,
    stripe_customer_id text,
    attempts integer,
    reclaimed boolean,
    outbound_started_at timestamptz,
    period_start timestamptz,
    period_end timestamptz,
    expected_period_microusd bigint
)
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_claimed public.period_settlement_ledger%rowtype;
    v_committed bigint;
begin
    if nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'settlement claim requires a non-blank owner';
    end if;
    if p_lease_seconds is null or p_lease_seconds < 15 or p_lease_seconds > 900 then
        raise exception 'settlement claim lease seconds must be between 15 and 900';
    end if;
    -- One at a time, exactly like the per-row claim: the send is a network call
    -- under a lease, and batching multiplies the ambiguous-outcome surface.
    if p_limit is distinct from 1 then
        raise exception 'settlement claim supports exactly one row per call';
    end if;
    if p_cap_microusd is null or p_cap_microusd <= 0 then
        raise exception 'settlement claim requires a positive cap';
    end if;

    ------------------------------------------------------------------
    -- SWEEP 1. Aged pending -> expired.
    --
    -- Keyed on period_end, matching the promoter's own reporting-window test.
    -- `outbound_started_at is null` is REQUIRED and differs from the per-row
    -- sweep: a marker-carrying row may already have been ingested by Stripe, and
    -- burying it in 'expired' would drop it out of the committed sum that every
    -- ceiling and customer-facing reader computes from status + marker.
    ------------------------------------------------------------------
    update public.period_settlement_ledger settlement
       set status = 'expired',
           last_error = 'Stripe 35-day reporting window elapsed',
           lease_owner = null,
           lease_expires_at = null
     where settlement.status = 'pending'
       and settlement.outbound_started_at is null
       and settlement.period_end < now() - interval '34 days';

    ------------------------------------------------------------------
    -- SWEEP 2. Stale outbound -> review. The marker survives, so the latch is
    -- satisfied and the fee stays counted as committed.
    ------------------------------------------------------------------
    update public.period_settlement_ledger settlement
       set status = 'review',
           last_error = 'ambiguous Stripe send exceeded safe replay window',
           lease_owner = null,
           lease_expires_at = null
     where settlement.status = 'sending'
       and settlement.lease_expires_at < now()
       and settlement.outbound_started_at < now() - interval '23 hours';

    ------------------------------------------------------------------
    -- SWEEP 3. Attempts exhausted -> review. With this state machine an
    -- attempts-exhausted row is 'pending' (the claim never moved it), and a
    -- 'sending' row that exhausted attempts necessarily carries a marker and is
    -- already covered by sweep 2.
    ------------------------------------------------------------------
    update public.period_settlement_ledger settlement
       set status = 'review',
           last_error = 'settlement recovery attempts exhausted',
           lease_owner = null,
           lease_expires_at = null
     where settlement.status = 'pending'
       and settlement.lease_expires_at < now()
       and settlement.attempts >= settlement.max_attempts;

    ------------------------------------------------------------------
    -- CLAIM. Oldest eligible promoted settlement, or a reclaimable one whose
    -- lease expired. SKIP LOCKED makes concurrent workers pick disjoint rows.
    ------------------------------------------------------------------
    select * into v_claimed
      from public.period_settlement_ledger settlement
     where settlement.fee_microusd > 0
       and settlement.superseded_at is null
       and (
             (settlement.status = 'pending'
              and settlement.attempts < settlement.max_attempts
              and (settlement.next_attempt_at is null or settlement.next_attempt_at <= now())
              and (settlement.lease_expires_at is null or settlement.lease_expires_at < now()))
          or (settlement.status = 'sending'
              and settlement.lease_expires_at < now())
           )
     order by settlement.next_attempt_at nulls first,
              settlement.period_end,
              settlement.id
       for update skip locked
     limit 1;

    if not found then
        return;
    end if;

    ------------------------------------------------------------------
    -- CAP. The cumulative ceiling of 202607280008 already governs how much may
    -- be COMMITTED for an (org, period); this is the worker's own weekly
    -- safety cap, a second independent brake on how much this process may send.
    -- A row already carrying a marker is exempt: its money is committed
    -- already, and refusing to finish it would strand it, not save it.
    ------------------------------------------------------------------
    -- 202607280034: computed UNCONDITIONALLY. 202607280029 computed it only on
    -- the non-reclaimed path, which left it NULL for a row that already carried
    -- an outbound marker -- and that row is reconciled too.
    select coalesce(sum(other.fee_microusd), 0)
      into v_committed
      from public.period_settlement_ledger other
     where other.organization_id = v_claimed.organization_id
       and other.period_start = v_claimed.period_start
       and other.period_end = v_claimed.period_end
       and other.id <> v_claimed.id
       and (other.status in ('sending', 'reported')
            or other.outbound_started_at is not null);

    if v_claimed.outbound_started_at is null then
        if v_committed + v_claimed.fee_microusd > p_cap_microusd then
            update public.period_settlement_ledger settlement
               set status = 'capped',
                   last_error = 'weekly safety cap reached',
                   lease_owner = null,
                   lease_expires_at = null
             where settlement.id = v_claimed.id;
            return;
        end if;
    end if;

    ------------------------------------------------------------------
    -- Take the lease. status is deliberately NOT changed -- see the header.
    ------------------------------------------------------------------
    update public.period_settlement_ledger settlement
       set lease_owner = p_owner,
           lease_expires_at = now() + make_interval(secs => p_lease_seconds),
           attempts = settlement.attempts + 1,
           last_attempt_at = now()
     where settlement.id = v_claimed.id
     returning * into v_claimed;

    return query
    select v_claimed.id,
           v_claimed.billing_owner_id,
           v_claimed.period_end,
           v_claimed.fee_microusd,
           account.stripe_customer_id,
           v_claimed.attempts,
           (v_claimed.outbound_started_at is not null),
           v_claimed.outbound_started_at,
           v_claimed.period_start,
           v_claimed.period_end,
           -- 202607280034. What the remote meter total must equal AFTER this send:
           -- every fee already committed for the period, plus this row. The old
           -- value was this row's fee alone, which api/billing_recovery.py then
           -- compared against Stripe's aggregate for the whole window, so the
           -- equality could never hold once a period carried more than one
           -- committed settlement.
           (v_committed + v_claimed.fee_microusd)
      from public.billing_accounts account
     where account.organization_id = v_claimed.organization_id;
end;
$$;

create or replace function public.complete_period_settlement_entry(
    p_settlement_id bigint,
    p_owner text,
    p_status text,
    p_error text default ''
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_done boolean := false;
begin
    if p_settlement_id is null
       or nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'invalid settlement completion parameters';
    end if;
    if p_status not in ('reported', 'review', 'dead') then
        raise exception 'settlement completion status is not terminal';
    end if;

    -- coalesce() on both stamps for the same latch reason as begin_send.
    -- settled_at is stamped only for 'reported' because that is the sole status
    -- meaning Stripe accepted the money.
    update public.period_settlement_ledger settlement
       set status = p_status,
           reported_at = case when p_status = 'reported'
                              then coalesce(settlement.reported_at, now())
                              else settlement.reported_at end,
           settled_at = case when p_status = 'reported'
                             then coalesce(settlement.settled_at, now())
                             else settlement.settled_at end,
           last_error = left(coalesce(p_error, ''), 500),
           lease_owner = null,
           lease_expires_at = null
     where settlement.id = p_settlement_id
       -- 202607280034. 'pending' is accepted ONLY with an outbound marker: that
       -- is a row release_period_settlement_claim returned after a Stripe 429
       -- while keeping its send evidence (202607280010's latches forbid erasing
       -- it), so it has provably begun sending -- the invariant 'sending' alone
       -- was standing in for. Without this the worker's move to 'review' matched
       -- no row, and the entry was re-leased every cycle emitting a spurious
       -- billing.lease_lost until the 23h sweep escalated it. A plain 'pending'
       -- row that never started is still refused.
       and (settlement.status = 'sending'
            or (settlement.status = 'pending'
                and settlement.outbound_started_at is not null))
       and settlement.lease_owner = p_owner
     returning true into v_done;

    return coalesce(v_done, false);
end;
$$;

-- 202607280032's contract: PostgreSQL grants EXECUTE to PUBLIC on creation and
-- ALTER DEFAULT PRIVILEGES cannot revoke that built-in default, so every
-- migration removes browser EXECUTE for the routines it defines.
revoke all on function public.claim_period_settlement_entries(text, integer, integer, bigint)
    from public, anon, authenticated;
revoke all on function public.complete_period_settlement_entry(bigint, text, text, text)
    from public, anon, authenticated;
grant execute on function public.claim_period_settlement_entries(text, integer, integer, bigint)
    to service_role;
grant execute on function public.complete_period_settlement_entry(bigint, text, text, text)
    to service_role;

do $contract$
declare
    v_claim text := (select prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                      where n.nspname = 'public' and p.proname = 'claim_period_settlement_entries');
    v_complete text := (select prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                         where n.nspname = 'public' and p.proname = 'complete_period_settlement_entry');
begin
    -- Exactly one claim function: an overload beside the real one would let
    -- PostgREST resolve to either, which is how the first draft of this
    -- migration would have failed.
    if (select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public' and p.proname = 'claim_period_settlement_entries') <> 1 then
        raise exception '202607280034 left more than one claim_period_settlement_entries'
            using errcode = '55000';
    end if;

    if has_function_privilege('anon',
        'public.claim_period_settlement_entries(text,integer,integer,bigint)', 'execute')
       or has_function_privilege('anon',
        'public.complete_period_settlement_entry(bigint,text,text,text)', 'execute') then
        raise exception 'the settlement claim path is browser-executable'
            using errcode = '42501';
    end if;

    -- DEFECT 1 actually landed.
    if v_claim not like '%v_committed + v_claimed.fee_microusd)%' then
        raise exception 'expected_period_microusd is still this row''s fee alone'
            using errcode = '55000';
    end if;

    -- DEFECT 2 actually landed.
    if v_complete not like '%outbound_started_at is not null))%' then
        raise exception 'a 429-released settlement still cannot be terminalized'
            using errcode = '55000';
    end if;

    -- 202607280029's load-bearing property survives: the claim still must not
    -- park a row in 'sending', which 202607280010's latches would strand.
    if v_claim like '%set status = ''sending''%' then
        raise exception
            '202607280034 reintroduced a status=sending claim, which 202607280010 strands forever'
            using errcode = '55000';
    end if;
end
$contract$;

commit;
