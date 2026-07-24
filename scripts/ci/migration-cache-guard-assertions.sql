\set ON_ERROR_STOP on

do $$
begin
    if to_regclass('public.semantic_cache') is null then
        raise exception 'compatibility guard removed the cache table';
    end if;
    if exists (select 1 from public.semantic_cache where exact_hash=repeat('1',64)) then
        raise exception 'compatibility guard retained legacy plaintext';
    end if;
    if to_regprocedure('public.semantic_cache_lookup(vector,text,double precision)') is not null then
        raise exception 'compatibility guard retained plaintext lookup signature';
    end if;
    if to_regprocedure(
        'public.semantic_cache_lookup(vector,text,double precision,text,text)'
    ) is not null or to_regprocedure(
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)'
    ) is not null then raise exception 'guard unexpectedly installed canonical cache RPCs'; end if;
    if has_table_privilege('service_role','public.semantic_cache','INSERT')
       or has_table_privilege('service_role','public.semantic_cache','UPDATE') then
        raise exception 'compatibility guard retained direct service-role writes';
    end if;
    if not exists (
        select 1 from pg_constraint
         where conrelid='public.semantic_cache'::regclass
           and conname='semantic_cache_no_plaintext'
    ) then raise exception 'compatibility guard lacks plaintext rejection'; end if;
end;
$$;
