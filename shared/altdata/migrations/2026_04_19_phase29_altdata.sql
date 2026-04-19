-- Phase 29: Alt-Data Ingestion
-- A uniform landing table for "everything that isn't OHLCV or news-text":
--   * funding rates, open interest, whale transfers
--   * social sentiment scores, subreddit activity
--   * on-chain metrics (active addresses, netflows)
--   * macro / economic releases
-- Each row is an immutable measurement. Dedupe happens at insert time via
-- (source, provider, scope_key, as_of) uniqueness.
-- Ownership: tickles_shared.
-- Rollback: DROP TABLE + view at bottom of this file (commented).

BEGIN;

CREATE TABLE IF NOT EXISTS public.alt_data_items (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,            -- funding_rate / open_interest / social / onchain / macro / ...
    provider        TEXT NOT NULL,            -- ccxt:binance / twitter / glassnode / fred / custom
    universe        TEXT,                     -- optional bucket (crypto / fx / equities)
    exchange        TEXT,                     -- optional venue
    symbol          TEXT,                     -- optional symbol (BTC/USDT, BTC, US-GDP, ...)
    scope_key       TEXT NOT NULL,            -- normalised key for dedup: e.g. 'ccxt:binance/BTCUSDT/funding'
    metric          TEXT NOT NULL,            -- funding_rate / oi_usd / sentiment_score / active_addresses / ...
    value_numeric   NUMERIC(30, 12),          -- primary numeric value
    value_text      TEXT,                     -- optional textual value (headline, symbol-list, ...)
    unit            TEXT,                     -- usd / pct / count / ...
    as_of           TIMESTAMPTZ NOT NULL,     -- when the measurement is "for"
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload         JSONB NOT NULL DEFAULT '{}'::JSONB,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    UNIQUE (source, provider, scope_key, metric, as_of)
);

CREATE INDEX IF NOT EXISTS alt_data_source_ts_idx
    ON public.alt_data_items (source, as_of DESC);
CREATE INDEX IF NOT EXISTS alt_data_provider_ts_idx
    ON public.alt_data_items (provider, as_of DESC);
CREATE INDEX IF NOT EXISTS alt_data_scope_ts_idx
    ON public.alt_data_items (scope_key, as_of DESC);
CREATE INDEX IF NOT EXISTS alt_data_symbol_ts_idx
    ON public.alt_data_items (exchange, symbol, as_of DESC)
    WHERE symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS alt_data_metric_ts_idx
    ON public.alt_data_items (metric, as_of DESC);

-- Latest measurement per (scope_key, metric) for quick "current view"
CREATE OR REPLACE VIEW public.alt_data_latest AS
SELECT DISTINCT ON (scope_key, metric)
    id, source, provider, universe, exchange, symbol, scope_key, metric,
    value_numeric, value_text, unit, as_of, ingested_at, payload, metadata
FROM public.alt_data_items
ORDER BY scope_key, metric, as_of DESC;

COMMIT;

-- -----------------------------------------------------------------------
-- Rollback (run manually if needed):
-- BEGIN;
-- DROP VIEW  IF EXISTS public.alt_data_latest;
-- DROP TABLE IF EXISTS public.alt_data_items;
-- COMMIT;
