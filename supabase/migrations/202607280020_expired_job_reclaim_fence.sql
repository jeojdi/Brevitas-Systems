-- Expired jobs must not be reclaimed and re-executed past their retention window.
--
-- claim_ai_job applies the retention check to the `queued` arm of its candidate
-- predicate only (202607200015_provider_outbound_ambiguity.sql:126-131):
--
--     (status = 'queued' and available_at <= now() and expires_at > now())
--     or (status in ('leased','running') and lease_expires_at <= now())
--
-- and the `expired` sweep immediately above it is likewise queued-only, so a row
-- that was leased before its expires_at is never terminalized by retention at
-- all. The two gaps compound: an abandoned job stays alive, and each lease lapse
-- makes it claimable again, arbitrarily far past the retention window its
-- expires_at declares. api/jobs.py:496-497 (SQLiteJobStore.claim) and
-- api/jobs.py:259 (InMemoryJobStore.claim) reproduce the same asymmetry and must
-- follow; they are not changed here.
--
-- The sweep is widened to lease-expired abandoned rows only, never to rows a live
-- worker still owns, and it runs after the provider-outbound ambiguity sweep so a
-- possibly-accepted upstream call keeps its 'provider_outcome_ambiguous' label.
--
-- The function is otherwise transcribed verbatim from 202607200015, which is
-- checksum-pinned and already applied to production. Signature, privileges and
-- the ambiguity fence are unchanged, so
-- scripts/ci/migration-provider-outbound-ambiguity-assertions.sql and
-- verifyProviderOutboundFenceContract() keep their meaning.
--
-- Forward-only and idempotent.

-- REVERSE: PITR-ONLY -- removing the reclaim fence reintroduces the stale-owner completion race it closes

begin;

do $migration_precondition$
begin
    if to_regprocedure('public.claim_ai_job(text,integer)') is null
       or to_regprocedure(
        'public.mark_ai_job_provider_outbound_started(uuid,text)'
    ) is null then
        raise exception using
            errcode = '55000',
            message = '202607280020 requires the provider-outbound job fence';
    end if;
end;
$migration_precondition$;

create or replace function public.claim_ai_job(
    p_worker_id text,
    p_lease_seconds integer default 180
) returns setof public.ai_jobs
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare selected_id uuid;
begin
    if p_worker_id is null
       or pg_catalog.char_length(p_worker_id) not between 1 and 128
       or p_worker_id !~ '^[A-Za-z0-9._:-]+$'
       or p_lease_seconds is null then
        return;
    end if;

    update public.ai_jobs
       set status = 'dead', completed_at = pg_catalog.now(),
           updated_at = pg_catalog.now(),
           lease_owner = null, lease_expires_at = null,
           last_error_code = 'provider_outcome_ambiguous'
     where operation = 'chat'
       and provider_outbound_started_at is not null
       and (
           status = 'queued'
           or (
               status in ('leased', 'running')
               and lease_expires_at <= pg_catalog.now()
           )
       );

    -- Retention terminalizes ABANDONED work too, not only work that never
    -- started. The original sweep was `status = 'queued'`-only, so a job that
    -- was leased before its expires_at and then abandoned was never
    -- terminalized: it simply waited for its lease to lapse and was reclaimed
    -- forever. The lease-expiry predicate is what keeps this safe -- a row a
    -- live worker still owns can never be terminalized -- and the ambiguity
    -- sweep above has already run, so an expired chat row that did POST
    -- upstream keeps its 'provider_outcome_ambiguous' label instead of being
    -- relabelled 'expired'.
    update public.ai_jobs
       set status = 'dead', completed_at = pg_catalog.now(),
           updated_at = pg_catalog.now(),
           lease_owner = null, lease_expires_at = null,
           last_error_code = 'expired'
     where expires_at <= pg_catalog.now()
       and (
           status = 'queued'
           or (
               status in ('leased', 'running')
               and lease_expires_at <= pg_catalog.now()
           )
       );

    update public.ai_jobs
       set status = 'dead', completed_at = pg_catalog.now(),
           updated_at = pg_catalog.now(),
           lease_owner = null, lease_expires_at = null,
           last_error_code = 'lease_expired'
     where status in ('leased', 'running')
       and lease_expires_at <= pg_catalog.now()
       and attempts >= max_attempts;

    select id into selected_id
      from public.ai_jobs
     where attempts < max_attempts
       and cancel_requested = false
       and provider_outbound_started_at is null
       and (
           (status = 'queued' and available_at <= pg_catalog.now()
            and expires_at > pg_catalog.now())
           or (
               status in ('leased', 'running')
               and lease_expires_at <= pg_catalog.now()
               -- The retention window applies to a reclaim exactly as it
               -- applies to a first claim. Without this the queued arm's
               -- `expires_at > now()` was trivially bypassable: let the lease
               -- lapse once and the job is re-executed past its retention
               -- window indefinitely.
               and expires_at > pg_catalog.now()
           )
       )
     order by available_at, created_at
     for update skip locked
     limit 1;
    if selected_id is null then return; end if;

    return query
    update public.ai_jobs
       set status = 'leased', attempts = attempts + 1,
           lease_owner = p_worker_id,
           lease_expires_at = pg_catalog.now() + pg_catalog.make_interval(
               secs => greatest(
                   10, least(p_lease_seconds, 3600)
               )
           ),
           updated_at = pg_catalog.now()
     where id = selected_id
     returning *;
end;
$$;

revoke all on function public.claim_ai_job(text, integer)
    from public, anon, authenticated, service_role;
grant execute on function public.claim_ai_job(text, integer) to service_role;

comment on function public.claim_ai_job(text, integer) is
    'Durable job claim: retention terminalizes abandoned leases and bounds reclaims, so an expired job can never be re-executed past its expires_at.';

commit;
