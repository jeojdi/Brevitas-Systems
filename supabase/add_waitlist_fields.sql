-- Migration: align waitlist table with the current waitlist form fields.
-- Run this in Supabase → SQL Editor on project ctlhawahnwcfzdikrcxr.
--
-- Existing columns (kept): id, email, name, company, role, use_case, source,
-- created_at, updated_at.
-- New columns added below match the fields the public waitlist form posts.

ALTER TABLE waitlist
  ADD COLUMN IF NOT EXISTS pipeline_shape TEXT,
  ADD COLUMN IF NOT EXISTS monthly_spend  VARCHAR(50),
  ADD COLUMN IF NOT EXISTS orchestrator   VARCHAR(100),
  ADD COLUMN IF NOT EXISTS notes          TEXT,
  ADD COLUMN IF NOT EXISTS design_partner BOOLEAN DEFAULT FALSE;

-- Optional: backfill notes from the legacy use_case field so older rows
-- show their context under the new column too.
UPDATE waitlist
   SET notes = use_case
 WHERE notes IS NULL
   AND use_case IS NOT NULL;

-- Verify:
-- SELECT column_name, data_type FROM information_schema.columns
--  WHERE table_name = 'waitlist' ORDER BY ordinal_position;
