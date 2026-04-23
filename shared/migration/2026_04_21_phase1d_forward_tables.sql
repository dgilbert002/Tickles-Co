-- ============================================================================
-- ClickHouse migration for Phase 1D: Forward Testing
-- ============================================================================

USE backtests;

CREATE TABLE IF NOT EXISTS backtest_forward_runs (
    forward_run_id     UUID DEFAULT generateUUIDv4(),
    strategy_id        String,                       -- Reference to Postgres strategies.id
    param_hash         FixedString(64),
    candle_data_hash   FixedString(64),
    instrument_id      UInt64,
    exchange           LowCardinality(String),
    symbol             LowCardinality(String),
    timeframe          LowCardinality(String),
    params             String CODEC(ZSTD(3)),
    initial_balance    Decimal(20, 8),
    current_balance    Decimal(20, 8),
    total_return_pct   Float64,
    sharpe_ratio       Float64,
    total_trades       UInt32,
    status             LowCardinality(String) DEFAULT 'active', -- active, paused, stopped
    started_at         DateTime64(3, 'UTC') DEFAULT now64(3),
    last_updated_at    DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (strategy_id, started_at);

CREATE TABLE IF NOT EXISTS backtest_forward_trades (
    forward_run_id    UUID,
    strategy_id       String,
    trade_index       UInt32,
    direction         Enum8('long' = 1, 'short' = 2),
    entry_at          DateTime64(3, 'UTC'),
    exit_at           Nullable(DateTime64(3, 'UTC')),
    entry_price       Decimal(20, 8),
    exit_price        Nullable(Decimal(20, 8)),
    quantity          Decimal(20, 8),
    net_pnl           Decimal(20, 8),
    fees              Decimal(20, 8),
    exit_reason       LowCardinality(String),        -- signal, sl, tp, manual
    pairing_key       String,                        -- (strategy_id, signal_timestamp_ms, symbol)
    created_at        DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(entry_at)
ORDER BY (strategy_id, entry_at);

