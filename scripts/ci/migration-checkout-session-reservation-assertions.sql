\set ON_ERROR_STOP on

-- Deterministic PostgreSQL proof for company Checkout reservation fencing.
-- The application separately proves Stripe page/status interpretation; this
-- fixture proves the transaction boundaries and stale-token exclusion.
begin;

insert into auth.users(id,email) values (
    'cc000000-0000-4000-8000-000000000001',
    'checkout-reservation-owner@example.invalid'
) on conflict (id) do nothing;

insert into public.organizations(id,name,billing_owner_id) values
    ('cc100000-0000-4000-8000-000000000001',
     'Checkout reservation fixture A',
     'cc000000-0000-4000-8000-000000000001'),
    ('cc200000-0000-4000-8000-000000000002',
     'Checkout reservation fixture B',
     'cc000000-0000-4000-8000-000000000001'),
    ('cc300000-0000-4000-8000-000000000003',
     'Checkout reservation fixture C',
     'cc000000-0000-4000-8000-000000000001')
on conflict (id) do nothing;

insert into public.billing_accounts(
    organization_id,user_id,stripe_customer_id,subscription_status,
    checkout_session_id
) values
    ('cc100000-0000-4000-8000-000000000001',
     'cc000000-0000-4000-8000-000000000001',
     'cus_checkout_reservation_a','canceled',null),
    ('cc200000-0000-4000-8000-000000000002',
     'cc000000-0000-4000-8000-000000000001',
     'cus_checkout_reservation_b','canceled',null),
    ('cc300000-0000-4000-8000-000000000003',
     'cc000000-0000-4000-8000-000000000001',
     'cus_checkout_reservation_c','canceled',null)
on conflict (organization_id) do update set
    user_id=excluded.user_id,
    stripe_customer_id=excluded.stripe_customer_id,
    stripe_subscription_id=null,
    subscription_status=excluded.subscription_status,
    checkout_session_id=null;

delete from public.billing_checkout_reservations
 where organization_id in (
    'cc100000-0000-4000-8000-000000000001',
    'cc200000-0000-4000-8000-000000000002',
    'cc300000-0000-4000-8000-000000000003'
 );

do $checkout_takeover$
declare
    v_first jsonb;
    v_busy jsonb;
    v_takeover jsonb;
    v_result jsonb;
    v_generation bigint;
begin
    v_first := public.reserve_billing_checkout_generation(
        'cc100000-0000-4000-8000-000000000001',
        'cus_checkout_reservation_a',
        'cc110000-0000-4000-8000-000000000001',
        300
    );
    if v_first->>'code' <> 'acquired'
       or v_first->>'mode' <> 'create_or_recover' then
        raise exception 'first Checkout generation was not acquired';
    end if;
    v_generation := (v_first->>'generation')::bigint;

    v_busy := public.reserve_billing_checkout_generation(
        'cc100000-0000-4000-8000-000000000001',
        'cus_checkout_reservation_a',
        'cc120000-0000-4000-8000-000000000002',
        300
    );
    if v_busy->>'code' <> 'busy' then
        raise exception 'concurrent Checkout token was not reported busy';
    end if;

    -- Simulate a process crash after Stripe create but before persistence.
    update public.billing_checkout_reservations
       set lease_expires_at=clock_timestamp()-interval '1 second'
     where organization_id='cc100000-0000-4000-8000-000000000001';
    v_takeover := public.reserve_billing_checkout_generation(
        'cc100000-0000-4000-8000-000000000001',
        'cus_checkout_reservation_a',
        'cc120000-0000-4000-8000-000000000002',
        300
    );
    if v_takeover->>'code' <> 'acquired'
       or (v_takeover->>'generation')::bigint <> v_generation then
        raise exception 'lease takeover changed the Checkout generation';
    end if;

    v_result := public.persist_billing_checkout_session(
        'cc100000-0000-4000-8000-000000000001',v_generation,
        'cc110000-0000-4000-8000-000000000001','cs_stale_owner'
    );
    if v_result->>'code' <> 'stale' then
        raise exception 'stale Checkout token persisted a session';
    end if;
    if public.release_billing_checkout_generation(
        'cc100000-0000-4000-8000-000000000001',v_generation,
        'cc110000-0000-4000-8000-000000000001',false
    ) then
        raise exception 'stale Checkout token released a generation';
    end if;

    v_result := public.persist_billing_checkout_session(
        'cc100000-0000-4000-8000-000000000001',v_generation,
        'cc120000-0000-4000-8000-000000000002','cs_recovered_after_crash'
    );
    if v_result->>'code' <> 'persisted'
       or not exists (
            select 1 from public.billing_accounts
             where organization_id='cc100000-0000-4000-8000-000000000001'
               and checkout_session_id='cs_recovered_after_crash'
       ) then
        raise exception 'crash-recovered Checkout session was not atomically persisted';
    end if;

    update public.billing_checkout_reservations
       set lease_expires_at=clock_timestamp()-interval '1 second'
     where organization_id='cc100000-0000-4000-8000-000000000001';
    v_takeover := public.reserve_billing_checkout_generation(
        'cc100000-0000-4000-8000-000000000001',
        'cus_checkout_reservation_a',
        'cc130000-0000-4000-8000-000000000003',
        300
    );
    if v_takeover->>'mode' <> 'inspect_persisted'
       or (v_takeover->>'generation')::bigint <> v_generation then
        raise exception 'persisted Checkout generation was not safely reclaimed';
    end if;
    v_result := public.persist_billing_checkout_session(
        'cc100000-0000-4000-8000-000000000001',v_generation,
        'cc120000-0000-4000-8000-000000000002','cs_different_after_expiry'
    );
    if v_result->>'code' <> 'stale' then
        raise exception 'expired lease authorized a different Checkout session';
    end if;
    v_result := public.persist_billing_checkout_session(
        'cc100000-0000-4000-8000-000000000001',v_generation,
        'cc130000-0000-4000-8000-000000000003','cs_different_current_owner'
    );
    if v_result->>'code' <> 'session_conflict' then
        raise exception 'persisted Checkout generation identity was overwritten';
    end if;
