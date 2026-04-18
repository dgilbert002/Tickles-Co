-- ============================================================================
-- tickles_shared — Postgres 16 DDL
-- JarvAIs V2.0 — Production-ready, Postgres-native
-- Translated from tickles_shared.sql (MySQL) with improvements:
--   * DATETIME(3) -> TIMESTAMPTZ(3) (timezone-aware, millisecond precision)
--   * JSON -> JSONB (indexable, compressed)
--   * ENUM types at schema level (reused across tables)
--   * Declarative partitioning on candles
--   * BRIN index on candles.timestamp (great for time-series at scale)
--   * Generic updated_at trigger function
-- ============================================================================
-- Run: PGPASSWORD='Tickles21!' psql -h 127.0.0.1 -U admin -d tickles_shared -f tickles_shared_pg.sql
-- ============================================================================

\c tickles_shared

-- Keep pgvector ready (already installed during bootstrap)
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- Generic updated_at trigger (reused across tables)
-- Postgres has no "ON UPDATE CURRENT_TIMESTAMP" — we use a trigger.
-- ============================================================================
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Enum types (schema-level, reused)
-- ============================================================================
DO $$ BEGIN
  CREATE TYPE timeframe_t AS ENUM ('1m','5m','15m','30m','1h','4h','1d','1w');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE asset_class_t AS ENUM ('crypto','cfd','stock','forex','commodity','index');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE direction_t AS ENUM ('long','short');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE indicator_category_t AS ENUM (
    'momentum','trend','volatility','volume','smart_money',
    'breakout','pullback','crash_protection','combination'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE indicator_direction_t AS ENUM ('bullish','bearish','neutral');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE sentiment_t AS ENUM ('bullish','bearish','neutral','mixed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE conflict_resolution_t AS ENUM ('sharpe','return','win_rate','first_signal');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE backtest_status_t AS ENUM ('pending','claimed','running','completed','failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================================
-- 1. instruments — Universal instrument registry
-- ============================================================================
CREATE TABLE instruments (
  id                          BIGSERIAL PRIMARY KEY,
  symbol                      VARCHAR(50) NOT NULL,
  exchange                    VARCHAR(50) NOT NULL,
  asset_class                 asset_class_t NOT NULL,
  base_currency               VARCHAR(20),
  quote_currency              VARCHAR(20),
  min_size                    NUMERIC(20,8),
  max_size                    NUMERIC(20,8),
  size_increment              NUMERIC(20,8),
  contract_multiplier         NUMERIC(20,8) NOT NULL DEFAULT 1.00000000,
  spread_pct                  NUMERIC(10,6),
  maker_fee_pct               NUMERIC(10,6),
  taker_fee_pct               NUMERIC(10,6),
  overnight_funding_long_pct  NUMERIC(15,10),
  overnight_funding_short_pct NUMERIC(15,10),
  margin_factor               NUMERIC(10,4),
  max_leverage                INT,
  opening_hours               JSONB,
  is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
  last_synced_at              TIMESTAMPTZ(3),
  created_at                  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at                  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_instruments_symbol_exchange UNIQUE (symbol, exchange)
);
CREATE INDEX idx_instruments_asset_class ON instruments(asset_class);
CREATE INDEX idx_instruments_active ON instruments(is_active) WHERE is_active;
CREATE TRIGGER trg_instruments_updated BEFORE UPDATE ON instruments
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 2. candles — All OHLCV data. PARTITIONED by month on timestamp (native PG).
-- Per owner's "store 1m, roll up everything" decision, this mostly holds 1m data.
-- ============================================================================
CREATE TABLE candles (
  id                    BIGSERIAL,
  instrument_id         BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  timeframe             timeframe_t NOT NULL,
  source                VARCHAR(30) NOT NULL,
  "timestamp"           TIMESTAMPTZ(3) NOT NULL,
  "open"                NUMERIC(20,8) NOT NULL,
  high                  NUMERIC(20,8) NOT NULL,
  low                   NUMERIC(20,8) NOT NULL,
  "close"               NUMERIC(20,8) NOT NULL,
  volume                NUMERIC(30,8),
  open_bid              NUMERIC(20,8),
  close_ask             NUMERIC(20,8),
  is_fake               BOOLEAN NOT NULL DEFAULT FALSE,
  fake_source_timestamp TIMESTAMPTZ(3),
  fake_comment          VARCHAR(200),
  data_hash             CHAR(64),
  created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id, "timestamp"),
  CONSTRAINT uq_candles_composite UNIQUE (instrument_id, source, timeframe, "timestamp")
) PARTITION BY RANGE ("timestamp");

-- Create monthly partitions 2024-01 through 2027-01
-- (extensible later via maintenance job)
DO $$
DECLARE
  start_date DATE := '2024-01-01';
  end_date   DATE := '2027-01-01';
  cur_date   DATE := start_date;
  next_date  DATE;
  part_name  TEXT;
BEGIN
  WHILE cur_date < end_date LOOP
    next_date := (cur_date + INTERVAL '1 month')::DATE;
    part_name := 'candles_' || TO_CHAR(cur_date, 'YYYY_MM');
    EXECUTE format(
      'CREATE TABLE IF NOT EXISTS %I PARTITION OF candles FOR VALUES FROM (%L) TO (%L)',
      part_name, cur_date, next_date
    );
    cur_date := next_date;
  END LOOP;
  -- Catch-all for anything beyond 2027-01
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS candles_future PARTITION OF candles FOR VALUES FROM (%L) TO (%L)',
    end_date, '9999-12-31'
  );
