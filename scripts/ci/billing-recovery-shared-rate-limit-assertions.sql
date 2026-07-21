\set ON_ERROR_STOP on

begin;

delete from public.shared_endpoint_rate_limits
 where endpoint_scope like 'billing_recovery.%';

do $$
declare
    v_result jsonb;
    v_index integer;
    v_actor uuid := 'b1000000-0000-4000-8000-000000000001';
    v_company uuid := 'b2000000-0000-4000-8000-000000000001';
begin
    for v_index in 1..5 loop
        v_result := public.consume_billing_recovery_attempt(v_actor, v_company);
        if v_result->>'code' <> 'accepted' then
            raise exception 'actor-company recovery limit denied attempt % too early: %',
                v_index, v_result;
        end if;
    end loop;
    v_result := public.consume_billing_recovery_attempt(v_actor, v_company);
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 900 then
        raise exception 'actor-company recovery limit did not close: %', v_result;
    end if;

    -- A different company is a different bounded identity, even for the same
    -- human actor. This request still consumes the shared global ceiling.
    v_result := public.consume_billing_recovery_attempt(
        v_actor,
        'b2000000-0000-4000-8000-000000000002'
    );
    if v_result->>'code' <> 'accepted' then
        raise exception 'billing recovery limiter crossed company identities: %',
            v_result;
    end if;

    delete from public.shared_endpoint_rate_limits
     where endpoint_scope like 'billing_recovery.%';
    for v_index in 1..60 loop
        v_result := public.consume_billing_recovery_attempt(
            pg_catalog.gen_random_uuid(),
            pg_catalog.gen_random_uuid()
        );
        if v_result->>'code' <> 'accepted' then
            raise exception 'global recovery limit denied attempt % too early: %',
                v_index, v_result;
        end if;
    end loop;
    v_result := public.consume_billing_recovery_attempt(
        pg_catalog.gen_random_uuid(),
        pg_catalog.gen_random_uuid()
    );
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 60 then
        raise exception 'global recovery limit did not close: %', v_result;
    end if;

    if exists (
        select 1 from public.shared_endpoint_rate_limits
         where endpoint_scope like 'billing_recovery.%'
           and (identity_hash !~ '^[0-9a-f]{64}$' or identity_hash like '%-%')
    ) then
        raise exception 'billing recovery limiter persisted a raw identity';
    end if;
end;
$$;

rollback;
