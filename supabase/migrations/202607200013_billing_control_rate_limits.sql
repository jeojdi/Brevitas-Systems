-- Shared, verified-identity admission for Stripe Checkout and Customer Portal.

begin;

create or replace function public.consume_billing_control_attempt(
    p_actor_user_id uuid,
    p_organization_id uuid,
    p_operation text
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions, pg_temp
as $function$
declare
    v_operation text := pg_catalog.lower(pg_catalog.btrim(p_operation));
    v_now timestamptz;
    v_global_window interval := interval '1 minute';
    v_global_limit integer := 120;
    v_identity_window interval;
    v_identity_limit integer;
    v_global_hash text := pg_catalog.repeat('0', 64);
    v_identity_hash text;
    v_identity_scope text;
    v_expires_at timestamptz;
    v_count integer;
    v_retry_after integer;
begin
    if p_actor_user_id is null or p_organization_id is null or p_operation is null
       or v_operation not in ('checkout', 'portal') then
        raise invalid_parameter_value using
            message = 'invalid billing control admission identity or operation';
    end if;

    if v_operation = 'checkout' then
        v_identity_window := interval '5 minutes';
        v_identity_limit := 5;
    else
        v_identity_window := interval '1 minute';
        v_identity_limit := 30;
    end if;
    v_identity_scope := 'billing_control.' || v_operation || '.actor_company';
    v_identity_hash := pg_catalog.encode(
        digest(
            p_actor_user_id::text || ':' ||
            p_organization_id::text || ':' || v_operation,
            'sha256'
        ),
        'hex'
    );

    -- Every application instance shares these locks and counters. The global
    -- namespace is always locked first to prevent lock-order inversion.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('brevitas.billing-control.global.v1', 0));
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'brevitas.billing-control.identity.v1:' || v_identity_hash,
            0
        ));
    v_now := pg_catalog.clock_timestamp();

    delete from public.shared_endpoint_rate_limits
     where endpoint_scope like 'billing_control.%'
       and expires_at <= v_now;

    -- Global attempts include identity-denied requests so distributed abuse
    -- cannot evade the shared database ceiling with many verified identities.
    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        'billing_control.global',
        v_global_hash,
        v_now,
        v_now + v_global_window,
        1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < v_global_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select expires_at into v_expires_at
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'billing_control.global'
           and identity_hash = v_global_hash;
        v_retry_after := greatest(
            1,
            pg_catalog.ceil(
                extract(epoch from (v_expires_at - v_now))
            )::integer
        );
        return pg_catalog.jsonb_build_object(
            'ok', false,
            'code', 'rate_limited',
            'retry_after_seconds', least(v_retry_after, 60)
        );
    end if;

    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        v_identity_scope,
        v_identity_hash,
        v_now,
        v_now + v_identity_window,
        1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < v_identity_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select expires_at into v_expires_at
          from public.shared_endpoint_rate_limits
         where endpoint_scope = v_identity_scope
           and identity_hash = v_identity_hash;
        v_retry_after := greatest(
            1,
            pg_catalog.ceil(
                extract(epoch from (v_expires_at - v_now))
            )::integer
        );
        return pg_catalog.jsonb_build_object(
            'ok', false,
            'code', 'rate_limited',
            'retry_after_seconds', least(v_retry_after, 300)
        );
    end if;

    return pg_catalog.jsonb_build_object('ok', true, 'code', 'accepted');
end;
$function$;

revoke all on function public.consume_billing_control_attempt(uuid, uuid, text)
    from public, anon, authenticated, service_role;
grant execute on function public.consume_billing_control_attempt(uuid, uuid, text)
    to service_role;

comment on function public.consume_billing_control_attempt(uuid, uuid, text) is
    'Atomic server-only Checkout/Portal admission using global and verified actor-company-operation limits.';

commit;
