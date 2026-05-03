-- ============================================================================
-- Phase 3B — Intelligence Pipeline Schema (Postgres)
-- Date: 2026-04-26
-- Target DBs: tickles_shared (trader_profiles) + tickles_[company] (interpretations, performance)
-- Author: Roo — Intelligence Pipeline Architecture
--
-- WHAT THIS DOES
--   1. Extends media_items.processing_status to include 'skipped_vision_unavailable'
--      for circuit-breaker handling when the vision LLM is down.
--   2. Creates public.trader_profiles in tickles_shared — shared catalog of
--      known traders/signal providers across all companies. Normalised handles,
--      platform identity, reputation baseline.
--   3. Creates public.signal_interpretations in tickles_[company] — per-company
--      table storing LLM + quant dual-track analysis of news_items + media_items.
--      Every row is reproducible: model_version, param_hash, candle_data_hash.
--   4. Creates public.trader_performance in tickles_[company] — per-company
--      scoring of trader accuracy over time. Links interpretations to actual
--      trades for ground-truth validation.
--
-- SAFETY
--   * Idempotent (IF NOT EXISTS / IF NOT EXISTS everywhere).
--   * Uses existing set_updated_at() trigger from tickles_shared.
--   * Cross-DB foreign keys are NOT enforced (trader_profiles lives in
--     tickles_shared; signal_interpretations and trader_performance live in
--     per-company DBs). Integrity is maintained by application code.
--   * All monetary values use NUMERIC(20,8); all timestamps TIMESTAMPTZ(3).
--
-- ROLLBACK
--   See companion file 2026_04_26_phase3b_intelligence_ROLLBACK.sql.
-- ============================================================================

-- ============================================================================
-- PART A — tickles_shared: extend media_items + create trader_profiles
-- ============================================================================
\c tickles_shared

-- ---------------------------------------------------------------------------
-- 1. Extend media_items.processing_status CHECK constraint
-- ---------------------------------------------------------------------------
-- The existing constraint is inline; Postgres auto-generates the name.
-- We drop and recreate it idempotently to add 'skipped_vision_unavailable'.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'public.media_items'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%processing_status%'
    LIMIT 1;

    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.media_items DROP CONSTRAINT %I', con_name);
    END IF;

    ALTER TABLE public.media_items
        ADD CONSTRAINT media_items_processing_status_check
        CHECK (processing_status IN (
            'pending','downloading','downloaded','analyzing',
            'analyzed','discarded','failed','skipped','skipped_vision_unavailable'
        ));
END $$;

-- ---------------------------------------------------------------------------
-- 2. trader_profiles — Shared catalog of known traders / signal providers
-- ---------------------------------------------------------------------------
-- Lives in tickles_shared so every company sees the same identity.
-- Cross-company insights (MemU) reference these IDs.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.trader_profiles (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Identity (platform + handle = unique)
    platform            VARCHAR(32)     NOT NULL
        CHECK (platform IN ('discord','telegram','twitter','tradingview','rss','api','unknown')),
    handle_raw          VARCHAR(255)    NOT NULL,
    handle_normalized   VARCHAR(255)    NOT NULL,
    display_name        VARCHAR(255),

    -- Classification
    trader_type         VARCHAR(32)     NOT NULL DEFAULT 'unknown'
        CHECK (trader_type IN ('pro','amateur','bot','news','unknown')),
    primary_asset_class VARCHAR(32)
        CHECK (primary_asset_class IN ('crypto','cfd','stock','forex','commodity','index')),
    primary_timeframe   VARCHAR(16)
        CHECK (primary_timeframe IN ('scalping','intraday','swing','position','unknown')),

    -- Reputation baseline (updated by PerformanceScorer)
    accuracy_score      NUMERIC(5,4)    DEFAULT NULL,
    accuracy_samples    INT             NOT NULL DEFAULT 0,
    avg_confidence      NUMERIC(5,4)    DEFAULT NULL,
    last_scored_at      TIMESTAMPTZ(3),

    -- Metadata
    first_seen_at       TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes               TEXT,
    metadata            JSONB,

    -- Deduplication
    CONSTRAINT uq_trader_platform_handle UNIQUE (platform, handle_normalized)
);

