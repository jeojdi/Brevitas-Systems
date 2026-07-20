-- Align the deployable Supabase chain with canonical API receipt accounting.
-- Safe to apply after migration 011 and safe to reapply. Historical usage and
-- billing evidence are never rewritten or deleted.

do $$
begin
    if to_regclass('public.usage_log') is null
       or to_regprocedure('public.queue_brevitas_fee()') is null
       or not exists (
            select 1
              from pg_trigger trigger_state
             where trigger_state.tgrelid='public.usage_log'::regclass
               and trigger_state.tgname='queue_brevitas_fee_after_usage'
               and trigger_state.tgfoid=to_regprocedure('public.queue_brevitas_fee()')
               and trigger_state.tgenabled<>'D'
               and not trigger_state.tgisinternal
       ) or not (select relation.relrowsecurity from pg_class relation
                  where relation.oid='public.usage_log'::regclass)
       or not coalesce((select procedure.prosecdef from pg_proc procedure
                         where procedure.oid=to_regprocedure(
                             'public.queue_brevitas_fee()')),false) then
        raise exception '202607170012 requires the canonical usage and billing trigger chain';
    end if;
end;
$$;

alter table public.usage_log
    add column if not exists cache_write_5m_tokens bigint not null default 0;
alter table public.usage_log
    add column if not exists cache_write_1h_tokens bigint not null default 0;
alter table public.usage_log
    add column if not exists cache_attributable boolean not null default false;

comment on column public.usage_log.cache_write_5m_tokens is
    'Anthropic 5-minute cache-write tokens; subset of cache_write_tokens.';
comment on column public.usage_log.cache_write_1h_tokens is
    'Anthropic 1-hour cache-write tokens; subset of cache_write_tokens.';
comment on column public.usage_log.cache_attributable is
    'True only when Brevitas, rather than the client or provider, caused the cache discount.';

-- Providers without tier detail legitimately report both tier columns as zero.
-- When tier detail is present, its partition must equal the persisted total.
do $$
begin
    alter table public.usage_log
        add constraint usage_log_receipt_cache_tiers_check check (
            cache_write_tokens >= 0
            and cache_write_5m_tokens >= 0
            and cache_write_1h_tokens >= 0
            and (
                (cache_write_5m_tokens=0 and cache_write_1h_tokens=0)
                or cache_write_5m_tokens+cache_write_1h_tokens=cache_write_tokens
            )
        ) not valid;
exception when duplicate_object then null;
end;
$$;
alter table public.usage_log
    validate constraint usage_log_receipt_cache_tiers_check;

-- Fail the migration if any application-persisted receipt or billing field has
-- drifted out of the authoritative Supabase schema.
do $$
declare
    required_column text;
begin
    if (select count(*) from information_schema.columns column_state
         where column_state.table_schema='public'
           and column_state.table_name='usage_log'
           and column_state.is_nullable='NO'
           and (
                (column_state.column_name in (
                    'cache_write_5m_tokens','cache_write_1h_tokens'
                ) and column_state.data_type='bigint')
                or (column_state.column_name='cache_attributable'
                    and column_state.data_type='boolean')
           ))<>3 then
        raise exception 'receipt accounting columns have unsafe types or nullability';
    end if;
    foreach required_column in array array[
        'organization_id','customer_id','authoritative',
        'fresh_input_tokens','cached_input_tokens','cache_write_tokens',
        'cache_write_5m_tokens','cache_write_1h_tokens','cache_attributable',
        'output_tokens','baseline_tokens','optimized_tokens','tokens_saved',
        'baseline_cost_usd','actual_cost_usd','measured_savings_usd',
        'verified_savings_usd','brevitas_fee_usd','quality_status',
        'pricing_status','pricing_version','receipt_source','request_id'
    ] loop
        if not exists (
            select 1 from information_schema.columns column_state
             where column_state.table_schema='public'
               and column_state.table_name='usage_log'
               and column_state.column_name=required_column
        ) then
            raise exception 'usage receipt column is missing: %', required_column;
        end if;
    end loop;
end;
$$;

-- Usage receipts are append-only through ordinary service credentials. The
-- database-owner compliance procedures retain their separately audited,
-- evidence-preserving deletion boundary.
revoke all on table public.usage_log from public, anon, authenticated, service_role;
grant select, insert on table public.usage_log to service_role;
revoke all on sequence public.usage_log_id_seq
    from public, anon, authenticated, service_role;
grant usage, select on sequence public.usage_log_id_seq to service_role;

-- Evidence-preserving rollback procedure:
-- 1. Roll application writers back before changing the validation layer.
-- 2. DROP CONSTRAINT IF EXISTS usage_log_receipt_cache_tiers_check.
-- 3. Keep all three columns, every usage row, the fee trigger, and hardened
--    privileges. Dropping populated columns or recreating billing evidence is
--    not an approved rollback.
