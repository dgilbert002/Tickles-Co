-- ============================================================================
-- tickles_company.sql — Per-company database template
-- JarvAIs V2.0 — Production-ready DDL
-- ============================================================================
-- Usage: Replace COMPANY_NAME with actual company slug before running.
--   For JarvAIs Trading Co:  sed 's/COMPANY_NAME/jarvais/g' tickles_company.sql | mysql -u root -p
--   For Capital CFD Co:      sed 's/COMPANY_NAME/capital/g' tickles_company.sql | mysql -u root -p
--   For Explorer/Sandbox:    sed 's/COMPANY_NAME/explorer/g' tickles_company.sql | mysql -u root -p
--
-- Requires: MySQL 8.0+, tickles_shared must exist first
-- ============================================================================

CREATE DATABASE IF NOT EXISTS tickles_COMPANY_NAME
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE tickles_COMPANY_NAME;

-- ============================================================================
-- 1. accounts — Exchange accounts for this company
-- Merges: JarvAIs trading_accounts + Capital 2.0 accounts
-- ============================================================================
CREATE TABLE accounts (
  id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
  exchange                VARCHAR(50) NOT NULL,
  account_id_external     VARCHAR(100),
  account_type            ENUM('demo','live') NOT NULL DEFAULT 'demo',
  api_key_ref             VARCHAR(100),
  balance                 DECIMAL(20,8) DEFAULT 0,
  equity                  DECIMAL(20,8) DEFAULT 0,
  margin_used             DECIMAL(20,8) DEFAULT 0,
  currency                VARCHAR(10) NOT NULL DEFAULT 'USD',
  is_active               BOOLEAN NOT NULL DEFAULT TRUE,
  session_state           JSON,
  session_state_version   INT NOT NULL DEFAULT 0,
  last_synced_at          DATETIME(3),
  created_at              DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at              DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_accounts_exchange_ext (exchange, account_id_external)
) ENGINE=InnoDB;

-- ============================================================================
-- 2. trades — THE trade table. Live, paper, shadow — all in one.
-- Merges: JarvAIs trades + live_trades + trade_dossiers + Capital 2.0 actual_trades
-- ============================================================================
CREATE TABLE trades (
  id                        BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id                BIGINT NOT NULL,
  instrument_id             BIGINT NOT NULL, -- FK to tickles_shared.instruments(id)
  strategy_id               BIGINT, -- FK to tickles_shared.strategies(id)
  trade_type                ENUM('live','paper','shadow') NOT NULL DEFAULT 'paper',
  direction                 ENUM('long','short') NOT NULL,
  status                    ENUM('pending','open','partial_close','closed','cancelled','failed') NOT NULL DEFAULT 'pending',

  -- Sizing and margin model
  quantity                  DECIMAL(20,8),
  quantity_type             ENUM('units','lots','contracts') NOT NULL DEFAULT 'units',
  contract_size             DECIMAL(20,8) NOT NULL DEFAULT 1.00000000,
  leverage                  INT NOT NULL DEFAULT 1,

  -- Prices (Rule 2: expected vs actual for slippage)
  entry_price               DECIMAL(20,8),
  exit_price                DECIMAL(20,8),
  expected_entry_price      DECIMAL(20,8),
  expected_exit_price       DECIMAL(20,8),
  stop_loss_price           DECIMAL(20,8),
  take_profit_1             DECIMAL(20,8),
  take_profit_2             DECIMAL(20,8),
  take_profit_3             DECIMAL(20,8),

  -- P&L (Rule 2: gross and net separately)
  gross_pnl                 DECIMAL(20,8),
  net_pnl                   DECIMAL(20,8),

  -- Slippage tracking (Rule 2)
  entry_slippage            DECIMAL(20,8),
  exit_slippage             DECIMAL(20,8),
  entry_slippage_pct        DECIMAL(10,6),
  exit_slippage_pct         DECIMAL(10,6),
  signal_to_fill_ms         INT,
  order_to_fill_ms          INT,

  -- Exchange references
  exchange_order_id         VARCHAR(100),
  exchange_deal_id          VARCHAR(100),

  -- Brain snapshot fields (promoted from JSON per Gemini fix)
  winning_strand_id         BIGINT, -- FK to tickles_shared.strategy_dna_strands(id)
  conflict_exists           BOOLEAN,
  brain_calc_price          DECIMAL(20,8),
  fake_candle_close         DECIMAL(20,8),
  price_variance_pct        DECIMAL(10,6),
  brain_snapshot_detail     JSON,
  brain_snapshot_version    INT NOT NULL DEFAULT 1,

  -- Rule 1 hashes
  candle_data_hash          CHAR(64),
  signal_params_hash        CHAR(64),

  -- Timing
  window_close_time         TIME,
  signal_at                 DATETIME(3),
  ordered_at                DATETIME(3),
  opened_at                 DATETIME(3),
  closed_at                 DATETIME(3),
  created_at                DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at                DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  INDEX idx_trades_account_status (account_id, status),
  INDEX idx_trades_instrument_opened (instrument_id, opened_at),
  INDEX idx_trades_strategy_closed (strategy_id, closed_at),
  INDEX idx_trades_type_status (trade_type, status),
  INDEX idx_trades_exchange_order (exchange_order_id),
  INDEX idx_trades_exchange_deal (exchange_deal_id),
  INDEX idx_trades_candle_hash (candle_data_hash),
  UNIQUE KEY uq_trades_signal_dedup (account_id, instrument_id, signal_params_hash),

  CONSTRAINT fk_trades_account FOREIGN KEY (account_id) REFERENCES accounts(id)
) ENGINE=InnoDB;

