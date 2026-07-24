\set ON_ERROR_STOP on

do $$
begin
    if to_regprocedure('public.semantic_cache_lookup(vector,text,double precision)') is not null then
        raise exception 'cache reapply restored the retired plaintext lookup';
    end if;
    if to_regprocedure(
        'public.semantic_cache_lookup(vector,text,double precision,text,text)'
    ) is null or to_regprocedure(
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)'
    ) is null then raise exception 'cache reapply did not restore encrypted bounded RPCs'; end if;
    if exists (
        select 1 from public.semantic_cache
         where response_json is not null
            or response_ciphertext = ''
            or expires_at <= created_at
            or expires_at > created_at + interval '24 hours'
    ) then raise exception 'cache rollback/reapply weakened content or TTL constraints'; end if;
    if has_table_privilege('service_role', 'public.semantic_cache', 'INSERT')
       or has_table_privilege('service_role', 'public.semantic_cache', 'UPDATE') then
        raise exception 'cache reapply restored direct service-role writes';
    end if;
end;
$$;
