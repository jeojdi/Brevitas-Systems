-- Legacy billing_events compatibility. New writes use usage_log, created by the
-- canonical 20260710 migration. Keep these additive columns so older deployments
-- and historical rows remain readable without maintaining a second write path.

alter table public.billing_events
    add column if not exists pipeline text not null default '',
    add column if not exists agent text not null default '',
    add column if not exists run_id text not null default '';

create index if not exists billing_events_pipeline_idx
    on public.billing_events(user_id, pipeline, ts desc)
    where pipeline <> '';

create index if not exists billing_events_agent_idx
    on public.billing_events(user_id, pipeline, agent, ts desc)
    where agent <> '';

create index if not exists billing_events_run_idx
    on public.billing_events(user_id, run_id, ts desc)
    where run_id <> '';
