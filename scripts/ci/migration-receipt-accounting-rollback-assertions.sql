\set ON_ERROR_STOP on

do $$
declare required_column text;
begin
    if exists (
        select 1 from pg_constraint constraint_state
         where constraint_state.conrelid='public.usage_log'::regclass
           and constraint_state.conname='usage_log_receipt_cache_tiers_check'
    ) then raise exception 'receipt validation constraint survived rollback'; end if;
    foreach required_column in array array[
        'cache_write_5m_tokens','cache_write_1h_tokens','cache_attributable'
    ] loop
        if not exists (
            select 1 from information_schema.columns column_state
             where column_state.table_schema='public'
               and column_state.table_name='usage_log'
               and column_state.column_name=required_column
        ) then raise exception 'receipt rollback removed persisted column: %',required_column; end if;
    end loop;
    if not exists (
        select 1 from public.usage_log usage
         where usage.request_id='release-receipt-accounting'
           and usage.cache_write_5m_tokens=10
           and usage.cache_write_1h_tokens=30
           and usage.cache_attributable
    ) or (select count(*) from public.billing_ledger ledger
           join public.usage_log usage on usage.id=ledger.usage_log_id
          where usage.request_id='release-receipt-accounting')<>1 then
        raise exception 'receipt rollback changed usage or billing evidence';
    end if;
    if not exists (
        select 1 from pg_trigger trigger_state
         where trigger_state.tgrelid='public.usage_log'::regclass
           and trigger_state.tgname='queue_brevitas_fee_after_usage'
           and trigger_state.tgenabled<>'D'
    ) then raise exception 'receipt rollback removed the billing trigger'; end if;
end;
$$;
