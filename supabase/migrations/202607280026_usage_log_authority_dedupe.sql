-- Make the usage_log dedupe key carry the AUTHORITY of the row.
--
-- THE DEFECT. 20260710_cloud_usage.sql:125-126 created
--     create unique index usage_log_request_unique
--         on public.usage_log(key_hash, request_id) where request_id <> '';
-- with no authoritative column, while 202607170001:157 later added
-- usage_log.authoritative and the receipt intake became authority-scoped. The
-- storage layer and the application layer therefore disagree about what a
-- duplicate IS. api/server.py::_record_usage_report probes
-- `has_request(key_hash, request_id, authoritative=...)`, which correctly says
-- "an analytics row with this id is not a duplicate of a billable one" -- and
-- then the INSERT hits the narrower unique index, loses, and the error is
-- swallowed into `{"duplicate": true}`. The caller is told its receipt was a
-- repeat; the billable row is silently gone.
--
-- WHY IT IS NOT CURRENTLY EXPLOITABLE, AND WHY THAT IS NOT ENOUGH. request_id
-- is derived from provider-visible material, so a caller can name an id it has
-- seen. What stops a cheap non-authoritative POST /v1/usage from pre-empting
-- the billable hosted-bridge receipt is the RECEIPT_ID_PREFIX namespace
-- reservation on the intake path, which rewrites a caller-supplied `proxy:` id
-- into `client:`. That control is real and stays. But it makes the storage
-- layer's correctness contingent on an application-layer rewrite: one refactor
-- of the intake path and lost revenue becomes reachable again, with no error
-- surfaced anywhere because the loss is reported to the caller as success.
-- After this migration the probe is genuinely load-bearing -- index and probe
-- agree on the same key -- and the namespace reservation goes back to being
-- defence in depth rather than the only defence.
--
-- WHAT CHANGES SEMANTICALLY. An authoritative row and a non-authoritative row
-- MAY now share (key_hash, request_id). That is the intent: they are different
-- facts about the same request (a billable settlement receipt and an analytics
-- observation), and the billing path already filters on authoritative. Two rows
-- of the SAME authority with the same id are still rejected, which is the
-- property that actually prevents double billing.
--
-- BUILD STRATEGY. The concurrent (non-transactional) index build form is not
-- available here: every forward migration from 202607200001 onward is required
-- by scripts/ci/verify-migrations.mjs to be one explicit transaction, and
-- scripts/ci/run-migration-tests.sh failure-injects each file before COMMIT to
-- prove a failed apply leaves no partial state. A split, non-transactional
-- build would forfeit both guarantees for this file. Instead the build is kept
-- short and safe: the new index is created BEFORE the old one is dropped (so
-- there is no instant without dedupe protection), and `lock_timeout` makes the
-- apply abort and be retried rather than queue behind a long reader while
-- holding writes off public.usage_log. Per MEMORY "no-billable-usage-since-jul-17"
-- production usage_log is small and idle, so the build is short; the timeout is
-- what keeps that assumption from being load-bearing.
--
-- The old index guarantees no widened-key duplicate can exist, so creation
-- cannot be blocked on any database that still has it. The precondition below
-- checks anyway and refuses with a count rather than deleting anything: usage
-- rows are billing evidence and a migration must never resolve a conflict by
-- destroying them.
--
-- Forward-only and idempotent.

-- REVERSE: DDL: create unique index if not exists usage_log_request_unique on public.usage_log(key_hash, request_id) where request_id <> ''; drop index if exists public.usage_log_request_authority_unique;

begin;

-- A stuck apply must fail, not hold writes off the usage table.
set local lock_timeout = '15s';

do $migration_precondition$
declare
    v_conflicts bigint;
begin
    if to_regclass('public.usage_log') is null
       or not exists (
           select 1
             from information_schema.columns
            where table_schema = 'public'
              and table_name = 'usage_log'
              and column_name = 'authoritative'
       ) then
        raise exception using
            errcode = '55000',
            message = '202607280026 requires 20260710 and 202607170001 first';
    end if;

    select count(*) into v_conflicts
      from (
        select 1
          from public.usage_log usage
         where usage.request_id <> ''
         group by usage.key_hash, usage.request_id, usage.authoritative
        having count(*) > 1
      ) conflicting;
    if v_conflicts > 0 then
        raise exception using
            errcode = '23505',
            message = pg_catalog.format(
                '202607280026 cannot widen the usage dedupe key: %s '
                || '(key_hash, request_id, authoritative) groups already hold '
                || 'more than one row', v_conflicts),
            hint = 'Reconcile the duplicate usage receipts by hand; this '
                || 'migration will not delete billing evidence to make an '
                || 'index build succeed.';
    end if;
end;
$migration_precondition$;

-- Widened first: the table is never left without a uniqueness guarantee.
create unique index if not exists usage_log_request_authority_unique
    on public.usage_log (key_hash, request_id, authoritative)
    where request_id <> '';

-- The narrow index is now strictly stronger than the intake contract, and it is
-- the object that turns a legitimate authoritative receipt into a swallowed
-- "duplicate". Its (key_hash, request_id) prefix survives as the leading
-- columns of the widened index, so the has_request probe keeps its index scan.
drop index if exists public.usage_log_request_unique;

comment on index public.usage_log_request_authority_unique is
    'Authority-scoped receipt dedupe: one row per (key_hash, request_id) per authority, matching the has_request probe in api/server.py.';

commit;
