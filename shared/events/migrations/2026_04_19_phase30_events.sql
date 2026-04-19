-- Phase 30: Events Calendar + windows
-- Every event (macro release, earnings, maintenance window, halving,
-- funding roll) is represented as a row with optional
-- ``window_before_minutes`` and ``window_after_minutes``. Active
-- windows are computed by the EventsService and consumed by
-- Guardrails, Treasury, and strategy code.
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.events_calendar (
    id                      BIGSERIAL PRIMARY KEY,
    kind                    TEXT NOT NULL,   -- macro / earnings / maintenance / funding_roll / halving / custom
    provider                TEXT NOT NULL,   -- source of truth: fred / manual / exchange / ...
    name                    TEXT NOT NULL,   -- 'US CPI YoY' / 'binance maintenance 2026-01-07' / ...
    universe                TEXT,            -- crypto / fx / equities
    exchange                TEXT,
    symbol                  TEXT,
    country                 TEXT,            -- ISO-3166 alpha-2 when applicable
    importance              SMALLINT NOT NULL DEFAULT 1,  -- 1=low, 2=med, 3=high
    event_time              TIMESTAMPTZ NOT NULL,
    window_before_minutes   INTEGER NOT NULL DEFAULT 0,
    window_after_minutes    INTEGER NOT NULL DEFAULT 0,
    payload                 JSONB NOT NULL DEFAULT '{}'::JSONB,
    metadata                JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dedupe_key              TEXT NOT NULL,
    UNIQUE (provider, dedupe_key)
);

CREATE INDEX IF NOT EXISTS events_calendar_time_idx
    ON public.events_calendar (event_time);
CREATE INDEX IF NOT EXISTS events_calendar_kind_time_idx
    ON public.events_calendar (kind, event_time);
CREATE INDEX IF NOT EXISTS events_calendar_symbol_time_idx
    ON public.events_calendar (exchange, symbol, event_time)
    WHERE symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_calendar_universe_time_idx
    ON public.events_calendar (universe, event_time)
    WHERE universe IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_calendar_importance_idx
    ON public.events_calendar (importance, event_time);

-- Convenience view: events whose trading window is active at NOW().
CREATE OR REPLACE VIEW public.events_active AS
SELECT *
FROM public.events_calendar
WHERE NOW() BETWEEN (event_time - make_interval(mins => window_before_minutes))
                AND (event_time + make_interval(mins => window_after_minutes));

-- Convenience view: upcoming events (next 7 days by default).
CREATE OR REPLACE VIEW public.events_upcoming AS
SELECT *
FROM public.events_calendar
WHERE event_time >= NOW()
ORDER BY event_time ASC;

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW  IF EXISTS public.events_upcoming;
-- DROP VIEW  IF EXISTS public.events_active;
-- DROP TABLE IF EXISTS public.events_calendar;
-- COMMIT;
