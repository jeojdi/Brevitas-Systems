-- Fence Stripe Checkout creation by company and stable idempotency generation.
--
-- A reservation generation never changes merely because an application lease
-- expires. That lets a replacement invocation retry Stripe with exactly the
-- same idempotency key after a crash. Generations older than the conservative
-- 23-hour Stripe replay window are recovery-only and cannot authorize another
-- create without operator review.

begin;

create table if not exists public.billing_checkout_reservations (
    organization_id uuid primary key
        references public.billing_accounts(organization_id) on delete cascade,
    stripe_customer_id text not null,
    generation bigint not null default 1 check (generation > 0),
    state text not null default 'reserved'
        check (state in ('reserved', 'persisted', 'manual_review')),
    reservation_token uuid,
    lease_expires_at timestamptz,
    generation_started_at timestamptz not null default now(),
    checkout_session_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint billing_checkout_reservation_lease_pair check (
        (reservation_token is null) = (lease_expires_at is null)
    ),
    constraint billing_checkout_reservation_state_identity check (
        (state = 'reserved' and checkout_session_id is null)
        or (state = 'persisted' and checkout_session_id is not null)
        or state = 'manual_review'
    )
);

create unique index if not exists billing_checkout_reservations_session_uidx
    on public.billing_checkout_reservations(checkout_session_id)
    where checkout_session_id is not null;
create index if not exists billing_checkout_reservations_lease_idx
    on public.billing_checkout_reservations(state, lease_expires_at);

alter table public.billing_checkout_reservations enable row level security;
revoke all on table public.billing_checkout_reservations
    from public, anon, authenticated;

-- Preserve a Checkout identity written by the pre-reservation route. Its
-- exact ID may be inspected once even though legacy metadata has no generation.
insert into public.billing_checkout_reservations (
    organization_id,
    stripe_customer_id,
    generation,
    state,
    generation_started_at,
    checkout_session_id,
    created_at,
    updated_at
)
select account.organization_id,
       account.stripe_customer_id,
       1,
       'persisted',
       coalesce(account.updated_at, account.created_at, now()),
       account.checkout_session_id,
       coalesce(account.created_at, now()),
       now()
  from public.billing_accounts account
 where account.stripe_customer_id is not null
   and account.checkout_session_id is not null
on conflict (organization_id) do nothing;

create or replace function public.reserve_billing_checkout_generation(
    p_organization_id uuid,
    p_stripe_customer_id text,
    p_reservation_token uuid,
    p_lease_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_account public.billing_accounts%rowtype;
    v_reservation public.billing_checkout_reservations%rowtype;
    v_mode text;
    v_retry_after integer;
begin
    if p_organization_id is null
       or nullif(pg_catalog.btrim(p_stripe_customer_id), '') is null
       or pg_catalog.length(p_stripe_customer_id) > 255
       or p_reservation_token is null
       or p_lease_seconds is null
       or p_lease_seconds not between 30 and 300 then
        raise invalid_parameter_value using
            message = 'invalid billing Checkout reservation parameters';
    end if;

    -- Every Checkout RPC locks the account first, then the reservation. This
    -- serializes even the first insert and shares a stable lock order with
    -- webhook writes to canonical billing state.
    select * into v_account
      from public.billing_accounts
     where organization_id = p_organization_id
     for update;

    if not found
       or v_account.stripe_customer_id is distinct from p_stripe_customer_id then
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'identity_mismatch');
    end if;
    if v_account.subscription_status in (
        'active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'
    ) then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'occupied');
    end if;

    select * into v_reservation
      from public.billing_checkout_reservations
     where organization_id = p_organization_id
     for update;

    if not found then
        if v_account.checkout_session_id is not null then
            insert into public.billing_checkout_reservations (
                organization_id, stripe_customer_id, generation, state,
                generation_started_at, checkout_session_id, updated_at
            ) values (
                p_organization_id, p_stripe_customer_id, 1, 'persisted',
                coalesce(v_account.updated_at, v_account.created_at, v_now),
                v_account.checkout_session_id, v_now
            ) returning * into v_reservation;
        else
            insert into public.billing_checkout_reservations (
                organization_id, stripe_customer_id, generation, state,
                generation_started_at, updated_at
            ) values (
                p_organization_id, p_stripe_customer_id, 1, 'reserved',
                v_now, v_now
            ) returning * into v_reservation;
        end if;
    end if;

    if v_reservation.stripe_customer_id is distinct from p_stripe_customer_id then
        update public.billing_checkout_reservations
           set state = 'manual_review',
               reservation_token = null,
               lease_expires_at = null,
               updated_at = v_now
         where organization_id = p_organization_id;
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'manual_review');
    end if;

    if v_reservation.state = 'manual_review' then
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'manual_review');
    end if;

    if v_reservation.lease_expires_at is not null
       and v_reservation.lease_expires_at > v_now
       and v_reservation.reservation_token is distinct from p_reservation_token then
        v_retry_after := greatest(
            1,
            least(
                p_lease_seconds,
                pg_catalog.ceil(extract(
                    epoch from (v_reservation.lease_expires_at - v_now)
                ))::integer
            )
        );
        return pg_catalog.jsonb_build_object(
            'ok', false,
            'code', 'busy',
            'retry_after_seconds', v_retry_after
        );
    end if;

    if v_reservation.state = 'persisted' then
        v_mode := 'inspect_persisted';
    elsif v_reservation.generation_started_at + interval '23 hours' > v_now then
        v_mode := 'create_or_recover';
    else
        v_mode := 'recover_only';
    end if;

    update public.billing_checkout_reservations
       set reservation_token = p_reservation_token,
           lease_expires_at = v_now
               + pg_catalog.make_interval(secs => p_lease_seconds),
           updated_at = v_now
     where organization_id = p_organization_id;

    return pg_catalog.jsonb_build_object(
        'ok', true,
        'code', 'acquired',
        'mode', v_mode,
        'generation', v_reservation.generation,
        'checkout_session_id', v_reservation.checkout_session_id
    );
