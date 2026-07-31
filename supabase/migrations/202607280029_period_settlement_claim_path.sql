-- The outbound claim/send path for public.period_settlement_ledger.
--
-- WHY THIS EXISTS. 202607280006 retired queue_brevitas_fee_after_usage, the only
-- writer into public.billing_ledger, and 202607280013 added the replacement
-- writer (settle_billing_period + promote_billing_period_settlement) over
-- period_settlement_ledger. But nothing could ever SEND a settlement:
-- public.claim_billing_ledger_entries (202607200006 around line 357) selects
-- `from public.billing_ledger` and returns billing_ledger row identity, and no
-- other RPC in the database reads period_settlement_ledger for sending. A
-- settlement promoted to 'pending' was therefore terminal in practice -- money
-- computed, approved by an operator, and unreachable. Proven live: caestus
-- settlement 1000000005 sat at status='pending', fee_microusd=1235092, with no
-- code path able to move it.
--
-- REVERSE: PITR-ONLY -- the six functions are droppable, but the revoked grants
-- are not restorable from this file alone.
--
-- THE ONE STRUCTURAL DEVIATION FROM THE PER-ROW PATH, AND WHY.
-- The claim MUST NOT set status='sending'. 202607280010's identity trigger
-- refuses any exit from 'sending'/'reported' unless outbound_started_at
-- survives, and it enforces that even for a row that never carried a marker.
-- A row parked in 'sending' with a NULL marker is therefore PERMANENTLY stuck:
-- it can never return to 'pending', so the settlement analogue of
-- release_billing_ledger_leases (202607170004 around line 390) would be
-- structurally impossible and every abandoned claim would strand a fee forever.
-- The claim here takes the lease ONLY and leaves status='pending'; the row
-- becomes 'sending' in the same UPDATE that stamps the marker
-- (mark_period_settlement_outbound_started), which the latch permits.
--
-- STRIPE IDENTITY. api/billing_recovery.py derives the meter-event identifier
-- and Idempotency-Key from the row id plus a per-ledger namespace:
-- 'brevitas-fee-{id}'/'brevitas-meter-{id}' for billing_ledger and
-- 'brevitas-settlement-{id}'/'brevitas-settlement-meter-{id}' here. Those
-- families are lexically disjoint for every id, which is strictly stronger than
-- 202607280007's id-space argument (settlement ids start at 1e9) because the
-- latter is a one-shot apply-time assertion over a growing sequence. Both hold.

begin;

set local statement_timeout = '120s';
set local lock_timeout = '15s';

do $migration_precondition$
begin
    if to_regclass('public.period_settlement_ledger') is null then
        raise exception using errcode = '55000',
            message = '202607280029 requires 202607280007';
    end if;
    if to_regprocedure('public.promote_billing_period_settlement(bigint,text,text)') is null then
        raise exception using errcode = '55000',
            message = '202607280029 requires 202607280013';
    end if;
    -- The latch semantics this whole file is shaped around.
    if to_regprocedure('public.prevent_period_settlement_identity_change()') is null then
        raise exception using errcode = '55000',
            message = '202607280029 requires 202607280010';
    end if;
    -- The per-row fee trigger must stay retired: a live per-row writer next to a
    -- settlement sender would bill the same usage twice, once per ledger.
    if exists (select 1 from pg_trigger
                where tgrelid = 'public.usage_log'::regclass
                  and not tgisinternal
                  and tgname = 'queue_brevitas_fee_after_usage') then
        raise exception using errcode = '55000',
            message = '202607280029 refuses to install beside the retired per-row fee trigger';
    end if;
end;
$migration_precondition$;


-- ---------------------------------------------------------------------------
-- CLAIM. One settlement, leased, with the three sweeps run first.
--
-- Column list is byte-identical to claim_billing_ledger_entries so
-- api.billing_recovery.BillingEntry.from_row parses it unchanged.
-- ---------------------------------------------------------------------------
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
    if v_claimed.outbound_started_at is null then
        select coalesce(sum(other.fee_microusd), 0)
          into v_committed
          from public.period_settlement_ledger other
         where other.organization_id = v_claimed.organization_id
           and other.period_start = v_claimed.period_start
           and other.period_end = v_claimed.period_end
           and other.id <> v_claimed.id
           and (other.status in ('sending', 'reported')
                or other.outbound_started_at is not null);

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
           v_claimed.fee_microusd
      from public.billing_accounts account
     where account.organization_id = v_claimed.organization_id;
