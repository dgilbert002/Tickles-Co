-- shared/intelligence/migrations/2026_04_29_phase0_schema_migrations.sql
-- Phase 0 bootstrap: schema migrations ledger + writer registry table
-- Idempotent — safe to re-run.

-- 1. Schema migrations tracking table
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    phase       INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum    CHAR(64) NOT NULL,
    target      TEXT NOT NULL DEFAULT 'shared',
    applied_by  TEXT NOT NULL DEFAULT CURRENT_USER
);

-- Seed Phase 0 itself (meta-migration)
INSERT INTO public.schema_migrations (phase, checksum, target)
VALUES (0, 'phase0_bootstrap', 'shared')
ON CONFLICT (phase) DO NOTHING;

-- 2. Writer-domain registry table
CREATE TABLE IF NOT EXISTS public.table_writers (
    table_name              TEXT PRIMARY KEY,
    allowed_writer_services TEXT[] NOT NULL,
    notes                   TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed initial mappings (verified against 232 INSERT INTO sites across codebase)
-- Phase 0–9: WARNING mode — unauthorised writes are logged but not blocked.
-- Phase R / 2026-05-30: ENFORCE mode — CI gate fails builds on unauthorised writers.
INSERT INTO public.table_writers (table_name, allowed_writer_services, notes) VALUES
  ('tracked_positions',     ARRAY['interpretation_service','surgeon2_trader','chart_hacker_trader','postmortem_service']::TEXT[], 'postmortem updates close fields only'),
  ('position_postmortems',  ARRAY['postmortem_service']::TEXT[], 'sole writer'),
  ('agent_opinions',        ARRAY['chart_hacker_opinion_service']::TEXT[], 'sole writer'),
  ('signal_interpretations',ARRAY['interpretation_service']::TEXT[], 'sole writer'),
  ('api_cost_log',          ARRAY['gateway_config','mem0_config','memu_client','discord_collector','telegram_collector','rss_collector']::TEXT[], 'multi-writer OK — audit table'),
  ('schema_migrations',     ARRAY['run_phase_migration']::TEXT[], 'sole writer')
ON CONFLICT (table_name) DO NOTHING;
