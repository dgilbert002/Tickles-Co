-- shared/intelligence/migrations/2026_05_02_phase10_writer_registry.sql
-- Writer-domain registry — the single source of truth for "which service is allowed
-- to write to which table".

CREATE TABLE IF NOT EXISTS public.table_writers (
    table_name              TEXT PRIMARY KEY,
    allowed_writer_services TEXT[] NOT NULL,
    notes                   TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed initial mappings (verified against current code)
INSERT INTO public.table_writers (table_name, allowed_writer_services, notes) VALUES
  ('tracked_positions',     ARRAY['interpretation_service','surgeon2_trader','chart_hacker_trader','postmortem_service']::TEXT[], 'postmortem updates close fields only'),
  ('position_postmortems',  ARRAY['postmortem_service']::TEXT[], 'sole writer'),
  ('agent_opinions',        ARRAY['chart_hacker_opinion_service']::TEXT[], 'sole writer'),
  ('signal_interpretations',ARRAY['interpretation_service']::TEXT[], 'sole writer'),
  ('memu_outbox',           ARRAY['interpretation_service','postmortem_service','memu_listener']::TEXT[], 'two producers, one consumer, one processor'),
  ('news_items',            ARRAY['discord_collector','telegram_collector','rss_collector','tradingview_monitor']::TEXT[], 'collectors only')
ON CONFLICT (table_name) DO UPDATE
   SET allowed_writer_services = EXCLUDED.allowed_writer_services,
       updated_at = now();
