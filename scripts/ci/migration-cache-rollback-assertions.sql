\set ON_ERROR_STOP on

do $$
begin
    if to_regclass('public.semantic_cache') is null then
        raise exception 'cache rollback removed the encrypted cache table';
    end if;
    if to_regprocedure(
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)'
    ) is not null or to_regprocedure(
        'public.semantic_cache_lookup(vector,text,double precision,text,text)'
    ) is not null then raise exception 'cache rollback left application RPCs installed'; end if;
    if exists (select 1 from public.semantic_cache where response_json is not null) then
        raise exception 'cache rollback restored plaintext content';
    end if;
end;
$$;
