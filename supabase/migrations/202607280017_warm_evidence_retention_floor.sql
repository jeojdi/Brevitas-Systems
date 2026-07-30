-- Stop the maintenance loop from deleting the warm-spend evidence settlement
-- depends on, and give warm prefix payloads an absolute retention bound.
--
-- THE MONEY BUG. public.purge_warm_state deletes warm_budget_ledger rows older
-- than its caller's retention window (202607280001_cache_warming.sql:754-755),
-- and api/worker.py calls it every 300s with BREVITAS_WARM_RETENTION_DAYS,
-- default 7. public.billing_period_settlement_evidence recomputes the warm
-- deduction from that same table (202607280008:344-350) precisely "so that no
-- settlement writer can supply it", and a Stripe period is exactly 7 days
-- (period_settlement_ledger_weekly_window, and validateStripeCatalog asserts
-- interval 'week'). Settlement is manual, so a period is written after it closes
-- -- by which time its earliest day has already been purged, and the rest follow
-- daily. The recomputation and the writer then BOTH read an emptying table, the
-- warm deduction shrinks toward zero, and the fee ceiling rises to the
-- undeducted savings: the exact overcharge 202607280008 exists to prevent, on
-- savings manufactured with the customer's own provider budget.
--
-- The fix keeps the two horizons separate instead of widening the caller bound.
-- warm_prefixes cleanup stays TTL-driven and unchanged (it is unrelated to
-- billing). The ledger delete floors its horizon at 365 days, so no caller can
-- shorten the financial evidence window below a year even by passing 7 -- and it
-- does so INSIDE the function, where the existing `p_retention_days not between
-- 1 and 365` precondition (:749-752) still holds. Raising the caller's argument
-- above 365 was not an option: that precondition runs before BOTH deletes, so a
-- 400-day argument would raise and also stop the expired-prefix cleanup, letting
-- prefixes accumulate until the 202607280001 row-cap backstop starts refusing
-- warm_prefix_observe.
--
-- Second, warm_prefixes.payload_ciphertext -- the encrypted prompt prefix of live
-- synchronous traffic -- is bounded only by
-- `expires_at <= last_seen_at + interval '7 days'`, and last_seen_at is refreshed
-- on every arrival, so a recurring prefix is retained on a rolling window with no
-- absolute ceiling. compliance_run_retention has never seen this table, and the
-- only other cleanup, enforce_warm_prefixes_absolute_bound(), returns early
-- unless the relation is estimated at a million rows. This adds the absolute cap
-- to the sweeper that already runs. A capped row is re-created by the next
-- arrival, so warming continues; only the retained payload is bounded.
--
-- The function signature, privileges and result schema are preserved, so
-- scripts/ci/migration-cache-warming-assertions.sql:32 stays green;
-- 'prefixes_absolute_deleted' is added to the result and no caller reads it yet.
-- api/store.py:3326-3338 is the declared SQLite mirror of this function and must
-- follow, together with the BREVITAS_WARM_RETENTION_DAYS default in
-- api/worker.py -- neither is edited here.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- lowering the retention floor back would permit deletion of warm-spend billing evidence

begin;

do $migration_precondition$
begin
    if to_regprocedure('public.purge_warm_state(integer)') is null then
        raise exception using
            errcode = '55000',
            message = '202607280017 requires public.purge_warm_state(integer)';
    end if;
end;
$migration_precondition$;

create or replace function public.purge_warm_state(
    p_retention_days integer
) returns jsonb as $$
declare
    -- The financial-evidence floor. warm_budget_ledger is the only record of
    -- what a warming ping cost, and every settlement and correction revision
    -- recomputes the deduction from it, so it outlives the maintenance window by
    -- construction rather than by configuration.
    v_ledger_retention_days integer := greatest(coalesce(p_retention_days, 0), 365);
    -- Absolute ceiling on a warm prefix payload, independent of how often the
    -- prefix is re-observed. Must stay above the rolling
    -- `last_seen_at + interval '7 days'` TTL so the two do not fight.
    v_prefix_absolute_days integer := 30;
    v_prefixes_deleted integer;
    v_prefixes_absolute_deleted integer;
    v_ledger_deleted integer;
begin
    if coalesce(p_retention_days, 0) not between 1 and 365 then
        raise exception 'warm retention bounds are invalid';
    end if;
    delete from public.warm_prefixes prefix where prefix.expires_at <= now();
    get diagnostics v_prefixes_deleted = row_count;
    delete from public.warm_prefixes prefix
     where prefix.created_at < now() - make_interval(days => v_prefix_absolute_days);
    get diagnostics v_prefixes_absolute_deleted = row_count;
    delete from public.warm_budget_ledger ledger
     where ledger.day < (now() at time zone 'utc')::date - v_ledger_retention_days;
    get diagnostics v_ledger_deleted = row_count;
    return jsonb_build_object(
        'schema', 'brevitas.warm-purge.v1', 'status', 'purged',
        'prefixes_deleted', v_prefixes_deleted,
        'prefixes_absolute_deleted', v_prefixes_absolute_deleted,
        'ledger_retention_days', v_ledger_retention_days,
        'ledger_deleted', v_ledger_deleted
    );
end;
$$ language plpgsql security definer set search_path = pg_catalog, public;

revoke all on function public.purge_warm_state(integer)
    from public, anon, authenticated;
grant execute on function public.purge_warm_state(integer)
    to service_role;

comment on function public.purge_warm_state(integer) is
    'Warming maintenance with separate horizons: expired and absolutely-aged prefixes are removed, while warm_budget_ledger is retained for at least 365 days because settlement recomputes the warm deduction from it.';

commit;
