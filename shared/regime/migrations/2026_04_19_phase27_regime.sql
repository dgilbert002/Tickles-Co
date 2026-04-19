-- Phase 27: Regime Service
--
-- The Regime Service looks at recent price/volatility behaviour and
-- labels the current market regime for a given (universe, symbol,
-- timeframe, venue) tuple. Downstream strategies, agents, and the
-- Treasury can subscribe to the label to adjust position sizing,
-- enable/disable styles, throttle collectors, etc.
--
-- Ownership: tickles_shared. All state tables are append-only; the
-- "current" view is derived via DISTINCT ON.
--
-- Rollback is at the bottom of the file (commented out).

BEGIN;

-- --------------------------------------------------------------------
-- regime_config
--
-- Declarative per-universe / per-symbol classifier settings. Operators
-- configure "for venue=binance symbol=BTC/USDT timeframe=1h, classify
-- as composite using trend_slow=200 vol_window=48 crash_dd=0.10". If
-- no row exists for a specific symbol, the universe-level default
-- applies. If no universe-level default exists either, the service
-- falls back to its built-in composite classifier defaults.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.regime_config (
    id                      BIGSERIAL PRIMARY KEY,
    universe                TEXT NOT NULL,           -- e.g. "crypto-majors"
    exchange                TEXT,                    -- NULL = any exchange
    symbol                  TEXT,                    -- NULL = whole universe
    timeframe               TEXT NOT NULL,           -- "1m" / "5m" / "1h" / "1d"
    classifier              TEXT NOT NULL,           -- trend / volatility / composite
    params                  JSONB NOT NULL DEFAULT '{}'::JSONB,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (universe, exchange, symbol, timeframe, classifier)
);
CREATE INDEX IF NOT EXISTS regime_config_universe_idx
    ON public.regime_config (universe, enabled);

-- --------------------------------------------------------------------
-- regime_states
--
-- Append-only snapshots of classifier output. Every tick of the
-- Regime Service writes one row per (universe, exchange, symbol,
-- timeframe, classifier). This is the source of truth for audit
-- trails; downstream consumers read the "current" view.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.regime_states (
    id                      BIGSERIAL PRIMARY KEY,
    universe                TEXT NOT NULL,
    exchange                TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    timeframe               TEXT NOT NULL,
    classifier              TEXT NOT NULL,
    regime                  TEXT NOT NULL,           -- bull / bear / sideways / crash / recovery / high_vol / low_vol / unknown
    confidence              NUMERIC(6, 4) NOT NULL DEFAULT 0,  -- 0..1
    trend_score             NUMERIC(12, 6),
    volatility              NUMERIC(12, 6),
    drawdown                NUMERIC(12, 6),
    features                JSONB NOT NULL DEFAULT '{}'::JSONB,
    sample_size             INTEGER NOT NULL DEFAULT 0,
    as_of                   TIMESTAMPTZ NOT NULL,            -- last candle ts
    recorded_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason                  TEXT,
    metadata                JSONB NOT NULL DEFAULT '{}'::JSONB
);
CREATE INDEX IF NOT EXISTS regime_states_key_ts_idx
    ON public.regime_states (universe, exchange, symbol, timeframe, classifier, as_of DESC);
CREATE INDEX IF NOT EXISTS regime_states_regime_idx
    ON public.regime_states (regime, as_of DESC);
CREATE INDEX IF NOT EXISTS regime_states_recorded_idx
    ON public.regime_states (recorded_at DESC);

-- --------------------------------------------------------------------
-- regime_current
--
-- DISTINCT ON view returning the latest row per
-- (universe, exchange, symbol, timeframe, classifier). This is what
-- strategies and the Treasury should read.
-- --------------------------------------------------------------------
CREATE OR REPLACE VIEW public.regime_current AS
SELECT DISTINCT ON (universe, exchange, symbol, timeframe, classifier)
    id, universe, exchange, symbol, timeframe, classifier, regime,
    confidence, trend_score, volatility, drawdown, features,
    sample_size, as_of, recorded_at, reason, metadata
FROM public.regime_states
ORDER BY universe, exchange, symbol, timeframe, classifier, as_of DESC;

COMMIT;

-- ROLLBACK (manual):
-- BEGIN;
--   DROP VIEW IF EXISTS public.regime_current;
--   DROP TABLE IF EXISTS public.regime_states;
--   DROP TABLE IF EXISTS public.regime_config;
-- COMMIT;
