-- wyfz (wyfzmfnswtzyhwbltbpy) usage_log remediation — 2026-07-27
--
-- WHY: The production API (Railway) writes usage rows to project wyfzmfnswtzyhwbltbpy,
-- but that project's public.usage_log is frozen at roughly the pre-enterprise-tenancy
-- schema. It is MISSING 11 columns the current code (_usage_row in api/store.py) writes,
-- so every POST /v1/usage fails with:
--   400 Bad Request  https://wyfzmfnswtzyhwbltbpy.supabase.co/rest/v1/usage_log
-- i.e. usage tracking is fully broken in production right now.
--
-- This script adds exactly the missing columns with the SAME types the canonical
-- migrations declare (202607170001 enterprise tenancy, 202607170012 cache columns,
-- receipt-accounting). It also repairs the known float/integer drift on this project.
--
-- OWNER-RUN ONLY. Requires the wyfz Postgres connection string (DB password) — the
-- service_role/PostgREST key cannot run DDL. Run inside a transaction against wyfz.
-- PREFERRED PATH is still the full docs/PROD_DB_RECONCILIATION.md chain; this is the
-- minimal interim to un-break the data plane. Take a backup / PITR checkpoint first.

begin;

-- Enterprise tenancy (202607170001)
alter table public.usage_log add column if not exists organization_id uuid;
alter table public.usage_log add column if not exists customer_id uuid;
alter table public.usage_log add column if not exists authoritative boolean not null default false;

-- Cache attribution (202607170012)
alter table public.usage_log add column if not exists cache_write_5m_tokens bigint not null default 0;
alter table public.usage_log add column if not exists cache_write_1h_tokens bigint not null default 0;
alter table public.usage_log add column if not exists cache_attributable boolean not null default false;

-- Receipt accounting
alter table public.usage_log add column if not exists provider_input_tokens_avoided bigint not null default 0;
alter table public.usage_log add column if not exists native_cache_discount_usd numeric(18,10);
alter table public.usage_log add column if not exists calls_avoided bigint not null default 0;
alter table public.usage_log add column if not exists transport_bytes_avoided bigint not null default 0;
alter table public.usage_log add column if not exists brevitas_incremental_savings_usd numeric(18,10);

-- Float drift repair (observed live: these two are double precision on wyfz)
alter table public.usage_log alter column brevitas_fee_usd type numeric(18,10) using brevitas_fee_usd::numeric(18,10);
alter table public.usage_log alter column cost_saved_usd  type numeric(18,10) using cost_saved_usd::numeric(18,10);

-- Integer drift repair (observed live: token counts are integer, should be bigint)
alter table public.usage_log alter column baseline_tokens  type bigint using baseline_tokens::bigint;
alter table public.usage_log alter column optimized_tokens type bigint using optimized_tokens::bigint;

commit;

-- CAVEAT: _usage_row sends organization_id/customer_id as "" (empty string) when unset.
-- An empty string will NOT cast to uuid. Confirm the insert path coerces "" -> NULL for
-- these columns (or that callers always pass a real uuid) BEFORE relying on this fix;
-- the full migration chain plus api/store.py normalization is the coherent path.
-- Verify after running:  select count(*) from public.usage_log;  then POST a usage row.
