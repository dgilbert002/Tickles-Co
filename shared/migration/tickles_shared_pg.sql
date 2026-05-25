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

-- Phase 9 additions — zone filter + context window + image dedup
ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS enrichment_status       TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS context_window          JSONB NULL,
    ADD COLUMN IF NOT EXISTS zone_filter_confidence  NUMERIC(4,3) NULL,
    ADD COLUMN IF NOT EXISTS zone_filter_reason      TEXT NULL,
    ADD COLUMN IF NOT EXISTS image_phash             CHAR(16) NULL,
    ADD COLUMN IF NOT EXISTS duplicate_of_id         BIGINT NULL REFERENCES news_items(id);

CREATE INDEX IF NOT EXISTS idx_news_items_phash_recent
    ON news_items (image_phash, collected_at DESC)
    WHERE image_phash IS NOT NULL AND duplicate_of_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_news_items_pending
    ON news_items (collected_at)
    WHERE enrichment_status = 'pending';

-- Phase 9 — collector_sources zone-filter + rate-limit overrides
ALTER TABLE collector_sources
    ADD COLUMN IF NOT EXISTS zone_filter_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS zone_filter_threshold  NUMERIC(4,3) NULL,
    ADD COLUMN IF NOT EXISTS rate_limit_msgs_per_sec INT NULL;

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
-- 15. tracked_positions — Shared Trade Ledger (Phase 2)
-- ============================================================================
-- Lives in tickles_shared.public, NOT per-company.  company_id scopes rows.
-- All Phase 2 columns baked in for new companies provisioned after Phase 2.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.tracked_positions (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source references
    news_item_id        BIGINT          NOT NULL,
    media_item_id       BIGINT,
    trader_profile_id   BIGINT          NOT NULL,
    signal_interpretation_id BIGINT,

    -- Instrument
    instrument_symbol   VARCHAR(50)     NOT NULL,
    instrument_exchange VARCHAR(50)     NOT NULL DEFAULT 'bybit',
    epic_code           VARCHAR(50),

    -- Trade parameters
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
    status              VARCHAR(16)     NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','open','partial_exit','closed','expired','invalidated','cancelled','deleted')),
    status_reason       VARCHAR(100),

    -- Current market state
    current_price       NUMERIC(20,8),
    price_updated_at    TIMESTAMPTZ(3),
    highest_price       NUMERIC(20,8),
    lowest_price        NUMERIC(20,8),

    -- P&L metrics
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

    -- Outcome
    outcome               VARCHAR(16)
        CHECK (outcome IN ('tp1_hit','tp2_hit','tp3_hit','sl_hit','breakeven','expired','manual_close','invalidated')),
    exit_price            NUMERIC(20,8),
    exit_timestamp        TIMESTAMPTZ(3),
    exit_reason           TEXT,

    -- Notional
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

    -- Phase 6 additions — normalised symbol, reason agreement, pgvector embedding
    instrument_symbol_normalised VARCHAR(64),
    reason_agreement_score  NUMERIC(4,3),
    entry_reason_trader_embedding vector(384),

    -- Phase 10 additions — actor_instance namespace + source_position_id for Surgeon2 migration
    actor_instance        TEXT            NOT NULL DEFAULT '',
    source_position_id  BIGINT          NULL,

    -- Bug B fix (2026-05-24 second-round audit): persist the dedup decision
    -- so the dashboard "duplicates today" KPI and the trade_dedup writer
    -- (`UPDATE … SET deduped_at = NOW() …`) have a column to read/write
    -- against on a freshly provisioned VPS. Previously this column was added
    -- by an out-of-band migration (`2026_05_24_bughunt_t0_t1_columns.sql`)
    -- and was missing from the canonical schema, so disaster-recovery
    -- restores would 500 the dedup write and the KPI query.
    deduped_at            TIMESTAMPTZ     NULL,

    -- Metadata
    created_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication (Phase 10: extended with actor_instance for multi-instance safety)
    CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction, actor_instance)
);

CREATE INDEX idx_tracked_pos_trader      ON public.tracked_positions (trader_profile_id);
CREATE INDEX idx_tracked_pos_status      ON public.tracked_positions (status);
CREATE INDEX idx_tracked_pos_symbol      ON public.tracked_positions (instrument_symbol, instrument_exchange);
CREATE INDEX idx_tracked_pos_company     ON public.tracked_positions (company_id);
CREATE INDEX idx_tracked_pos_signal_ts   ON public.tracked_positions (signal_timestamp);
CREATE INDEX idx_tracked_pos_open        ON public.tracked_positions (status, trader_profile_id) WHERE status = 'open';
CREATE INDEX idx_tp_actor_type           ON public.tracked_positions (actor_type);
CREATE INDEX idx_tp_actor_id             ON public.tracked_positions (actor_id);
CREATE INDEX idx_tp_postmortem           ON public.tracked_positions (postmortem_status) WHERE postmortem_status = 'pending';
CREATE INDEX idx_tp_closed_at            ON public.tracked_positions (closed_at) WHERE closed_at IS NOT NULL;
CREATE INDEX idx_tp_correlation_id       ON public.tracked_positions (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX idx_tp_symbol_norm          ON public.tracked_positions (instrument_symbol_normalised) WHERE instrument_symbol_normalised IS NOT NULL;

-- Bug B fix (2026-05-24): partial index on deduped_at for the dashboard KPI
-- counter that reads `WHERE deduped_at >= now() - interval '24h'`.
CREATE INDEX IF NOT EXISTS idx_tracked_positions_deduped_at
    ON public.tracked_positions (deduped_at)
    WHERE deduped_at IS NOT NULL;

-- Phase 10 — extended UNIQUE for multi-instance actor safety
CREATE UNIQUE INDEX uniq_tracked_positions_actor
    ON public.tracked_positions (actor_type, actor_id, actor_instance, source_position_id)
    WHERE source_position_id IS NOT NULL;

CREATE INDEX idx_tp_entry_reason_embed_cosine
  ON public.tracked_positions
  USING ivfflat (entry_reason_trader_embedding vector_cosine_ops)
  WITH (lists = 50);

CREATE TRIGGER trg_tracked_positions_updated
    BEFORE UPDATE ON public.tracked_positions
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================================
-- [AG] Reason-freeze trigger — hindsight-bias guard
-- ============================================================================

CREATE OR REPLACE FUNCTION public.fn_freeze_entry_reasons()
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
    BEFORE UPDATE ON public.tracked_positions
    FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_entry_reasons();

-- ============================================================================
-- 16. prompt_versions — LLM prompt registry (Phase 6)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.prompt_versions (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT        NOT NULL,
  version       VARCHAR(32) NOT NULL,
  prompt_hash   CHAR(16)    NOT NULL,
  system        TEXT,
  body          TEXT        NOT NULL,
  taxonomy_rule TEXT,
  model_hint    TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by    TEXT,
  notes         TEXT,
  UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_name ON public.prompt_versions (name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_hash ON public.prompt_versions (prompt_hash);

-- ============================================================================
-- 17. memu_outbox — Durable broadcast outbox (Phase 7)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.memu_outbox (
    id            BIGSERIAL PRIMARY KEY,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ NULL,
    last_error    TEXT NULL,
    attempt_count INT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memu_outbox_unprocessed
    ON public.memu_outbox (created_at)
    WHERE processed_at IS NULL;

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
