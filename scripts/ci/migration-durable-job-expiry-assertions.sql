\set ON_ERROR_STOP on

-- Behavioural contract for 202607280020_expired_job_reclaim_fence.sql. All
-- fixture state is discarded by the trailing rollback, so this file is
-- re-runnable and order-independent.
begin;

insert into auth.users (id, email) values (
    '7e000000-0000-4000-8000-000000000001', 'job-expiry@example.invalid');
insert into public.organizations (id, name, billing_owner_id) values (
    '7e100000-0000-4000-8000-000000000001', 'Job expiry fixture',
    '7e000000-0000-4000-8000-000000000001');
insert into public.customers (id, organization_id, external_id) values (
    '7e200000-0000-4000-8000-000000000001',
    '7e100000-0000-4000-8000-000000000001', 'job-expiry-customer');
insert into public.api_keys (
    key_hash, name, owner_id, organization_id, key_type, environment, key_prefix
) values (
    repeat('7e', 32), 'job expiry key',
    '7e000000-0000-4000-8000-000000000001',
    '7e100000-0000-4000-8000-000000000001', 'device', 'production',
    'bvt_job0001');

insert into public.ai_jobs (
    id, organization_id, customer_id, key_hash, idempotency_key, operation,
    payload_ciphertext, status, attempts, max_attempts, available_at,
    lease_owner, lease_expires_at, created_at, expires_at
) values (
    -- Abandoned past its retention window: leased before expires_at, lease long
    -- lapsed. Before this fix the queued-only sweep never terminalized it and
    -- the reclaim arm of the candidate predicate had no expires_at check, so it
    -- was re-executed on every lease lapse, forever.
    '7e300000-0000-4000-8000-000000000001',
    '7e100000-0000-4000-8000-000000000001',
    '7e200000-0000-4000-8000-000000000001', repeat('7e', 32),
    'job-expiry-abandoned', 'chat', 'job-expiry-abandoned-payload',
    'leased', 1, 3, now() - interval '3 hours',
    'assert-dead-worker', now() - interval '2 hours',
    now() - interval '3 hours', now() - interval '90 minutes'
), (
    -- Leased, lapsed, but still inside its window: it must remain claimable.
    '7e300000-0000-4000-8000-000000000002',
    '7e100000-0000-4000-8000-000000000001',
    '7e200000-0000-4000-8000-000000000001', repeat('7e', 32),
    'job-expiry-live', 'chat', 'job-expiry-live-payload',
    'leased', 1, 3, now() - interval '3 hours',
    'assert-dead-worker', now() - interval '2 hours',
    now() - interval '3 hours', now() + interval '2 hours'
);

do $$
declare
    v_claimed uuid;
    v_row public.ai_jobs%rowtype;
begin
    select claimed.id into v_claimed
      from public.claim_ai_job('assert-expiry-worker', 60) claimed;

    select * into v_row from public.ai_jobs job
     where job.id = '7e300000-0000-4000-8000-000000000001';
    if v_row.status <> 'dead' or v_row.last_error_code <> 'expired'
       or v_row.lease_owner is not null or v_row.lease_expires_at is not null
       or v_row.completed_at is null then
        raise exception
            'an abandoned job past its retention window was not terminalized: % / %',
            v_row.status, v_row.last_error_code;
    end if;
    if v_claimed = '7e300000-0000-4000-8000-000000000001' then
        raise exception 'an expired job was reclaimed for re-execution';
    end if;

    -- The reclaim path itself must still work for a lapsed lease inside the
    -- window: this fence must not disable lease recovery.
    if v_claimed is distinct from '7e300000-0000-4000-8000-000000000002' then
        raise exception
            'a lapsed lease inside its retention window was not reclaimed (got %)',
            v_claimed;
    end if;
    select * into v_row from public.ai_jobs job
     where job.id = '7e300000-0000-4000-8000-000000000002';
    if v_row.status <> 'leased' or v_row.lease_owner <> 'assert-expiry-worker'
       or v_row.attempts <> 2 then
        raise exception 'the reclaimed job was not leased to the new worker';
    end if;

    -- A queued job that has already expired is still terminalized rather than
    -- claimed, exactly as before.
    insert into public.ai_jobs (
        id, organization_id, customer_id, key_hash, idempotency_key, operation,
        payload_ciphertext, status, available_at, created_at, expires_at
    ) values (
        '7e300000-0000-4000-8000-000000000003',
        '7e100000-0000-4000-8000-000000000001',
        '7e200000-0000-4000-8000-000000000001', repeat('7e', 32),
        'job-expiry-queued', 'chat', 'job-expiry-queued-payload',
        'queued', now() - interval '2 hours', now() - interval '2 hours',
        now() - interval '1 hour'
    );
    perform 1 from public.claim_ai_job('assert-expiry-worker-2', 60);
    select * into v_row from public.ai_jobs job
     where job.id = '7e300000-0000-4000-8000-000000000003';
    if v_row.status <> 'dead' or v_row.last_error_code <> 'expired' then
        raise exception 'an expired queued job is no longer terminalized';
    end if;
end;
$$;

rollback;
