-- Shared, atomic abuse control for waitlist and manual billing recovery.
-- This forward migration is the final release-integrated 20260720 suffix.

begin;

create table if not exists public.shared_endpoint_rate_limits (
    endpoint_scope varchar(64) not null,
    identity_hash varchar(64) not null,
    window_started_at timestamptz not null,
    expires_at timestamptz not null,
    request_count integer not null,
    primary key (endpoint_scope, identity_hash),
    constraint shared_endpoint_rate_limits_scope_check
        check (endpoint_scope ~ '^[a-z][a-z0-9_.:-]{0,63}$'),
    constraint shared_endpoint_rate_limits_identity_check
        check (identity_hash ~ '^[0-9a-f]{64}$'),
    constraint shared_endpoint_rate_limits_count_check
        check (request_count between 1 and 1000000),
    constraint shared_endpoint_rate_limits_window_check
        check (expires_at > window_started_at)
);

create index if not exists shared_endpoint_rate_limits_expiry_idx
    on public.shared_endpoint_rate_limits (expires_at);

alter table public.shared_endpoint_rate_limits enable row level security;
revoke all on table public.shared_endpoint_rate_limits
    from public, anon, authenticated, service_role;

-- PostgreSQL cannot replace a function's return type in place. Remove the
-- boolean form from migration 200002 before installing the result contract.
drop function if exists public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
);

create function public.submit_waitlist_signup(
    p_email text,
    p_name text default null,
    p_company text default null,
    p_role text default null,
    p_pipeline_shape text default null,
    p_monthly_spend text default null,
    p_orchestrator text default null,
    p_notes text default null,
    p_design_partner boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions, pg_temp
as $function$
declare
    v_email text := pg_catalog.lower(pg_catalog.btrim(p_email));
    v_now timestamptz;
    v_global_window interval := interval '1 minute';
    v_global_limit integer := 120;
    v_identity_window interval := interval '10 minutes';
    v_identity_limit integer := 3;
    v_global_hash text := pg_catalog.repeat('0', 64);
    v_identity_hash text;
    v_expires_at timestamptz;
    v_count integer;
    v_retry_after integer;
    v_created boolean := false;
begin
    -- Repeat the server validation at the authoritative mutation boundary.
    if v_email is null
       or pg_catalog.char_length(v_email) not between 3 and 254
       or v_email !~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$' then
        raise check_violation using message = 'invalid waitlist submission';
    end if;

    -- pgcrypto is installed by the existing company-administration chain.
    -- The limiter stores no raw address; normalization makes case/space
    -- variants consume the same shared identity window.
    v_identity_hash := pg_catalog.encode(digest(v_email, 'sha256'), 'hex');

    -- Every Vercel instance reaches this transaction. The fixed global lock
    -- serializes global admission; the identity lock makes the intended
    -- namespace explicit and remains safe if more endpoint scopes are added.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('brevitas.waitlist.global.v1', 0));
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'brevitas.waitlist.identity.v1:' || v_identity_hash, 0));
    v_now := pg_catalog.clock_timestamp();

    -- Expired fixed-window rows carry no evidence and are removed under the
    -- global namespace lock so randomized identities cannot grow this table
    -- without also being bounded by the global admission window.
    delete from public.shared_endpoint_rate_limits
     where endpoint_scope in ('waitlist.global','waitlist.identity')
       and expires_at <= v_now;

    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        'waitlist.global', v_global_hash, v_now, v_now + v_global_window, 1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < v_global_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select expires_at into v_expires_at
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'waitlist.global'
           and identity_hash = v_global_hash;
        v_retry_after := greatest(
            1, pg_catalog.ceil(extract(epoch from (v_expires_at - v_now)))::integer);
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'rate_limited',
            'retry_after_seconds', least(v_retry_after, 60));
    end if;

    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        'waitlist.identity', v_identity_hash, v_now, v_now + v_identity_window, 1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < v_identity_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select expires_at into v_expires_at
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'waitlist.identity'
           and identity_hash = v_identity_hash;
        v_retry_after := greatest(
            1, pg_catalog.ceil(extract(epoch from (v_expires_at - v_now)))::integer);
        return pg_catalog.jsonb_build_object(
            'ok', false, 'code', 'rate_limited',
            'retry_after_seconds', least(v_retry_after, 600));
    end if;

    insert into public.waitlist (
        email, name, company, role, pipeline_shape, monthly_spend,
        orchestrator, notes, design_partner
    ) values (
        v_email,
        nullif(pg_catalog.btrim(p_name), ''),
        nullif(pg_catalog.btrim(p_company), ''),
        nullif(pg_catalog.btrim(p_role), ''),
        nullif(pg_catalog.btrim(p_pipeline_shape), ''),
        nullif(pg_catalog.btrim(p_monthly_spend), ''),
        nullif(pg_catalog.btrim(p_orchestrator), ''),
        nullif(pg_catalog.btrim(p_notes), ''),
        coalesce(p_design_partner, false)
    )
    on conflict (email) do nothing
    returning true into v_created;

    return pg_catalog.jsonb_build_object(
        'ok', true, 'code', 'accepted', 'created', coalesce(v_created, false));