END $$;

CREATE INDEX idx_candles_instrument_tf ON candles(instrument_id, timeframe);
-- BRIN is ideal for time-ordered inserts (candles always append) — tiny index, fast range scans
CREATE INDEX idx_candles_timestamp_brin ON candles USING BRIN ("timestamp");

-- ============================================================================
-- 3. indicator_catalog
-- ============================================================================
CREATE TABLE indicator_catalog (
  id              SERIAL PRIMARY KEY,
  name            VARCHAR(100) NOT NULL,
  category        indicator_category_t NOT NULL,
  direction       indicator_direction_t NOT NULL,
  description     TEXT,
  default_params  JSONB,
  param_ranges    JSONB,
  source_system   VARCHAR(30),
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_indicator_catalog_name UNIQUE (name)
);
CREATE TRIGGER trg_indicator_catalog_updated BEFORE UPDATE ON indicator_catalog
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 4. indicators — Cached computed indicator values
-- ============================================================================
CREATE TABLE indicators (
  id               BIGSERIAL PRIMARY KEY,
  instrument_id    BIGINT NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
  timeframe        timeframe_t NOT NULL,
  indicator_name   VARCHAR(100) NOT NULL,
  params_hash      CHAR(64),
  params           JSONB,
  "signal"         BOOLEAN,
  value            NUMERIC(20,8),
  metadata         JSONB,
  calculated_at    TIMESTAMPTZ(3) NOT NULL,
  candle_timestamp TIMESTAMPTZ(3) NOT NULL,
  created_at       TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_indicator_lookup UNIQUE (instrument_id, timeframe, indicator_name, params_hash)
);
CREATE INDEX idx_indicators_lookup ON indicators(instrument_id, indicator_name, timeframe);
CREATE INDEX idx_indicators_hash ON indicators(params_hash);