end;
$checkout_takeover$;

do $checkout_advance$
declare
    v_result jsonb;
    v_generation bigint;
begin
    v_result := public.reserve_billing_checkout_generation(
        'cc200000-0000-4000-8000-000000000002',
        'cus_checkout_reservation_b',
        'cc210000-0000-4000-8000-000000000001',
        300
    );
    v_generation := (v_result->>'generation')::bigint;
    v_result := public.persist_billing_checkout_session(
        'cc200000-0000-4000-8000-000000000002',v_generation,
        'cc210000-0000-4000-8000-000000000001','cs_terminal_exact'
    );
    if v_result->>'code' <> 'persisted' then
        raise exception 'advance fixture could not persist its Checkout session';
    end if;
    if not public.release_billing_checkout_generation(
        'cc200000-0000-4000-8000-000000000002',v_generation,
        'cc210000-0000-4000-8000-000000000001',false
    ) then
        raise exception 'current Checkout token could not release persisted generation';
    end if;
    v_result := public.reserve_billing_checkout_generation(
        'cc200000-0000-4000-8000-000000000002',
        'cus_checkout_reservation_b',
        'cc220000-0000-4000-8000-000000000002',
        300
    );
    v_result := public.advance_billing_checkout_generation(
        'cc200000-0000-4000-8000-000000000002',v_generation,
        'cc220000-0000-4000-8000-000000000002','cs_terminal_exact',300
    );
    if v_result->>'code' <> 'advanced'
       or (v_result->>'generation')::bigint <> v_generation+1
       or not exists (
            select 1 from public.billing_checkout_reservations
             where organization_id='cc200000-0000-4000-8000-000000000002'
               and generation=v_generation+1
               and state='reserved'
               and checkout_session_id is null
       ) then
        raise exception 'current Checkout token did not advance the exact persisted session';
    end if;

    update public.billing_accounts
       set subscription_status='past_due'
     where organization_id='cc200000-0000-4000-8000-000000000002';
    v_result := public.persist_billing_checkout_session(
        'cc200000-0000-4000-8000-000000000002',v_generation+1,
        'cc220000-0000-4000-8000-000000000002','cs_must_be_blocked'
    );
    if v_result->>'code' <> 'occupied' then
        raise exception 'occupying subscription did not block Checkout persistence';
    end if;
end;
$checkout_advance$;

do $checkout_old_generation$
declare
    v_first jsonb;
    v_takeover jsonb;
begin
    v_first := public.reserve_billing_checkout_generation(
        'cc300000-0000-4000-8000-000000000003',
        'cus_checkout_reservation_c',
        'cc310000-0000-4000-8000-000000000001',
        300
    );
    update public.billing_checkout_reservations
       set generation_started_at=clock_timestamp()-interval '24 hours',
           lease_expires_at=clock_timestamp()-interval '1 second'
     where organization_id='cc300000-0000-4000-8000-000000000003';
    v_takeover := public.reserve_billing_checkout_generation(
        'cc300000-0000-4000-8000-000000000003',
        'cus_checkout_reservation_c',
        'cc320000-0000-4000-8000-000000000002',
        300
    );
    if v_takeover->>'mode' <> 'recover_only'
       or v_takeover->>'generation' <> v_first->>'generation' then
        raise exception 'expired generation was allowed to create again';
    end if;
end;
$checkout_old_generation$;

do $checkout_permissions$
begin
    if not (select relrowsecurity from pg_class
             where oid='public.billing_checkout_reservations'::regclass)
       or has_function_privilege(
            'anon',
            'public.reserve_billing_checkout_generation(uuid,text,uuid,integer)',
            'EXECUTE')
       or has_function_privilege(
            'authenticated',
            'public.persist_billing_checkout_session(uuid,bigint,uuid,text)',
            'EXECUTE')
       or not has_function_privilege(
            'service_role',
            'public.advance_billing_checkout_generation(uuid,bigint,uuid,text,integer)',
            'EXECUTE') then
        raise exception 'Checkout reservation privilege boundary is invalid';
    end if;
end;
$checkout_permissions$;

rollback;
