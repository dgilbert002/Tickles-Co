-- ============================================================================
-- tickles_shared.sql — Shared database for all trading companies
-- JarvAIs V2.0 — Production-ready DDL
-- ============================================================================
-- Run: mysql -u root -p < tickles_shared.sql
-- Requires: MySQL 8.0+ (for RANGE partitioning, DATETIME(3), JSON)
-- ============================================================================

CREATE DATABASE IF NOT EXISTS tickles_shared
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE tickles_shared;

-- ============================================================================
-- 1. instruments — Universal instrument registry
-- Merges: JarvAIs market_symbols + Capital 2.0 epics + marketInfo
-- ============================================================================
CREATE TABLE instruments (
  id                          BIGINT AUTO_INCREMENT PRIMARY KEY,
  symbol                      VARCHAR(50) NOT NULL,
  exchange                    VARCHAR(50) NOT NULL,
  asset_class                 ENUM('crypto','cfd','stock','forex','commodity','index') NOT NULL,
  base_currency               VARCHAR(20),
  quote_currency              VARCHAR(20),
  min_size                    DECIMAL(20,8),
  max_size                    DECIMAL(20,8),
  size_increment              DECIMAL(20,8),
  contract_multiplier         DECIMAL(20,8) NOT NULL DEFAULT 1.00000000,
  spread_pct                  DECIMAL(10,6),
  maker_fee_pct               DECIMAL(10,6),
  taker_fee_pct               DECIMAL(10,6),
  overnight_funding_long_pct  DECIMAL(15,10),
  overnight_funding_short_pct DECIMAL(15,10),
  margin_factor               DECIMAL(10,4),
  max_leverage                INT,
  opening_hours               JSON,
  is_active                   BOOLEAN NOT NULL DEFAULT TRUE,
  last_synced_at              DATETIME(3),
  created_at                  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at                  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_instruments_symbol_exchange (symbol, exchange),
  INDEX idx_instruments_asset_class (asset_class),
  INDEX idx_instruments_active (is_active)
) ENGINE=InnoDB;

-- ============================================================================
-- 2. candles — All OHLCV data, all instruments, all timeframes
-- Merges: JarvAIs candles + Capital 2.0 candles
-- PARTITIONED by month for 1m data performance (Rule 3)
-- ============================================================================
CREATE TABLE candles (
  id                    BIGINT AUTO_INCREMENT,
  instrument_id         BIGINT NOT NULL,
  timeframe             ENUM('1m','5m','15m','30m','1h','4h','1d','1w') NOT NULL,
  source                VARCHAR(30) NOT NULL,
  timestamp             DATETIME(3) NOT NULL,
  `open`                DECIMAL(20,8) NOT NULL,
  high                  DECIMAL(20,8) NOT NULL,
  low                   DECIMAL(20,8) NOT NULL,
  `close`               DECIMAL(20,8) NOT NULL,
  volume                DECIMAL(30,8),
  open_bid              DECIMAL(20,8),
  close_ask             DECIMAL(20,8),
  is_fake               BOOLEAN NOT NULL DEFAULT FALSE,
  fake_source_timestamp DATETIME(3),
  fake_comment          VARCHAR(200),
  data_hash             CHAR(64),
  created_at            DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  PRIMARY KEY (id, timestamp),
  UNIQUE KEY uq_candles_composite (instrument_id, source, timeframe, timestamp),
  INDEX idx_candles_instrument_tf (instrument_id, timeframe),
  INDEX idx_candles_timestamp (timestamp)
) ENGINE=InnoDB
PARTITION BY RANGE (TO_DAYS(timestamp)) (
  PARTITION p2024_01 VALUES LESS THAN (TO_DAYS('2024-02-01')),
  PARTITION p2024_02 VALUES LESS THAN (TO_DAYS('2024-03-01')),
  PARTITION p2024_03 VALUES LESS THAN (TO_DAYS('2024-04-01')),
  PARTITION p2024_04 VALUES LESS THAN (TO_DAYS('2024-05-01')),
  PARTITION p2024_05 VALUES LESS THAN (TO_DAYS('2024-06-01')),
  PARTITION p2024_06 VALUES LESS THAN (TO_DAYS('2024-07-01')),
  PARTITION p2024_07 VALUES LESS THAN (TO_DAYS('2024-08-01')),
  PARTITION p2024_08 VALUES LESS THAN (TO_DAYS('2024-09-01')),
  PARTITION p2024_09 VALUES LESS THAN (TO_DAYS('2024-10-01')),
  PARTITION p2024_10 VALUES LESS THAN (TO_DAYS('2024-11-01')),
  PARTITION p2024_11 VALUES LESS THAN (TO_DAYS('2024-12-01')),
  PARTITION p2024_12 VALUES LESS THAN (TO_DAYS('2025-01-01')),
  PARTITION p2025_01 VALUES LESS THAN (TO_DAYS('2025-02-01')),
  PARTITION p2025_02 VALUES LESS THAN (TO_DAYS('2025-03-01')),
  PARTITION p2025_03 VALUES LESS THAN (TO_DAYS('2025-04-01')),
  PARTITION p2025_04 VALUES LESS THAN (TO_DAYS('2025-05-01')),
  PARTITION p2025_05 VALUES LESS THAN (TO_DAYS('2025-06-01')),
  PARTITION p2025_06 VALUES LESS THAN (TO_DAYS('2025-07-01')),
  PARTITION p2025_07 VALUES LESS THAN (TO_DAYS('2025-08-01')),
  PARTITION p2025_08 VALUES LESS THAN (TO_DAYS('2025-09-01')),
  PARTITION p2025_09 VALUES LESS THAN (TO_DAYS('2025-10-01')),
  PARTITION p2025_10 VALUES LESS THAN (TO_DAYS('2025-11-01')),
  PARTITION p2025_11 VALUES LESS THAN (TO_DAYS('2025-12-01')),
  PARTITION p2025_12 VALUES LESS THAN (TO_DAYS('2026-01-01')),
  PARTITION p2026_01 VALUES LESS THAN (TO_DAYS('2026-02-01')),
  PARTITION p2026_02 VALUES LESS THAN (TO_DAYS('2026-03-01')),
  PARTITION p2026_03 VALUES LESS THAN (TO_DAYS('2026-04-01')),
  PARTITION p2026_04 VALUES LESS THAN (TO_DAYS('2026-05-01')),
  PARTITION p2026_05 VALUES LESS THAN (TO_DAYS('2026-06-01')),
  PARTITION p2026_06 VALUES LESS THAN (TO_DAYS('2026-07-01')),
  PARTITION p2026_07 VALUES LESS THAN (TO_DAYS('2026-08-01')),
  PARTITION p2026_08 VALUES LESS THAN (TO_DAYS('2026-09-01')),
  PARTITION p2026_09 VALUES LESS THAN (TO_DAYS('2026-10-01')),
  PARTITION p2026_10 VALUES LESS THAN (TO_DAYS('2026-11-01')),
  PARTITION p2026_11 VALUES LESS THAN (TO_DAYS('2026-12-01')),
  PARTITION p2026_12 VALUES LESS THAN (TO_DAYS('2027-01-01')),
  PARTITION p_future VALUES LESS THAN MAXVALUE
);

