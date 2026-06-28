-- Phase F: Add per-pipeline/per-agent/per-run tracking labels to billing_events
-- Enables sliced attribution and analytics across pipeline → agent → run dimensions

-- Add new columns to billing_events table
ALTER TABLE billing_events
ADD COLUMN IF NOT EXISTS pipeline TEXT NOT NULL DEFAULT '',
ADD COLUMN IF NOT EXISTS agent TEXT NOT NULL DEFAULT '',
ADD COLUMN IF NOT EXISTS run_id TEXT NOT NULL DEFAULT '';

-- Create index for efficient filtering by pipeline
CREATE INDEX IF NOT EXISTS idx_billing_events_pipeline
ON billing_events(user_id, pipeline)
WHERE pipeline != '';

-- Create index for efficient filtering by agent within pipeline
CREATE INDEX IF NOT EXISTS idx_billing_events_pipeline_agent
ON billing_events(user_id, pipeline, agent)
WHERE pipeline != '' AND agent != '';

-- Create index for efficient filtering by run_id
CREATE INDEX IF NOT EXISTS idx_billing_events_run_id
ON billing_events(user_id, run_id)
WHERE run_id != '';

-- Create composite index for date range + pipeline queries (common analytics query)
CREATE INDEX IF NOT EXISTS idx_billing_events_date_pipeline
ON billing_events(user_id, created_at DESC, pipeline)
WHERE pipeline != '';

-- Create view: savings_by_pipeline
-- Aggregates savings metrics per pipeline per month, with RLS
CREATE OR REPLACE VIEW savings_by_pipeline AS
SELECT
  user_id,
  DATE_TRUNC('month', created_at) AS month,
  pipeline,
  COUNT(*) AS calls,
  SUM(baseline_tokens) AS baseline_tokens,
  SUM(optimized_tokens) AS optimized_tokens,
  SUM(baseline_tokens - optimized_tokens) AS tokens_saved,
  ROUND(
    100.0 * SUM(baseline_tokens - optimized_tokens) /
    NULLIF(SUM(baseline_tokens), 0),
    2
  ) AS savings_pct,
  ROUND(SUM(cost_saved_usd)::NUMERIC, 2) AS cost_saved_usd,
  ROUND((SUM(cost_saved_usd) * 0.10)::NUMERIC, 2) AS brevitas_fee_usd
FROM billing_events
WHERE pipeline != ''
GROUP BY user_id, DATE_TRUNC('month', created_at), pipeline;

-- Create view: savings_by_agent
-- Aggregates savings metrics per agent within pipeline per month, with RLS
CREATE OR REPLACE VIEW savings_by_agent AS
SELECT
  user_id,
  DATE_TRUNC('month', created_at) AS month,
  pipeline,
  agent,
  COUNT(*) AS calls,
  SUM(baseline_tokens) AS baseline_tokens,
  SUM(optimized_tokens) AS optimized_tokens,
  SUM(baseline_tokens - optimized_tokens) AS tokens_saved,
  ROUND(
    100.0 * SUM(baseline_tokens - optimized_tokens) /
    NULLIF(SUM(baseline_tokens), 0),
    2
  ) AS savings_pct,
  ROUND(SUM(cost_saved_usd)::NUMERIC, 2) AS cost_saved_usd
FROM billing_events
WHERE agent != ''
GROUP BY user_id, DATE_TRUNC('month', created_at), pipeline, agent;

-- Create view: savings_by_run
-- Aggregates all agents within a run (trace view)
CREATE OR REPLACE VIEW savings_by_run AS
SELECT
  user_id,
  run_id,
  pipeline,
  created_at,
  COUNT(*) AS agent_calls,
  SUM(baseline_tokens) AS baseline_tokens,
  SUM(optimized_tokens) AS optimized_tokens,
  SUM(baseline_tokens - optimized_tokens) AS tokens_saved,
  ROUND(
    100.0 * SUM(baseline_tokens - optimized_tokens) /
    NULLIF(SUM(baseline_tokens), 0),
    2
  ) AS savings_pct,
  ROUND(SUM(cost_saved_usd)::NUMERIC, 2) AS cost_saved_usd
FROM billing_events
WHERE run_id != ''
GROUP BY user_id, run_id, pipeline, created_at;

-- Enable RLS on views (views inherit parent table RLS if parent has it enabled)
-- billing_events already has RLS enabled, so views are automatically scoped to user_id

-- Create function to update billing_events with labels from Brevitas API calls
-- This is called by the mirror writer to ensure labels are populated
CREATE OR REPLACE FUNCTION update_billing_labels(
  p_user_id UUID,
  p_pipeline TEXT,
  p_agent TEXT,
  p_run_id TEXT,
  p_session_id TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
  UPDATE billing_events
  SET pipeline = COALESCE(NULLIF(p_pipeline, ''), pipeline),
      agent = COALESCE(NULLIF(p_agent, ''), agent),
      run_id = COALESCE(NULLIF(p_run_id, ''), run_id)
  WHERE user_id = p_user_id AND session_id = p_session_id;
END;
$$;

-- Grant execute on label update function to authenticated users
GRANT EXECUTE ON FUNCTION update_billing_labels(UUID, TEXT, TEXT, TEXT, TEXT) TO authenticated;

-- Comment on new columns and views for documentation
COMMENT ON COLUMN billing_events.pipeline IS 'Pipeline name (e.g., campaign-launch). Allows slicing savings by workflow.';
COMMENT ON COLUMN billing_events.agent IS 'Agent role name (e.g., copywriter, researcher). Enables per-agent attribution within a pipeline.';
COMMENT ON COLUMN billing_events.run_id IS 'Run identifier (trace ID). Groups all agents in a single pipeline execution.';

COMMENT ON VIEW savings_by_pipeline IS
'Monthly savings aggregated by pipeline. For each pipeline, shows calls, tokens saved, savings %, and Brevitas fee. Subject to RLS (user_id).';

COMMENT ON VIEW savings_by_agent IS
'Monthly savings aggregated by agent within each pipeline. For each agent in each pipeline, shows calls, tokens saved, and savings %. Subject to RLS (user_id).';

COMMENT ON VIEW savings_by_run IS
'Per-run (trace) savings. Groups all agents in a single pipeline execution (run_id), showing total calls, tokens, and $ saved per run. Subject to RLS (user_id).';
