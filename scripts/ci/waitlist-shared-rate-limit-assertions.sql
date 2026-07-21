\set ON_ERROR_STOP on

begin;

delete from public.shared_endpoint_rate_limits;
delete from public.waitlist
 where email like 'shared-limit-%@example.invalid';

do $$
declare
    v_result jsonb;
    v_index integer;
begin
    -- Case and surrounding whitespace must share one normalized identity.
    for v_index in 1..3 loop
        v_result := public.submit_waitlist_signup(
            case v_index
              when 1 then ' Shared-Limit-Identity@Example.Invalid '
              when 2 then 'shared-limit-identity@example.invalid'
              else 'SHARED-LIMIT-IDENTITY@EXAMPLE.INVALID'
            end,
            null,null,null,null,null,null,null,false
        );
        if v_result->>'code' <> 'accepted' then
            raise exception 'normalized waitlist identity was denied too early: %', v_result;
        end if;
    end loop;
    v_result := public.submit_waitlist_signup(
        'shared-limit-identity@example.invalid',
        null,null,null,null,null,null,null,false
    );
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 600 then
        raise exception 'normalized waitlist identity limit did not close: %', v_result;
    end if;
    if (select count(*) from public.waitlist
         where email='shared-limit-identity@example.invalid') <> 1 then
        raise exception 'duplicate waitlist identity changed persistence cardinality';
    end if;

    delete from public.shared_endpoint_rate_limits;
    for v_index in 1..120 loop
        v_result := public.submit_waitlist_signup(
            pg_catalog.format('shared-limit-global-%s@example.invalid', v_index),
            null,null,null,null,null,null,null,false
        );
        if v_result->>'code' <> 'accepted' then
            raise exception 'global waitlist limit denied request % too early: %',
                v_index,v_result;
        end if;
    end loop;
    v_result := public.submit_waitlist_signup(
        'shared-limit-global-overflow@example.invalid',
        null,null,null,null,null,null,null,false
    );
    if v_result->>'code' <> 'rate_limited'
       or (v_result->>'retry_after_seconds')::integer not between 1 and 60 then
        raise exception 'global waitlist limit did not close: %', v_result;
    end if;
    if exists (
        select 1 from public.waitlist
         where email='shared-limit-global-overflow@example.invalid'
    ) then
        raise exception 'globally denied waitlist request was persisted';
    end if;
    if exists (
        select 1 from public.shared_endpoint_rate_limits
         where identity_hash !~ '^[0-9a-f]{64}$'
            or identity_hash like '%@%'
    ) then
        raise exception 'waitlist limiter persisted a raw identity';
    end if;
end;
$$;

rollback;
