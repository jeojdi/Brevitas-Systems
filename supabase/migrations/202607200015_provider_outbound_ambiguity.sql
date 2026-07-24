-- Fence durable chat jobs immediately before an unsupported billable provider POST.
-- A marked job may have been accepted remotely; without verified provider
-- idempotency or reconciliation it must never be automatically replayed.

begin;

alter table public.ai_jobs
    add column if not exists provider_outbound_started_at timestamptz,
    add column if not exists provider_outbound_attempt integer;

alter table public.ai_jobs
    drop constraint if exists ai_jobs_provider_outbound_identity_check;
alter table public.ai_jobs
    add constraint ai_jobs_provider_outbound_identity_check check (
        (
            provider_outbound_started_at is null
            and provider_outbound_attempt is null
        )
        or (
            provider_outbound_started_at is not null
            and provider_outbound_attempt is not null
            and operation = 'chat'
            and provider_outbound_attempt between 1 and attempts
            and provider_outbound_attempt <= max_attempts
            and provider_outbound_started_at >= created_at
        )
    ) not valid;
alter table public.ai_jobs
    validate constraint ai_jobs_provider_outbound_identity_check;

create index if not exists ai_jobs_provider_outbound_ambiguity_idx
    on public.ai_jobs(status, lease_expires_at, provider_outbound_started_at)
    where provider_outbound_started_at is not null;

create or replace function public.mark_ai_job_provider_outbound_started(
    p_job_id uuid,
    p_worker_id text
) returns setof public.ai_jobs
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if p_job_id is null
       or p_worker_id is null
       or pg_catalog.char_length(p_worker_id) not between 1 and 128
       or p_worker_id !~ '^[A-Za-z0-9._:-]+$' then
        return;
    end if;

    return query
    update public.ai_jobs
       set provider_outbound_started_at = pg_catalog.now(),
           provider_outbound_attempt = attempts,
           updated_at = pg_catalog.now()
     where id = p_job_id
       and lease_owner = p_worker_id
       and status = 'running'
       and lease_expires_at > pg_catalog.now()
       and operation = 'chat'
       and cancel_requested = false
       and provider_outbound_started_at is null
       and provider_outbound_attempt is null
     returning *;
end;
$$;

revoke all on function public.mark_ai_job_provider_outbound_started(uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.mark_ai_job_provider_outbound_started(uuid, text)
    to service_role;

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

    update public.ai_jobs
       set status = 'dead', completed_at = pg_catalog.now(),
           updated_at = pg_catalog.now(),
           last_error_code = 'expired'
     where status = 'queued' and expires_at <= pg_catalog.now();

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

commit;

-- Rollback (manual and destructive):
-- drop function if exists public.mark_ai_job_provider_outbound_started(uuid, text);
-- drop index if exists public.ai_jobs_provider_outbound_ambiguity_idx;
-- alter table public.ai_jobs drop constraint if exists ai_jobs_provider_outbound_identity_check;
-- alter table public.ai_jobs drop column if exists provider_outbound_attempt;
-- alter table public.ai_jobs drop column if exists provider_outbound_started_at;
-- Restore claim_ai_job from 202607170003_durable_jobs.sql after removing the columns.