CREATE INDEX IF NOT EXISTS idx_trader_platform      ON public.trader_profiles (platform);
CREATE INDEX IF NOT EXISTS idx_trader_type          ON public.trader_profiles (trader_type);
CREATE INDEX IF NOT EXISTS idx_trader_accuracy      ON public.trader_profiles (accuracy_score) WHERE accuracy_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trader_last_seen     ON public.trader_profiles (last_seen_at);

-- ---------------------------------------------------------------------------
-- 3. Seed system_config entries for intelligence pipeline
-- ---------------------------------------------------------------------------

INSERT INTO public.system_config (namespace, config_key, config_value) VALUES
    ('intelligence', 'chart_hacker_model_primary',   'anthropic/claude-sonnet-4'),
    ('intelligence', 'chart_hacker_model_fallback',  'google/gemini-2.0-flash-001'),
    ('intelligence', 'chart_hacker_cron',            '*/5 * * * *'),
    ('intelligence', 'chart_hacker_budget_usd_day',  '5.0'),
    ('intelligence', 'interpretation_max_age_hours', '24'),
    ('intelligence', 'performance_lookback_days',    '30'),
    ('intelligence', 'min_accuracy_samples',       '5')
ON CONFLICT (namespace, config_key) DO NOTHING;


-- ============================================================================
-- PART B — tickles_[company]: signal_interpretations + trader_performance
-- ============================================================================
\c tickles_COMPANY_NAME

-- ---------------------------------------------------------------------------
-- 4. signal_interpretations — Dual-track LLM + Quant analysis output
-- ---------------------------------------------------------------------------
-- Every interpretation is reproducible: model_version, param_hash, candle_data_hash.
-- Composite UNIQUE prevents duplicate analysis of the same news item with the
-- same model + parameters.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.signal_interpretations (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source references (cross-DB FKs — not enforced, maintained by app code)
    news_item_id        BIGINT          NOT NULL,
    media_item_id       BIGINT,                         -- nullable: text-only signals have no media
    trader_profile_id   BIGINT          NOT NULL,       -- FK to tickles_shared.trader_profiles

    -- Model provenance (Rule 1: Backtest ≡ Live)
    model_version       VARCHAR(100)    NOT NULL,
    param_hash          CHAR(64)        NOT NULL,
    candle_data_hash    CHAR(64),                       -- null if no market data context used

    -- LLM track output
    llm_direction       VARCHAR(8)      NOT NULL
        CHECK (llm_direction IN ('long','short','neutral','unclear','conflict')),
    llm_confidence      NUMERIC(5,4)    NOT NULL
        CHECK (llm_confidence BETWEEN 0.0 AND 1.0),
    llm_reasoning       TEXT,                           -- free-form reasoning from LLM
    llm_levels          JSONB,                          -- {entry, stop_loss, take_profit_1, ...}

    -- Quant track output
    quant_direction     VARCHAR(8)      NOT NULL
        CHECK (quant_direction IN ('long','short','neutral','unclear','conflict')),
    quant_confidence    NUMERIC(5,4)    NOT NULL
        CHECK (quant_confidence BETWEEN 0.0 AND 1.0),
    quant_indicators    JSONB,                          -- which indicators fired and their readings

    -- Consensus engine
    consensus_direction VARCHAR(8)      NOT NULL
        CHECK (consensus_direction IN ('long','short','neutral','unclear','conflict')),
    consensus_confidence NUMERIC(5,4)   NOT NULL
        CHECK (consensus_confidence BETWEEN 0.0 AND 1.0),
    consensus_method    VARCHAR(32)     NOT NULL DEFAULT 'weighted_average'
        CHECK (consensus_method IN ('weighted_average','llm_wins','quant_wins','veto','unclear')),

    -- Target instrument (resolved from news item + media analysis)
    instrument_symbol   VARCHAR(50),
    instrument_exchange VARCHAR(50),

    -- Freshness guard
    market_data_fresh BOOLEAN         NOT NULL DEFAULT FALSE,
    market_data_at    TIMESTAMPTZ(3),

    -- Cost tracking
    llm_cost_usd      NUMERIC(10,6)   NOT NULL DEFAULT 0,
    quant_cost_usd    NUMERIC(10,6)   NOT NULL DEFAULT 0,

    -- Metadata
    created_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication: one interpretation per (news_item, model, params)
    CONSTRAINT uq_interpretation_dedup UNIQUE (news_item_id, model_version, param_hash)
);

