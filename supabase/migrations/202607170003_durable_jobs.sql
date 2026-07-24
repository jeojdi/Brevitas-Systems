-- Durable asynchronous AI jobs. Redis carries job IDs only; Postgres is truth.

create table if not exists public.ai_jobs (
    id uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations(id) on delete restrict,
    customer_id uuid not null,
    key_hash text not null references public.api_keys(key_hash) on delete restrict,
    idempotency_key text not null,
    operation text not null default 'chat' check (operation in ('chat', 'compress')),
    provider text not null default '',
    model text not null default '',
    payload_ciphertext text not null,
    result_ciphertext text,
    status text not null default 'queued' check (
        status in ('queued', 'leased', 'running', 'succeeded', 'failed', 'cancelled', 'dead')
    ),
    attempts integer not null default 0 check (attempts >= 0),
    max_attempts integer not null default 3 check (max_attempts between 1 and 10),
    available_at timestamptz not null default now(),
    lease_owner text,
    lease_expires_at timestamptz,
    cancel_requested boolean not null default false,
    last_error_code text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    expires_at timestamptz not null default (now() + interval '24 hours'),
    foreign key (organization_id, customer_id)
        references public.customers(organization_id, id) on delete restrict,
    unique (organization_id, customer_id, idempotency_key)
);

create index if not exists ai_jobs_claim_idx
    on public.ai_jobs(status, available_at, lease_expires_at, created_at);
create index if not exists ai_jobs_tenant_idx
    on public.ai_jobs(organization_id, customer_id, created_at desc);
create index if not exists ai_jobs_expiry_idx on public.ai_jobs(expires_at);

alter table public.ai_jobs enable row level security;
revoke all on public.ai_jobs from public, anon, authenticated;
grant select, insert, update, delete on public.ai_jobs to service_role;

create or replace function public.claim_ai_job(
    p_worker_id text,
    p_lease_seconds integer default 180
) returns setof public.ai_jobs
language plpgsql
security definer
set search_path = public
as $$
declare selected_id uuid;
begin
    update public.ai_jobs
       set status = 'dead', completed_at = now(), updated_at = now(),
           last_error_code = 'expired'
     where status = 'queued' and expires_at <= now();

    -- A worker may die after consuming its final allowed attempt. Such rows are
    -- terminal, not permanently stuck in `running` after the lease expires.
    update public.ai_jobs
       set status = 'dead', completed_at = now(), updated_at = now(),
           lease_owner = null, lease_expires_at = null,
           last_error_code = 'lease_expired'
     where status in ('leased', 'running')
       and lease_expires_at < now()
       and attempts >= max_attempts;

    select id into selected_id
      from public.ai_jobs
     where attempts < max_attempts
       and cancel_requested = false
       and (
           (status = 'queued' and available_at <= now() and expires_at > now())
           or (status in ('leased', 'running') and lease_expires_at < now())
       )
     order by available_at, created_at
     for update skip locked
     limit 1;
    if selected_id is null then return; end if;

    return query
    update public.ai_jobs
       set status = 'leased', attempts = attempts + 1,
           lease_owner = p_worker_id,
           lease_expires_at = now() + make_interval(secs => greatest(10, least(p_lease_seconds, 3600))),
           updated_at = now()
     where id = selected_id
     returning *;
end;
$$;

revoke all on function public.claim_ai_job(text, integer) from public, anon, authenticated;
grant execute on function public.claim_ai_job(text, integer) to service_role;

create or replace function public.purge_expired_ai_jobs()
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare removed bigint;
begin
    delete from public.ai_jobs
     where expires_at <= now()
       and status in ('succeeded', 'failed', 'cancelled', 'dead');
    get diagnostics removed = row_count;
    return removed;
end;
$$;

revoke all on function public.purge_expired_ai_jobs() from public, anon, authenticated;
grant execute on function public.purge_expired_ai_jobs() to service_role;