end;
$function$;

revoke all on function public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
) from public, anon, authenticated, service_role;
grant execute on function public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
) to service_role;

create or replace function public.consume_billing_recovery_attempt(
    p_actor_user_id uuid,
    p_organization_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, public, extensions, pg_temp
as $function$
declare
    v_now timestamptz;
    v_global_window interval := interval '1 minute';
    v_global_limit integer := 60;
    v_actor_company_window interval := interval '15 minutes';
    v_actor_company_limit integer := 5;
    v_global_hash text := pg_catalog.repeat('0', 64);
    v_actor_company_hash text;
    v_expires_at timestamptz;
    v_count integer;
    v_retry_after integer;
begin
    if p_actor_user_id is null or p_organization_id is null then
        raise invalid_parameter_value using
            message = 'invalid billing recovery admission identity';
    end if;

    -- The shared limiter retains neither a user id nor a company id. The route
    -- supplies only identities already authenticated and authorized by the
    -- canonical billing-company resolver.
    v_actor_company_hash := pg_catalog.encode(
        digest(
            p_actor_user_id::text || ':' || p_organization_id::text,
            'sha256'
        ),
        'hex'
    );

    -- All instances use the same database locks and counters. Global is always
    -- acquired first, preventing lock-order inversions under concurrent abuse.
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('brevitas.billing-recovery.global.v1', 0));
    perform pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'brevitas.billing-recovery.actor-company.v1:' ||
            v_actor_company_hash,
            0
        ));
    v_now := pg_catalog.clock_timestamp();

    delete from public.shared_endpoint_rate_limits
     where endpoint_scope in (
            'billing_recovery.global',
            'billing_recovery.actor_company'
       )
       and expires_at <= v_now;

    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values (
        'billing_recovery.global',
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
         where endpoint_scope = 'billing_recovery.global'
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
        'billing_recovery.actor_company',
        v_actor_company_hash,
        v_now,
        v_now + v_actor_company_window,
        1
    )
    on conflict (endpoint_scope, identity_hash) do update
       set request_count = public.shared_endpoint_rate_limits.request_count + 1
     where public.shared_endpoint_rate_limits.expires_at > v_now
       and public.shared_endpoint_rate_limits.request_count < v_actor_company_limit
    returning request_count, expires_at into v_count, v_expires_at;

    if not found then
        select expires_at into v_expires_at
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'billing_recovery.actor_company'
           and identity_hash = v_actor_company_hash;
        v_retry_after := greatest(
            1,
            pg_catalog.ceil(
                extract(epoch from (v_expires_at - v_now))
            )::integer
        );
        return pg_catalog.jsonb_build_object(
            'ok', false,
            'code', 'rate_limited',
            'retry_after_seconds', least(v_retry_after, 900)
        );
    end if;

    return pg_catalog.jsonb_build_object('ok', true, 'code', 'accepted');
end;
$function$;

revoke all on function public.consume_billing_recovery_attempt(uuid, uuid)
    from public, anon, authenticated, service_role;
grant execute on function public.consume_billing_recovery_attempt(uuid, uuid)
    to service_role;

comment on table public.shared_endpoint_rate_limits is
    'Content-free fixed-window counters for server-only shared endpoint admission.';
comment on function public.submit_waitlist_signup(
    text, text, text, text, text, text, text, text, boolean
) is
    'Atomic server-only waitlist admission: shared global/email-hash limits plus idempotent insert.';
comment on function public.consume_billing_recovery_attempt(uuid, uuid) is
    'Atomic server-only billing recovery admission: shared global and actor-company limits.';

commit;
