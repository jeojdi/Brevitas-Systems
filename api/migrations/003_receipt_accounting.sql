-- Receipt-aligned accounting additions.
-- Safe to run repeatedly on existing Supabase projects.

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
