\set ON_ERROR_STOP on

begin;

delete from public.shared_endpoint_rate_limits
 where endpoint_scope like 'billing_control.%';

do $$
declare
    v_result jsonb;
    v_index integer;
    v_actor uuid := 'bc100000-0000-4000-8000-000000000001';
    v_company uuid := 'bc200000-0000-4000-8000-000000000001';
begin
    for v_index in 1..5 loop
        v_result := public.consume_billing_control_attempt(
            v_actor, v_company, 'checkout');
        if v_result->>'code' <> 'accepted' then
            raise exception 'checkout identity limit denied successful attempt % too early: %',
                v_index, v_result;
        end if;
    end loop;
    if (select request_count
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'billing_control.checkout.actor_company') <> 5 then
        raise exception 'successful checkout admissions were not counted exactly';
    end if;
    v_result := public.consume_billing_control_attempt(
        v_actor, v_company, 'checkout');
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 300 then
        raise exception 'checkout identity limit did not close: %', v_result;
    end if;

    -- Company and operation are distinct verified identity partitions.
    v_result := public.consume_billing_control_attempt(
        v_actor, 'bc200000-0000-4000-8000-000000000002', 'checkout');
    if v_result->>'code' <> 'accepted' then
        raise exception 'billing control limiter crossed company identities: %', v_result;
    end if;
    v_result := public.consume_billing_control_attempt(
        v_actor, v_company, 'portal');
    if v_result->>'code' <> 'accepted' then
        raise exception 'billing control limiter crossed operation identities: %', v_result;
    end if;
    if (select request_count
          from public.shared_endpoint_rate_limits
         where endpoint_scope = 'billing_control.portal.actor_company') <> 1 then
        raise exception 'successful portal admission was not counted';
    end if;

    begin
        perform public.consume_billing_control_attempt(v_actor, v_company, 'status');
        raise exception 'unsupported billing control operation was admitted';
    exception
        when invalid_parameter_value then null;
    end;

    delete from public.shared_endpoint_rate_limits
     where endpoint_scope like 'billing_control.%';
    for v_index in 1..120 loop
        v_result := public.consume_billing_control_attempt(
            pg_catalog.gen_random_uuid(),
            pg_catalog.gen_random_uuid(),
            case when v_index % 2 = 0 then 'checkout' else 'portal' end
        );
        if v_result->>'code' <> 'accepted' then
            raise exception 'global billing control limit denied attempt % too early: %',
                v_index, v_result;
        end if;
    end loop;
    v_result := public.consume_billing_control_attempt(
        pg_catalog.gen_random_uuid(), pg_catalog.gen_random_uuid(), 'portal');
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 60 then
        raise exception 'global billing control limit did not close: %', v_result;
    end if;

    if exists (
        select 1 from public.shared_endpoint_rate_limits
         where endpoint_scope like 'billing_control.%'
           and (identity_hash !~ '^[0-9a-f]{64}$' or identity_hash like '%-%')
    ) then
        raise exception 'billing control limiter persisted a raw identity';
    end if;
    if pg_catalog.has_function_privilege(
        'anon',
        'public.consume_billing_control_attempt(uuid,uuid,text)',
        'execute'
    ) or pg_catalog.has_function_privilege(
        'authenticated',
        'public.consume_billing_control_attempt(uuid,uuid,text)',
        'execute'
    ) then
        raise exception 'billing control limiter is callable by an end-user role';
    end if;
end;
$$;

rollback;
