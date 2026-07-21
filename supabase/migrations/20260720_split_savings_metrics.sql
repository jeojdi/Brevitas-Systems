-- Keep unlike optimization mechanisms out of one ambiguous "token savings" number.
-- Historical rows default to zero/unknown rather than being reclassified without evidence.

alter table public.usage_log
    add column if not exists provider_input_tokens_avoided bigint not null default 0;
alter table public.usage_log
    add column if not exists native_cache_discount_usd numeric(18,10);
alter table public.usage_log
    add column if not exists calls_avoided bigint not null default 0;
alter table public.usage_log
    add column if not exists transport_bytes_avoided bigint not null default 0;
alter table public.usage_log
    add column if not exists brevitas_incremental_savings_usd numeric(18,10);

comment on column public.usage_log.provider_input_tokens_avoided is
    'Provider input tokens not sent versus the same request before an input-reducing transform.';
comment on column public.usage_log.native_cache_discount_usd is
    'Net provider-native cache read discount after cache-write premiums; not necessarily Brevitas-attributable.';
comment on column public.usage_log.calls_avoided is
    'Provider model calls skipped by exact or semantic response replay.';
comment on column public.usage_log.transport_bytes_avoided is
    'Client/proxy transport bytes avoided; does not imply fewer provider tokens.';
comment on column public.usage_log.brevitas_incremental_savings_usd is
    'Actual-cost delta from an isolated paired control arm; null when no control was measured.';
