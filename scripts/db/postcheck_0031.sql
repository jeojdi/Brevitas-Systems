-- Postconditions for 202607280031 on wyfz. Read-only.
select 'config_columns_added' as metric,
       (select count(*)::text from information_schema.columns
         where table_schema='public' and table_name='billing_halting_conditions'
           and column_name in ('cache_anchor_materiality_floor_usd','cache_anchor_lookback_days',
                               'max_cache_savings_per_spend_ratio',
                               'max_unanchored_zero_spend_savings_usd',
                               'max_total_savings_per_spend_ratio')) as value,
       '5 expected' as expectation
union all
select 'guard4_default', (select max_unanchored_zero_spend_savings_usd::text
                            from public.billing_halting_conditions where singleton), '1.0000 expected'
union all
select 'guard5_default', (select max_total_savings_per_spend_ratio::text
                            from public.billing_halting_conditions where singleton), '20000.000 expected'
union all
select 'usage_log_anchor_column',
       (exists (select 1 from information_schema.columns where table_schema='public'
                 and table_name='usage_log' and column_name='savings_anchor_request_id'))::text,
       'true expected'
union all
select 'semantic_cache_origin_column',
       (exists (select 1 from information_schema.columns where table_schema='public'
                 and table_name='semantic_cache' and column_name='origin_request_id'))::text,
       'true expected -- without it every replay is unanchored and bills 0'
union all
select 'evidence_fn_widened',
       (to_regprocedure('public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)')
        is not null)::text, 'true expected (4-arg form)'
union all
select 'evidence_anon_executable',
       has_function_privilege('anon',
         'public.billing_period_settlement_evidence(uuid,timestamptz,timestamptz,bigint)','execute')::text,
       'false expected -- 202607280032 contract'
union all
select 'live_settlements_disturbed',
       (select count(*)::text from public.period_settlement_ledger where superseded_at is null),
       '0 expected -- nothing existed before the apply';