-- ============================================================================
-- 3. indicator_catalog — Registry of all ~250 indicators
-- ============================================================================
CREATE TABLE indicator_catalog (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  name            VARCHAR(100) NOT NULL,
  category        ENUM('momentum','trend','volatility','volume','smart_money','breakout','pullback','crash_protection','combination') NOT NULL,
  direction       ENUM('bullish','bearish','neutral') NOT NULL,
  description     TEXT,
  default_params  JSON,
  param_ranges    JSON,
  source_system   VARCHAR(30),
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_indicator_catalog_name (name)
) ENGINE=InnoDB;

-- ============================================================================
-- 4. indicators — Computed indicator values (cached for reuse/audit)
-- ============================================================================
CREATE TABLE indicators (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id    BIGINT NOT NULL,
  timeframe        ENUM('1m','5m','15m','30m','1h','4h','1d','1w') NOT NULL,
  indicator_name   VARCHAR(100) NOT NULL,
  params_hash      CHAR(64),
  params           JSON,
  `signal`         BOOLEAN,
  value            DECIMAL(20,8),
  metadata         JSON,
  calculated_at    DATETIME(3) NOT NULL,
  candle_timestamp DATETIME(3) NOT NULL,
  created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_indicator_lookup (instrument_id, timeframe, indicator_name, params_hash),
  INDEX idx_indicators_lookup (instrument_id, indicator_name, timeframe),
  INDEX idx_indicators_hash (params_hash),
  CONSTRAINT fk_indicators_instrument FOREIGN KEY (instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 5. strategies — Strategy definitions
-- Merges: Capital 2.0 savedStrategies + strategy_templates
-- ============================================================================
CREATE TABLE strategies (
  id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
  name                 VARCHAR(200) NOT NULL,
  description          TEXT,
  instrument_id        BIGINT, -- Note: cross-database FK to tickles_shared.instruments not enforced
  asset_class          ENUM('crypto','cfd','stock','forex','commodity','index'),
  conflict_resolution  ENUM('sharpe','return','win_rate','first_signal') NOT NULL DEFAULT 'sharpe',
  halt_threshold_pct   DECIMAL(5,2) NOT NULL DEFAULT 80.00,
  is_active            BOOLEAN NOT NULL DEFAULT TRUE,
  is_archived          BOOLEAN NOT NULL DEFAULT FALSE,
  created_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at           DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  INDEX idx_strategies_active_class (is_active, asset_class)
) ENGINE=InnoDB;

-- ============================================================================
-- 6. strategy_dna_strands — Normalized from JSON (Capital 2.0 dnaStrands)
-- Each row = one indicator config attached to a strategy
-- ============================================================================
CREATE TABLE strategy_dna_strands (
  id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
  strategy_id           BIGINT NOT NULL,
  indicator_catalog_id  INT NOT NULL,
  timeframe             ENUM('1m','5m','15m','30m','1h','4h','1d','1w') NOT NULL DEFAULT '5m',
  params                JSON,
  params_hash           CHAR(64),
  source_backtest_id    BIGINT,
  priority              INT NOT NULL DEFAULT 0,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_strand_dedup (strategy_id, indicator_catalog_id, timeframe, params_hash),
  INDEX idx_strands_indicator (indicator_catalog_id),
  INDEX idx_strands_strategy (strategy_id),

  CONSTRAINT fk_strands_strategy FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
  CONSTRAINT fk_strands_catalog FOREIGN KEY (indicator_catalog_id) REFERENCES indicator_catalog(id) ON DELETE RESTRICT
) ENGINE=InnoDB;

-- ============================================================================
-- 7. strategy_windows — Normalized from JSON (Capital 2.0 windowConfig)
-- ============================================================================
CREATE TABLE strategy_windows (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  strategy_id         BIGINT NOT NULL,
  window_close_time   TIME NOT NULL,
  allocation_pct      DECIMAL(5,2) NOT NULL,
  carry_over_enabled  BOOLEAN NOT NULL DEFAULT FALSE,
  is_active           BOOLEAN NOT NULL DEFAULT TRUE,

  UNIQUE KEY uq_window_dedup (strategy_id, window_close_time),

  CONSTRAINT fk_windows_strategy FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 8. backtest_results — One row per completed backtest, dedup by param_hash
-- Merges: Capital 2.0 backtestResults + testedHashes
-- ============================================================================
CREATE TABLE backtest_results (
  id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id         BIGINT NOT NULL, -- Note: cross-database FK to tickles_shared.instruments not enforced
  indicator_name        VARCHAR(100) NOT NULL,
  param_hash            CHAR(64) NOT NULL,
  params                JSON NOT NULL,
  timeframe             ENUM('1m','5m','15m','30m','1h','4h','1d','1w') NOT NULL DEFAULT '5m',
  date_from             DATE NOT NULL,
  date_to               DATE NOT NULL,
  initial_balance       DECIMAL(20,8),
  final_balance         DECIMAL(20,8),
  total_return_pct      DECIMAL(10,4),
  total_trades          INT,
  win_rate_pct          DECIMAL(5,2),
  sharpe_ratio          DECIMAL(10,4),
  max_drawdown_pct      DECIMAL(10,4),
  profit_factor         DECIMAL(10,4),
  total_fees            DECIMAL(20,8) DEFAULT 0,
  total_spread_costs    DECIMAL(20,8) DEFAULT 0,
  total_overnight_costs DECIMAL(20,8) DEFAULT 0,
  candle_data_hash      CHAR(64),
  engine_version        VARCHAR(20),
  run_duration_ms       INT,
  created_at            DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_backtest_param_hash (param_hash),
  INDEX idx_backtest_instrument (instrument_id, indicator_name),
  INDEX idx_backtest_return (total_return_pct),
  INDEX idx_backtest_sharpe (sharpe_ratio)
) ENGINE=InnoDB;

-- ============================================================================
-- 9. backtest_trade_details — Normalized from backtestResults.trades JSON
-- ============================================================================
CREATE TABLE backtest_trade_details (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  backtest_result_id  BIGINT NOT NULL,
  trade_index         INT NOT NULL,
  entry_price         DECIMAL(20,8) NOT NULL,
  exit_price          DECIMAL(20,8),
  direction           ENUM('long','short') NOT NULL,
  entry_at            DATETIME(3),
  exit_at             DATETIME(3),
  quantity            DECIMAL(20,8),
  gross_pnl           DECIMAL(20,8),
  spread_cost         DECIMAL(20,8) DEFAULT 0,
  overnight_cost      DECIMAL(20,8) DEFAULT 0,
  net_pnl             DECIMAL(20,8),
  is_winner           BOOLEAN,
  window_close_time   TIME,
  signal_candle_hash  CHAR(64),

  INDEX idx_bt_details_result (backtest_result_id),

  CONSTRAINT fk_bt_details_result FOREIGN KEY (backtest_result_id) REFERENCES backtest_results(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 10. backtest_queue — Work queue for parallel backtest execution
-- ============================================================================
CREATE TABLE backtest_queue (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  param_hash      CHAR(64) NOT NULL,
  instrument_id   BIGINT,
  indicator_name  VARCHAR(100),
  params          JSON,
  status          ENUM('pending','claimed','running','completed','failed') NOT NULL DEFAULT 'pending',
  worker_id       VARCHAR(50),
  claimed_at      DATETIME(3),
  completed_at    DATETIME(3),
  result_id       BIGINT,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_queue_param_hash (param_hash),
  INDEX idx_queue_status (status, claimed_at)
) ENGINE=InnoDB;

-- ============================================================================
-- 11. news_items — All ingested news, social, TradingView ideas
-- From: JarvAIs news_items
-- ============================================================================
CREATE TABLE news_items (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  hash_key      CHAR(64) NOT NULL,
  source        VARCHAR(50) NOT NULL,
  headline      TEXT,
  content       MEDIUMTEXT,
  sentiment     ENUM('bullish','bearish','neutral','mixed'),
  instruments   JSON,
  published_at  DATETIME(3),
  collected_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_news_hash (hash_key),
  INDEX idx_news_source_date (source, collected_at),
  INDEX idx_news_published (published_at)
) ENGINE=InnoDB;

-- ============================================================================
-- 12. derivatives_snapshots — OI, funding, liquidation (CoinGlass etc.)
-- New for V2
-- ============================================================================
CREATE TABLE derivatives_snapshots (
  id                       BIGINT AUTO_INCREMENT PRIMARY KEY,
  instrument_id            BIGINT NOT NULL, -- Note: cross-database FK to tickles_shared.instruments not enforced
  snapshot_at              DATETIME(3) NOT NULL,
  open_interest            DECIMAL(30,8),
  funding_rate             DECIMAL(15,10),
  long_short_ratio         DECIMAL(10,4),
  liquidation_volume_24h   DECIMAL(30,8),
  source                   VARCHAR(50) NOT NULL,
  created_at               DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_deriv_snapshot (instrument_id, source, snapshot_at),
  INDEX idx_deriv_instrument_time (instrument_id, snapshot_at)
) ENGINE=InnoDB;

-- ============================================================================
-- 13. system_config — Global key-value with namespaces
-- Merges: JarvAIs system_config + config + Capital 2.0 settings
-- ============================================================================
CREATE TABLE system_config (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  namespace     VARCHAR(50) NOT NULL,
  config_key    VARCHAR(100) NOT NULL,
  config_value  TEXT,
  is_secret     BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_config_ns_key (namespace, config_key)
) ENGINE=InnoDB;

-- ============================================================================
-- 14. api_cost_log — Every external API call with cost attribution
-- Merges: JarvAIs ai_api_log + Capital 2.0 apiCallLogs
-- ============================================================================
CREATE TABLE api_cost_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  provider    VARCHAR(50) NOT NULL,
  model       VARCHAR(100),
  role        VARCHAR(50),
  context     VARCHAR(100),
  tokens_in   INT DEFAULT 0,
  tokens_out  INT DEFAULT 0,
  cost_usd    DECIMAL(10,6) DEFAULT 0,
  latency_ms  INT,
  company_id  VARCHAR(50), -- Note: cross-database FK to tickles_[company].accounts not enforced
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_cost_company_date (company_id, created_at),
  INDEX idx_cost_role_date (role, created_at)
) ENGINE=InnoDB;

-- ============================================================================
-- Seed default system_config entries
-- ============================================================================
INSERT INTO system_config (namespace, config_key, config_value) VALUES
  ('global', 'version', '2.0.0'),
  ('global', 'environment', 'development'),
  ('backtest', 'max_concurrent_workers', '6'),
  ('backtest', 'default_initial_balance', '500'),
  ('risk', 'max_concurrent_llm_calls', '4'),
  ('risk', 'global_max_drawdown_pct', '25'),
  ('indicators', 'cache_max_entries', '200'),
  ('indicators', 'cache_ttl_seconds', '900'),
  ('candles', 'cache_max_entries', '500'),
  ('candles', 'cache_ttl_seconds', '300'),
  ('candles', 'retention_1m_days', '30'),
  ('candles', 'retention_5m_days', '90'),
  ('candles', 'retention_15m_days', '180'),
  ('candles', 'retention_1h_days', '730'),
  ('candles', 'retention_4h_days', '0'),
  ('candles', 'retention_1d_days', '0'),
  ('db', 'pool_size_per_service', '10'),
  ('db', 'pool_max_total', '50');
