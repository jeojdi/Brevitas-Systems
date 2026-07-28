-- Widen legacy billing_events money and token columns to match usage_log.
--
-- 20260626_create_billing.sql declared the legacy billing_events table with
-- cost_saved_usd / brevitas_fee_usd as numeric(12,8) and baseline_tokens /
-- compressed_tokens as integer. The canonical receipt store (usage_log) uses
-- numeric(18,10) money and bigint token counts, and reconciliation compares the
-- two, so the narrow legacy types silently truncate large fees and overflow
-- 32-bit token counts. Bring billing_events up to the same precision.
--
-- tokens_saved is a stored generated column derived from baseline_tokens and
-- compressed_tokens, and the billing_monthly view reads tokens_saved and both
-- money columns, so both dependents are dropped, the base columns widened, then
-- the dependents rebuilt. Forward-only and idempotent: re-running is a no-op on
-- already-wide columns and rebuilds identical dependents.

begin;

-- Release the dependents that block an in-place type change.
drop view if exists public.billing_monthly;
alter table public.billing_events drop column if exists tokens_saved;

alter table public.billing_events
    alter column baseline_tokens   type bigint using baseline_tokens::bigint,
    alter column compressed_tokens type bigint using compressed_tokens::bigint,
    alter column cost_saved_usd    type numeric(18,10) using cost_saved_usd::numeric(18,10),
    alter column brevitas_fee_usd  type numeric(18,10) using brevitas_fee_usd::numeric(18,10);

-- Rebuild the generated column over the now-bigint base columns.
alter table public.billing_events
    add column if not exists tokens_saved bigint
        generated always as (baseline_tokens - compressed_tokens) stored;

-- Rebuild the monthly summary view exactly as declared in 20260626_create_billing.sql.
create or replace view public.billing_monthly as
select
  user_id,
  date_trunc('month', ts)::date as month,
  count(*)                       as calls,
  sum(tokens_saved)              as tokens_saved,
  sum(cost_saved_usd)            as cost_saved_usd,
  sum(brevitas_fee_usd)          as brevitas_fee_usd
from public.billing_events
group by user_id, date_trunc('month', ts);

commit;
