-- Renew and fence durable Stripe webhook processing leases.
--
-- A renewal may extend only a live lease owned by the caller. It never
-- resurrects an expired lease: after serverless suspension, the invocation
-- must prove ownership again and retry the delivery if another owner won.

begin;

create or replace function public.renew_stripe_webhook_event_lease(
    p_event_id text,
    p_lease_owner uuid,
    p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    renewed_count integer;
    renewal_time timestamptz := clock_timestamp();
begin
    if nullif(btrim(p_event_id), '') is null
       or length(p_event_id) > 255
       or p_lease_owner is null
       or p_lease_seconds is null
       or p_lease_seconds not between 15 and 300 then
        raise exception using
            errcode = '22023',
            message = 'invalid Stripe webhook lease renewal parameters';
    end if;

    update public.stripe_webhook_events
       set lease_expires_at = renewal_time + make_interval(secs => p_lease_seconds),
           updated_at = renewal_time
     where event_id = p_event_id
       and status = 'processing'
       and lease_owner = p_lease_owner
       and lease_expires_at > renewal_time;
    get diagnostics renewed_count = row_count;
    return renewed_count = 1;
end;
$$;

-- These wrappers make renewal and the canonical billing-account CAS one
-- PostgreSQL transaction. The renewal UPDATE locks the inbox row until the CAS
-- commits, so a concurrent claim cannot reclaim between an application-level
-- ownership check and its database business-state write.
create or replace function public.compare_and_set_stripe_subscription_snapshot_for_webhook(
    p_event_id text,
    p_lease_owner uuid,
    p_lease_seconds integer,
    p_organization_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_type text,
    p_stripe_subscription_id text,
    p_subscription_status text,
    p_billing_started_at timestamptz,
    p_current_period_start timestamptz,
    p_current_period_end timestamptz
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if not public.renew_stripe_webhook_event_lease(
        p_event_id, p_lease_owner, p_lease_seconds
    ) then
        raise exception using
            errcode = '55000',
            message = 'Stripe webhook lease is not owned for subscription reconciliation';
    end if;
    return public.compare_and_set_stripe_subscription_snapshot(
        p_organization_id,
        p_expected_revision,
        p_event_created,
        p_event_id,
        p_event_type,
        p_stripe_subscription_id,
        p_subscription_status,
        p_billing_started_at,
        p_current_period_start,
        p_current_period_end
    );
end;
$$;

create or replace function public.compare_and_set_stripe_invoice_snapshot_for_webhook(
    p_event_id text,
    p_lease_owner uuid,
    p_lease_seconds integer,
    p_organization_id uuid,
    p_expected_revision bigint,
    p_event_created bigint,
    p_event_type text,
    p_last_invoice_id text,
    p_last_invoice_status text
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
begin
    if not public.renew_stripe_webhook_event_lease(
        p_event_id, p_lease_owner, p_lease_seconds
    ) then
        raise exception using
            errcode = '55000',
            message = 'Stripe webhook lease is not owned for invoice reconciliation';
    end if;
    return public.compare_and_set_stripe_invoice_snapshot(
        p_organization_id,
        p_expected_revision,
        p_event_created,
        p_event_id,
        p_event_type,
        p_last_invoice_id,
        p_last_invoice_status
    );
end;
$$;

-- Completion is the acknowledgement fence. Even an owner UUID still stored on
-- an expired row is stale and must not convert retryable work into processed.
create or replace function public.mark_stripe_webhook_event_processed(
    p_event_id text,
    p_lease_owner uuid
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    completed_count integer;
    completion_time timestamptz := clock_timestamp();
begin
    update public.stripe_webhook_events
       set status = 'processed',
           processed_at = completion_time,
           lease_owner = null,
           lease_expires_at = null,
           last_error = '',
           updated_at = completion_time
     where event_id = p_event_id
       and status = 'processing'
       and lease_owner = p_lease_owner
       and lease_expires_at > completion_time;
    get diagnostics completed_count = row_count;
    return completed_count = 1;
end;
$$;

-- Failure cleanup remains an optimization. It can release only the caller's
-- still-live lease; an expired or reclaimed row is left for its current owner.
create or replace function public.fail_stripe_webhook_event(
    p_event_id text,
    p_lease_owner uuid,
    p_error text
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $$
declare
    failed_count integer;
    failure_time timestamptz := clock_timestamp();
begin
    update public.stripe_webhook_events
       set lease_owner = null,
           lease_expires_at = failure_time,
           last_error = left(coalesce(p_error, 'webhook application failed'), 480),
           updated_at = failure_time
     where event_id = p_event_id
       and status = 'processing'
       and lease_owner = p_lease_owner
       and lease_expires_at > failure_time;
    get diagnostics failed_count = row_count;
    return failed_count = 1;
end;
$$;

revoke all on function public.renew_stripe_webhook_event_lease(text, uuid, integer)
    from public, anon, authenticated;
revoke all on function public.compare_and_set_stripe_subscription_snapshot_for_webhook(
    text,uuid,integer,uuid,bigint,bigint,text,text,text,timestamptz,timestamptz,timestamptz
) from public, anon, authenticated;
revoke all on function public.compare_and_set_stripe_invoice_snapshot_for_webhook(
    text,uuid,integer,uuid,bigint,bigint,text,text,text
) from public, anon, authenticated;
revoke all on function public.mark_stripe_webhook_event_processed(text, uuid)
    from public, anon, authenticated;
revoke all on function public.fail_stripe_webhook_event(text, uuid, text)
    from public, anon, authenticated;
grant execute on function public.renew_stripe_webhook_event_lease(text, uuid, integer)
    to service_role;
grant execute on function public.compare_and_set_stripe_subscription_snapshot_for_webhook(
    text,uuid,integer,uuid,bigint,bigint,text,text,text,timestamptz,timestamptz,timestamptz
) to service_role;
grant execute on function public.compare_and_set_stripe_invoice_snapshot_for_webhook(
    text,uuid,integer,uuid,bigint,bigint,text,text,text
) to service_role;
grant execute on function public.mark_stripe_webhook_event_processed(text, uuid)
    to service_role;
grant execute on function public.fail_stripe_webhook_event(text, uuid, text)
    to service_role;

-- Prove renewal, expiry fencing, takeover, and stale-owner exclusion against
-- PostgreSQL in the same transaction as the function definitions.
do $$
declare
    test_event_id constant text := 'evt_brevitas_migration_webhook_renewal';
    first_owner constant uuid := '30000000-0000-4000-8000-000000000003';
    second_owner constant uuid := '40000000-0000-4000-8000-000000000004';
    outcome text;
begin
    delete from public.stripe_webhook_events where event_id = test_event_id;

    outcome := public.claim_stripe_webhook_event(
        test_event_id, 'invoice.paid', first_owner, 30
    );
    if outcome <> 'claimed' then raise exception 'webhook renewal fixture claim failed'; end if;
    if not public.renew_stripe_webhook_event_lease(test_event_id, first_owner, 30) then
        raise exception 'live webhook lease was not renewed';
    end if;
    outcome := public.claim_stripe_webhook_event(
        test_event_id, 'invoice.paid', second_owner, 30
    );
    if outcome <> 'busy' then raise exception 'renewed webhook lease was reclaimed'; end if;

    update public.stripe_webhook_events
       set lease_expires_at = clock_timestamp() - interval '1 second'
     where event_id = test_event_id;
    if public.renew_stripe_webhook_event_lease(test_event_id, first_owner, 30) then
        raise exception 'expired webhook lease was resurrected';
    end if;
    if public.mark_stripe_webhook_event_processed(test_event_id, first_owner) then
        raise exception 'expired webhook owner completed an event';
    end if;

    outcome := public.claim_stripe_webhook_event(
        test_event_id, 'invoice.paid', second_owner, 30
    );
    if outcome <> 'claimed' then raise exception 'expired webhook lease was not reclaimed'; end if;
    if public.mark_stripe_webhook_event_processed(test_event_id, first_owner) then
        raise exception 'stale webhook owner completed a reclaimed event';
    end if;
    if public.fail_stripe_webhook_event(test_event_id, first_owner, 'stale failure') then
        raise exception 'stale webhook owner failed a reclaimed event';
    end if;
    if not public.renew_stripe_webhook_event_lease(test_event_id, second_owner, 30) then
        raise exception 'current webhook owner could not renew after takeover';
    end if;
    if not public.mark_stripe_webhook_event_processed(test_event_id, second_owner) then
        raise exception 'current webhook owner could not complete after renewal';
    end if;

    delete from public.stripe_webhook_events where event_id = test_event_id;
end;
$$;

commit;