-- ============================================================================
-- 3. trade_cost_entries — Unified fee ledger (Rule 2)
-- One row per fee event. Multi-day overnight = one row per night.
-- ============================================================================
CREATE TABLE trade_cost_entries (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_id    BIGINT NOT NULL,
  cost_type   ENUM('maker_fee','taker_fee','spread','overnight_funding','swap','commission','guaranteed_stop','slippage','other') NOT NULL,
  amount      DECIMAL(20,8) NOT NULL,
  currency    VARCHAR(10) NOT NULL DEFAULT 'USD',
  accrued_at  DATETIME(3),
  description VARCHAR(200),
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_costs_trade (trade_id),
  INDEX idx_costs_type_date (cost_type, accrued_at),

  CONSTRAINT fk_costs_trade FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 4. order_events — Full order lifecycle tracking
-- New for V2: neither legacy system had this
-- ============================================================================
CREATE TABLE order_events (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_id            BIGINT NOT NULL,
  event_type          ENUM('submitted','accepted','partial_fill','filled','cancelled','rejected','amended','expired') NOT NULL,
  price               DECIMAL(20,8),
  quantity_filled     DECIMAL(20,8),
  exchange_timestamp  DATETIME(3),
  raw_response        JSON,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_events_trade (trade_id, created_at),

  CONSTRAINT fk_events_trade FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 5. trade_validations — Rule 1 enforcement
-- Per-company (not shared) because it FKs to company trades (Gemini fix #2)
-- ============================================================================
CREATE TABLE trade_validations (
  id                      BIGINT AUTO_INCREMENT PRIMARY KEY,
  trade_id                BIGINT NOT NULL,
  strategy_id             BIGINT NOT NULL,
  backtest_result_id      BIGINT,
  signal_match            BOOLEAN,
  entry_price_delta       DECIMAL(20,8),
  exit_price_delta        DECIMAL(20,8),
  pnl_delta               DECIMAL(20,8),
  pnl_delta_pct           DECIMAL(10,6),
  slippage_contribution   DECIMAL(20,8),
  fee_contribution        DECIMAL(20,8),
  data_drift_detected     BOOLEAN NOT NULL DEFAULT FALSE,
  original_candle_hash    CHAR(64),
  validation_candle_hash  CHAR(64),
  validated_at            DATETIME(3),
  created_at              DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_validations_trade (trade_id),
  INDEX idx_validations_strategy (strategy_id, validated_at),

  CONSTRAINT fk_validations_trade FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================================================================
-- 6. balance_snapshots — Periodic account balance for reconciliation
-- ============================================================================
CREATE TABLE balance_snapshots (
  id                BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id        BIGINT NOT NULL,
  balance           DECIMAL(20,8),
  equity            DECIMAL(20,8),
  margin_used       DECIMAL(20,8),
  unrealized_pnl    DECIMAL(20,8),
  snapshot_source   ENUM('exchange_api','calculated','manual') NOT NULL DEFAULT 'exchange_api',
  snapshot_at       DATETIME(3) NOT NULL,
  created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_balance_account_time (account_id, snapshot_at),

  CONSTRAINT fk_balance_account FOREIGN KEY (account_id) REFERENCES accounts(id)
) ENGINE=InnoDB;

-- ============================================================================
-- 7. leverage_history — Audit trail for leverage changes
-- New for V2: neither legacy system persisted these
-- ============================================================================
CREATE TABLE leverage_history (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  account_id      BIGINT NOT NULL,
  instrument_id   BIGINT NOT NULL, -- FK to tickles_shared.instruments(id)
  old_leverage    INT,
  new_leverage    INT NOT NULL,
  changed_by      VARCHAR(50),
  reason          VARCHAR(200),
  changed_at      DATETIME(3) NOT NULL,
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_leverage_account_time (account_id, changed_at),

  CONSTRAINT fk_leverage_account FOREIGN KEY (account_id) REFERENCES accounts(id)
) ENGINE=InnoDB;

-- ============================================================================
-- 8. agent_state — Per-agent runtime state with optimistic concurrency
-- ============================================================================
CREATE TABLE agent_state (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_name          VARCHAR(100) NOT NULL,
  status              ENUM('active','paused','error','stopped') NOT NULL DEFAULT 'stopped',
  last_heartbeat_at   DATETIME(3),
  last_error          TEXT,
  state_data          JSON,
  state_version       INT NOT NULL DEFAULT 0,
  created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_agent_name (agent_name)
) ENGINE=InnoDB;

-- ============================================================================
-- 9. strategy_lifecycle — Audit trail of strategy status transitions
-- ============================================================================
CREATE TABLE strategy_lifecycle (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  strategy_id   BIGINT NOT NULL,
  from_status   VARCHAR(30),
  to_status     VARCHAR(30) NOT NULL,
  changed_by    VARCHAR(100),
  reason        TEXT,
  changed_at    DATETIME(3) NOT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),

  INDEX idx_lifecycle_strategy (strategy_id, changed_at)
) ENGINE=InnoDB;

-- ============================================================================
-- 10. company_config — Per-company settings
-- ============================================================================
CREATE TABLE company_config (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  config_key    VARCHAR(100) NOT NULL,
  config_value  TEXT,
  updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),

  UNIQUE KEY uq_company_config_key (config_key)
) ENGINE=InnoDB;

-- ============================================================================
-- Seed default company_config entries
-- ============================================================================
INSERT INTO company_config (config_key, config_value) VALUES
  ('company_name', 'COMPANY_NAME'),
  ('max_drawdown_pct', '25'),
  ('max_daily_trades', '20'),
  ('max_concurrent_positions', '5'),
  ('trading_capital', '500'),
  ('risk_per_trade_pct', '2'),
  ('approval_mode', 'human_all'),
  ('halt_threshold_pct', '80'),
  ('llm_budget_monthly_usd', '200');
