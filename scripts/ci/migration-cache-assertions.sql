\set ON_ERROR_STOP on

do $$
declare
    relation_oid oid := 'public.semantic_cache'::regclass;
    future_created timestamptz;
    future_expires timestamptz;
    past_created timestamptz;
    past_expires timestamptz;
    v_vector vector(384) := ('[' || repeat('0,', 383) || '1]')::vector;
begin
    if exists (select 1 from public.semantic_cache where exact_hash = repeat('1', 64)) then
        raise exception 'legacy plaintext cache row survived the canonical migration';
    end if;
    if to_regprocedure('public.semantic_cache_lookup(vector,text,double precision)') is not null then
        raise exception 'retired plaintext lookup signature survived the upgrade';
    end if;
    if to_regprocedure(
        'public.semantic_cache_lookup(vector,text,double precision,text,text)'
    ) is null then raise exception 'encrypted tenant-scoped lookup signature is missing'; end if;
    if to_regprocedure(
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)'
    ) is null then raise exception 'bounded cache store signature is missing'; end if;

    if not exists (select 1 from pg_extension where extname = 'vector') then
        raise exception 'pgvector extension is missing';
    end if;
    if (select format_type(attribute.atttypid, attribute.atttypmod)
          from pg_attribute attribute
         where attribute.attrelid = relation_oid and attribute.attname = 'embedding')
       <> 'vector(384)' then raise exception 'semantic cache embedding type drifted'; end if;
    if not (select relation.relrowsecurity from pg_class relation where relation.oid = relation_oid) then
        raise exception 'semantic cache RLS is disabled';
    end if;
    if has_table_privilege('anon', relation_oid, 'SELECT')
       or has_table_privilege('anon', relation_oid, 'INSERT')
       or has_table_privilege('anon', relation_oid, 'UPDATE')
       or has_table_privilege('anon', relation_oid, 'DELETE')
       or has_table_privilege('authenticated', relation_oid, 'SELECT')
       or has_table_privilege('authenticated', relation_oid, 'INSERT')
       or has_table_privilege('authenticated', relation_oid, 'UPDATE')
       or has_table_privilege('authenticated', relation_oid, 'DELETE') then
        raise exception 'browser roles can access encrypted cache rows';
    end if;
    if has_table_privilege('service_role', relation_oid, 'INSERT')
       or has_table_privilege('service_role', relation_oid, 'UPDATE')
       or not has_table_privilege('service_role', relation_oid, 'SELECT')
       or not has_table_privilege('service_role', relation_oid, 'DELETE') then
        raise exception 'service_role direct cache grants violate the bounded-write contract';
    end if;

    if not has_function_privilege(
        'service_role',
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)',
        'EXECUTE'
    ) or not has_function_privilege(
        'service_role',
        'public.semantic_cache_lookup(vector,text,double precision,text,text)',
        'EXECUTE'
    ) then raise exception 'service_role cache RPC grants are missing'; end if;
    if has_function_privilege(
        'anon',
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)',
        'EXECUTE'
    ) or has_function_privilege(
        'authenticated',
        'public.semantic_cache_lookup(vector,text,double precision,text,text)',
        'EXECUTE'
    ) then raise exception 'browser roles can execute cache RPCs'; end if;

    if not exists (
        select 1 from pg_proc procedure
        join pg_namespace namespace on namespace.oid = procedure.pronamespace
        where namespace.nspname = 'public'
          and procedure.proname = 'semantic_cache_store_bounded'
          and 'search_path=pg_catalog, public, extensions' = any(procedure.proconfig)
    ) or not exists (
        select 1 from pg_proc procedure
        join pg_namespace namespace on namespace.oid = procedure.pronamespace
        where namespace.nspname = 'public'
          and procedure.proname = 'normalize_semantic_cache_write'
          and 'search_path=pg_catalog, public' = any(procedure.proconfig)
    ) then raise exception 'cache security-definer search_path is not fixed'; end if;

    delete from public.semantic_cache
     where tenant_namespace in (repeat('a', 64), repeat('b', 64), repeat('f', 64));

    insert into public.semantic_cache(
        exact_hash, context_hash, model_id, embedding, response_json,
        response_ciphertext, tenant_namespace, created_at, expires_at
    ) values (
        repeat('f', 64), repeat('e', 64), 'future:model', null,
        'ciphertext-future', repeat('f', 64),
        clock_timestamp() + interval '10 days', clock_timestamp() + interval '20 days'
    );
    select created_at, expires_at into future_created, future_expires
      from public.semantic_cache where exact_hash = repeat('f', 64);
    if future_created < clock_timestamp() - interval '10 seconds'
       or future_created > clock_timestamp() + interval '1 second'
       or future_expires <= future_created
       or future_expires > future_created + interval '24 hours' then
        raise exception 'future direct-owner timestamps were not normalized to the DB clock';
    end if;

    insert into public.semantic_cache(
        exact_hash, context_hash, model_id, embedding, response_json,
        response_ciphertext, tenant_namespace, created_at, expires_at
    ) values (
        repeat('d', 64), repeat('c', 64), 'past:model', '{"plaintext":true}'::jsonb,
        'ciphertext-past', repeat('f', 64),
        clock_timestamp() - interval '90 days', clock_timestamp() + interval '90 days'
    );
    select created_at, expires_at into past_created, past_expires
      from public.semantic_cache where exact_hash = repeat('d', 64);
    if past_created < clock_timestamp() - interval '10 seconds'
       or past_created > clock_timestamp() + interval '1 second'
       or past_expires <= past_created
       or past_expires > past_created + interval '24 hours'
       or (select response_json from public.semantic_cache where exact_hash = repeat('d', 64)) is not null then
        raise exception 'backdated/plaintext direct-owner write was not normalized';
    end if;

    begin
        insert into public.semantic_cache(
            exact_hash, context_hash, model_id, response_json,
            response_ciphertext, tenant_namespace, expires_at
        ) values (
            repeat('9', 64), repeat('8', 64), 'invalid:model', null,
            '', repeat('7', 64), clock_timestamp() + interval '1 hour'
        );
        raise exception 'empty ciphertext unexpectedly passed the cache constraint';
    exception when check_violation then null;
    end;

    perform public.semantic_cache_store_bounded(
        repeat('a', 64), repeat('b', 64), 'tenant:model', v_vector,
        'ciphertext-tenant-a', repeat('a', 64), 5, 3, 3600, 100
    );
    if (select count(*) from public.semantic_cache_lookup(
        v_vector, repeat('b', 64), 0.99, repeat('a', 64), 'tenant:model'
    )) <> 1 then raise exception 'tenant/model cache lookup missed its own ciphertext'; end if;
    if (select count(*) from public.semantic_cache_lookup(
        v_vector, repeat('b', 64), 0.99, repeat('b', 64), 'tenant:model'
    )) <> 0 or (select count(*) from public.semantic_cache_lookup(
        v_vector, repeat('b', 64), 0.99, repeat('a', 64), 'other:model'
    )) <> 0 then raise exception 'cache lookup crossed tenant or model scope'; end if;
end;
$$;
