\set ON_ERROR_STOP on

-- Migration 015 must fence a durable chat job before a potentially billable
-- provider POST. A marked job is never reclaimed automatically because the
-- supported provider contracts do not offer verified result reconciliation.

do $$
declare
    v_function oid;
    v_search_path text;
begin
    foreach v_function in array array[
        to_regprocedure('public.mark_ai_job_provider_outbound_started(uuid,text)'),
        to_regprocedure('public.claim_ai_job(text,integer)')
    ]
    loop
        if v_function is null then
            raise exception 'provider outbound fencing function is missing';
        end if;
        select array_to_string(proconfig, ',') into v_search_path
          from pg_proc where oid = v_function;
        if not (select prosecdef from pg_proc where oid = v_function)
           or v_search_path <> 'search_path=pg_catalog, public, pg_temp'
           or not has_function_privilege('service_role', v_function, 'EXECUTE')
           or has_function_privilege('anon', v_function, 'EXECUTE')
           or has_function_privilege('authenticated', v_function, 'EXECUTE')
           or exists (
                select 1
                  from pg_proc procedure
                  cross join lateral aclexplode(coalesce(
                      procedure.proacl,
                      acldefault('f', procedure.proowner)
                  )) privilege
                 where procedure.oid = v_function
                   and privilege.grantee = 0
                   and privilege.privilege_type = 'EXECUTE'
           ) then
            raise exception 'provider outbound fencing function security is unsafe: %',
                v_function::regprocedure;
        end if;
    end loop;

    if not exists (
        select 1
          from pg_constraint constraint_state
         where constraint_state.conrelid = 'public.ai_jobs'::regclass
           and constraint_state.conname = 'ai_jobs_provider_outbound_identity_check'
           and constraint_state.convalidated
    ) or not exists (
        select 1
          from pg_index index_state
          join pg_class index_relation on index_relation.oid = index_state.indexrelid
         where index_state.indrelid = 'public.ai_jobs'::regclass
           and index_relation.relname = 'ai_jobs_provider_outbound_ambiguity_idx'
           and index_state.indisvalid
           and pg_get_expr(index_state.indpred, index_state.indrelid)
               = '(provider_outbound_started_at IS NOT NULL)'
    ) then
        raise exception 'provider outbound constraint or partial index is missing';
    end if;
end;
$$;

insert into public.customers(
    id, organization_id, external_id, display_name
) values (
    '15000000-0000-4000-8000-000000000015',
    '10000000-0000-4000-8000-000000000001',
    'provider-outbound-fence-fixture',
    'Provider outbound fence fixture'
) on conflict (organization_id, external_id) do update set
    display_name = excluded.display_name;

do $$
declare
    v_job_id uuid;
    v_claim public.ai_jobs%rowtype;
    v_count integer;
