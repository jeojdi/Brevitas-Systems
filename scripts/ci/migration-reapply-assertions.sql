\set ON_ERROR_STOP on

do $$
begin
    if to_regprocedure('public.usage_page(text,uuid,text,timestamptz,bigint,integer)') is null then
        raise exception 'usage_page was not restored by reapply';
    end if;
    if not exists (
        select 1 from pg_class relation
        join pg_index index_state on index_state.indexrelid = relation.oid
        where relation.relname = 'usage_log_org_page_idx'
          and index_state.indisvalid and index_state.indisready
    ) then raise exception 'database-scaling index was not restored valid and ready'; end if;
end;
$$;
