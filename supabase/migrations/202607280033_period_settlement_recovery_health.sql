-- REVERSE: DDL: drop function public.period_settlement_recovery_health()
--
-- The health probe for public.period_settlement_ledger.
--
-- WHY THIS EXISTS. api/billing_recovery.py's SupabaseSettlementStore maps
-- RPCS["health"] to period_settlement_recovery_health, but 202607280029 shipped
-- the six claim/send RPCs and billing_period_settlement_history WITHOUT it. The
-- name therefore resolved to nothing: SupabaseBillingStore._rpc calls
-- response.raise_for_status(), so PostgREST's PGRST202 became an exception, and
-- BillingRecoveryProcessor.check_health explicitly lets a settlement failure
-- fail the whole cycle ("A settlement failure raising here fails the cycle
-- exactly as a billing_ledger failure does"). Every health cycle would have
-- raised.
--
-- This was LATENT rather than live only because
-- billing_settlement_recovery_is_enabled() defaults OFF and its own docstring
-- anticipates exactly this state -- "A database that stops short of the
-- settlement migrations (no claim_period_settlement_entries) must be a no-op,
-- not a stream of RPC-not-found errors on every cycle". The landmine sat
-- directly under the go-live step, since setting
-- BREVITAS_BILLING_SETTLEMENT_ENABLED=true is what arms it.
--
-- The unit test could not catch this: it drives FakeRpcSession, which answers
-- any function name, so it asserted the RPC NAMES the store sends and passed
-- while the function did not exist anywhere. It was found by querying
-- production for the names the store actually calls.
--
-- SHAPE. Byte-identical output columns to billing_recovery_health
-- (202607170004:413) because BillingHealth.from_row parses both, and the
-- settlement path reuses the same _health_metrics/_health_alerts machinery with
-- only the alert names and lag threshold differing.
--
-- Two deliberate differences from the billing_ledger original, both forced by
-- this ledger's shape rather than by preference:
--
--   1. LIVE ROWS ONLY. period_settlement_ledger is a revision chain: a
--      correction is a NEW row and the predecessor is stamped superseded_at and
--      otherwise left exactly as the customer saw it (202607280007). A
--      superseded revision is history, not work. Counting it would report
--      permanent phantom backlog that no worker will ever drain, and
--      oldest_pending_seconds would grow without bound and page forever.
--      superseded_at -- not superseded_by -- is what marks a row non-live, per
--      that migration's own correction flow.
--
--   2. created_at, NOT occurred_at. billing_ledger measures pending age from
--      occurred_at, the moment the billable event happened. This ledger has no
--      such column; a settlement's period_start is the START of the week it
--      bills for, so measuring from it would report a ~7-day lag on a row that
--      was written seconds ago and trip the lag alert immediately.
--
-- STALE SENDING. 'sending' with an expired lease, matching the original. Note a
-- stale CLAIM does not appear here and must not: 202607280029's claim leaves the
-- row 'pending' precisely so 202607280010's identity latches can never strand it
-- (a row parked in 'sending' with a NULL outbound marker is permanently stuck).
-- An abandoned claim is therefore a 'pending' row with an expired lease, which
-- the next claim reaps -- it is not a stuck send and must not page as one.
-- BillingHealth has exactly five fields, so this reports the same five.

begin;

create or replace function public.period_settlement_recovery_health()
returns table (
    pending_count bigint,
    review_count bigint,
    dead_count bigint,
    stale_sending_count bigint,
    oldest_pending_seconds bigint
)
language sql
security definer
-- pg_catalog first and pg_temp last: the hardened search_path this chain uses
-- everywhere, so a caller cannot shadow a referenced object with a temp one.
set search_path = pg_catalog, public, pg_temp
as $$
    select count(*) filter (where status = 'pending'),
           count(*) filter (where status = 'review'),
           count(*) filter (where status = 'dead'),
           count(*) filter (where status = 'sending' and lease_expires_at < now()),
           coalesce(
               extract(epoch from now() - min(created_at)
                   filter (where status = 'pending'))::bigint,
               0)
      from public.period_settlement_ledger
     where superseded_at is null;
$$;

-- 202607280032's contract: every function this chain owns ends by removing
-- browser EXECUTE, because PostgreSQL grants EXECUTE to PUBLIC on creation and
-- ALTER DEFAULT PRIVILEGES cannot revoke that built-in default. Without these
-- two lines the new function is born anon-executable and
-- assert_browser_role_function_privileges() fails the build.
revoke all on function public.period_settlement_recovery_health()
    from public, anon, authenticated;
grant execute on function public.period_settlement_recovery_health() to service_role;

do $$
begin
    -- Read-only probe, so it is safe to prove it actually answers rather than
    -- merely that it exists. A signature that parses but errors at run time is
    -- the failure mode this migration was written to remove.
    perform * from public.period_settlement_recovery_health();

    if has_function_privilege('anon', 'public.period_settlement_recovery_health()', 'execute')
       or has_function_privilege('authenticated', 'public.period_settlement_recovery_health()', 'execute') then
        raise exception
            'period_settlement_recovery_health is browser-executable'
            using errcode = '42501';
    end if;

    if not has_function_privilege('service_role', 'public.period_settlement_recovery_health()', 'execute') then
        raise exception
            'period_settlement_recovery_health is not executable by service_role, so the recovery worker still cannot read settlement health'
            using errcode = '42501';
    end if;
end
$$;

commit;
