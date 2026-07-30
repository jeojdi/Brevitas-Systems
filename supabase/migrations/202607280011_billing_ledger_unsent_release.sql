-- Give an attempt back when Stripe rate-limits a send that it provably did not
-- ingest (defect WR-2 / FIX-10).
--
-- WHAT WAS WRONG
--
-- api/billing_recovery.py's worker sets the durable outbound marker
-- (mark_billing_outbound_started) BEFORE it POSTs the meter event, so that a
-- crash mid-send is never mistaken for "never sent". When the POST then fails,
-- the worker's only release path is release_billing_ledger_leases
-- (202607170004:390-407), which by design touches only rows whose
-- outbound_started_at is NULL: releasing an ambiguous row to `pending` would
-- authorise a blind duplicate send. That is correct for every ambiguous
-- outcome, and it makes the release a documented NO-OP once the marker is set.
--
-- The worker nevertheless called it on the one outcome that is NOT ambiguous.
-- The result was a slow, silent escalation of a fee that Stripe never saw:
--
--   1. the release does nothing, so the row stays `sending` with its marker;
--   2. lease expiry lets claim_billing_ledger_entries reclaim it, and every
--      reclaim re-increments attempts (202607170004:290);
--   3. after max_attempts (default 5) the sweep in
--      202607200006:349-353 parks it in `review`;
--   4. status/route.ts then blanks the customer-facing figure.
--
-- So ~4-5 sustained rate-limited cycles bury a provably-unsent, perfectly
-- chargeable fee in manual review.
--
-- WHY RESETTING THE MARKER IS SOUND HERE, AND ONLY HERE
--
-- HTTP 429 is documented non-ingestion. api/billing_recovery.py:372-378 raises
-- StripeUnavailable for status 429 alone and states the invariant in the
-- exception's own docstring: "the meter event was not processed... there is no
-- chance Stripe silently accepted." Every other failure class maps elsewhere —
-- 5xx/408/409 and idempotency_error raise StripeAmbiguous, other 4xx raise
-- StripeRejected — and none of them may use this function. Because a 429
-- carries zero probability that Stripe holds the event, clearing
-- outbound_started_at does not create a duplicate-charge path: the next attempt
-- is a first send, not a replay, and it still reuses the same stable identifier
-- (brevitas-fee-<id>) and Idempotency-Key, so even a wrong caller would be
-- deduplicated by Stripe for at least 24 hours.
--
-- Decrementing attempts mirrors release_billing_ledger_leases exactly
-- (greatest(0, attempts - 1)), which exists so that a claim whose attempt was
-- never spent against Stripe does not count against max_attempts.
--
-- FENCING AND PRIVILEGE POSTURE
--
-- Fenced on BOTH the entry id and the lease owner, plus status = 'sending', the
-- same way complete_billing_ledger_entry does (202607170004:359-380): a worker
-- that lost its lease to a reclaim no longer matches lease_owner, so its late
-- 429 handler cannot rewrite a row another worker now owns. `found` is returned
-- so the caller can tell a real release from a fenced-out no-op.
--
-- SECURITY DEFINER + `revoke from public, anon, authenticated` + `grant execute
-- to service_role`, identical to every other recovery RPC. service_role gains
-- NO table write privilege on billing_ledger — 202607220001:60-92 deliberately
-- leaves it with SELECT only, and this migration does not widen that. The
-- ledger's immutability triggers (prevent_billing_ledger_delete,
-- prevent_billing_ledger_identity_change) continue to apply to this UPDATE; it
-- touches no identity, amount, or creation column.

begin;

do $migration_precondition$
begin
    if to_regclass('public.billing_ledger') is null then
        raise exception '202607280011 requires public.billing_ledger';
    end if;
    if to_regprocedure('public.release_billing_ledger_leases(text)') is null then
        raise exception
            '202607280011 requires the 202607170004 billing recovery contract';
    end if;
end;
$migration_precondition$;