begin
    -- A crash before the marker remains reclaimable and consumes one new
    -- bounded attempt.
    v_job_id := '15000000-0000-4000-8000-000000000101';
    insert into public.ai_jobs(
        id, organization_id, customer_id, key_hash, idempotency_key,
        operation, payload_ciphertext, status, attempts, max_attempts,
        available_at, lease_owner, lease_expires_at, expires_at
    ) values (
        v_job_id,
        '10000000-0000-4000-8000-000000000001',
        '15000000-0000-4000-8000-000000000015',
        'release-key-a', 'provider-fence-crash-before',
        'chat', 'fixture-ciphertext', 'running', 1, 3,
        now() - interval '2 minutes', 'crashed-before-worker',
        now() - interval '1 minute', now() + interval '1 hour'
    ) on conflict (id) do update set
        status = 'running', attempts = 1, max_attempts = 3,
        lease_owner = 'crashed-before-worker',
        lease_expires_at = now() - interval '1 minute',
        provider_outbound_started_at = null,
        provider_outbound_attempt = null,
        completed_at = null, last_error_code = '';

    select * into v_claim
      from public.claim_ai_job('replacement-before-worker', 180)
     limit 1;
    if v_claim.id is distinct from v_job_id
       or v_claim.status <> 'leased'
       or v_claim.attempts <> 2
       or v_claim.lease_owner <> 'replacement-before-worker' then
        raise exception 'unmarked provider job was not safely reclaimed';
    end if;
    update public.ai_jobs
       set status = 'succeeded', completed_at = now(),
           lease_owner = null, lease_expires_at = null
     where id = v_job_id;

    -- Only the active owning worker may set the marker, and it may do so once.
    v_job_id := '15000000-0000-4000-8000-000000000102';
    insert into public.ai_jobs(
        id, organization_id, customer_id, key_hash, idempotency_key,
        operation, payload_ciphertext, status, attempts, max_attempts,
        available_at, lease_owner, lease_expires_at, expires_at
    ) values (
        v_job_id,
        '10000000-0000-4000-8000-000000000001',
        '15000000-0000-4000-8000-000000000015',
        'release-key-a', 'provider-fence-crash-after',
        'chat', 'fixture-ciphertext', 'running', 1, 3,
        now(), 'provider-owner-worker', now() + interval '5 minutes',
        now() + interval '1 hour'
    ) on conflict (id) do update set
        status = 'running', attempts = 1, max_attempts = 3,
        lease_owner = 'provider-owner-worker',
        lease_expires_at = now() + interval '5 minutes',
        provider_outbound_started_at = null,
        provider_outbound_attempt = null,
        completed_at = null, last_error_code = '';

    select count(*) into v_count
      from public.mark_ai_job_provider_outbound_started(
          v_job_id, 'wrong-provider-worker'
      );
    if v_count <> 0 then
        raise exception 'non-owner set the provider outbound marker';
    end if;
    select count(*) into v_count
      from public.mark_ai_job_provider_outbound_started(
          v_job_id, 'provider-owner-worker'
      );
    if v_count <> 1 or not exists (
        select 1 from public.ai_jobs
         where id = v_job_id
           and provider_outbound_started_at is not null
           and provider_outbound_attempt = attempts
           and provider_outbound_attempt = 1
    ) then
        raise exception 'owner could not persist the provider outbound marker';
    end if;
    select count(*) into v_count
      from public.mark_ai_job_provider_outbound_started(
          v_job_id, 'provider-owner-worker'
      );
    if v_count <> 0 then
        raise exception 'provider outbound marker was overwritten';
    end if;

    begin
        update public.ai_jobs
           set provider_outbound_attempt = null
         where id = v_job_id;
        raise exception 'partial provider outbound identity was accepted';
    exception when check_violation then
        null;
    end;

    update public.ai_jobs
       set lease_expires_at = now() - interval '1 minute'
     where id = v_job_id;
    select count(*) into v_count
      from public.claim_ai_job('replacement-after-worker', 180);
    if v_count <> 0 or not exists (
        select 1 from public.ai_jobs
         where id = v_job_id
           and status = 'dead'
           and attempts = 1
           and last_error_code = 'provider_outcome_ambiguous'
           and provider_outbound_started_at is not null
           and provider_outbound_attempt = 1
           and lease_owner is null
           and lease_expires_at is null
    ) then
        raise exception 'marked provider job was replayed or lost its evidence';
    end if;

    update public.ai_jobs
       set status = 'succeeded'
     where id = v_job_id
       and lease_owner = 'provider-owner-worker'
       and status in ('leased', 'running')
       and lease_expires_at > now();
    get diagnostics v_count = row_count;
    if v_count <> 0 then
        raise exception 'stale provider worker committed after reclaim';
    end if;

    -- A committed marker whose RPC representation was lost may be followed by
    -- an application requeue. The persisted marker still wins at the next claim.
    v_job_id := '15000000-0000-4000-8000-000000000103';
    insert into public.ai_jobs(
        id, organization_id, customer_id, key_hash, idempotency_key,
        operation, payload_ciphertext, status, attempts, max_attempts,
        available_at, lease_owner, lease_expires_at, expires_at
    ) values (
        v_job_id,
        '10000000-0000-4000-8000-000000000001',
        '15000000-0000-4000-8000-000000000015',
        'release-key-a', 'provider-fence-response-loss',
        'chat', 'fixture-ciphertext', 'running', 1, 3,
        now(), 'response-loss-worker', now() + interval '5 minutes',
        now() + interval '1 hour'
    ) on conflict (id) do update set
        status = 'running', attempts = 1, max_attempts = 3,
        lease_owner = 'response-loss-worker',
        lease_expires_at = now() + interval '5 minutes',
        provider_outbound_started_at = null,
        provider_outbound_attempt = null,
        completed_at = null, last_error_code = '';
    perform * from public.mark_ai_job_provider_outbound_started(
        v_job_id, 'response-loss-worker'
    );
    update public.ai_jobs
       set status = 'queued', available_at = now(),
           lease_owner = null, lease_expires_at = null
     where id = v_job_id;
    select count(*) into v_count
      from public.claim_ai_job('response-loss-replacement', 180);
    if v_count <> 0 or not exists (
        select 1 from public.ai_jobs
         where id = v_job_id
           and status = 'dead'
           and attempts = 1
           and last_error_code = 'provider_outcome_ambiguous'
           and provider_outbound_started_at is not null
    ) then
        raise exception 'response-loss requeue bypassed provider marker';
    end if;

    -- Compression never enters a provider outbound state.
    v_job_id := '15000000-0000-4000-8000-000000000104';
    insert into public.ai_jobs(
        id, organization_id, customer_id, key_hash, idempotency_key,
        operation, payload_ciphertext, status, attempts, max_attempts,
        available_at, lease_owner, lease_expires_at, expires_at
    ) values (
        v_job_id,
        '10000000-0000-4000-8000-000000000001',
        '15000000-0000-4000-8000-000000000015',
        'release-key-a', 'provider-fence-compress',
        'compress', 'fixture-ciphertext', 'running', 1, 3,
        now(), 'compression-worker', now() + interval '5 minutes',
        now() + interval '1 hour'
    ) on conflict (id) do update set
        operation = 'compress', status = 'running', attempts = 1,
        lease_owner = 'compression-worker',
        lease_expires_at = now() + interval '5 minutes',
        provider_outbound_started_at = null,
        provider_outbound_attempt = null,
        completed_at = null, last_error_code = '';
    select count(*) into v_count
      from public.mark_ai_job_provider_outbound_started(
          v_job_id, 'compression-worker'
      );
    if v_count <> 0 then
        raise exception 'compression job received a provider outbound marker';
    end if;
    update public.ai_jobs
       set status = 'cancelled', completed_at = now(),
           lease_owner = null, lease_expires_at = null
     where id = v_job_id;

    -- Invalid worker identities fail closed without mutating claim state.
    select count(*) into v_count
      from public.claim_ai_job('invalid worker identity', 180);
    if v_count <> 0 then
        raise exception 'invalid worker identity claimed a job';
    end if;
end;
$$;
