-- ============================================================================
-- ClickHouse schema for JarvAIs V2 backtest storage
-- Purpose: Raw per-backtest rows at massive scale (millions+ rows/hour).
-- Postgres holds curated/approved backtests; ClickHouse holds the sweep.
-- ============================================================================
-- Run: clickhouse-client --user admin --password 'Tickles21!' --multiquery < clickhouse_schema.sql
-- ============================================================================

CREATE DATABASE IF NOT EXISTS backtests;
USE backtests;

-- ============================================================================
-- 1. backtest_runs — one row per completed backtest run
-- Wide denormalized row; ClickHouse is column-store so cheap selectors matter.
-- ============================================================================
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id             UUID DEFAULT generateUUIDv4(),
    param_hash         FixedString(64),              -- SHA-256 of params
    candle_data_hash   FixedString(64),              -- SHA-256 of underlying candles
    instrument_id      UInt64,
    exchange           LowCardinality(String),
    symbol             LowCardinality(String),
    indicator_name     LowCardinality(String),
    timeframe          LowCardinality(String),
    params             String CODEC(ZSTD(3)),        -- JSON params (compressed)
    date_from          Date,
    date_to            Date,
    initial_balance    Decimal(20, 8),
    final_balance      Decimal(20, 8),
    total_return_pct   Float64,
    sharpe_ratio       Float64,
    sortino_ratio      Float64,
    max_drawdown_pct   Float64,
    profit_factor      Float64,
    win_rate_pct       Float32,
    total_trades       UInt32,
    total_fees         Decimal(20, 8),
    total_spread_costs Decimal(20, 8),
    total_overnight    Decimal(20, 8),
    engine_version     LowCardinality(String),
    run_duration_ms    UInt32,
    worker_id          LowCardinality(String),
    created_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    -- Guardrails extensions (Phase 8): deflated sharpe, promoted status
    deflated_sharpe    Float64,
    oos_sharpe         Float64,
    oos_return_pct     Float64,
    promotion_status   LowCardinality(String) DEFAULT 'candidate',
    parent_run_id      Nullable(UUID),               -- lineage
    notes              String CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(created_at)
ORDER BY (instrument_id, indicator_name, timeframe, sharpe_ratio, created_at)
SETTINGS index_granularity = 8192;

-- ============================================================================
-- 2. backtest_trades — per-trade detail (optional, enabled via config)
-- Same raw style. Partition by month to keep parts bounded.
-- ============================================================================
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id            UUID,
    trade_index       UInt32,
    direction         Enum8('long' = 1, 'short' = 2),
    entry_at          DateTime64(3, 'UTC'),
    exit_at           Nullable(DateTime64(3, 'UTC')),
    entry_price       Decimal(20, 8),
    exit_price        Nullable(Decimal(20, 8)),
    quantity          Decimal(20, 8),
    gross_pnl         Decimal(20, 8),
    net_pnl           Decimal(20, 8),
    fees              Decimal(20, 8),
    slippage          Decimal(20, 8),
    window_close_time Nullable(String),
    signal_hash       FixedString(64)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(entry_at)
ORDER BY (run_id, trade_index);

-- ============================================================================
-- 3. signal_feed — optional detailed per-candle signal output (heavy)
-- Disabled by default; enable via backtest.emit_signal_feed = true
-- ============================================================================
CREATE TABLE IF NOT EXISTS signal_feed (
    run_id         UUID,
    candle_ts      DateTime64(3, 'UTC'),
    signal_fired   Bool,
    signal_value   Float64,
    meta           String CODEC(ZSTD(3))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(candle_ts)
ORDER BY (run_id, candle_ts);

-- ============================================================================
-- 4. Materialized view: top-N by sharpe for quick agent dashboards
-- Kept fresh incrementally; no scan of full table for "show me best strategies"
-- ============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS top_sharpe_per_indicator
ENGINE = ReplacingMergeTree(created_at)
ORDER BY (instrument_id, indicator_name, timeframe)
POPULATE AS
SELECT
    instrument_id,
    indicator_name,
    timeframe,
    argMax(run_id, sharpe_ratio)           AS top_run_id,
    max(sharpe_ratio)                      AS top_sharpe,
    argMax(total_return_pct, sharpe_ratio) AS top_return_pct,
    argMax(max_drawdown_pct, sharpe_ratio) AS top_drawdown,
    max(created_at)                        AS created_at
FROM backtest_runs
WHERE sharpe_ratio > 0
GROUP BY instrument_id, indicator_name, timeframe;

-- ============================================================================
-- 5. Agent activity log (cheap event trail — Mem0 complement)
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_events (
    event_id        UUID DEFAULT generateUUIDv4(),
    agent_name      LowCardinality(String),
    event_type      LowCardinality(String),       -- task_start, task_end, decision, error, learning
    company_id      LowCardinality(String),
    trace_id        String,
    payload         String CODEC(ZSTD(3)),
    tokens_in       UInt32 DEFAULT 0,
    tokens_out      UInt32 DEFAULT 0,
    cost_usd        Decimal(10, 6) DEFAULT 0,
    latency_ms      UInt32 DEFAULT 0,
    created_at      DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(created_at)
ORDER BY (agent_name, created_at);

-- ============================================================================
-- Verification
-- ============================================================================
SELECT
    database,
    name AS table,
    engine,
    total_rows
FROM system.tables
WHERE database = 'backtests'
ORDER BY name;
