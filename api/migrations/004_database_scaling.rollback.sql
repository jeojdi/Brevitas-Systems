-- Rollback for 004_database_scaling.sql. Run only after deploying application code
-- that no longer calls these RPCs. CONCURRENTLY cannot run inside a transaction.

drop function if exists public.usage_page(text,uuid,text,timestamptz,bigint,integer);
drop function if exists public.usage_stats(text,uuid,text);
drop function if exists public.usage_breakdown(text,uuid,text,integer);
drop function if exists public.usage_grouped(text,uuid,text,text,text,timestamptz,timestamptz,integer);
drop function if exists public.admin_usage_report(jsonb,integer);
drop function if exists public.admin_key_repository_usage(integer);
drop function if exists public.admin_usage_report_page(jsonb,text,text,numeric,text,integer);

drop index concurrently if exists public.usage_log_org_page_idx;
drop index concurrently if exists public.usage_log_owner_page_idx;
drop index concurrently if exists public.usage_log_key_page_idx;
drop index concurrently if exists public.usage_log_org_customer_page_idx;
drop index concurrently if exists public.usage_log_org_pipeline_idx;
drop index concurrently if exists public.usage_log_org_agent_idx;
drop index concurrently if exists public.usage_log_org_run_idx;
drop index concurrently if exists public.usage_log_admin_project_idx;
drop index concurrently if exists public.usage_log_admin_client_idx;
drop index concurrently if exists public.usage_log_admin_provider_idx;
drop index concurrently if exists public.usage_log_admin_model_idx;
