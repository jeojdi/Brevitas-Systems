-- Zero-downtime pre-stage for a large usage_log.
-- Run each statement outside a transaction before recording migration 004.
-- This companion is idempotent, but it is not a substitute for 004: the main
-- migration records the schema version and installs the RPC/permission contracts.

create index concurrently if not exists usage_log_org_page_idx
    on public.usage_log (organization_id, ts desc, id desc);
create index concurrently if not exists usage_log_owner_page_idx
    on public.usage_log (owner_id, ts desc, id desc);
create index concurrently if not exists usage_log_key_page_idx
    on public.usage_log (key_hash, ts desc, id desc);
create index concurrently if not exists usage_log_org_customer_page_idx
    on public.usage_log (organization_id, customer_id, ts desc, id desc);
create index concurrently if not exists usage_log_org_pipeline_idx
    on public.usage_log (organization_id, pipeline, ts desc, id desc);
create index concurrently if not exists usage_log_org_agent_idx
    on public.usage_log (organization_id, agent, ts desc, id desc);
create index concurrently if not exists usage_log_org_run_idx
    on public.usage_log (organization_id, run_id, ts desc, id desc);
create index concurrently if not exists usage_log_admin_project_idx
    on public.usage_log (project, ts desc, id desc);
create index concurrently if not exists usage_log_admin_client_idx
    on public.usage_log (client, ts desc, id desc);
create index concurrently if not exists usage_log_admin_provider_idx
    on public.usage_log (provider, ts desc, id desc);
create index concurrently if not exists usage_log_admin_model_idx
    on public.usage_log (model, ts desc, id desc);
