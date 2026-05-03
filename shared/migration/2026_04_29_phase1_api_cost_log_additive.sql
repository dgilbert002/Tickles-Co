-- ============================================================================
-- Phase 1 Migration: api_cost_log additive columns + cost_log wiring
-- Created: 2026-04-29
-- Purpose: Extend api_cost_log with Phase 1 tracking columns (no drops, no NOT NULL retrofits)
-- ============================================================================

-- 1. Additive columns for api_cost_log (all nullable or with safe defaults)
ALTER TABLE api_cost_log
    ADD COLUMN IF NOT EXISTS operation      TEXT,
    ADD COLUMN IF NOT EXISTS agent_id       TEXT,
    ADD COLUMN IF NOT EXISTS temperature    NUMERIC(4,2),
    ADD COLUMN IF NOT EXISTS correlation_id  TEXT,
    ADD COLUMN IF NOT EXISTS request_path   TEXT,
    ADD COLUMN IF NOT EXISTS response_path  TEXT,
    ADD COLUMN IF NOT EXISTS success        BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS http_status    INT,
    ADD COLUMN IF NOT EXISTS extra          JSONB;

-- 2. Unique constraint for idempotency: duplicate (correlation_id, operation) pairs are ignored
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_cost_log_correlation_operation
    ON api_cost_log (correlation_id, operation)
    WHERE correlation_id IS NOT NULL AND operation IS NOT NULL;

-- 3. Index for spend-tracker rollups by role + company + day
CREATE INDEX IF NOT EXISTS idx_api_cost_log_role_company_day
    ON api_cost_log (role, company_id, created_at)
    WHERE created_at > NOW() - INTERVAL '90 days';

-- 4. Index for loop-detector time-series queries
CREATE INDEX IF NOT EXISTS idx_api_cost_log_agent_created
    ON api_cost_log (agent_id, created_at)
    WHERE agent_id IS NOT NULL;

-- 5. Widen cost_usd from NUMERIC(10,6) to NUMERIC(20,8) per standard
ALTER TABLE api_cost_log
    ALTER COLUMN cost_usd TYPE NUMERIC(20,8);

-- 6. Record migration
INSERT INTO schema_migrations (phase, name, applied_at, checksum)
VALUES (
    1,
    'api_cost_log_additive_columns',
    NOW(),
    'phase1_additive_2026_04_29'
)
ON CONFLICT (phase, name) DO UPDATE SET
    applied_at = EXCLUDED.applied_at,
    checksum   = EXCLUDED.checksum;