-- ============================================================================
-- 5. strategies
-- ============================================================================
CREATE TABLE strategies (
  id                   BIGSERIAL PRIMARY KEY,
  name                 VARCHAR(200) NOT NULL,
  description          TEXT,
  instrument_id        BIGINT,  -- not FK-enforced (may reference archived/deleted)
  asset_class          asset_class_t,
  conflict_resolution  conflict_resolution_t NOT NULL DEFAULT 'sharpe',
  halt_threshold_pct   NUMERIC(5,2) NOT NULL DEFAULT 80.00,
  is_active            BOOLEAN NOT NULL DEFAULT TRUE,
  is_archived          BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_strategies_active_class ON strategies(is_active, asset_class) WHERE is_active;
CREATE TRIGGER trg_strategies_updated BEFORE UPDATE ON strategies
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 6. strategy_dna_strands
-- ============================================================================
CREATE TABLE strategy_dna_strands (
  id                    BIGSERIAL PRIMARY KEY,
  strategy_id           BIGINT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  indicator_catalog_id  INT NOT NULL REFERENCES indicator_catalog(id) ON DELETE RESTRICT,
  timeframe             timeframe_t NOT NULL DEFAULT '5m',
  params                JSONB,
  params_hash           CHAR(64),
  source_backtest_id    BIGINT,
  priority              INT NOT NULL DEFAULT 0,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strand_dedup UNIQUE (strategy_id, indicator_catalog_id, timeframe, params_hash)
);
CREATE INDEX idx_strands_indicator ON strategy_dna_strands(indicator_catalog_id);
CREATE INDEX idx_strands_strategy ON strategy_dna_strands(strategy_id);

-- ============================================================================
-- 7. strategy_windows
-- ============================================================================
CREATE TABLE strategy_windows (
  id                  BIGSERIAL PRIMARY KEY,
  strategy_id         BIGINT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  window_close_time   TIME NOT NULL,
  allocation_pct      NUMERIC(5,2) NOT NULL,
  carry_over_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,
  CONSTRAINT uq_window_dedup UNIQUE (strategy_id, window_close_time)
);

-- ============================================================================
-- 8. backtest_results — governance/warm-tier (ClickHouse holds the raw sweep)
-- ============================================================================
CREATE TABLE backtest_results (
  id                    BIGSERIAL PRIMARY KEY,
  instrument_id         BIGINT NOT NULL,
  indicator_name        VARCHAR(100) NOT NULL,
  param_hash            CHAR(64) NOT NULL,
  params                JSONB NOT NULL,
  timeframe             timeframe_t NOT NULL DEFAULT '5m',
  date_from             DATE NOT NULL,
  date_to               DATE NOT NULL,
  initial_balance       NUMERIC(20,8),
  final_balance         NUMERIC(20,8),
  total_return_pct      NUMERIC(10,4),
  total_trades          INT,
  win_rate_pct          NUMERIC(5,2),
  sharpe_ratio          NUMERIC(10,4),
  max_drawdown_pct      NUMERIC(10,4),
  profit_factor         NUMERIC(10,4),
  total_fees            NUMERIC(20,8) DEFAULT 0,
  total_spread_costs    NUMERIC(20,8) DEFAULT 0,
  total_overnight_costs NUMERIC(20,8) DEFAULT 0,
  candle_data_hash      CHAR(64),
  engine_version        VARCHAR(20),
  run_duration_ms       INT,
  -- Promotion status for strategy genealogy (Phase 8 guardrails)
  promotion_status      VARCHAR(30) DEFAULT 'candidate',  -- candidate, approved, live, archived
  parent_strategy_id    BIGINT,
  deflated_sharpe       NUMERIC(10,4),  -- for multiple-hypothesis adjustment
  oos_sharpe            NUMERIC(10,4),
  oos_return_pct        NUMERIC(10,4),
  verified_by           VARCHAR(100),
  verified_at           TIMESTAMPTZ(3),
  clickhouse_run_id     UUID,  -- cross-reference to ClickHouse raw runs
  created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_backtest_param_hash UNIQUE (param_hash)
);
CREATE INDEX idx_backtest_instrument ON backtest_results(instrument_id, indicator_name);
CREATE INDEX idx_backtest_return ON backtest_results(total_return_pct);
CREATE INDEX idx_backtest_sharpe ON backtest_results(sharpe_ratio);
CREATE INDEX idx_backtest_promotion ON backtest_results(promotion_status, sharpe_ratio);
CREATE INDEX idx_backtest_parent ON backtest_results(parent_strategy_id);

-- ============================================================================
-- 9. backtest_trade_details
-- ============================================================================
CREATE TABLE backtest_trade_details (
  id                  BIGSERIAL PRIMARY KEY,
  backtest_result_id  BIGINT NOT NULL REFERENCES backtest_results(id) ON DELETE CASCADE,
  trade_index         INT NOT NULL,
  entry_price         NUMERIC(20,8) NOT NULL,
  exit_price          NUMERIC(20,8),
  direction           direction_t NOT NULL,
  entry_at            TIMESTAMPTZ(3),
  exit_at             TIMESTAMPTZ(3),
  quantity            NUMERIC(20,8),
  gross_pnl           NUMERIC(20,8),
  spread_cost         NUMERIC(20,8) DEFAULT 0,
  overnight_cost      NUMERIC(20,8) DEFAULT 0,
  net_pnl             NUMERIC(20,8),
  is_winner           BOOLEAN,
  window_close_time   TIME,
  signal_candle_hash  CHAR(64)
);
CREATE INDEX idx_bt_details_result ON backtest_trade_details(backtest_result_id);

-- ============================================================================
-- 10. backtest_queue
-- ============================================================================
CREATE TABLE backtest_queue (
  id              BIGSERIAL PRIMARY KEY,
  param_hash      CHAR(64) NOT NULL,
  instrument_id   BIGINT,
  indicator_name  VARCHAR(100),
  params          JSONB,
  status          backtest_status_t NOT NULL DEFAULT 'pending',
  worker_id       VARCHAR(50),
  claimed_at      TIMESTAMPTZ(3),
  completed_at    TIMESTAMPTZ(3),
  result_id       BIGINT,
  created_at      TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_queue_param_hash UNIQUE (param_hash)
);
CREATE INDEX idx_queue_status ON backtest_queue(status, claimed_at);

-- ============================================================================
-- 11. news_items
-- ============================================================================
CREATE TABLE news_items (
  id            BIGSERIAL PRIMARY KEY,
  hash_key      CHAR(64) NOT NULL,
  source        VARCHAR(50) NOT NULL,
  headline      TEXT,
  content       TEXT,
  sentiment     sentiment_t,
  instruments   JSONB,
  published_at  TIMESTAMPTZ(3),
  collected_at  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_news_hash UNIQUE (hash_key)
);
CREATE INDEX idx_news_source_date ON news_items(source, collected_at);
CREATE INDEX idx_news_published ON news_items(published_at);
-- GIN index for JSONB instrument extraction
CREATE INDEX idx_news_instruments ON news_items USING GIN (instruments);

-- ============================================================================
-- 12. derivatives_snapshots
-- ============================================================================
CREATE TABLE derivatives_snapshots (
  id                       BIGSERIAL PRIMARY KEY,
  instrument_id            BIGINT NOT NULL,
  snapshot_at              TIMESTAMPTZ(3) NOT NULL,
  open_interest            NUMERIC(30,8),
  funding_rate             NUMERIC(15,10),
  long_short_ratio         NUMERIC(10,4),
  liquidation_volume_24h   NUMERIC(30,8),
  source                   VARCHAR(50) NOT NULL,
  created_at               TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_deriv_snapshot UNIQUE (instrument_id, source, snapshot_at)
);
CREATE INDEX idx_deriv_instrument_time ON derivatives_snapshots(instrument_id, snapshot_at);

-- ============================================================================
-- 13. system_config
-- ============================================================================
CREATE TABLE system_config (
  id            SERIAL PRIMARY KEY,
  namespace     VARCHAR(50) NOT NULL,
  config_key    VARCHAR(100) NOT NULL,
  config_value  TEXT,
  is_secret     BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_config_ns_key UNIQUE (namespace, config_key)
);
CREATE TRIGGER trg_system_config_updated BEFORE UPDATE ON system_config
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 14. api_cost_log
-- ============================================================================
CREATE TABLE api_cost_log (
  id          BIGSERIAL PRIMARY KEY,
  provider    VARCHAR(50) NOT NULL,
  model       VARCHAR(100),
  role        VARCHAR(50),
  context     VARCHAR(100),
  tokens_in   INT DEFAULT 0,
  tokens_out  INT DEFAULT 0,
  cost_usd    NUMERIC(10,6) DEFAULT 0,
  latency_ms  INT,
  company_id  VARCHAR(50),
  created_at  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_cost_company_date ON api_cost_log(company_id, created_at);
CREATE INDEX idx_cost_role_date ON api_cost_log(role, created_at);

-- ============================================================================
-- Seed default system_config
-- ============================================================================
INSERT INTO system_config (namespace, config_key, config_value) VALUES
  ('global', 'version', '2.0.0'),
  ('global', 'environment', 'development'),
  ('global', 'db_engine', 'postgres'),
  ('global', 'db_migrated_at', CURRENT_TIMESTAMP::TEXT),
  ('backtest', 'max_concurrent_workers', '6'),
  ('backtest', 'default_initial_balance', '500'),
  ('backtest', 'clickhouse_enabled', 'true'),
  ('risk', 'max_concurrent_llm_calls', '4'),
  ('risk', 'global_max_drawdown_pct', '25'),
  ('indicators', 'cache_max_entries', '200'),
  ('indicators', 'cache_ttl_seconds', '900'),
  ('candles', 'cache_max_entries', '500'),
  ('candles', 'cache_ttl_seconds', '300'),
  ('candles', 'retention_1m_days', '90'),
  ('candles', 'retention_5m_days', '0'),
  ('candles', 'retention_15m_days', '0'),
  ('candles', 'retention_1h_days', '0'),
  ('candles', 'retention_4h_days', '0'),
  ('candles', 'retention_1d_days', '0'),
  ('db', 'pool_size_per_service', '10'),
  ('db', 'pool_max_total', '50'),
  ('guardrails', 'approval_mode', 'human_all'),
  ('guardrails', 'daily_loss_killswitch_usd', '50'),
  ('guardrails', 'daily_loss_killswitch_pct', '10')
ON CONFLICT (namespace, config_key) DO NOTHING;

-- ============================================================================
-- Grants: schemy gets read on everything (observer role)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO schemy;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO schemy;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO schemy;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO schemy;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO schemy;

-- ============================================================================
-- Done
-- ============================================================================
SELECT 'tickles_shared migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE schemaname='public') AS tables,
       (SELECT COUNT(*) FROM system_config) AS config_rows;
