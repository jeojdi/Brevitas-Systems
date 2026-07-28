-- Production reconciliation prelude.
--
-- Run this ONCE against the production database immediately before
-- scripts/db/apply-migrations.sh. It repairs column-TYPE drift that the
-- migration chain cannot fix on its own.
--
-- WHY THIS EXISTS
-- ---------------
-- Production (Supabase project wyfzmfnswtzyhwbltbpy) was assembled piecemeal
-- through the SQL editor rather than by running supabase/migrations in order.
-- Its usage_log therefore holds money in floating point and token counts in
-- 32-bit integers, where the canonical chain declares numeric(18,10) and
-- bigint (supabase/migrations/20260710_cloud_usage.sql:91).
--
-- Every migration that touches these columns uses "add column if not exists",
-- which is a no-op against a column that already exists with the WRONG TYPE.
-- A forward-only chain can therefore never repair this, and CI cannot detect
-- it, because CI always builds from the canonical migrations where the types
-- are already correct.
--
-- Without this prelude, applying the outstanding migrations against production
-- fails partway through, at:
--
--   202607170006_database_scaling.sql
--   ERROR: function round(double precision, integer) does not exist
--          round(coalesce(sum(brevitas_fee_usd), 0), 2)
--
-- Verified: against a fixture rebuilt to production's exact live shape, this
-- prelude followed by the full 46-migration chain yields a schema identical to
-- a fresh canonical install.
--
-- INDEPENDENT OF MIGRATIONS: baseline_cost_usd, actual_cost_usd,
-- measured_savings_usd, verified_savings_usd, cost_saved_usd and brevitas_fee_usd
-- are billing amounts. Holding any of them as double precision means sums
-- accumulate binary floating-point error. This prelude is a correctness fix in
-- its own right.

begin;

-- Pre-flight: refuse to run if any live value would not survive the cast.
-- numeric(18,10) allows 8 digits left of the decimal point.
do $$
declare
    bad_fee   bigint;
    bad_saved bigint;
    bad_base  bigint;
    bad_opt   bigint;
begin
    select count(*) into bad_fee
      from public.usage_log where abs(brevitas_fee_usd) >= 1e8;
    select count(*) into bad_saved
      from public.usage_log where abs(cost_saved_usd) >= 1e8;
    select count(*) into bad_base
      from public.usage_log where baseline_tokens is null;
    select count(*) into bad_opt
      from public.usage_log where optimized_tokens is null;

    if bad_fee > 0 or bad_saved > 0 then
        raise exception
            'refusing to cast: % fee and % savings rows exceed numeric(18,10) range',
            bad_fee, bad_saved;
    end if;
    if bad_base > 0 or bad_opt > 0 then
        raise exception
            'refusing to cast: % null baseline and % null optimized token rows',
            bad_base, bad_opt;
    end if;
end $$;

-- Float money -> exact decimal, 32-bit token counts -> 64-bit. All six money
-- columns rounded by 202607170006 are coerced here, and every token counter the
-- canonical schema declares as bigint is widened. No-ops where already correct.
alter table public.usage_log
    alter column brevitas_fee_usd type numeric(18,10) using brevitas_fee_usd::numeric,
    alter column cost_saved_usd   type numeric(18,10) using cost_saved_usd::numeric,
    alter column baseline_tokens      type bigint,
    alter column optimized_tokens     type bigint,
    alter column tokens_saved         type bigint,
    alter column fresh_input_tokens   type bigint,
    alter column cached_input_tokens  type bigint,
    alter column cache_write_tokens   type bigint,
    alter column output_tokens        type bigint;

-- Normalise the remaining money columns to the canonical precision. No-ops if the
-- column is already numeric(18,10).
alter table public.usage_log
    alter column baseline_cost_usd    type numeric(18,10),
    alter column actual_cost_usd      type numeric(18,10),
    alter column measured_savings_usd type numeric(18,10),
    alter column verified_savings_usd type numeric(18,10);

commit;

-- NOT REPAIRED HERE -- documented, harmless, deliberately left alone:
--
--   usage_log.cached_tokens   production-only column; defined by NO migration
--                             in supabase/migrations. Orphan from a hand-run.
--   waitlist.source           production-only columns, originating from the
--   waitlist.use_case         un-versioned supabase/add_waitlist_fields.sql,
--                             which was applied by hand outside the chain.
--
-- All three are nullable-or-defaulted extras. They do not block the chain and
-- dropping them would destroy data, so they stay until someone decides.