end;
$function$;

create or replace function public.persist_billing_checkout_session(
    p_organization_id uuid,
    p_generation bigint,
    p_reservation_token uuid,
    p_checkout_session_id text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_account public.billing_accounts%rowtype;
    v_reservation public.billing_checkout_reservations%rowtype;
begin
    if p_organization_id is null
       or p_generation is null or p_generation <= 0
       or p_reservation_token is null
       or nullif(pg_catalog.btrim(p_checkout_session_id), '') is null
       or pg_catalog.length(p_checkout_session_id) > 255 then
        raise invalid_parameter_value using
            message = 'invalid billing Checkout persistence parameters';
    end if;

    select * into v_account
      from public.billing_accounts
     where organization_id = p_organization_id
     for update;
    if not found then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'stale');
    end if;

    select * into v_reservation
      from public.billing_checkout_reservations
     where organization_id = p_organization_id
     for update;

    if not found
       or v_reservation.generation <> p_generation
       or v_reservation.reservation_token is distinct from p_reservation_token
       or v_reservation.lease_expires_at is null
       or v_reservation.lease_expires_at <= v_now then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'stale');
    end if;
    if v_reservation.state = 'manual_review' then
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'manual_review');
    end if;
    if v_account.subscription_status in (
        'active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'
    ) then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'occupied');
    end if;
    if v_account.stripe_customer_id is distinct from
       v_reservation.stripe_customer_id then
        update public.billing_checkout_reservations
           set state = 'manual_review',
               reservation_token = null,
               lease_expires_at = null,
               updated_at = v_now
         where organization_id = p_organization_id;
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'manual_review');
    end if;

    if (v_reservation.checkout_session_id is not null
        and v_reservation.checkout_session_id <> p_checkout_session_id)
       or (v_account.checkout_session_id is not null
           and v_account.checkout_session_id <> p_checkout_session_id) then
        update public.billing_checkout_reservations
           set state = 'manual_review',
               reservation_token = null,
               lease_expires_at = null,
               updated_at = v_now
         where organization_id = p_organization_id;
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'session_conflict');
    end if;

    update public.billing_checkout_reservations
       set state = 'persisted',
           checkout_session_id = p_checkout_session_id,
           updated_at = v_now
     where organization_id = p_organization_id;
    update public.billing_accounts
       set checkout_session_id = p_checkout_session_id,
           updated_at = v_now
     where organization_id = p_organization_id;

    return pg_catalog.jsonb_build_object(
        'ok', true,
        'code', 'persisted',
        'generation', p_generation,
        'checkout_session_id', p_checkout_session_id
    );
end;
$function$;

