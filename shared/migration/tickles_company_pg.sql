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
  -- Phase 10 — actor_instance namespace for multi-instance safety
  actor_instance      TEXT NOT NULL DEFAULT '',
  CONSTRAINT uq_agent_name UNIQUE (agent_name, actor_instance)
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
-- 11. signal_interpretations — Dual-track LLM + Quant analysis output
-- ============================================================================
-- Phase 2: all new columns baked in for new companies

CREATE TABLE signal_interpretations (
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

    -- Phase 2 additions — prefilter / vision / prompt / raw I/O / dual-reason / tags
    prefilter_provider        VARCHAR(32),
    prefilter_model           VARCHAR(128),
    prefilter_temperature     NUMERIC(4,2),
    prefilter_result          VARCHAR(32),
    prefilter_cost_usd        NUMERIC(20,8) DEFAULT 0,
    vision_provider           VARCHAR(32),
    vision_model_requested    VARCHAR(128),
    vision_model_resolved     VARCHAR(128),
    vision_temperature        NUMERIC(4,2),
    prompt_version            VARCHAR(32),
    prompt_hash               CHAR(16),
    llm_raw_request_path      TEXT,
    llm_raw_response_path     TEXT,
    trader_stated_thesis      TEXT,
    llm_inferred_thesis       TEXT,
    reason_agreement_score    NUMERIC(4,3),
    pattern_tags              JSONB,
    setup_tags                JSONB,
    regime_tags               JSONB,
    session_tags              JSONB,
    instrument_symbol_normalised VARCHAR(64),
    instrument_exchange       VARCHAR(32),
    correlation_id            VARCHAR(36),

    -- Phase 9 — provenance: where did the symbol come from?
    instrument_resolved_from  TEXT NULL
        CHECK (instrument_resolved_from IN ('message','context','inferred','unknown')),

    -- Metadata
    created_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication: one interpretation per (news_item, model, params)
    CONSTRAINT uq_interpretation_dedup UNIQUE (news_item_id, model_version, param_hash)
);

CREATE INDEX idx_interp_news_item      ON signal_interpretations (news_item_id);
CREATE INDEX idx_interp_media_item     ON signal_interpretations (media_item_id) WHERE media_item_id IS NOT NULL;
CREATE INDEX idx_interp_trader         ON signal_interpretations (trader_profile_id);
CREATE INDEX idx_interp_consensus_dir  ON signal_interpretations (consensus_direction);
CREATE INDEX idx_interp_created        ON signal_interpretations (created_at);
CREATE INDEX idx_interp_instrument     ON signal_interpretations (instrument_symbol, instrument_exchange);
CREATE INDEX idx_interp_model_hash     ON signal_interpretations (model_version, param_hash);
CREATE INDEX idx_si_prompt_version     ON signal_interpretations (prompt_version);
CREATE INDEX idx_si_pattern_tags       ON signal_interpretations USING GIN (pattern_tags);
CREATE INDEX idx_si_setup_tags         ON signal_interpretations USING GIN (setup_tags);
CREATE INDEX idx_si_regime_tags        ON signal_interpretations USING GIN (regime_tags);
CREATE INDEX idx_si_session_tags       ON signal_interpretations USING GIN (session_tags);
CREATE INDEX idx_si_symbol_norm        ON signal_interpretations (instrument_symbol_normalised);
CREATE INDEX idx_si_exchange           ON signal_interpretations (instrument_exchange);
CREATE INDEX idx_si_correlation_id     ON signal_interpretations (correlation_id) WHERE correlation_id IS NOT NULL;

CREATE TRIGGER trg_signal_interpretations_updated
    BEFORE UPDATE ON signal_interpretations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 12. tracked_positions — Core Trade Tracking
-- ============================================================================
-- Phase 2: all new columns baked in for new companies

