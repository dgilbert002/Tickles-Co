-- Run this ONCE to create the technique grading infrastructure.
-- Safe to re-run: IF NOT EXISTS on everything.

-- Per-technique win/loss ledger. One row per (technique, trader, symbol, timeframe).
CREATE TABLE IF NOT EXISTS technique_stats (
    id              BIGSERIAL PRIMARY KEY,
    technique       VARCHAR(80)  NOT NULL,
    trader_handle   VARCHAR(120) NOT NULL DEFAULT '',
    symbol_base     VARCHAR(20)  NOT NULL DEFAULT '',
    timeframe       VARCHAR(8)   NOT NULL DEFAULT '',
    wins            INTEGER      NOT NULL DEFAULT 0,
    losses          INTEGER      NOT NULL DEFAULT 0,
    total_pnl_usd   NUMERIC(20,8) NOT NULL DEFAULT 0,
    sample_count    INTEGER      NOT NULL DEFAULT 0,
    sum_rr          NUMERIC(14,4) NOT NULL DEFAULT 0,
    sum_win_pct     NUMERIC(14,4) NOT NULL DEFAULT 0,
    sum_loss_pct    NUMERIC(14,4) NOT NULL DEFAULT 0,
    last_outcome    VARCHAR(8),
    last_position_id BIGINT,
    description     TEXT,
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (technique, trader_handle, symbol_base, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_technique_stats_perf
    ON technique_stats ((wins::float / GREATEST(wins + losses, 1)) DESC, sample_count DESC);

-- Human-readable descriptions for techniques (seeded + auto-populated by postmortem).
CREATE TABLE IF NOT EXISTS technique_catalog (
    technique   VARCHAR(80) PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    category    VARCHAR(40) NOT NULL DEFAULT 'observed',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Postmortem technique validation column (JSON array of {technique, played_out, note}).
ALTER TABLE position_postmortems
    ADD COLUMN IF NOT EXISTS techniques_validated JSONB;

COMMENT ON TABLE technique_stats IS
  'Per-technique win/loss ledger. Techniques are observed by the vision LLM per chart (max 5), stored in signal_interpretations.pattern_tags, and graded here when the tracked position closes (validation-aware after postmortem). The recall context injects top performers so chart_hacker learns which techniques actually pay.';

COMMENT ON TABLE technique_catalog IS
  'Human-readable descriptions for trading techniques detected by the vision LLM. Seeded for common SMC/fibonacci/pattern/session techniques; auto-populated from postmortem validation notes for new LLM-invented techniques.';
