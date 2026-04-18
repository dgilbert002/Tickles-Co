-- ============================================================================
-- tickles_[company] — Postgres 16 DDL (company-scoped)
-- JarvAIs V2.0 — Per-company database template
-- ============================================================================
-- Usage: Replace COMPANY_NAME with actual company slug before running:
--   sed 's/COMPANY_NAME/jarvais/g' tickles_company_pg.sql | psql ...
-- ============================================================================
-- Assumes tickles_[company] DB already exists and admin owns it.
-- ============================================================================

\c tickles_COMPANY_NAME

-- Reusable updated_at trigger function (same as shared)
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Company-local enum types
-- ============================================================================
DO $$ BEGIN
  CREATE TYPE account_type_t AS ENUM ('demo','live');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE trade_type_t AS ENUM ('live','paper','shadow');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE trade_direction_t AS ENUM ('long','short');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE trade_status_t AS ENUM ('pending','open','partial_close','closed','cancelled','failed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE quantity_type_t AS ENUM ('units','lots','contracts');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE cost_type_t AS ENUM (
    'maker_fee','taker_fee','spread','overnight_funding',
    'swap','commission','guaranteed_stop','slippage','other'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE order_event_t AS ENUM (
    'submitted','accepted','partial_fill','filled',
    'cancelled','rejected','amended','expired'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE snapshot_source_t AS ENUM ('exchange_api','calculated','manual');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE agent_status_t AS ENUM ('active','paused','error','stopped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================================
-- 1. accounts
-- ============================================================================
CREATE TABLE accounts (
  id                      BIGSERIAL PRIMARY KEY,
  exchange                VARCHAR(50) NOT NULL,
  account_id_external     VARCHAR(100),
  account_type            account_type_t NOT NULL DEFAULT 'demo',
  api_key_ref             VARCHAR(100),
  balance                 NUMERIC(20,8) DEFAULT 0,
  equity                  NUMERIC(20,8) DEFAULT 0,
  margin_used             NUMERIC(20,8) DEFAULT 0,
  currency                VARCHAR(10) NOT NULL DEFAULT 'USD',
  is_active               BOOLEAN NOT NULL DEFAULT TRUE,
  session_state           JSONB,
  session_state_version   INT NOT NULL DEFAULT 0,
  last_synced_at          TIMESTAMPTZ(3),
  created_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_accounts_exchange_ext UNIQUE (exchange, account_id_external)
);
CREATE TRIGGER trg_accounts_updated BEFORE UPDATE ON accounts
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 2. trades
-- ============================================================================
CREATE TABLE trades (
  id                        BIGSERIAL PRIMARY KEY,
  account_id                BIGINT NOT NULL REFERENCES accounts(id),
  instrument_id             BIGINT NOT NULL,  -- cross-DB to tickles_shared.instruments
  strategy_id               BIGINT,           -- cross-DB to tickles_shared.strategies
  trade_type                trade_type_t NOT NULL DEFAULT 'paper',
  direction                 trade_direction_t NOT NULL,
  status                    trade_status_t NOT NULL DEFAULT 'pending',
  quantity                  NUMERIC(20,8),
  quantity_type             quantity_type_t NOT NULL DEFAULT 'units',
  contract_size             NUMERIC(20,8) NOT NULL DEFAULT 1.00000000,
  leverage                  INT NOT NULL DEFAULT 1,
  entry_price               NUMERIC(20,8),
  exit_price                NUMERIC(20,8),
  expected_entry_price      NUMERIC(20,8),
  expected_exit_price       NUMERIC(20,8),
  stop_loss_price           NUMERIC(20,8),
  take_profit_1             NUMERIC(20,8),
  take_profit_2             NUMERIC(20,8),
  take_profit_3             NUMERIC(20,8),
  gross_pnl                 NUMERIC(20,8),
  net_pnl                   NUMERIC(20,8),
  entry_slippage            NUMERIC(20,8),
  exit_slippage             NUMERIC(20,8),
  entry_slippage_pct        NUMERIC(10,6),
  exit_slippage_pct         NUMERIC(10,6),
  signal_to_fill_ms         INT,
  order_to_fill_ms          INT,
  exchange_order_id         VARCHAR(100),
  exchange_deal_id          VARCHAR(100),
  winning_strand_id         BIGINT,
  conflict_exists           BOOLEAN,
  brain_calc_price          NUMERIC(20,8),
  fake_candle_close         NUMERIC(20,8),
  price_variance_pct        NUMERIC(10,6),
  brain_snapshot_detail     JSONB,
  brain_snapshot_version    INT NOT NULL DEFAULT 1,
  candle_data_hash          CHAR(64),
  signal_params_hash        CHAR(64),
  window_close_time         TIME,
  signal_at                 TIMESTAMPTZ(3),
  ordered_at                TIMESTAMPTZ(3),
  opened_at                 TIMESTAMPTZ(3),
  closed_at                 TIMESTAMPTZ(3),
  created_at                TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at                TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_trades_signal_dedup UNIQUE (account_id, instrument_id, signal_params_hash)
);
CREATE INDEX idx_trades_account_status ON trades(account_id, status);
CREATE INDEX idx_trades_instrument_opened ON trades(instrument_id, opened_at);
CREATE INDEX idx_trades_strategy_closed ON trades(strategy_id, closed_at);
CREATE INDEX idx_trades_type_status ON trades(trade_type, status);
CREATE INDEX idx_trades_exchange_order ON trades(exchange_order_id);
CREATE INDEX idx_trades_exchange_deal ON trades(exchange_deal_id);
CREATE INDEX idx_trades_candle_hash ON trades(candle_data_hash);
-- Partial index for active/open trades (most commonly queried)
CREATE INDEX idx_trades_open_only ON trades(account_id, instrument_id)
  WHERE status IN ('pending','open','partial_close');
CREATE TRIGGER trg_trades_updated BEFORE UPDATE ON trades
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 3. trade_cost_entries
-- ============================================================================
CREATE TABLE trade_cost_entries (
  id          BIGSERIAL PRIMARY KEY,
  trade_id    BIGINT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
  cost_type   cost_type_t NOT NULL,
  amount      NUMERIC(20,8) NOT NULL,
  currency    VARCHAR(10) NOT NULL DEFAULT 'USD',
  accrued_at  TIMESTAMPTZ(3),
  description VARCHAR(200),
  created_at  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_costs_trade ON trade_cost_entries(trade_id);
CREATE INDEX idx_costs_type_date ON trade_cost_entries(cost_type, accrued_at);

-- ============================================================================
-- 4. order_events
-- ============================================================================
CREATE TABLE order_events (
  id                  BIGSERIAL PRIMARY KEY,
  trade_id            BIGINT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
  event_type          order_event_t NOT NULL,
  price               NUMERIC(20,8),
  quantity_filled     NUMERIC(20,8),
  exchange_timestamp  TIMESTAMPTZ(3),
  raw_response        JSONB,
  created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_events_trade ON order_events(trade_id, created_at);

-- ============================================================================
-- 5. trade_validations
-- ============================================================================
CREATE TABLE trade_validations (
  id                      BIGSERIAL PRIMARY KEY,
  trade_id                BIGINT NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
  strategy_id             BIGINT NOT NULL,
  backtest_result_id      BIGINT,
  signal_match            BOOLEAN,
  entry_price_delta       NUMERIC(20,8),
  exit_price_delta        NUMERIC(20,8),
  pnl_delta               NUMERIC(20,8),
  pnl_delta_pct           NUMERIC(10,6),
  slippage_contribution   NUMERIC(20,8),
  fee_contribution        NUMERIC(20,8),
  data_drift_detected     BOOLEAN NOT NULL DEFAULT FALSE,
  original_candle_hash    CHAR(64),
  validation_candle_hash  CHAR(64),
  validated_at            TIMESTAMPTZ(3),
  created_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_validations_trade ON trade_validations(trade_id);
CREATE INDEX idx_validations_strategy ON trade_validations(strategy_id, validated_at);

-- ============================================================================
-- 6. balance_snapshots
-- ============================================================================
CREATE TABLE balance_snapshots (
  id                BIGSERIAL PRIMARY KEY,
  account_id        BIGINT NOT NULL REFERENCES accounts(id),
  balance           NUMERIC(20,8),
  equity            NUMERIC(20,8),
  margin_used       NUMERIC(20,8),
  unrealized_pnl    NUMERIC(20,8),
  snapshot_source   snapshot_source_t NOT NULL DEFAULT 'exchange_api',
  snapshot_at       TIMESTAMPTZ(3) NOT NULL,
  created_at        TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_balance_account_time ON balance_snapshots(account_id, snapshot_at);

-- ============================================================================
-- 7. leverage_history
-- ============================================================================
CREATE TABLE leverage_history (
  id              BIGSERIAL PRIMARY KEY,
  account_id      BIGINT NOT NULL REFERENCES accounts(id),
  instrument_id   BIGINT NOT NULL,
  old_leverage    INT,
  new_leverage    INT NOT NULL,
  changed_by      VARCHAR(50),
  reason          VARCHAR(200),
  changed_at      TIMESTAMPTZ(3) NOT NULL,
  created_at      TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_leverage_account_time ON leverage_history(account_id, changed_at);

-- ============================================================================
-- 8. agent_state
-- ============================================================================
CREATE TABLE agent_state (
  id                  BIGSERIAL PRIMARY KEY,
  agent_name          VARCHAR(100) NOT NULL,
  status              agent_status_t NOT NULL DEFAULT 'stopped',
  last_heartbeat_at   TIMESTAMPTZ(3),
  last_error          TEXT,
  state_data          JSONB,
  state_version       INT NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_agent_name UNIQUE (agent_name)
);
CREATE TRIGGER trg_agent_state_updated BEFORE UPDATE ON agent_state
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 9. strategy_lifecycle
-- ============================================================================
CREATE TABLE strategy_lifecycle (
  id            BIGSERIAL PRIMARY KEY,
  strategy_id   BIGINT NOT NULL,
  from_status   VARCHAR(30),
  to_status     VARCHAR(30) NOT NULL,
  changed_by    VARCHAR(100),
  reason        TEXT,
  changed_at    TIMESTAMPTZ(3) NOT NULL,
  created_at    TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_lifecycle_strategy ON strategy_lifecycle(strategy_id, changed_at);

-- ============================================================================
-- 10. company_config
-- ============================================================================
CREATE TABLE company_config (
  id            SERIAL PRIMARY KEY,
  config_key    VARCHAR(100) NOT NULL,
  config_value  TEXT,
  updated_at    TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_company_config_key UNIQUE (config_key)
);
CREATE TRIGGER trg_company_config_updated BEFORE UPDATE ON company_config
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- Seed company_config
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
  ('llm_budget_monthly_usd', '200')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================================
-- Grants to schemy (read-only observer)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO schemy;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO schemy;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO schemy;

SELECT 'tickles_COMPANY_NAME migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE schemaname='public') AS tables;
