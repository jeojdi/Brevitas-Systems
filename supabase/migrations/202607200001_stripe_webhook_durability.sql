-- Durable, crash-recoverable Stripe webhook inbox.
--
-- Historical rows were inserted only after Stripe signature verification, but
-- the original schema treated insertion (claim) as completion. Preserve those
-- rows as completed while making every new claim an expiring lease. A delivery
-- receives a successful acknowledgement only after mark_*_processed succeeds.

begin;

alter table public.stripe_webhook_events
    alter column processed_at drop not null,
    alter column processed_at drop default,
    add column if not exists status text not null default 'processed',
    add column if not exists attempts integer not null default 1,
    add column if not exists lease_owner uuid,
    add column if not exists lease_expires_at timestamptz,
    add column if not exists last_error text not null default '',
    add column if not exists updated_at timestamptz not null default now();

do $$
begin
    alter table public.stripe_webhook_events
        add constraint stripe_webhook_events_status_check
        check (status in ('processing', 'processed'));
exception
    when duplicate_object then null;
end;
$$;

do $$
begin
    alter table public.stripe_webhook_events
        add constraint stripe_webhook_events_attempts_check
        check (attempts >= 1);
exception
    when duplicate_object then null;
end;
$$;

create index if not exists stripe_webhook_events_reclaim_idx
    on public.stripe_webhook_events (lease_expires_at, event_id)
    where status = 'processing';

-- Return values are deliberately closed: claimed means this caller owns the
-- lease, processed is the only terminal duplicate, and busy must be answered
-- with a retryable non-2xx response by the HTTP handler.
create or replace function public.claim_stripe_webhook_event(
    p_event_id text,
    p_event_type text,
    p_lease_owner uuid,
    p_lease_seconds integer
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    inserted_count integer;
    current_event public.stripe_webhook_events%rowtype;
begin
    if nullif(btrim(p_event_id), '') is null
       or length(p_event_id) > 255
       or nullif(btrim(p_event_type), '') is null
       or length(p_event_type) > 255
       or p_lease_owner is null
       or p_lease_seconds not between 15 and 300 then
        raise exception using
            errcode = '22023',
            message = 'invalid Stripe webhook claim parameters';
    end if;

    insert into public.stripe_webhook_events (
        event_id, event_type, status, processed_at, attempts,
        lease_owner, lease_expires_at, last_error, updated_at
    ) values (
        p_event_id, p_event_type, 'processing', null, 1,
        p_lease_owner,
        clock_timestamp() + make_interval(secs => p_lease_seconds),
        '', clock_timestamp()
    )
    on conflict (event_id) do nothing;
    get diagnostics inserted_count = row_count;
    if inserted_count = 1 then
        return 'claimed';
    end if;

    select webhook.* into current_event
      from public.stripe_webhook_events webhook
     where webhook.event_id = p_event_id
     for update;
    if not found then
        -- A privileged manual deletion raced the claim. Never acknowledge it.
        raise exception 'Stripe webhook inbox row disappeared during claim';
    end if;
    if current_event.event_type <> p_event_type then
        raise exception using
            errcode = '22023',
            message = 'Stripe event id was reused with a different event type';
    end if;
    if current_event.status = 'processed' then
        return 'processed';
    end if;
    if current_event.lease_expires_at is not null
       and current_event.lease_expires_at > clock_timestamp() then
        return 'busy';
    end if;

    update public.stripe_webhook_events
       set attempts = least(attempts::bigint + 1, 2147483647)::integer,
           lease_owner = p_lease_owner,
           lease_expires_at = clock_timestamp() + make_interval(secs => p_lease_seconds),
           last_error = '',
           updated_at = clock_timestamp()
     where event_id = p_event_id;
    return 'claimed';
end;
$$;

create or replace function public.mark_stripe_webhook_event_processed(
    p_event_id text,
    p_lease_owner uuid
)
returns boolean
language sql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    update public.stripe_webhook_events
       set status = 'processed',
           processed_at = clock_timestamp(),
           lease_owner = null,
           lease_expires_at = null,
           last_error = '',
           updated_at = clock_timestamp()
     where event_id = p_event_id
       and status = 'processing'
       and lease_owner = p_lease_owner
    returning true;
$$;

-- Failure cleanup is an optimization, not a correctness dependency. If this
-- RPC itself fails, the unchanged claim still becomes retryable at lease expiry.
create or replace function public.fail_stripe_webhook_event(
    p_event_id text,
    p_lease_owner uuid,
    p_error text
)
returns boolean
language sql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
    update public.stripe_webhook_events
       set lease_owner = null,
           lease_expires_at = clock_timestamp(),
           last_error = left(coalesce(p_error, 'webhook application failed'), 480),
           updated_at = clock_timestamp()
     where event_id = p_event_id
       and status = 'processing'
       and lease_owner = p_lease_owner
    returning true;
$$;

revoke insert, update, delete, truncate
    on table public.stripe_webhook_events
    from anon, authenticated, service_role;
grant select on table public.stripe_webhook_events to service_role;

revoke all on function public.claim_stripe_webhook_event(text, text, uuid, integer)
    from public, anon, authenticated;
revoke all on function public.mark_stripe_webhook_event_processed(text, uuid)
    from public, anon, authenticated;
revoke all on function public.fail_stripe_webhook_event(text, uuid, text)
    from public, anon, authenticated;
grant execute on function public.claim_stripe_webhook_event(text, text, uuid, integer)
    to service_role;
grant execute on function public.mark_stripe_webhook_event_processed(text, uuid)
    to service_role;
grant execute on function public.fail_stripe_webhook_event(text, uuid, text)
    to service_role;

-- Migration-time state-machine checks. These prove claim-before-apply recovery,
-- stale takeover ownership, and terminal duplicate behavior against PostgreSQL.
do $$
declare
    test_event_id constant text := 'evt_brevitas_migration_webhook_durability';
    first_owner constant uuid := '10000000-0000-4000-8000-000000000001';
    second_owner constant uuid := '20000000-0000-4000-8000-000000000002';
    outcome text;
begin
    delete from public.stripe_webhook_events where event_id = test_event_id;

    outcome := public.claim_stripe_webhook_event(test_event_id, 'invoice.paid', first_owner, 30);
    if outcome <> 'claimed' then raise exception 'fresh webhook claim failed'; end if;

    outcome := public.claim_stripe_webhook_event(test_event_id, 'invoice.paid', second_owner, 30);
    if outcome <> 'busy' then raise exception 'concurrent webhook claim was not excluded'; end if;

    -- Simulate a process death after claim and before business state was applied.
    update public.stripe_webhook_events
       set lease_expires_at = clock_timestamp() - interval '1 second'
     where event_id = test_event_id;
    outcome := public.claim_stripe_webhook_event(test_event_id, 'invoice.paid', second_owner, 30);
    if outcome <> 'claimed' then raise exception 'stale webhook claim was not retryable'; end if;

    if public.mark_stripe_webhook_event_processed(test_event_id, first_owner) then
        raise exception 'stale webhook owner completed a reclaimed event';
    end if;
    if not public.mark_stripe_webhook_event_processed(test_event_id, second_owner) then
        raise exception 'current webhook owner could not complete its event';
    end if;

    outcome := public.claim_stripe_webhook_event(test_event_id, 'invoice.paid', first_owner, 30);
    if outcome <> 'processed' then raise exception 'completed webhook was not deduplicated'; end if;

    delete from public.stripe_webhook_events where event_id = test_event_id;
end;
$$;

commit;
