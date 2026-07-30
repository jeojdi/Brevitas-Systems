-- 202607280025: the waitlist NETWORK budget.
--
-- Opens its own transaction and ends in ROLLBACK, so it is rerunnable,
-- order-independent, and leaves no limiter rows behind.
\set ON_ERROR_STOP on

begin;

delete from public.shared_endpoint_rate_limits;

do $$
declare
    v_result json;
    v_index integer;
    v_network text := pg_catalog.repeat('a1', 32);
    v_other text := pg_catalog.repeat('b2', 32);
    v_rows bigint;
begin
    -- Shape of the cross-lane contract: exactly {"allowed", "retry_after_seconds"}.
    v_result := public.consume_waitlist_network_budget(v_network, 3, 600);
    if (select count(*) from json_object_keys(v_result)) <> 2
       or v_result->>'allowed' is null
       or v_result->>'retry_after_seconds' is null then
        raise exception 'network budget result is not the declared contract: %', v_result;
    end if;
    if (v_result->>'allowed')::boolean is not true
       or (v_result->>'retry_after_seconds')::integer <> 0 then
        raise exception 'first network request was not admitted cleanly: %', v_result;
    end if;
    if pg_get_function_result(
           to_regprocedure('public.consume_waitlist_network_budget(text,integer,integer)')
       ) <> 'json' then
        raise exception 'network budget must return json per the cross-lane contract';
    end if;

    -- The window closes at the limit and reports a bounded retry hint.
    for v_index in 2..3 loop
        v_result := public.consume_waitlist_network_budget(v_network, 3, 600);
        if (v_result->>'allowed')::boolean is not true then
            raise exception 'network budget denied request % too early: %', v_index, v_result;
        end if;
    end loop;
    v_result := public.consume_waitlist_network_budget(v_network, 3, 600);
    if (v_result->>'allowed')::boolean is not false
       or (v_result->>'retry_after_seconds')::integer not between 1 and 600 then
        raise exception 'network budget did not close: %', v_result;
    end if;

    -- A DIFFERENT network is unaffected. This is the whole point of the
    -- migration: one network's flood must not deny everyone else.
    v_result := public.consume_waitlist_network_budget(v_other, 3, 600);
    if (v_result->>'allowed')::boolean is not true then
        raise exception 'an unrelated network inherited another network''s denial: %', v_result;
    end if;

    -- Nothing but salted hashes is retained, and only under this scope.
    if exists (
        select 1 from public.shared_endpoint_rate_limits
         where endpoint_scope = 'waitlist.network'
           and (identity_hash !~ '^[0-9a-f]{64}$'
                or identity_hash like '%.%'
                or identity_hash like '%:%')
    ) then
        raise exception 'network limiter persisted something that is not a salted hash';
    end if;

    -- The column constraint is what makes the privacy claim structural, not a
    -- convention: a raw address cannot be stored even by a direct insert.
    begin
        insert into public.shared_endpoint_rate_limits (
            endpoint_scope, identity_hash, window_started_at, expires_at, request_count
        ) values (
            'waitlist.network', '203.0.113.0/24',
            pg_catalog.clock_timestamp(),
            pg_catalog.clock_timestamp() + interval '1 minute', 1
        );
        raise exception 'the shared limiter accepted a raw network address';
    exception
        when check_violation then null;
    end;

    -- Malformed budgets fail closed and loudly rather than widening the window.
    begin
        perform public.consume_waitlist_network_budget('203.0.113.7', 3, 600);
        raise exception 'network budget accepted a raw address as its key';
    exception
        when invalid_parameter_value then null;
    end;
    begin
        perform public.consume_waitlist_network_budget(v_other, 0, 600);
        raise exception 'network budget accepted a zero limit';
    exception
        when invalid_parameter_value then null;
    end;
    begin
        perform public.consume_waitlist_network_budget(v_other, 3, 0);
        raise exception 'network budget accepted a zero window';
    exception
        when invalid_parameter_value then null;
    end;
    begin
        perform public.consume_waitlist_network_budget(v_other, 3, null);
        raise exception 'network budget accepted a null window';
    exception
        when invalid_parameter_value then null;
    end;

    -- An expired window reopens rather than staying permanently denied.
    update public.shared_endpoint_rate_limits
       set window_started_at = pg_catalog.clock_timestamp() - interval '2 hours',
           expires_at = pg_catalog.clock_timestamp() - interval '1 hour'
     where endpoint_scope = 'waitlist.network'
       and identity_hash = v_network;
    v_result := public.consume_waitlist_network_budget(v_network, 3, 600);
    if (v_result->>'allowed')::boolean is not true then
        raise exception 'an expired network window never reopened: %', v_result;
    end if;

    -- Retention: expired rows for OTHER networks are swept by ordinary traffic,
    -- so the per-network key space cannot grow without bound.
    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    )
    select 'waitlist.network',
           pg_catalog.encode(digest('sweep-' || generated::text, 'sha256'), 'hex'),
           pg_catalog.clock_timestamp() - interval '2 hours',
           pg_catalog.clock_timestamp() - interval '1 hour',
           1
      from generate_series(1, 40) generated
    on conflict do nothing;
    perform public.consume_waitlist_network_budget(v_network, 100, 600);
    select count(*) into v_rows
      from public.shared_endpoint_rate_limits
     where endpoint_scope = 'waitlist.network'
       and expires_at <= pg_catalog.clock_timestamp();
    if v_rows <> 0 then
        raise exception 'the inline retention sweep left % expired network rows', v_rows;
    end if;

    -- The janitor is bounded and removes ONLY expired rows, across every scope.
    insert into public.shared_endpoint_rate_limits (
        endpoint_scope, identity_hash, window_started_at, expires_at, request_count
    ) values
      ('waitlist.global', pg_catalog.repeat('0', 64),
       pg_catalog.clock_timestamp() - interval '2 hours',
       pg_catalog.clock_timestamp() - interval '1 hour', 5),
      ('billing_recovery.global', pg_catalog.repeat('1', 64),
       pg_catalog.clock_timestamp() - interval '2 hours',
       pg_catalog.clock_timestamp() - interval '1 hour', 5)
    on conflict (endpoint_scope, identity_hash) do update
       set expires_at = excluded.expires_at;
    if public.purge_expired_shared_endpoint_rate_limits(1) <> 1 then
        raise exception 'the limiter janitor ignored its row bound';
    end if;
    perform public.purge_expired_shared_endpoint_rate_limits(5000);
    select count(*) into v_rows
      from public.shared_endpoint_rate_limits
     where expires_at <= pg_catalog.clock_timestamp();
    if v_rows <> 0 then
        raise exception 'the limiter janitor left % expired rows', v_rows;
    end if;
    select count(*) into v_rows
      from public.shared_endpoint_rate_limits
     where expires_at > pg_catalog.clock_timestamp();
    if v_rows = 0 then
        raise exception 'the limiter janitor deleted a live budget';
    end if;
end;
$$;

-- Server-only: neither browser role may execute either function, and
-- service_role must be able to execute both.
do $$
declare
    v_grantee text;
begin
    foreach v_grantee in array array['anon', 'authenticated', 'public'] loop
        if has_function_privilege(
               v_grantee,
               'public.consume_waitlist_network_budget(text,integer,integer)',
               'EXECUTE')
           or has_function_privilege(
               v_grantee,
               'public.purge_expired_shared_endpoint_rate_limits(integer)',
               'EXECUTE') then
            raise exception 'browser role % can execute a server-only limiter function', v_grantee;
        end if;
    end loop;
    if not has_function_privilege(
           'service_role',
           'public.consume_waitlist_network_budget(text,integer,integer)',
           'EXECUTE')
       or not has_function_privilege(
           'service_role',
           'public.purge_expired_shared_endpoint_rate_limits(integer)',
           'EXECUTE') then
        raise exception 'service_role lost execute on the waitlist network limiter';
    end if;
    if (select prosecdef from pg_proc
         where oid = 'public.consume_waitlist_network_budget(text,integer,integer)'::regprocedure)
       is not true then
        raise exception 'the waitlist network limiter is not security definer';
    end if;
end;
$$;

rollback;
