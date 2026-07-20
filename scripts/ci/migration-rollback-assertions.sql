\set ON_ERROR_STOP on

do $$
begin
    if to_regprocedure('public.usage_page(text,uuid,text,timestamptz,bigint,integer)') is not null then
        raise exception 'usage_page survived database-scaling rollback';
    end if;
    if to_regclass('public.usage_log_org_page_idx') is not null then
        raise exception 'usage_log_org_page_idx survived database-scaling rollback';
    end if;
end;
$$;