CREATE TABLE tracked_positions (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source references
    news_item_id        BIGINT          NOT NULL,
    media_item_id       BIGINT,
    trader_profile_id   BIGINT          NOT NULL,
    signal_interpretation_id BIGINT,

    -- Instrument (resolved from message + catalog)
    instrument_symbol   VARCHAR(50)     NOT NULL,
    instrument_exchange VARCHAR(50)     NOT NULL DEFAULT 'bybit',
    epic_code           VARCHAR(50),

    -- Trade parameters (extracted from message or chart analysis)
    direction           VARCHAR(8)      NOT NULL
        CHECK (direction IN ('long','short')),
    entry_price         NUMERIC(20,8),
    stop_loss           NUMERIC(20,8),
    take_profit_1       NUMERIC(20,8),
    take_profit_2       NUMERIC(20,8),
    take_profit_3       NUMERIC(20,8),
    position_size       NUMERIC(20,8),
    leverage            NUMERIC(5,2),

    -- Detection metadata
    detection_method    VARCHAR(32)     NOT NULL DEFAULT 'manual'
        CHECK (detection_method IN ('manual','llm_vision','text_parser','quant_pattern','agent_override')),
    detection_confidence NUMERIC(5,4)   NOT NULL DEFAULT 0.0,
    raw_signal_text     TEXT,
    signal_timestamp    TIMESTAMPTZ(3)  NOT NULL,

    -- Status lifecycle
    status              VARCHAR(16)     NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','partial_exit','closed','expired','invalidated','cancelled')),
    status_reason       VARCHAR(100),

    -- Current market state (updated by PositionMonitor daemon)
    current_price       NUMERIC(20,8),
    price_updated_at    TIMESTAMPTZ(3),
    highest_price       NUMERIC(20,8),
    lowest_price        NUMERIC(20,8),

    -- P&L metrics (updated continuously)
    unrealized_pnl_pct  NUMERIC(10,4),
    unrealized_pnl_usd  NUMERIC(20,8),
    realized_pnl_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,
    realized_pnl_usd    NUMERIC(20,8)   NOT NULL DEFAULT 0,
    max_drawdown_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,
    max_profit_pct      NUMERIC(10,4)   NOT NULL DEFAULT 0,

    -- Distance metrics
    distance_to_entry_pct NUMERIC(10,4),
    distance_to_sl_pct    NUMERIC(10,4),
    distance_to_tp1_pct   NUMERIC(10,4),
    risk_reward_ratio     NUMERIC(10,4),

    -- Time metrics
    time_in_trade_minutes INT             NOT NULL DEFAULT 0,
    time_to_tp1_minutes   INT,
    time_to_sl_minutes    INT,
    expiry_at             TIMESTAMPTZ(3),

    -- Outcome (filled when position closes)
    outcome               VARCHAR(16)
        CHECK (outcome IN ('tp1_hit','tp2_hit','tp3_hit','sl_hit','breakeven','expired','manual_close','invalidated')),
    exit_price            NUMERIC(20,8),
    exit_timestamp        TIMESTAMPTZ(3),
    exit_reason           TEXT,

    -- Notional for P&L calc (from env or config)
    notional_usd          NUMERIC(20,8)   NOT NULL DEFAULT 1000.0,

    -- Company context — NO DEFAULT, must be explicit
    company_id            VARCHAR(50)     NOT NULL,

    -- Phase 2 additions — actor / department / legs / reasons / postmortem
    actor_type              VARCHAR(32),
    actor_id                VARCHAR(128),
    department              VARCHAR(64),
    position_kind           VARCHAR(32),
    asset_class             VARCHAR(32),
    venue                   VARCHAR(64),
    legs                    JSONB,
    sl_history              JSONB,
    partial_closes          JSONB,
    entry_reason_trader     TEXT,
    entry_reason_llm        TEXT,
    entry_reason_agent      TEXT,
    entry_reason_frozen_at  TIMESTAMPTZ(3),
    exit_reason_trader      TEXT,
    exit_reason_llm         TEXT,
    exit_reason_system      TEXT,
    closed_at               TIMESTAMPTZ(3),
    realized_pnl_usd_final  NUMERIC(20,8),
    postmortem_status       VARCHAR(32) DEFAULT 'pending',
    correlation_id          VARCHAR(36),

    -- Metadata
    created_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication: one position per (news_item, trader, symbol, direction)
    CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction)
);

CREATE INDEX idx_tracked_pos_trader      ON tracked_positions (trader_profile_id);
CREATE INDEX idx_tracked_pos_status      ON tracked_positions (status);
CREATE INDEX idx_tracked_pos_symbol      ON tracked_positions (instrument_symbol, instrument_exchange);
CREATE INDEX idx_tracked_pos_company     ON tracked_positions (company_id);
CREATE INDEX idx_tracked_pos_signal_ts   ON tracked_positions (signal_timestamp);
CREATE INDEX idx_tracked_pos_open        ON tracked_positions (status, trader_profile_id) WHERE status = 'open';
CREATE INDEX idx_tp_actor_type           ON tracked_positions (actor_type);
CREATE INDEX idx_tp_actor_id             ON tracked_positions (actor_id);
CREATE INDEX idx_tp_postmortem           ON tracked_positions (postmortem_status) WHERE postmortem_status = 'pending';
CREATE INDEX idx_tp_closed_at            ON tracked_positions (closed_at) WHERE closed_at IS NOT NULL;
CREATE INDEX idx_tp_correlation_id       ON tracked_positions (correlation_id) WHERE correlation_id IS NOT NULL;