create or replace function public.release_billing_ledger_unsent(
    p_entry_id bigint,
    p_owner text
)
returns boolean
language plpgsql
security definer
-- Pin pg_catalog first and pg_temp last, matching the sibling money migrations
-- (202607280008, 202607280010, 202607280013). A bare `= public` leaves pg_temp
-- implicitly searched FIRST, so any caller able to create a temp relation could
-- shadow a name this SECURITY DEFINER body resolves. Only service_role holds
-- EXECUTE here, so this is hardening rather than a live hole -- but this
-- function moves money state, so it gets the strict form.
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if nullif(btrim(coalesce(p_owner, '')), '') is null or p_entry_id is null then
        raise exception 'invalid billing unsent release parameters';
    end if;
    -- Only a definitively non-ingested send (HTTP 429) may reach this. The
    -- outbound marker is cleared because the next attempt is a first send, not
    -- an ambiguous replay; the attempt is refunded because it was never spent.
    update public.billing_ledger
       set status = 'pending',
           attempts = greatest(0, attempts - 1),
           outbound_started_at = null,
           lease_owner = null,
           lease_expires_at = null,
           next_attempt_at = now(),
           last_error = 'Stripe rate limited the request; the meter event was not processed'
     where id = p_entry_id
       and status = 'sending'
       and lease_owner = p_owner;
    return found;
end;
$$;

revoke all on function public.release_billing_ledger_unsent(bigint, text)
    from public, anon, authenticated;
grant execute on function public.release_billing_ledger_unsent(bigint, text)
    to service_role;

comment on function public.release_billing_ledger_unsent(bigint, text) is
    'Lease-fenced release of a billing_ledger row after an HTTP 429 from Stripe, '
    'the only documented non-ingestion outcome. Clears outbound_started_at and '
    'refunds the attempt. Never call it for an ambiguous send.';

do $unsent_release_contract$
declare
    definition text;
begin
    select prosrc into definition
      from pg_proc
     where oid = to_regprocedure('public.release_billing_ledger_unsent(bigint,text)');
    if definition is null then
        raise exception '202607280011 did not create the unsent release RPC';
    end if;

    -- The lease fence is the whole safety argument: without BOTH predicates a
    -- worker that lost its lease could clear another owner's outbound marker.
    if definition not like '%and lease_owner = p_owner%'
       or definition not like '%where id = p_entry_id%'
       or definition not like '%and status = ''sending''%' then
        raise exception '202607280011 left the unsent release unfenced';
    end if;
    if definition not like '%outbound_started_at = null%'
       or definition not like '%greatest(0, attempts - 1)%' then
        raise exception '202607280011 does not release a provably unsent row';
    end if;

    if not (
        select prosecdef from pg_proc
         where oid = to_regprocedure('public.release_billing_ledger_unsent(bigint,text)')
    ) then
        raise exception '202607280011 must own the ledger write, not its caller';
    end if;

    -- Browser roles may never reach any recovery RPC.
    if has_function_privilege('anon',
           'public.release_billing_ledger_unsent(bigint,text)', 'EXECUTE')
       or has_function_privilege('authenticated',
           'public.release_billing_ledger_unsent(bigint,text)', 'EXECUTE') then
        raise exception '202607280011 exposed a ledger write to a browser role';
    end if;
    if not has_function_privilege('service_role',
           'public.release_billing_ledger_unsent(bigint,text)', 'EXECUTE') then
        raise exception '202607280011 did not leave the release callable by the worker';
    end if;

    -- 202607220001 gives service_role SELECT on billing_ledger and nothing
    -- more. Every mutation must stay behind a definer RPC.
    if has_table_privilege('service_role', 'public.billing_ledger', 'UPDATE')
       or has_table_privilege('service_role', 'public.billing_ledger', 'INSERT')
       or has_table_privilege('service_role', 'public.billing_ledger', 'DELETE') then
        raise exception '202607280011 widened direct service_role writes on billing_ledger';
    end if;
end;
$unsent_release_contract$;

commit;

-- ROLLBACK PROCEDURE (pause billing workers first; deploy the worker revision
-- whose StripeUnavailable branch does not call release_unsent, then):
-- 1. DROP FUNCTION IF EXISTS public.release_billing_ledger_unsent(bigint,text);
-- Rows already returned to `pending` by this function were never ingested by
-- Stripe and remain correct to send; no data repair is required.