end;
$$;


-- ---------------------------------------------------------------------------
-- BEGIN SEND. status and marker move in ONE update, which is the only
-- transition into 'sending' the latch permits.
-- ---------------------------------------------------------------------------
create or replace function public.mark_period_settlement_outbound_started(
    p_settlement_id bigint,
    p_owner text
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_marked boolean := false;
begin
    if p_settlement_id is null
       or nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'invalid settlement outbound parameters';
    end if;

    -- coalesce() on the marker is required, not stylistic: a plain now() on an
    -- already-stamped row hits 202607280010's write-once latch and RAISES,
    -- aborting the caller's transaction instead of returning a value. Accepting
    -- 'sending' as an input status makes a reclaim idempotent.
    update public.period_settlement_ledger settlement
       set status = 'sending',
           outbound_started_at = coalesce(settlement.outbound_started_at, now()),
           last_attempt_at = now()
     where settlement.id = p_settlement_id
       and settlement.status in ('pending', 'sending')
       and settlement.lease_owner = p_owner
       and settlement.lease_expires_at > now()
     returning true into v_marked;

    return coalesce(v_marked, false);
end;
$$;


-- ---------------------------------------------------------------------------
-- RENEW. The predicate must accept 'pending' as well as 'sending': the worker's
-- heartbeat runs BEFORE begin_send, while the row is still 'pending'. A copied
-- `status = 'sending'` predicate would make every pre-send heartbeat return
-- false, so every settlement would be treated as lease-lost and none would ever
-- send.
-- ---------------------------------------------------------------------------
create or replace function public.renew_period_settlement_lease(
    p_settlement_id bigint,
    p_owner text,
    p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_renewed boolean := false;
begin
    if p_settlement_id is null
       or nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'invalid settlement lease renewal parameters';
    end if;
    if p_lease_seconds is null or p_lease_seconds < 15 or p_lease_seconds > 900 then
        raise exception 'settlement lease seconds must be between 15 and 900';
    end if;

    update public.period_settlement_ledger settlement
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where settlement.id = p_settlement_id
       and settlement.status in ('pending', 'sending')
       and settlement.lease_owner = p_owner
       and settlement.lease_expires_at > now()
     returning true into v_renewed;

    return coalesce(v_renewed, false);
end;
$$;


-- ---------------------------------------------------------------------------
-- COMPLETE. Terminal outcomes only, fenced on the lease owner.
-- ---------------------------------------------------------------------------
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
       and settlement.status = 'sending'
       and settlement.lease_owner = p_owner
     returning true into v_done;

    return coalesce(v_done, false);
end;
$$;


-- ---------------------------------------------------------------------------
-- RELEASE (never sent). Drops a lease taken but not acted on. Unlike the
-- per-row version this does not set status='pending' -- the row already is.
-- The `outbound_started_at is null` predicate is kept for the identical reason:
-- never authorise a blind duplicate send.
-- ---------------------------------------------------------------------------
create or replace function public.release_period_settlement_leases(p_owner text)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_released integer := 0;
begin
    if nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'settlement lease release requires a non-blank owner';
    end if;

    with released as (
        update public.period_settlement_ledger settlement
           set lease_owner = null,
               lease_expires_at = null,
               attempts = greatest(0, settlement.attempts - 1),
               next_attempt_at = now()
         where settlement.status = 'pending'
           and settlement.lease_owner = p_owner
           and settlement.outbound_started_at is null
        returning 1
    )
    select count(*)::integer into v_released from released;

    return v_released;
end;
$$;


-- ---------------------------------------------------------------------------
-- RELEASE AFTER A 429. Returns a rate-limited settlement to 'pending' and
-- refunds the attempt, WITHOUT clearing outbound_started_at.
--
-- NAMING, DELIBERATELY. scripts/ci/migration-settlement-writer-assertions.sql
-- fails the build if public.release_period_settlement_unsent(bigint,text)
-- exists at all, on the grounds that clearing the send marker is exactly what
-- 202607280010 forbids. That rule is correct and is NOT worked around here: the
-- marker is left in place, which keeps the fee inside the cumulative committed
-- sum so the period cannot be re-billed. The different name signals the
-- different contract. The self-check below asserts this body never mentions the
-- marker column, so the guarantee is enforced rather than asserted in prose.
--
-- HTTP 429 is documented non-ingestion, so refunding the attempt is sound. On
-- the next claim the row reports reclaimed=true (marker set), so the worker
-- reconciles against the Stripe aggregate before re-sending, and the re-send
-- reuses the same stable identifier, which Stripe deduplicates.
-- ---------------------------------------------------------------------------
create or replace function public.release_period_settlement_claim(
    p_settlement_id bigint,
    p_owner text
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    v_released boolean := false;
begin
    if p_settlement_id is null
       or nullif(btrim(coalesce(p_owner, '')), '') is null then
        raise exception 'invalid settlement claim release parameters';
    end if;

    update public.period_settlement_ledger settlement
       set status = 'pending',
           attempts = greatest(0, settlement.attempts - 1),
           lease_owner = null,
           lease_expires_at = null,
           next_attempt_at = now(),
           last_error = 'Stripe rate limited the request; the meter event was not processed'
     where settlement.id = p_settlement_id
       and settlement.status = 'sending'
       and settlement.lease_owner = p_owner
     returning true into v_released;

    return coalesce(v_released, false);
end;
$$;


-- ---------------------------------------------------------------------------
-- READ SIDE: settlement history for the customer-facing status route.
--
-- billing_period_settlement_summary answers only for the CURRENT period anchor
-- and refuses any other with 'period_anchor_mismatch'. That is correct for the
-- open week's projection, but it means a week that was actually settled,
-- promoted and reported still reads $0 / 'accruing' to the customer -- the
-- defect the dress rehearsal surfaced. This is the additive, anchor-free read.
--
-- The money predicates below are the summary's own, verbatim
-- (202607280013 around line 1102), so the two reads can never disagree about an
-- overlapping period. In particular `committed` counts 'pending' -- the moment a
-- draft is promoted the money IS queued, and a customer must see that as
-- committed rather than zero.
--
-- Anchor-free ON PURPOSE: a broken current anchor is exactly when a customer
-- most needs to see what has already been billed, and history does not depend
-- on the anchor.
-- ---------------------------------------------------------------------------
create or replace function public.billing_period_settlement_history(
    p_organization_id uuid,
    p_limit integer default 12
)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public, pg_temp
set timezone = 'UTC'
as $$
declare
    v_periods jsonb;
begin
    if p_organization_id is null then
        return jsonb_build_object('ok', false, 'code', 'organization_required');
    end if;
    if p_limit is null or p_limit < 1 or p_limit > 100 then
        return jsonb_build_object('ok', false, 'code', 'invalid_limit');
    end if;

    select coalesce(jsonb_agg(period order by period_start desc), '[]'::jsonb)
      into v_periods
      from (
        select settlement.period_start,
               jsonb_build_object(
                   'period_start', settlement.period_start,
                   'period_end', settlement.period_end,
                   -- The live (non-superseded) row is the one that carries the
                   -- period's identity for a reader.
                   'settlement_id', max(settlement.id) filter (
                       where settlement.superseded_at is null),
                   'settlement_status', max(settlement.status) filter (
                       where settlement.superseded_at is null),
                   'settled_fee_microusd', coalesce(max(settlement.fee_microusd) filter (
                       where settlement.superseded_at is null
                         and settlement.status not in ('draft', 'void')), 0),
                   'reported_fee_microusd', coalesce(sum(settlement.fee_microusd) filter (
                       where settlement.status = 'reported'), 0),
                   'committed_fee_microusd', coalesce(sum(settlement.fee_microusd) filter (
                       where settlement.status in ('pending', 'sending', 'reported')
                          or settlement.outbound_started_at is not null), 0),
                   'reported_at', max(settlement.reported_at),
                   'settled_at', max(settlement.settled_at)
               ) as period
          from public.period_settlement_ledger settlement
         where settlement.organization_id = p_organization_id
         group by settlement.period_start, settlement.period_end
         order by settlement.period_start desc
         limit p_limit
      ) grouped;

    return jsonb_build_object('ok', true, 'periods', v_periods);
end;
$$;


-- ---------------------------------------------------------------------------
-- Privileges. Identical posture to every other money RPC: no PostgREST role may
-- execute these, and none gains any table privilege on period_settlement_ledger.
-- ---------------------------------------------------------------------------
revoke all on function public.billing_period_settlement_history(uuid, integer)
    from public, anon, authenticated;
grant execute on function public.billing_period_settlement_history(uuid, integer)
    to service_role;

revoke all on function public.claim_period_settlement_entries(text, integer, integer, bigint)
    from public, anon, authenticated;
revoke all on function public.mark_period_settlement_outbound_started(bigint, text)
    from public, anon, authenticated;
revoke all on function public.renew_period_settlement_lease(bigint, text, integer)
    from public, anon, authenticated;
revoke all on function public.complete_period_settlement_entry(bigint, text, text, text)
    from public, anon, authenticated;
revoke all on function public.release_period_settlement_leases(text)
    from public, anon, authenticated;
revoke all on function public.release_period_settlement_claim(bigint, text)
    from public, anon, authenticated;

grant execute on function public.claim_period_settlement_entries(text, integer, integer, bigint)
    to service_role;
grant execute on function public.mark_period_settlement_outbound_started(bigint, text)
    to service_role;
grant execute on function public.renew_period_settlement_lease(bigint, text, integer)
    to service_role;
grant execute on function public.complete_period_settlement_entry(bigint, text, text, text)
    to service_role;
grant execute on function public.release_period_settlement_leases(text)
    to service_role;
grant execute on function public.release_period_settlement_claim(bigint, text)
    to service_role;


-- ---------------------------------------------------------------------------
-- Self-checks. Each one is a property this file would be dangerous without.
-- ---------------------------------------------------------------------------
do $migration_selfcheck$
declare
    v_src text;
    v_role text;
begin
    -- 1. The 429 path must never touch the send marker. This is the machine
    --    form of the CI rule that forbids a *_unsent settlement function.
    select prosrc into v_src
      from pg_proc
     where oid = 'public.release_period_settlement_claim(bigint,text)'::regprocedure;
    if position('outbound_started_at' in v_src) > 0 then
        raise exception using errcode = '55000',
            message = 'release_period_settlement_claim must never write outbound_started_at';
    end if;

    -- 2. The claim must not move a row into 'sending'; only the marker update
    --    may, or abandoned claims become unrecoverable under the latch.
    select prosrc into v_src
      from pg_proc
     where oid = 'public.claim_period_settlement_entries(text,integer,integer,bigint)'::regprocedure;
    if position('''sending''' in v_src) = 0 then
        raise exception using errcode = '55000',
            message = 'claim body no longer references sending; sweep 2 is missing';
    end if;
    if v_src ~* 'set\s+status\s*=\s*''sending''' then
        raise exception using errcode = '55000',
            message = 'the settlement claim must not set status to sending';
    end if;

    -- 3. No browser role may reach any of these.
    foreach v_role in array array['anon', 'authenticated'] loop
        if has_function_privilege(v_role,
              'public.claim_period_settlement_entries(text,integer,integer,bigint)', 'EXECUTE')
           or has_function_privilege(v_role,
              'public.complete_period_settlement_entry(bigint,text,text,text)', 'EXECUTE')
           or has_function_privilege(v_role,
              'public.release_period_settlement_claim(bigint,text)', 'EXECUTE') then
            raise exception using errcode = '55000',
                message = format('%s can execute a settlement send RPC', v_role);
        end if;
    end loop;

    -- 4. service_role still holds no direct DML on the ledger: every mutation
    --    must go through these SECURITY DEFINER functions so the lease fence и
    --    the latches cannot be bypassed.
    if has_table_privilege('service_role', 'public.period_settlement_ledger', 'UPDATE')
       or has_table_privilege('service_role', 'public.period_settlement_ledger', 'INSERT')
       or has_table_privilege('service_role', 'public.period_settlement_ledger', 'DELETE') then
        raise exception using errcode = '55000',
            message = 'service_role gained direct DML on period_settlement_ledger';
    end if;
end;
$migration_selfcheck$;

comment on function public.claim_period_settlement_entries(text, integer, integer, bigint) is
    'Leases one promoted settlement for sending. Leaves status pending: only the outbound marker update may enter sending, or the 202607280010 latch strands abandoned claims.';
comment on function public.release_period_settlement_claim(bigint, text) is
    'Returns a rate-limited settlement to pending and refunds the attempt. Never clears outbound_started_at, so the fee stays inside the cumulative committed sum.';

commit;