CREATE INDEX IF NOT EXISTS idx_interp_news_item      ON public.signal_interpretations (news_item_id);
CREATE INDEX IF NOT EXISTS idx_interp_media_item     ON public.signal_interpretations (media_item_id) WHERE media_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_interp_trader         ON public.signal_interpretations (trader_profile_id);
CREATE INDEX IF NOT EXISTS idx_interp_consensus_dir  ON public.signal_interpretations (consensus_direction);
CREATE INDEX IF NOT EXISTS idx_interp_created        ON public.signal_interpretations (created_at);
CREATE INDEX IF NOT EXISTS idx_interp_instrument     ON public.signal_interpretations (instrument_symbol, instrument_exchange);
CREATE INDEX IF NOT EXISTS idx_interp_model_hash     ON public.signal_interpretations (model_version, param_hash);

DROP TRIGGER IF EXISTS trg_signal_interpretations_updated ON public.signal_interpretations;
CREATE TRIGGER trg_signal_interpretations_updated
    BEFORE UPDATE ON public.signal_interpretations
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 5. trader_performance — Per-company scoring of trader accuracy
-- ---------------------------------------------------------------------------
-- Links signal_interpretations to actual trades for ground-truth validation.
-- Updated by PerformanceScorer daemon after trades close.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.trader_performance (
    id                  BIGSERIAL       PRIMARY KEY,

    -- References (cross-DB FK to tickles_shared.trader_profiles, not enforced)
    trader_profile_id   BIGINT          NOT NULL,

    -- Scoring window
    score_period        VARCHAR(16)     NOT NULL DEFAULT 'rolling_30d'
        CHECK (score_period IN ('rolling_7d','rolling_30d','rolling_90d','all_time')),
    scored_at           TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Accuracy metrics
    total_signals       INT             NOT NULL DEFAULT 0,
    validated_signals   INT             NOT NULL DEFAULT 0,   -- signals with closed trade outcomes
    correct_direction   INT             NOT NULL DEFAULT 0,   -- consensus_direction matched trade P&L sign
    accuracy_pct        NUMERIC(5,4)    DEFAULT NULL,           -- correct_direction / validated_signals

    -- Confidence calibration
    avg_confidence      NUMERIC(5,4)    DEFAULT NULL,
    confidence_calibration NUMERIC(5,4)  DEFAULT NULL,           -- correlation(confidence, accuracy)

    -- P&L attribution
    total_pnl_usd       NUMERIC(20,8)   DEFAULT 0,
    avg_pnl_per_signal  NUMERIC(20,8)   DEFAULT NULL,
    max_win_usd         NUMERIC(20,8)   DEFAULT NULL,
    max_loss_usd        NUMERIC(20,8)   DEFAULT NULL,

    -- Risk-adjusted metrics
    sharpe_ratio        NUMERIC(10,4)   DEFAULT NULL,
    max_drawdown_pct    NUMERIC(10,4)   DEFAULT NULL,

    -- Metadata
    calculation_params  JSONB,                              -- {lookback_days, min_samples, ...}
    metadata            JSONB,

    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- One score per trader per period (recomputed, not appended)
    CONSTRAINT uq_trader_performance_period UNIQUE (trader_profile_id, score_period)
);

CREATE INDEX IF NOT EXISTS idx_perf_trader          ON public.trader_performance (trader_profile_id);
CREATE INDEX IF NOT EXISTS idx_perf_period          ON public.trader_performance (score_period);
CREATE INDEX IF NOT EXISTS idx_perf_accuracy        ON public.trader_performance (accuracy_pct) WHERE accuracy_pct IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_perf_scored_at       ON public.trader_performance (scored_at);

DROP TRIGGER IF EXISTS trg_trader_performance_updated ON public.trader_performance;
CREATE TRIGGER trg_trader_performance_updated
    BEFORE UPDATE ON public.trader_performance
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================================
-- Sanity queries (run manually after applying)
-- ============================================================================
-- \d+ public.trader_profiles
-- \d+ public.signal_interpretations
-- \d+ public.trader_performance
-- SELECT COUNT(*) FROM public.trader_profiles;
-- SELECT COUNT(*) FROM public.signal_interpretations;
-- SELECT COUNT(*) FROM public.trader_performance;
-- ============================================================================
