\set ON_ERROR_STOP on

do $$
declare
    checked_role text;
    checked_privilege text;
begin
    if not exists (
        select 1 from pg_constraint constraint_state
         where constraint_state.conrelid='public.usage_log'::regclass
           and constraint_state.conname='usage_log_receipt_cache_tiers_check'
           and constraint_state.convalidated
    ) then raise exception 'receipt cache-tier constraint is missing or invalid'; end if;
    if not has_table_privilege('service_role','public.usage_log','SELECT')
       or not has_table_privilege('service_role','public.usage_log','INSERT')
       or not has_sequence_privilege(
            'service_role','public.usage_log_id_seq','USAGE') then
        raise exception 'service_role cannot append/read usage receipts';
    end if;
    foreach checked_privilege in array array[
        'UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
    ] loop
        if has_table_privilege(
            'service_role','public.usage_log',checked_privilege
        ) then raise exception 'unsafe service usage privilege: %',checked_privilege; end if;
    end loop;
    foreach checked_role in array array['anon','authenticated'] loop
        foreach checked_privilege in array array[
            'SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'
        ] loop
            if has_table_privilege(checked_role,'public.usage_log',checked_privilege) then
                raise exception 'unsafe usage receipt privilege: % %',
                    checked_role,checked_privilege;
            end if;
        end loop;
    end loop;
    if exists (
        select 1 from aclexplode(coalesce(
            (select relation.relacl from pg_class relation
              where relation.oid='public.usage_log'::regclass),
            acldefault('r',(select relation.relowner from pg_class relation
                            where relation.oid='public.usage_log'::regclass))
        )) privilege
         where privilege.grantee=0
    ) then raise exception 'PUBLIC has a direct usage receipt privilege'; end if;
    if not exists (
        select 1 from pg_trigger trigger_state
         where trigger_state.tgrelid='public.usage_log'::regclass
           and trigger_state.tgname='queue_brevitas_fee_after_usage'
           and trigger_state.tgenabled<>'D' and not trigger_state.tgisinternal
    ) then raise exception 'usage billing trigger is absent or disabled'; end if;
end;
$$;

insert into public.usage_log(
    key_hash,owner_id,organization_id,ts,request_id,project,
    baseline_tokens,optimized_tokens,tokens_saved,
    fresh_input_tokens,cached_input_tokens,cache_write_tokens,
    cache_write_5m_tokens,cache_write_1h_tokens,cache_attributable,
    output_tokens,baseline_cost_usd,actual_cost_usd,measured_savings_usd,
    verified_savings_usd,brevitas_fee_usd,authoritative,pricing_status,
    pricing_version,receipt_source
) values (
    'release-key-a','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    '10000000-0000-4000-8000-000000000001',now(),
    'release-receipt-accounting','receipt-alignment',200,120,80,
    50,20,40,10,30,true,10,0.002,0.0012,0.0008,
    0.0008,0.0002,true,'priced','release-v1','proxy'
) on conflict(key_hash,request_id) where request_id<>'' do nothing;

do $$
begin
    if not exists (
        select 1 from public.usage_log usage
         where usage.request_id='release-receipt-accounting'
           and usage.cache_write_tokens=40
           and usage.cache_write_5m_tokens=10
           and usage.cache_write_1h_tokens=30
           and usage.cache_attributable
           and usage.receipt_source='proxy'
           and usage.authoritative
    ) or (select count(*) from public.billing_ledger ledger
           join public.usage_log usage on usage.id=ledger.usage_log_id
          where usage.request_id='release-receipt-accounting')<>1 then
        raise exception 'receipt fields were not persisted with one billing trigger result';
    end if;
    begin
        insert into public.usage_log(
            key_hash,request_id,cache_write_tokens,
            cache_write_5m_tokens,cache_write_1h_tokens
        ) values ('release-key-a','release-invalid-cache-tiers',10,8,8);
        raise exception 'inconsistent cache tiers unexpectedly persisted';
    exception when check_violation then null;
    end;
    begin
        insert into public.usage_log(
            key_hash,request_id,cache_write_tokens,
            cache_write_5m_tokens,cache_write_1h_tokens
        ) values ('release-key-a','release-invalid-zero-total-tier',0,1,0);
        raise exception 'positive cache tier with zero total unexpectedly persisted';
    exception when check_violation then null;
    end;
    begin
        insert into public.usage_log(
            key_hash,request_id,cache_write_tokens,
            cache_write_5m_tokens,cache_write_1h_tokens
        ) values ('release-key-a','release-invalid-negative-total',-1,0,0);
        raise exception 'negative cache-write total unexpectedly persisted';
    exception when check_violation then null;
    end;
    begin
        insert into public.usage_log(
            key_hash,request_id,cache_write_tokens,
            cache_write_5m_tokens,cache_write_1h_tokens
        ) values ('release-key-a','release-invalid-negative-5m',0,-1,0);
        raise exception 'negative 5-minute cache tier unexpectedly persisted';
    exception when check_violation then null;
    end;
    begin
        insert into public.usage_log(
            key_hash,request_id,cache_write_tokens,
            cache_write_5m_tokens,cache_write_1h_tokens
        ) values ('release-key-a','release-invalid-negative-1h',0,0,-1);
        raise exception 'negative 1-hour cache tier unexpectedly persisted';
    exception when check_violation then null;
    end;
    if exists (
        select 1 from public.usage_log
         where request_id in (
            'release-invalid-cache-tiers','release-invalid-zero-total-tier',
            'release-invalid-negative-total','release-invalid-negative-5m',
            'release-invalid-negative-1h'
         )
    ) then raise exception 'invalid cache-tier row survived its failed insert'; end if;
end;
$$;
