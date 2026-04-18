-- ═══════════════════════════════════════════════════════════════════
-- JarvAIs Database Schema v5.0
-- MySQL 8.0+
-- ═══════════════════════════════════════════════════════════════════

CREATE DATABASE IF NOT EXISTS jarvais
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE jarvais;

-- ───────────────────────────────────────────────────────────────────
-- Table: ea_signals
-- Every raw signal received from the UltimateMultiStrategy EA.
-- Includes hypothetical tracking for EA baseline comparison.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ea_signals (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    account_id          VARCHAR(50)     NOT NULL,
    timestamp           DATETIME(3)     NOT NULL,
    symbol              VARCHAR(20)     NOT NULL,
    direction           ENUM('BUY','SELL') NOT NULL,
    votes_long          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    votes_short         DECIMAL(10,4)   NOT NULL DEFAULT 0,
    vote_ratio          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    market_state        VARCHAR(30)     DEFAULT NULL,
    htf_trend           VARCHAR(30)     DEFAULT NULL,
    htf_context         VARCHAR(30)     DEFAULT NULL,
    strategy_details    JSON            DEFAULT NULL,
    indicator_values    JSON            DEFAULT NULL,
    ea_stop_loss        DECIMAL(15,5)   DEFAULT NULL,
    ea_take_profit      DECIMAL(15,5)   DEFAULT NULL,
    ea_lot_size         DECIMAL(10,4)   DEFAULT NULL,
    current_price       DECIMAL(15,5)   DEFAULT NULL,
    spread_points       DECIMAL(10,2)   DEFAULT NULL,
    status              ENUM('received','rejected_risk','approved','vetoed','executed')
                        NOT NULL DEFAULT 'received',
    -- Hypothetical tracking (what would happen if taken blindly)
    hypothetical_entry  DECIMAL(15,5)   DEFAULT NULL,
    hypothetical_exit   DECIMAL(15,5)   DEFAULT NULL,
    hypothetical_pnl    DECIMAL(15,2)   DEFAULT NULL,
    hypothetical_closed BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_signals_account_ts (account_id, timestamp),
    INDEX idx_signals_symbol (symbol),
    INDEX idx_signals_status (status),
    INDEX idx_signals_created (created_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: ai_decisions
-- The AI's complete decision process for each signal, including
-- the full Trader-Coach dialogue and dossier.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_decisions (
    id                              INT AUTO_INCREMENT PRIMARY KEY,
    signal_id                       INT             NOT NULL,
    account_id                      VARCHAR(50)     NOT NULL,
    timestamp                       DATETIME(3)     NOT NULL,
    model_used                      VARCHAR(50)     NOT NULL,
    decision                        ENUM('Approve','Veto') NOT NULL,
    confidence                      INT             NOT NULL CHECK (confidence BETWEEN 1 AND 100),
    trader_reasoning                TEXT            DEFAULT NULL,
    coach_assessment                TEXT            DEFAULT NULL,
    coach_challenges                TEXT            DEFAULT NULL,
    trader_response_to_challenges   TEXT            DEFAULT NULL,
    full_dossier                    MEDIUMTEXT      DEFAULT NULL,
    full_dialogue                   MEDIUMTEXT      DEFAULT NULL,
    suggested_lot_modifier          DECIMAL(3,2)    DEFAULT 1.00,
    suggested_tp_levels             JSON            DEFAULT NULL,
    suggested_sl_level              DECIMAL(15,5)   DEFAULT NULL,
    is_free_trade                   BOOLEAN         NOT NULL DEFAULT FALSE,
    risk_mode                       VARCHAR(30)     DEFAULT NULL,
    token_count_input               INT             DEFAULT 0,
    token_count_output              INT             DEFAULT 0,
    api_cost_usd                    DECIMAL(10,6)   DEFAULT 0,
    latency_ms                      INT             DEFAULT 0,
    created_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (signal_id) REFERENCES ea_signals(id) ON DELETE CASCADE,
    INDEX idx_decisions_account (account_id),
    INDEX idx_decisions_signal (signal_id),
    INDEX idx_decisions_decision (decision),
    INDEX idx_decisions_confidence (confidence)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: trades
-- Every executed trade with full lifecycle tracking.
-- Links back to the signal and AI decision that created it.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trades (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    decision_id             INT             DEFAULT NULL,
    signal_id               INT             DEFAULT NULL,
    account_id              VARCHAR(50)     NOT NULL,
    is_live                 BOOLEAN         NOT NULL DEFAULT FALSE,
    is_free_trade           BOOLEAN         NOT NULL DEFAULT FALSE,
    magic_number            INT             NOT NULL,
    mt5_ticket              BIGINT          DEFAULT NULL,
    symbol                  VARCHAR(20)     NOT NULL,
    direction               ENUM('BUY','SELL') NOT NULL,
    entry_price             DECIMAL(15,5)   NOT NULL,
    exit_price              DECIMAL(15,5)   DEFAULT NULL,
    stop_loss               DECIMAL(15,5)   DEFAULT NULL,
    original_stop_loss      DECIMAL(15,5)   DEFAULT NULL,
    take_profit_1           DECIMAL(15,5)   DEFAULT NULL,
    take_profit_2           DECIMAL(15,5)   DEFAULT NULL,
    take_profit_3           DECIMAL(15,5)   DEFAULT NULL,
    lot_size                DECIMAL(10,4)   NOT NULL,
    original_lot_size       DECIMAL(10,4)   NOT NULL,
    open_time               DATETIME(3)     NOT NULL,
    close_time              DATETIME(3)     DEFAULT NULL,
    pnl_usd                 DECIMAL(15,2)   DEFAULT NULL,
    pnl_pips                DECIMAL(10,1)   DEFAULT NULL,
    commission              DECIMAL(10,2)   DEFAULT 0,
    swap                    DECIMAL(10,2)   DEFAULT 0,
    status                  ENUM('open','partial_closed','closed','cancelled')
                            NOT NULL DEFAULT 'open',
    close_reason            VARCHAR(50)     DEFAULT NULL,
    ai_confidence_at_entry  INT             DEFAULT NULL,
    balance_at_entry        DECIMAL(15,2)   DEFAULT NULL,
    risk_percent_used       DECIMAL(5,2)    DEFAULT NULL,
    maturity_phase          INT             NOT NULL DEFAULT 1,
    -- Break-even tracking
    be_triggered            BOOLEAN         NOT NULL DEFAULT FALSE,
    be_trigger_time         DATETIME(3)     DEFAULT NULL,
    -- Partial close tracking
    partial_close_pct       DECIMAL(5,2)    DEFAULT 0,
    partial_close_pnl       DECIMAL(15,2)   DEFAULT 0,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    FOREIGN KEY (decision_id) REFERENCES ai_decisions(id) ON DELETE SET NULL,
    FOREIGN KEY (signal_id) REFERENCES ea_signals(id) ON DELETE SET NULL,
    INDEX idx_trades_account (account_id),
    INDEX idx_trades_status (status),
    INDEX idx_trades_symbol (symbol),
    INDEX idx_trades_magic (magic_number),
    INDEX idx_trades_open_time (open_time),
    UNIQUE INDEX idx_trades_magic_account (magic_number, account_id)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: trade_lessons
-- Post-mortem analysis for each closed trade.
-- The AI's forensic review of what worked, what failed, and why.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trade_lessons (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    trade_id                INT             NOT NULL,
    signal_id               INT             DEFAULT NULL,
    account_id              VARCHAR(50)     NOT NULL,
    timestamp               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    model_used              VARCHAR(50)     DEFAULT NULL,
    outcome                 ENUM('WIN','LOSS','BREAKEVEN') NOT NULL,
    pnl_usd                 DECIMAL(15,2)   DEFAULT NULL,
    what_worked             TEXT            DEFAULT NULL,
    what_failed             TEXT            DEFAULT NULL,
    lesson_text             TEXT            DEFAULT NULL,
    lesson_type             VARCHAR(50)     DEFAULT 'general',
    source_model            VARCHAR(100)    DEFAULT NULL,
    source_trade_id         INT             DEFAULT NULL,
    confidence_calibration  TEXT            DEFAULT NULL,
    full_post_mortem        MEDIUMTEXT      DEFAULT NULL,
    -- Factors analyzed (structured for querying)
    technical_factors       JSON            DEFAULT NULL,
    timing_factors          JSON            DEFAULT NULL,
    fundamental_factors     JSON            DEFAULT NULL,
    decision_quality        JSON            DEFAULT NULL,
    execution_factors       JSON            DEFAULT NULL,
    qdrant_vector_id        VARCHAR(100)    DEFAULT NULL,
    token_count             INT             DEFAULT 0,
    api_cost_usd            DECIMAL(10,6)   DEFAULT 0,

    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE,
    FOREIGN KEY (signal_id) REFERENCES ea_signals(id) ON DELETE SET NULL,
    INDEX idx_lessons_account (account_id),
    INDEX idx_lessons_outcome (outcome),
    INDEX idx_lessons_trade (trade_id)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: daily_performance
-- Daily rollup of trading performance per account.
-- Includes EA baseline vs JarvAIs comparison (AI Alpha).
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_performance (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL,
    date                    DATE            NOT NULL,
    -- Signal counts
    total_signals           INT             NOT NULL DEFAULT 0,
    signals_approved        INT             NOT NULL DEFAULT 0,
    signals_vetoed          INT             NOT NULL DEFAULT 0,
    signals_rejected_risk   INT             NOT NULL DEFAULT 0,
    -- Trade results
    total_trades            INT             NOT NULL DEFAULT 0,
    wins                    INT             NOT NULL DEFAULT 0,
    losses                  INT             NOT NULL DEFAULT 0,
    breakevens              INT             NOT NULL DEFAULT 0,
    win_rate                DECIMAL(5,2)    DEFAULT NULL,
    -- P&L
    total_pnl               DECIMAL(15,2)   NOT NULL DEFAULT 0,
    ea_hypothetical_pnl     DECIMAL(15,2)   NOT NULL DEFAULT 0,
    ai_alpha                DECIMAL(15,2)   NOT NULL DEFAULT 0,
    -- Free trades
    free_trades_count       INT             NOT NULL DEFAULT 0,
    free_trades_pnl         DECIMAL(15,2)   NOT NULL DEFAULT 0,
    -- Confidence metrics
    avg_confidence          DECIMAL(5,2)    DEFAULT NULL,
    avg_confidence_winners  DECIMAL(5,2)    DEFAULT NULL,
    avg_confidence_losers   DECIMAL(5,2)    DEFAULT NULL,
    -- Risk metrics
    max_drawdown            DECIMAL(15,2)   DEFAULT 0,
    max_drawdown_pct        DECIMAL(5,2)    DEFAULT 0,
    -- Costs
    total_api_cost          DECIMAL(10,4)   NOT NULL DEFAULT 0,
    total_token_input       INT             NOT NULL DEFAULT 0,
    total_token_output      INT             NOT NULL DEFAULT 0,
    -- System state
    maturity_phase          INT             NOT NULL DEFAULT 1,
    daily_review_summary    TEXT            DEFAULT NULL,
    balance_eod             DECIMAL(15,2)   DEFAULT NULL,
    balance_sod             DECIMAL(15,2)   DEFAULT NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX idx_daily_account_date (account_id, date),
    INDEX idx_daily_date (date)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: self_discovered_patterns
-- Patterns the AI has identified from its own trading history.
-- These are used to enhance future trading decisions.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS self_discovered_patterns (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL DEFAULT 'collective',
    discovered_date         DATE            NOT NULL,
    pattern_description     TEXT            NOT NULL,
    conditions              JSON            DEFAULT NULL,
    sample_size             INT             NOT NULL DEFAULT 0,
    win_rate                DECIMAL(5,2)    DEFAULT NULL,
    avg_pnl                 DECIMAL(15,2)   DEFAULT NULL,
    avg_confidence          DECIMAL(5,2)    DEFAULT NULL,
    confidence_level        ENUM('Low','Medium','High','Very High') NOT NULL DEFAULT 'Low',
    is_active               BOOLEAN         NOT NULL DEFAULT TRUE,
    last_validated          DATE            DEFAULT NULL,
    validation_notes        TEXT            DEFAULT NULL,
    qdrant_vector_id        VARCHAR(100)    DEFAULT NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_patterns_account (account_id),
    INDEX idx_patterns_active (is_active),
    INDEX idx_patterns_confidence (confidence_level)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: config
-- Runtime configuration stored in the database.
-- Allows UI and AI self-adjustment to persist settings.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL DEFAULT 'global',
    config_key              VARCHAR(100)    NOT NULL,
    config_value            TEXT            DEFAULT NULL,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    updated_by              VARCHAR(50)     NOT NULL DEFAULT 'user',

    UNIQUE INDEX idx_config_account_key (account_id, config_key)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: ai_api_log
-- Detailed log of every AI API call for cost tracking and debugging.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_api_log (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL,
    timestamp               DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    provider                VARCHAR(30)     NOT NULL,
    model                   VARCHAR(50)     NOT NULL,
    role                    VARCHAR(30)     NOT NULL COMMENT 'analyst, trader, coach, post_mortem, daily_review',
    signal_id               INT             DEFAULT NULL,
    trade_id                INT             DEFAULT NULL,
    token_count_input       INT             NOT NULL DEFAULT 0,
    token_count_output      INT             NOT NULL DEFAULT 0,
    cost_usd                DECIMAL(10,6)   NOT NULL DEFAULT 0,
    latency_ms              INT             NOT NULL DEFAULT 0,
    success                 BOOLEAN         NOT NULL DEFAULT TRUE,
    error_message           TEXT            DEFAULT NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_api_log_account (account_id),
    INDEX idx_api_log_ts (timestamp),
    INDEX idx_api_log_provider (provider, model)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: position_events
-- Tracks every change to an open position (SL moves, partial closes,
-- break-even triggers, etc.) for complete audit trail.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS position_events (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    trade_id                INT             NOT NULL,
    account_id              VARCHAR(50)     NOT NULL,
    timestamp               DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    event_type              VARCHAR(50)     NOT NULL
        COMMENT 'opened, sl_moved, be_triggered, partial_close, tp1_hit, tp2_hit, tp3_hit, trailing_update, closed',
    old_value               VARCHAR(100)    DEFAULT NULL,
    new_value               VARCHAR(100)    DEFAULT NULL,
    description             TEXT            DEFAULT NULL,
    current_price           DECIMAL(15,5)   DEFAULT NULL,
    current_pnl             DECIMAL(15,2)   DEFAULT NULL,

    FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE,
    INDEX idx_events_trade (trade_id),
    INDEX idx_events_account_ts (account_id, timestamp)
) ENGINE=InnoDB;

-- ═══════════════════════════════════════════════════════════════════
-- SCHEMA v5.1 ADDITIONS — Role Cost Tracking, Model History, Memory
-- ═══════════════════════════════════════════════════════════════════

-- ───────────────────────────────────────────────────────────────────
-- Table: role_model_history
-- Complete log of which AI model was assigned to each role,
-- with performance metrics during that assignment period.
-- Enables side-by-side model comparison for the same role.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS role_model_history (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL DEFAULT 'global',
    role                    VARCHAR(30)     NOT NULL
        COMMENT 'trader, coach, analyst, postmortem',
    model                   VARCHAR(80)     NOT NULL
        COMMENT 'e.g. gpt-4.1-mini, claude-opus-4-20250514',
    assigned_at             DATETIME        NOT NULL,
    replaced_at             DATETIME        DEFAULT NULL
        COMMENT 'NULL = currently active model',
    -- Performance during this assignment
    signals_processed       INT             NOT NULL DEFAULT 0,
    trades_executed          INT             NOT NULL DEFAULT 0,
    wins                    INT             NOT NULL DEFAULT 0,
    losses                  INT             NOT NULL DEFAULT 0,
    win_rate                DECIMAL(5,2)    DEFAULT NULL,
    total_pnl               DECIMAL(15,2)   NOT NULL DEFAULT 0,
    avg_confidence          DECIMAL(5,2)    DEFAULT NULL,
    -- Cost during this assignment
    total_api_calls         INT             NOT NULL DEFAULT 0,
    total_input_tokens      BIGINT          NOT NULL DEFAULT 0,
    total_output_tokens     BIGINT          NOT NULL DEFAULT 0,
    total_cost_usd          DECIMAL(10,6)   NOT NULL DEFAULT 0,
    avg_latency_ms          INT             NOT NULL DEFAULT 0,
    -- Computed score (0-100)
    score                   DECIMAL(5,1)    DEFAULT NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_rmh_account_role (account_id, role),
    INDEX idx_rmh_model (model),
    INDEX idx_rmh_assigned (assigned_at),
    INDEX idx_rmh_active (replaced_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: role_daily_costs
-- Daily aggregated cost and performance per role per account.
-- Feeds the "Cost Tracking Per Role" table and charts.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS role_daily_costs (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL,
    date                    DATE            NOT NULL,
    role                    VARCHAR(30)     NOT NULL
        COMMENT 'trader, coach, analyst, postmortem, daily_review',
    model                   VARCHAR(80)     DEFAULT NULL,
    -- Token counts
    api_calls               INT             NOT NULL DEFAULT 0,
    input_tokens            BIGINT          NOT NULL DEFAULT 0,
    output_tokens           BIGINT          NOT NULL DEFAULT 0,
    -- Cost
    cost_usd                DECIMAL(10,6)   NOT NULL DEFAULT 0,
    -- Performance contribution
    signals_processed       INT             NOT NULL DEFAULT 0,
    trades_contributed      INT             NOT NULL DEFAULT 0,
    pnl_contribution        DECIMAL(15,2)   NOT NULL DEFAULT 0,
    -- Latency
    avg_latency_ms          INT             NOT NULL DEFAULT 0,
    max_latency_ms          INT             NOT NULL DEFAULT 0,
    -- Errors
    error_count             INT             NOT NULL DEFAULT 0,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX idx_rdc_account_date_role (account_id, date, role),
    INDEX idx_rdc_date (date),
    INDEX idx_rdc_role (role)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: role_score_snapshots
-- Periodic snapshots of each role's score for trend charting.
-- Captured at the end of each day (or after each N trades).
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS role_score_snapshots (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    account_id              VARCHAR(50)     NOT NULL,
    date                    DATE            NOT NULL,
    trader_score            DECIMAL(5,1)    DEFAULT NULL,
    coach_score             DECIMAL(5,1)    DEFAULT NULL,
    analyst_score           DECIMAL(5,1)    DEFAULT NULL,
    postmortem_score        DECIMAL(5,1)    DEFAULT NULL,
    created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE INDEX idx_rss_account_date (account_id, date),
    INDEX idx_rss_date (date)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: memory_audit
-- Audit trail for all memory operations (Mem0 + Qdrant + MySQL).
-- Every memory stored is logged here for accountability.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_audit (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    collection              VARCHAR(50)     NOT NULL
        COMMENT 'trade_lessons, daily_reviews, patterns, market_context, human_interventions',
    role                    VARCHAR(30)     NOT NULL DEFAULT 'all',
    text_preview            VARCHAR(2000)   DEFAULT NULL,
    metadata_json           TEXT            DEFAULT NULL,
    stored_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    storage_backend         VARCHAR(30)     NOT NULL DEFAULT 'mem0'
        COMMENT 'mem0, qdrant_direct, mysql_fallback',

    INDEX idx_ma_collection (collection),
    INDEX idx_ma_role (role),
    INDEX idx_ma_stored (stored_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: memory_fallback
-- Fallback storage when both Mem0 and Qdrant are unavailable.
-- These records should be migrated to Mem0/Qdrant when they come back.
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_fallback (
    id                      INT AUTO_INCREMENT PRIMARY KEY,
    collection              VARCHAR(50)     NOT NULL,
    text_content            TEXT            NOT NULL,
    metadata_json           TEXT            DEFAULT NULL,
    stored_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    migrated_to_mem0        BOOLEAN         NOT NULL DEFAULT FALSE,
    migrated_at             DATETIME        DEFAULT NULL,

    INDEX idx_mf_collection (collection),
    INDEX idx_mf_migrated (migrated_to_mem0),
    INDEX idx_mf_stored (stored_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Add role column to ai_api_log if not already present
-- (The role column already exists in v5.0, but adding an index
-- specifically for role-based cost aggregation)
-- ───────────────────────────────────────────────────────────────────
-- Add indexes for role-based cost aggregation on ai_api_log
-- Using CREATE INDEX IF NOT EXISTS (MySQL 8.0 compatible)
SET @exist := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'ai_api_log' AND index_name = 'idx_api_log_role');
SET @sqlstmt := IF(@exist > 0, 'SELECT "Index idx_api_log_role already exists"', 'CREATE INDEX idx_api_log_role ON ai_api_log (role)');
PREPARE stmt FROM @sqlstmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @exist := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'ai_api_log' AND index_name = 'idx_api_log_role_model');
SET @sqlstmt := IF(@exist > 0, 'SELECT "Index idx_api_log_role_model already exists"', 'CREATE INDEX idx_api_log_role_model ON ai_api_log (role, model)');
PREPARE stmt FROM @sqlstmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @exist := (SELECT COUNT(*) FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'ai_api_log' AND index_name = 'idx_api_log_account_role_ts');
SET @sqlstmt := IF(@exist > 0, 'SELECT "Index idx_api_log_account_role_ts already exists"', 'CREATE INDEX idx_api_log_account_role_ts ON ai_api_log (account_id, role, timestamp)');
PREPARE stmt FROM @sqlstmt;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
USE jarvais;

-- ───────────────────────────────────────────────────────────────────
-- Table: news_items
-- All collected news, TradingView minds/ideas, and AI summaries
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS news_items (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    hash_key        VARCHAR(64)     NOT NULL,
    source          VARCHAR(100)    NOT NULL DEFAULT 'unknown',
    headline        VARCHAR(500)    NOT NULL DEFAULT '',
    detail          MEDIUMTEXT      DEFAULT NULL,
    url             VARCHAR(500)    DEFAULT NULL,
    category        VARCHAR(50)     DEFAULT 'news',
    relevance       VARCHAR(20)     DEFAULT 'medium',
    sentiment       VARCHAR(20)     DEFAULT 'neutral',
    symbols         JSON            DEFAULT NULL,
    tags            JSON            DEFAULT NULL,
    published_at    DATETIME        DEFAULT NULL,
    collected_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- TradingView-specific fields
    author          VARCHAR(100)    DEFAULT NULL,
    author_badge    VARCHAR(50)     DEFAULT NULL,
    source_detail   VARCHAR(100)    DEFAULT NULL,
    chart_image_url VARCHAR(500)    DEFAULT NULL,
    ai_analysis     MEDIUMTEXT      DEFAULT NULL,
    direction       VARCHAR(10)     DEFAULT NULL,
    boosts          INT             DEFAULT 0,
    comments_count  INT             DEFAULT 0,
    tv_timeframe    VARCHAR(10)     DEFAULT NULL,
    external_id     VARCHAR(100)    DEFAULT NULL,
    media_url       TEXT            DEFAULT NULL,
    media_type      VARCHAR(50)     DEFAULT NULL,
    is_valid        TINYINT(1)      DEFAULT 1,
    UNIQUE KEY uk_hash (hash_key),
    INDEX idx_news_source (source),
    INDEX idx_news_collected (collected_at),
    INDEX idx_news_category (category),
    INDEX idx_news_sentiment (sentiment),
    INDEX idx_news_author (author),
    INDEX idx_news_source_detail (source_detail),
    INDEX idx_news_external_id (external_id)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: alpha_summaries
-- AI-generated summaries for each timeframe
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alpha_summaries (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    account_id        VARCHAR(50)     NOT NULL DEFAULT 'default',
    pane_id           VARCHAR(20)     NOT NULL,
    timeframe_minutes INT             NOT NULL,
    summary_text      MEDIUMTEXT      NOT NULL,
    news_count        INT             DEFAULT 0,
    symbols_mentioned TEXT            DEFAULT NULL,
    sentiment_overall VARCHAR(20)     DEFAULT 'neutral',
    model_used        VARCHAR(50)     DEFAULT NULL,
    token_count       INT             DEFAULT 0,
    cost_usd          DECIMAL(10,6)   DEFAULT 0,
    created_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_summary_pane (pane_id, created_at),
    INDEX idx_summary_tf (timeframe_minutes, created_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: alpha_filter_prefs
-- User preferences for source/market filtering and AI submission
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alpha_filter_prefs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    account_id      VARCHAR(50)     NOT NULL DEFAULT 'default',
    filter_type     ENUM('view', 'ai_submit') NOT NULL,
    source          VARCHAR(100)    NOT NULL,
    sub_source      VARCHAR(100)    DEFAULT NULL,
    sub_sub_source  VARCHAR(100)    DEFAULT NULL,
    user_name       VARCHAR(100)    DEFAULT NULL,
    enabled         BOOLEAN         NOT NULL DEFAULT TRUE,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_filter (account_id, filter_type, source, sub_source, sub_sub_source, user_name),
    INDEX idx_filter_type (filter_type)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: alpha_market_filter_prefs
-- User preferences for market/symbol filtering (independent axis)
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alpha_market_filter_prefs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    account_id      VARCHAR(50)     NOT NULL DEFAULT 'default',
    filter_type     ENUM('view', 'ai_submit') NOT NULL,
    symbol          VARCHAR(20)     NOT NULL,
    enabled         BOOLEAN         NOT NULL DEFAULT TRUE,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_market_filter (account_id, filter_type, symbol),
    INDEX idx_market_type (filter_type)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: tradingview_ideas_tracking
-- Track TradingView ideas for play-forward analysis
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tradingview_ideas_tracking (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    idea_id         VARCHAR(50)     NOT NULL,
    news_item_id    INT             DEFAULT NULL,
    symbol          VARCHAR(20)     NOT NULL,
    direction       VARCHAR(10)     DEFAULT NULL,
    chart_timeframe VARCHAR(10)     DEFAULT NULL,
    author          VARCHAR(100)    DEFAULT NULL,
    entry_price     DECIMAL(15,5)   DEFAULT NULL,
    target_price    DECIMAL(15,5)   DEFAULT NULL,
    stop_price      DECIMAL(15,5)   DEFAULT NULL,
    estimated_expiry DATETIME       DEFAULT NULL,
    status          ENUM('tracking', 'hit_target', 'hit_stop', 'expired', 'unknown') NOT NULL DEFAULT 'tracking',
    initial_chart_url VARCHAR(500)  DEFAULT NULL,
    latest_screenshot VARCHAR(500)  DEFAULT NULL,
    ai_initial_analysis MEDIUMTEXT  DEFAULT NULL,
    ai_latest_analysis  MEDIUMTEXT  DEFAULT NULL,
    ai_score        INT             DEFAULT NULL,
    ai_end_date     DATETIME        DEFAULT NULL,
    check_count     INT             DEFAULT 0,
    last_checked_at DATETIME        DEFAULT NULL,
    -- v5.2 additions for idea_monitor.py
    idea_url        VARCHAR(500)    DEFAULT NULL,
    check_interval_minutes INT      DEFAULT NULL,
    next_check_at   DATETIME        DEFAULT NULL,
    monitoring_end_at DATETIME      DEFAULT NULL,
    outcome         VARCHAR(30)     DEFAULT NULL,
    outcome_score   DECIMAL(5,2)    DEFAULT NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_idea (idea_id),
    INDEX idx_tracking_status (status),
    INDEX idx_tracking_symbol (symbol),
    INDEX idx_tracking_expiry (estimated_expiry),
    INDEX idx_tracking_next_check (next_check_at)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: idea_snapshots
-- Individual check snapshots for tracked ideas
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idea_snapshots (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    tracking_id         INT             NOT NULL,
    idea_id             VARCHAR(50)     NOT NULL,
    check_number        INT             NOT NULL DEFAULT 0,
    check_interval_label VARCHAR(50),
    screenshot_path     VARCHAR(500),
    ai_analysis         MEDIUMTEXT,
    ai_score            DECIMAL(5,2)    DEFAULT NULL,
    ai_opinion_changed  TINYINT(1)      DEFAULT 0,
    comment_count       INT             DEFAULT 0,
    new_comments_json   MEDIUMTEXT,
    status              VARCHAR(20)     DEFAULT 'tracking',
    snapshot_data       JSON,
    created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_snapshot_idea (idea_id),
    INDEX idx_snapshot_tracking (tracking_id)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: idea_comments
-- Comments on TradingView ideas
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS idea_comments (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    idea_id             VARCHAR(50)     NOT NULL,
    comment_id          VARCHAR(50)     NOT NULL,
    author              VARCHAR(100),
    text                MEDIUMTEXT,
    likes               INT             DEFAULT 0,
    parent_id           VARCHAR(50),
    posted_at           DATETIME,
    fetched_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_comment (idea_id, comment_id),
    INDEX idx_comment_idea (idea_id)
) ENGINE=InnoDB;

-- ───────────────────────────────────────────────────────────────────
-- Table: prompt_versions
-- Versioned AI prompts for all roles and data sources
-- ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_versions (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    role            VARCHAR(50)     NOT NULL,
    category        VARCHAR(30)     NOT NULL DEFAULT 'role',
    version         INT             NOT NULL DEFAULT 1,
    prompt_name     VARCHAR(200)    DEFAULT NULL,
    description     TEXT            DEFAULT NULL,
    system_prompt   MEDIUMTEXT      NOT NULL,
    user_prompt_template MEDIUMTEXT  DEFAULT NULL,
    is_active       TINYINT(1)      NOT NULL DEFAULT 0,
    total_trades    INT             DEFAULT 0,
    winning_trades  INT             DEFAULT 0,
    total_pnl       DECIMAL(15,2)   DEFAULT 0.00,
    avg_confidence  DECIMAL(5,2)    DEFAULT 0.00,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at    DATETIME        DEFAULT NULL,
    deactivated_at  DATETIME        DEFAULT NULL,
    INDEX idx_pv_role (role),
    INDEX idx_pv_category (category),
    INDEX idx_pv_active (is_active),
    UNIQUE KEY uk_role_version (role, version)
) ENGINE=InnoDB;
