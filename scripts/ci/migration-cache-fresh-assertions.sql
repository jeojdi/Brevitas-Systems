\set ON_ERROR_STOP on

do $$
begin
    if to_regclass('public.semantic_cache') is null then
        raise exception 'fresh canonical cache table is missing';
    end if;
    if (select attribute.attnotnull from pg_attribute attribute
         where attribute.attrelid='public.semantic_cache'::regclass
           and attribute.attname='response_json') then
        raise exception 'fresh cache requires plaintext response_json';
    end if;
    if not (select attribute.attnotnull from pg_attribute attribute
         where attribute.attrelid='public.semantic_cache'::regclass
           and attribute.attname='response_ciphertext') then
        raise exception 'fresh cache ciphertext is nullable';
    end if;
    if (select attribute.atthasdef from pg_attribute attribute
         where attribute.attrelid='public.semantic_cache'::regclass
           and attribute.attname='response_ciphertext') then
        raise exception 'fresh cache ciphertext has an unsafe empty default';
    end if;
    if not (select relation.relrowsecurity from pg_class relation
             where relation.oid='public.semantic_cache'::regclass) then
        raise exception 'fresh cache RLS is disabled';
    end if;
    if has_table_privilege('service_role','public.semantic_cache','INSERT')
       or has_table_privilege('service_role','public.semantic_cache','UPDATE') then
        raise exception 'fresh cache grants direct service-role writes';
    end if;
    if to_regprocedure(
        'public.semantic_cache_store_bounded(text,text,text,vector,text,text,integer,integer,integer,integer)'
    ) is null or to_regprocedure(
        'public.semantic_cache_lookup(vector,text,double precision,text,text)'
    ) is null then raise exception 'fresh encrypted cache RPCs are missing'; end if;
end;
$$;
