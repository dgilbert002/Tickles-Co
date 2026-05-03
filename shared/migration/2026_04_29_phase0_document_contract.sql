-- Phase 0 migration — Document Contract & Pre-Kickoff Infrastructure
-- Created: 2026-04-29
-- Purpose: schema_migrations ledger + writer-registry table + api_cost_log index

-- 1. schema_migrations ledger (idempotent phase tracking)
CREATE TABLE IF NOT EXISTS schema_migrations (
    phase         INT PRIMARY KEY,
    checksum      VARCHAR(64) NOT NULL,  -- SHA-256 of migration file
    target        VARCHAR(32) NOT NULL DEFAULT 'shared',  -- 'shared' | company name
    applied_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes         TEXT
);

COMMENT ON TABLE schema_migrations IS
    'Idempotent migration ledger. Each phase has exactly one row. Checksum prevents silent re-runs of modified files.';

-- 2. table_writers — writer-domain registry for single-writer policy [BC]
CREATE TABLE IF NOT EXISTS table_writers (
    table_name                VARCHAR(128) PRIMARY KEY,
    allowed_writer_services   TEXT[] NOT NULL DEFAULT '{}',
    notes                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE table_writers IS
    'Single-writer policy registry. WARNING mode until 2026-05-30, then ENFORCE mode.';

-- 3. Index on api_cost_log for spend tracker queries [BP]
CREATE INDEX IF NOT EXISTS idx_api_cost_log_created_at
    ON api_cost_log (created_at);

CREATE INDEX IF NOT EXISTS idx_api_cost_log_company_role
    ON api_cost_log (company_id, role, created_at);

-- 4. Seed initial writer registrations (WARNING mode — audit these during 30-day grace)
INSERT INTO table_writers (table_name, allowed_writer_services, notes)
VALUES
    ('signal_interpretations', ARRAY['interpretation_service'], 'Phase 3B — dual-track LLM+quant consensus'),
    ('tracked_positions',      ARRAY['surgeon2_trader'],         'Phase 6 — position writer daemon'),
    ('trader_performance',     ARRAY['performance_scorer'],      'Phase 3B — accuracy scoring'),
    ('trader_profiles',        ARRAY['interpretation_service'],    'Phase 3B — shared catalog'),
    ('media_items',            ARRAY['discord_collector', 'telegram_collector'], 'Phase 3A — media ingestion'),
    ('api_cost_log',           ARRAY['gateway_config'],          'Phase 1 — universal LLM gateway')
ON CONFLICT (table_name) DO UPDATE SET
    allowed_writer_services = EXCLUDED.allowed_writer_services,
    notes = COALESCE(table_writers.notes, '') || E'\n' || EXCLUDED.notes,
    updated_at = NOW();
