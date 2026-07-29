-- Phase 0 of the billing correctness rollout: make "no accidental invoice" a
-- structural property instead of a coincidence of an empty table.
--
-- Today the per-row trigger queue_brevitas_fee_after_usage inserts one
-- billing_ledger row per authoritative, priced usage_log row. That per-row
-- model is being replaced by a period-aggregate settlement ledger (Phase 3),
-- because a per-row ledger cannot express netting: a week whose net savings
-- are negative still bills every positive row in it.
--
-- Until the period ledger exists and has been reconciled against a real
-- provider invoice, nothing should be capable of queueing a fee at all. This
-- migration removes that capability at the schema level.
--
-- Safety at time of writing: 0 billing_ledger rows, 0 billing_accounts rows.
-- Nothing is migrated because nothing has ever settled. No historical usage or
-- billing evidence is rewritten or deleted.
--
-- The function public.queue_brevitas_fee() is deliberately RETAINED (and stays
-- revoked from public/anon/authenticated). Keeping it makes any future
-- re-enable a single CREATE TRIGGER rather than a reconstruction, and keeps
-- the object available for inspection while the replacement is being built.
--
-- REPLAY WARNING: migration 202607170012 guards on this trigger being present
-- and will raise if replayed after this point. That chain must not be replayed
-- blind against a database where this migration has been applied.

begin;

drop trigger if exists queue_brevitas_fee_after_usage on public.usage_log;

comment on function public.queue_brevitas_fee() is
    'RETIRED 202607280006. Retained but intentionally unattached: per-row fee '
    'queueing cannot express period netting. Superseded by the period '
    'settlement ledger. Do not re-attach without reinstating the Phase 4 '
    'halting conditions and a reconciled provider invoice.';

do $$
begin
    if exists (
        select 1
          from pg_trigger trigger_state
         where trigger_state.tgrelid='public.usage_log'::regclass
           and trigger_state.tgname='queue_brevitas_fee_after_usage'
           and not trigger_state.tgisinternal
    ) then
        raise exception '202607280006 failed to retire the per-row fee trigger';
    end if;
end;
$$;

commit;