CREATE TRIGGER trg_tracked_positions_updated
    BEFORE UPDATE ON tracked_positions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- [AG] Reason-freeze trigger — hindsight-bias guard
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_freeze_entry_reasons()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.entry_reason_frozen_at IS NOT NULL THEN
        IF NEW.entry_reason_trader IS DISTINCT FROM OLD.entry_reason_trader
           OR NEW.entry_reason_llm IS DISTINCT FROM OLD.entry_reason_llm
           OR NEW.entry_reason_agent IS DISTINCT FROM OLD.entry_reason_agent
           OR NEW.entry_reason_frozen_at IS DISTINCT FROM OLD.entry_reason_frozen_at THEN
            RAISE EXCEPTION 'entry_reason_* fields are frozen on tracked_positions.id=% (frozen_at=%)',
                OLD.id, OLD.entry_reason_frozen_at;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tp_freeze_entry_reasons
    BEFORE UPDATE ON tracked_positions
    FOR EACH ROW EXECUTE FUNCTION fn_freeze_entry_reasons();

-- ============================================================================
-- 13. position_updates — Snapshots of position state over time
-- ============================================================================
CREATE TABLE position_updates (
    id                  BIGSERIAL       PRIMARY KEY,
    position_id         BIGINT          NOT NULL REFERENCES tracked_positions(id) ON DELETE CASCADE,
    status              VARCHAR(16)     NOT NULL DEFAULT 'open',
    current_price       NUMERIC(20,8),
    unrealized_pnl_pct  NUMERIC(10,4),
    unrealized_pnl_usd  NUMERIC(20,8),
    realized_pnl_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,
    realized_pnl_usd    NUMERIC(20,8)   NOT NULL DEFAULT 0,
    max_drawdown_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,
    max_profit_pct      NUMERIC(10,4)   NOT NULL DEFAULT 0,
    distance_to_entry_pct NUMERIC(10,4),
    distance_to_sl_pct    NUMERIC(10,4),
    distance_to_tp1_pct   NUMERIC(10,4),
    time_in_trade_minutes INT             NOT NULL DEFAULT 0,
    snapshot_at         TIMESTAMPTZ(3)  NOT NULL,
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_pos_updates_position ON position_updates(position_id, snapshot_at);

-- ============================================================================
-- 14. agent_opinions — ChartHacker / agent review of positions
-- ============================================================================
-- Phase 8: ChartHackerOpinionService — critic role, dedup by price bucket + hour
CREATE TABLE agent_opinions (
    id                  BIGSERIAL       PRIMARY KEY,
    position_id         BIGINT          NOT NULL REFERENCES tracked_positions(id) ON DELETE CASCADE,
    agent_id            VARCHAR(100)    NOT NULL DEFAULT 'chart_hacker',
    agent_type          VARCHAR(32)     NOT NULL DEFAULT 'critic',
    opinion_type        VARCHAR(32)     NOT NULL DEFAULT 'entry_review'
        CHECK (opinion_type IN ('entry_review','mid_position_review','exit_review')),
    would_take_trade    BOOLEAN,
    confidence          NUMERIC(5,4)    NOT NULL DEFAULT 0.0,
    reasoning           TEXT,
    suggested_entry     NUMERIC(20,8),
    suggested_sl        NUMERIC(20,8),
    suggested_tp1       NUMERIC(20,8),
    suggested_tp2       NUMERIC(20,8),
    suggested_tp3       NUMERIC(20,8),
    suggested_size      NUMERIC(20,8),
    agent_pnl_pct       NUMERIC(10,4),
    performance_delta   NUMERIC(10,4),
    lessons_learned     TEXT,
    model_version       VARCHAR(100),
    param_hash          CHAR(64),
    cost_usd            NUMERIC(20,8)   DEFAULT 0,
    latency_ms          INT,

    -- Phase 8 additions — dedup + confidence gate
    snapshot_price_bucket_pct INT       NOT NULL DEFAULT 0,
    hour_bucket_utc     TIMESTAMPTZ(3)  NOT NULL DEFAULT date_trunc('hour', CURRENT_TIMESTAMP),
    memo_confidence     NUMERIC(4,3)    NULL,
    is_published        BOOLEAN         NOT NULL DEFAULT TRUE,

    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_agent_opinion_position_agent UNIQUE (position_id, agent_name, opinion_type)
);

-- Phase 8 dedup index: one opinion per (position, price_bucket, hour, agent)
CREATE UNIQUE INDEX IF NOT EXISTS uniq_agent_opinions_dedup
    ON agent_opinions (position_id, snapshot_price_bucket_pct, hour_bucket_utc, agent_id);

-- Phase 8: dashboard query only sees published opinions
CREATE INDEX IF NOT EXISTS idx_agent_opinions_published_open
    ON agent_opinions (position_id, created_at DESC)
    WHERE is_published = TRUE;

CREATE INDEX idx_opinions_position ON agent_opinions(position_id);
CREATE INDEX idx_opinions_agent ON agent_opinions(agent_name, created_at);
CREATE TRIGGER trg_agent_opinions_updated
    BEFORE UPDATE ON agent_opinions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 15. position_postmortems — LLM post-trade analysis
-- ============================================================================
CREATE TABLE position_postmortems (
    id                      BIGSERIAL PRIMARY KEY,
    position_id             BIGINT          NOT NULL,
    postmortem_version      VARCHAR(32)     NOT NULL,
    postmortem_provider     VARCHAR(32)     NOT NULL,
    postmortem_model        VARCHAR(128)    NOT NULL,
    param_hash              CHAR(16)        NOT NULL,
    candle_data_hash        CHAR(16),
    what_happened           TEXT            NOT NULL,
    why_it_worked           TEXT,
    why_it_failed           TEXT,
    trader_thesis_validated BOOLEAN,
    llm_thesis_validated    BOOLEAN,
    pattern_confirmed       JSONB,
    pattern_failed          JSONB,
    regime_at_entry         VARCHAR(64),
    regime_at_exit          VARCHAR(64),
    lessons_for_actor       TEXT,
    lessons_for_company     TEXT,
    cost_usd                NUMERIC(20,8)   DEFAULT 0,
    latency_ms              INT,
    llm_raw_request_path    TEXT,
    llm_raw_response_path   TEXT,
    prompt_version          VARCHAR(32)     NOT NULL,
    correlation_id          VARCHAR(36),
    created_at              TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Composite UNIQUE: one postmortem per (position, version, prompt)
    CONSTRAINT uq_postmortem_composite UNIQUE (position_id, postmortem_version, prompt_version)
);

CREATE INDEX idx_pm_position_id        ON position_postmortems (position_id);
CREATE INDEX idx_pm_postmortem_version ON position_postmortems (postmortem_version);
CREATE INDEX idx_pm_param_hash         ON position_postmortems (param_hash);
CREATE INDEX idx_pm_correlation_id     ON position_postmortems (correlation_id) WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- 16. trader_performance — Per-trader performance cache
-- ============================================================================
CREATE TABLE trader_performance (
    id                  BIGSERIAL       PRIMARY KEY,
    trader_profile_id   BIGINT          NOT NULL,
    total_signals       INT             NOT NULL DEFAULT 0,
    total_positions     INT             NOT NULL DEFAULT 0,
    win_count           INT             NOT NULL DEFAULT 0,
    loss_count          INT             NOT NULL DEFAULT 0,
    breakeven_count     INT             NOT NULL DEFAULT 0,
    total_pnl_pct       NUMERIC(10,4)   NOT NULL DEFAULT 0,
    total_pnl_usd       NUMERIC(20,8)   NOT NULL DEFAULT 0,
    avg_win_pct         NUMERIC(10,4)   NOT NULL DEFAULT 0,
    avg_loss_pct        NUMERIC(10,4)   NOT NULL DEFAULT 0,
    max_drawdown_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,
    sharpe_ratio        NUMERIC(10,4),
    win_rate            NUMERIC(5,4)    NOT NULL DEFAULT 0,
    profit_factor       NUMERIC(10,4)   NOT NULL DEFAULT 0,
    avg_risk_reward     NUMERIC(10,4)   NOT NULL DEFAULT 0,
    best_trade_pnl_pct  NUMERIC(10,4)   NOT NULL DEFAULT 0,
    worst_trade_pnl_pct NUMERIC(10,4)   NOT NULL DEFAULT 0,
    streak_current      INT             NOT NULL DEFAULT 0,
    streak_max_win      INT             NOT NULL DEFAULT 0,
    streak_max_loss     INT             NOT NULL DEFAULT 0,
    last_trade_at       TIMESTAMPTZ(3),
    calculated_at       TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_trader_perf_profile UNIQUE (trader_profile_id)
);
CREATE TRIGGER trg_trader_performance_updated
    BEFORE UPDATE ON trader_performance
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- Grants to schemy (read-only observer)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO schemy;

-- ============================================================================
-- Phase 11 — Platform-Agnostic Edge Score + actor_performance + CoachService
-- ============================================================================
-- [BE] edge_score formula — available-component normalisation
-- [BF] Minimum-sample gate — closed_position_count >= 3 for leaderboard
-- [BG] CoachService prompt A/B ledger
-- [K] trader_performance reconciliation — dual-write during 90-day window
-- ============================================================================

-- ============================================================================
-- 1. actor_performance — Cross-actor performance rollup (replaces
--    trader_performance for new consumers; trader_performance kept for
--    90-day backwards compat per Phase 11 §E)
-- ============================================================================
CREATE TABLE IF NOT EXISTS actor_performance (
    id                      BIGSERIAL PRIMARY KEY,
    actor_type              TEXT NOT NULL,
    actor_id                TEXT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,                 -- exclusive
    closed_position_count   INT  NOT NULL,
    edge_score              NUMERIC(5,4) NOT NULL,         -- [0.0000, 1.0000]
    components_jsonb        JSONB NOT NULL,                -- per-component score + available flag
    weights_used_jsonb      JSONB NOT NULL,                -- weights AFTER renormalisation
    confidence_low          BOOLEAN NOT NULL DEFAULT FALSE,
    formula_version         INT NOT NULL,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (actor_type, actor_id, period_start, period_end, formula_version)
);

CREATE INDEX IF NOT EXISTS idx_actor_perf_lookup
    ON actor_performance (actor_type, actor_id, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_actor_perf_period
    ON actor_performance (period_start, period_end, edge_score DESC);

-- ============================================================================
-- 2. actor_leaderboard view — ranked, hides rows with < 3 closed positions
-- ============================================================================
CREATE OR REPLACE VIEW actor_leaderboard AS
SELECT
    actor_type, actor_id, period_start, period_end,
    closed_position_count, edge_score, confidence_low,
    components_jsonb, formula_version,
    RANK() OVER (PARTITION BY period_start, period_end ORDER BY edge_score DESC, closed_position_count DESC) AS rank
FROM actor_performance
WHERE closed_position_count >= 3;     -- BF: hide pure-noise rows entirely

-- ============================================================================
-- 3. edge_score_changes — audit trail for > 0.05 day-over-day delta
-- ============================================================================
CREATE TABLE IF NOT EXISTS edge_score_changes (
    id           BIGSERIAL PRIMARY KEY,
    actor_type   TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    period_end   DATE NOT NULL,
    score_before NUMERIC(5,4),
    score_after  NUMERIC(5,4) NOT NULL,
    delta        NUMERIC(6,4) NOT NULL,
    components_before JSONB,
    components_after  JSONB NOT NULL,
    note         TEXT NULL,              -- e.g. 'prompt_promoted'
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_edge_score_changes_actor
    ON edge_score_changes (actor_type, actor_id, period_end DESC);

-- ============================================================================
-- 4. prompt_assignments — CoachService [BG] variant assignment ledger
-- ============================================================================
CREATE TABLE IF NOT EXISTS prompt_assignments (
    id           BIGSERIAL PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    assignment_day DATE NOT NULL,
    prompt_name  TEXT NOT NULL,           -- e.g. 'chart_analysis'
    variant      TEXT NOT NULL,           -- e.g. 'v1' or 'v2'
    prompt_hash  CHAR(16) NOT NULL,       -- references prompt_versions
    UNIQUE (actor_id, assignment_day, prompt_name)
);

CREATE INDEX IF NOT EXISTS idx_prompt_assignments_lookup
    ON prompt_assignments (actor_id, prompt_name, assignment_day DESC);

-- ============================================================================
-- 5. Deprecation note on trader_performance (Phase 11 §E)
-- ============================================================================
-- trader_performance is DEPRECATED as of 2026-05-03.
-- EdgeScorer dual-writes Discord-actor rows for 90-day backwards compat.
-- After 2026-08-01, dual-write stops and table is truncated (kept as empty
-- shell so \d and any forgotten consumer don't 500).
-- See shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md §3.2 for mapping.
-- ============================================================================
GRANT SELECT ON ALL TABLES IN SCHEMA public TO schemy;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO schemy;

SELECT 'tickles_COMPANY_NAME migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE schemaname='public') AS tables;