create or replace function public.advance_billing_checkout_generation(
    p_organization_id uuid,
    p_generation bigint,
    p_reservation_token uuid,
    p_expected_checkout_session_id text,
    p_lease_seconds integer
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_account public.billing_accounts%rowtype;
    v_reservation public.billing_checkout_reservations%rowtype;
    v_next_generation bigint;
begin
    if p_organization_id is null
       or p_generation is null or p_generation <= 0
       or p_generation = 9223372036854775807
       or p_reservation_token is null
       or nullif(pg_catalog.btrim(p_expected_checkout_session_id), '') is null
       or pg_catalog.length(p_expected_checkout_session_id) > 255
       or p_lease_seconds is null
       or p_lease_seconds not between 30 and 300 then
        raise invalid_parameter_value using
            message = 'invalid billing Checkout advance parameters';
    end if;

    select * into v_account
      from public.billing_accounts
     where organization_id = p_organization_id
     for update;
    if not found then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'stale');
    end if;

    select * into v_reservation
      from public.billing_checkout_reservations
     where organization_id = p_organization_id
     for update;

    if not found
       or v_reservation.generation <> p_generation
       or v_reservation.reservation_token is distinct from p_reservation_token
       or v_reservation.lease_expires_at is null
       or v_reservation.lease_expires_at <= v_now then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'stale');
    end if;
    if v_reservation.state <> 'persisted'
       or v_reservation.checkout_session_id is distinct from
          p_expected_checkout_session_id
       or v_account.checkout_session_id is distinct from
          p_expected_checkout_session_id then
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'session_conflict');
    end if;
    if v_account.subscription_status in (
        'active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'
    ) then
        return pg_catalog.jsonb_build_object('ok', false, 'code', 'occupied');
    end if;

    v_next_generation := p_generation + 1;
    update public.billing_checkout_reservations
       set generation = v_next_generation,
           state = 'reserved',
           reservation_token = p_reservation_token,
           lease_expires_at = v_now
               + pg_catalog.make_interval(secs => p_lease_seconds),
           generation_started_at = v_now,
           checkout_session_id = null,
           updated_at = v_now
     where organization_id = p_organization_id;
    update public.billing_accounts
       set checkout_session_id = null,
           updated_at = v_now
     where organization_id = p_organization_id;

    return pg_catalog.jsonb_build_object(
        'ok', true,
        'code', 'advanced',
        'mode', 'create_or_recover',
        'generation', v_next_generation
    );
end;
$function$;

create or replace function public.release_billing_checkout_generation(
    p_organization_id uuid,
    p_generation bigint,
    p_reservation_token uuid,
    p_manual_review boolean
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public, pg_temp
as $function$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_released_count integer;
begin
    if p_organization_id is null
       or p_generation is null or p_generation <= 0
       or p_reservation_token is null
       or p_manual_review is null then
        raise invalid_parameter_value using
            message = 'invalid billing Checkout release parameters';
    end if;

    update public.billing_checkout_reservations
       set state = case
               when p_manual_review then 'manual_review'
               else state
           end,
           reservation_token = null,
           lease_expires_at = null,
           updated_at = v_now
     where organization_id = p_organization_id
       and generation = p_generation
       and reservation_token = p_reservation_token
       and lease_expires_at > v_now;
    get diagnostics v_released_count = row_count;
    return v_released_count = 1;
end;
$function$;

revoke all on function public.reserve_billing_checkout_generation(
    uuid, text, uuid, integer
) from public, anon, authenticated, service_role;
revoke all on function public.persist_billing_checkout_session(
    uuid, bigint, uuid, text
) from public, anon, authenticated, service_role;
revoke all on function public.advance_billing_checkout_generation(
    uuid, bigint, uuid, text, integer
) from public, anon, authenticated, service_role;
revoke all on function public.release_billing_checkout_generation(
    uuid, bigint, uuid, boolean
) from public, anon, authenticated, service_role;

grant execute on function public.reserve_billing_checkout_generation(
    uuid, text, uuid, integer
) to service_role;
grant execute on function public.persist_billing_checkout_session(
    uuid, bigint, uuid, text
) to service_role;
grant execute on function public.advance_billing_checkout_generation(
    uuid, bigint, uuid, text, integer
) to service_role;
grant execute on function public.release_billing_checkout_generation(
    uuid, bigint, uuid, boolean
) to service_role;

comment on table public.billing_checkout_reservations is
    'Company-scoped, lease-fenced Stripe Checkout idempotency generations.';
comment on function public.reserve_billing_checkout_generation(
    uuid, text, uuid, integer
) is 'Claims one company Checkout generation without changing it on lease takeover.';
comment on function public.persist_billing_checkout_session(
    uuid, bigint, uuid, text
) is 'Atomically persists the fenced Checkout session into reservation and billing account state.';
comment on function public.advance_billing_checkout_generation(
    uuid, bigint, uuid, text, integer
) is 'Advances only a live-token persisted generation whose exact terminal session was inspected.';
comment on function public.release_billing_checkout_generation(
    uuid, bigint, uuid, boolean
) is 'Releases only the current live Checkout lease and optionally fences it for manual review.';

commit;
