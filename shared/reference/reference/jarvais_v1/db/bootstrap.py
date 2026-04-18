"""
JarvAIs Database Bootstrap
===========================
Called on every startup from run_dashboard.py.
Creates ALL required tables (idempotent) and seeds prompt_versions + known_models
if they are empty. Safe to run repeatedly — uses CREATE TABLE IF NOT EXISTS
and INSERT IGNORE throughout.

Tables created:
  - prompt_versions (with v6 columns)
  - known_models
  - prompt_change_proposals
  - ceo_daily_reports
  - model_performance
  - alpha_source_scores
  - role_model_history
  - role_score_snapshots
  - system_config
  - user_preferences
  - managed_sources
  - ai_api_log
  - alpha_summaries
  - alpha_filter_prefs
  - alpha_market_filter_prefs
  - news_items
  - trades
  - daily_performance
  - trade_lessons
  - self_discovered_patterns
  - config
  - role_daily_costs
  - memory_audit
  - memory_fallback
  - telegram_channels
  - tradingview_ideas_tracking
  - idea_snapshots
  - idea_comments
  - ea_signals
  - position_events
  - ai_decisions
  - parsed_signals
  - signal_provider_scores
  - signal_update_tracker
  - signal_updates
  - market_symbols
  - user_follow_preferences
  - agent_profiles
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger("jarvais.bootstrap")


# ─────────────────────────────────────────────────────────────────────
# Table Schemas — CREATE TABLE IF NOT EXISTS (idempotent)
# ─────────────────────────────────────────────────────────────────────

TABLES = [
    # ── prompt_versions (full v6 schema) ──
    """CREATE TABLE IF NOT EXISTS prompt_versions (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        role            VARCHAR(50)     NOT NULL,
        category        VARCHAR(30)     NOT NULL DEFAULT 'role',
        version         INT             NOT NULL DEFAULT 1,
        prompt_name     VARCHAR(200)    DEFAULT NULL,
        description     TEXT            DEFAULT NULL,
        system_prompt   MEDIUMTEXT      NOT NULL,
        user_prompt_template MEDIUMTEXT DEFAULT NULL,
        model           VARCHAR(80)     DEFAULT NULL
            COMMENT 'AI model assigned to this prompt version',
        model_provider  VARCHAR(30)     DEFAULT NULL
            COMMENT 'Provider: openrouter, anthropic, google, openai (embeddings/whisper only)',
        is_active       TINYINT(1)      NOT NULL DEFAULT 0,
        total_trades    INT             DEFAULT 0,
        winning_trades  INT             DEFAULT 0,
        losing_trades   INT             DEFAULT 0,
        win_rate        DECIMAL(5,2)    DEFAULT NULL,
        total_pnl       DECIMAL(15,2)   DEFAULT 0.00,
        avg_pnl         DECIMAL(15,2)   DEFAULT 0.00,
        sharpe_ratio    DECIMAL(8,4)    DEFAULT NULL,
        max_drawdown    DECIMAL(15,2)   DEFAULT 0.00,
        total_cost_usd  DECIMAL(10,4)   DEFAULT 0.0000,
        score           DECIMAL(5,1)    DEFAULT NULL,
        changed_by      VARCHAR(50)     DEFAULT 'human_owner',
        change_reason   TEXT            DEFAULT NULL,
        parent_version_id INT           DEFAULT NULL,
        avg_confidence  DECIMAL(5,2)    DEFAULT 0.00,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        activated_at    DATETIME        DEFAULT NULL,
        deactivated_at  DATETIME        DEFAULT NULL,
        UNIQUE KEY uk_role_version (role, version),
        INDEX idx_pv_role (role),
        INDEX idx_pv_category (category),
        INDEX idx_pv_active (is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── known_models ──
    """CREATE TABLE IF NOT EXISTS known_models (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        provider        VARCHAR(30)     NOT NULL,
        model_id        VARCHAR(80)     NOT NULL,
        display_name    VARCHAR(100)    DEFAULT NULL,
        supports_vision TINYINT(1)      DEFAULT 0,
        supports_function_calling TINYINT(1) DEFAULT 0,
        context_window  INT             DEFAULT NULL,
        input_price_per_1m  DECIMAL(10,4) DEFAULT NULL,
        output_price_per_1m DECIMAL(10,4) DEFAULT NULL,
        max_output_tokens INT           DEFAULT NULL COMMENT 'Max output tokens for this model',
        max_tokens_param VARCHAR(30)    DEFAULT 'max_tokens' COMMENT 'Parameter name: max_tokens, max_completion_tokens, max_output_tokens',
        temperature_max DECIMAL(3,1)   DEFAULT 2.0 COMMENT 'Max temperature allowed',
        supports_audio  TINYINT(1)      DEFAULT 0 COMMENT 'Supports audio input',
        supports_video  TINYINT(1)      DEFAULT 0 COMMENT 'Supports video input',
        is_available    TINYINT(1)      NOT NULL DEFAULT 1,
        is_recommended  TINYINT(1)      NOT NULL DEFAULT 0,
        discovered_at   DATETIME        DEFAULT NULL,
        last_verified_at DATETIME       DEFAULT NULL,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE INDEX idx_km_provider_model (provider, model_id),
        INDEX idx_km_available (is_available),
        INDEX idx_km_recommended (is_recommended)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── prompt_change_proposals ──
    """CREATE TABLE IF NOT EXISTS prompt_change_proposals (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        proposing_role  VARCHAR(50)     NOT NULL,
        proposing_model VARCHAR(80)     DEFAULT NULL,
        target_role     VARCHAR(50)     NOT NULL,
        change_type     ENUM('prompt', 'model', 'both') NOT NULL DEFAULT 'prompt',
        current_prompt_id INT           DEFAULT NULL,
        current_model   VARCHAR(80)     DEFAULT NULL,
        proposed_prompt MEDIUMTEXT      DEFAULT NULL,
        proposed_model  VARCHAR(80)     DEFAULT NULL,
        reason          TEXT            NOT NULL,
        evidence        MEDIUMTEXT      NOT NULL,
        expected_improvement TEXT       DEFAULT NULL,
        supporting_trades JSON          DEFAULT NULL,
        supporting_stats JSON           DEFAULT NULL,
        status          ENUM('pending','approved','rejected','implemented','monitoring','expired')
                        NOT NULL DEFAULT 'pending',
        reviewed_by     VARCHAR(50)     DEFAULT NULL,
        review_notes    TEXT            DEFAULT NULL,
        reviewed_at     DATETIME        DEFAULT NULL,
        implemented_prompt_id INT       DEFAULT NULL,
        monitoring_start DATETIME       DEFAULT NULL,
        monitoring_end  DATETIME        DEFAULT NULL,
        monitoring_trades INT           DEFAULT 0,
        monitoring_win_rate DECIMAL(5,2) DEFAULT NULL,
        monitoring_pnl  DECIMAL(15,2)   DEFAULT NULL,
        monitoring_verdict ENUM('improved','no_change','worse','pending') DEFAULT 'pending',
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_pcp_status (status),
        INDEX idx_pcp_target (target_role),
        INDEX idx_pcp_proposer (proposing_role),
        INDEX idx_pcp_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── ceo_daily_reports ──
    """CREATE TABLE IF NOT EXISTS ceo_daily_reports (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'global',
        report_date     DATE            NOT NULL,
        report_type     ENUM('daily','day_so_far') NOT NULL DEFAULT 'daily',
        model_used      VARCHAR(80)     DEFAULT NULL,
        executive_summary MEDIUMTEXT    NOT NULL,
        pnl_summary     TEXT            DEFAULT NULL,
        trade_analysis  MEDIUMTEXT      DEFAULT NULL,
        role_performance MEDIUMTEXT     DEFAULT NULL,
        mistake_analysis MEDIUMTEXT     DEFAULT NULL,
        alpha_quality   TEXT            DEFAULT NULL,
        pending_proposals TEXT          DEFAULT NULL,
        recommendations MEDIUMTEXT      DEFAULT NULL,
        total_trades    INT             DEFAULT 0,
        total_pnl       DECIMAL(15,2)   DEFAULT 0.00,
        win_rate        DECIMAL(5,2)    DEFAULT NULL,
        total_api_cost  DECIMAL(10,4)   DEFAULT 0.00,
        sharpe_ratio    DECIMAL(8,4)    DEFAULT NULL,
        mistakes_identified INT         DEFAULT 0,
        recurring_mistakes INT          DEFAULT 0,
        token_count     INT             DEFAULT 0,
        cost_usd        DECIMAL(10,6)   DEFAULT 0.00,
        generated_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_cdr_date (report_date),
        INDEX idx_cdr_type (report_type),
        INDEX idx_cdr_account (account_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── model_performance ──
    """CREATE TABLE IF NOT EXISTS model_performance (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        model           VARCHAR(80)     NOT NULL,
        provider        VARCHAR(30)     NOT NULL,
        role            VARCHAR(50)     NOT NULL,
        period_start    DATE            NOT NULL,
        period_end      DATE            DEFAULT NULL,
        total_trades    INT             DEFAULT 0,
        winning_trades  INT             DEFAULT 0,
        losing_trades   INT             DEFAULT 0,
        win_rate        DECIMAL(5,2)    DEFAULT NULL,
        total_pnl       DECIMAL(15,2)   DEFAULT 0.00,
        avg_pnl         DECIMAL(15,2)   DEFAULT 0.00,
        sharpe_ratio    DECIMAL(8,4)    DEFAULT NULL,
        max_drawdown    DECIMAL(15,2)   DEFAULT 0.00,
        total_api_calls INT             DEFAULT 0,
        total_input_tokens BIGINT       DEFAULT 0,
        total_output_tokens BIGINT      DEFAULT 0,
        total_cost_usd  DECIMAL(10,4)   DEFAULT 0.00,
        avg_latency_ms  INT             DEFAULT 0,
        avg_confidence  DECIMAL(5,2)    DEFAULT NULL,
        veto_rate       DECIMAL(5,2)    DEFAULT NULL,
        score           DECIMAL(5,1)    DEFAULT NULL,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_mp_model (model),
        INDEX idx_mp_role (role),
        INDEX idx_mp_provider (provider),
        INDEX idx_mp_score (score),
        UNIQUE INDEX idx_mp_model_role_period (model, role, period_start)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── alpha_source_scores ──
    """CREATE TABLE IF NOT EXISTS alpha_source_scores (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        source          VARCHAR(100)    NOT NULL,
        sub_source      VARCHAR(100)    DEFAULT NULL,
        sub_sub_source  VARCHAR(100)    DEFAULT NULL,
        period_start    DATE            NOT NULL,
        period_end      DATE            DEFAULT NULL,
        times_cited     INT             DEFAULT 0,
        times_cited_winner INT          DEFAULT 0,
        times_cited_loser INT           DEFAULT 0,
        total_pnl_when_cited DECIMAL(15,2) DEFAULT 0.00,
        avg_pnl_when_cited DECIMAL(15,2) DEFAULT 0.00,
        win_rate_when_cited DECIMAL(5,2) DEFAULT NULL,
        signal_accuracy DECIMAL(5,2)    DEFAULT NULL,
        timeliness_score DECIMAL(5,2)   DEFAULT NULL,
        score           DECIMAL(5,1)    DEFAULT NULL,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ass_source (source),
        INDEX idx_ass_sub (sub_source),
        INDEX idx_ass_subsub (sub_sub_source),
        INDEX idx_ass_score (score),
        INDEX idx_ass_period (period_start)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── role_model_history ──
    """CREATE TABLE IF NOT EXISTS role_model_history (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'global',
        role            VARCHAR(30)     NOT NULL,
        model           VARCHAR(80)     NOT NULL,
        assigned_at     DATETIME        NOT NULL,
        replaced_at     DATETIME        DEFAULT NULL,
        signals_processed INT           NOT NULL DEFAULT 0,
        trades_executed INT             NOT NULL DEFAULT 0,
        wins            INT             NOT NULL DEFAULT 0,
        losses          INT             NOT NULL DEFAULT 0,
        win_rate        DECIMAL(5,2)    DEFAULT NULL,
        total_pnl       DECIMAL(15,2)   NOT NULL DEFAULT 0.00,
        avg_confidence  DECIMAL(5,2)    DEFAULT NULL,
        total_api_calls INT             NOT NULL DEFAULT 0,
        total_input_tokens BIGINT       NOT NULL DEFAULT 0,
        total_output_tokens BIGINT      NOT NULL DEFAULT 0,
        total_cost_usd  DECIMAL(10,6)   NOT NULL DEFAULT 0.000000,
        avg_latency_ms  INT             NOT NULL DEFAULT 0,
        score           DECIMAL(5,1)    DEFAULT NULL,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        KEY idx_rmh_account_role (account_id, role),
        KEY idx_rmh_model (model),
        KEY idx_rmh_assigned (assigned_at),
        KEY idx_rmh_active (replaced_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── role_score_snapshots ──
    """CREATE TABLE IF NOT EXISTS role_score_snapshots (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL,
        date            DATE            NOT NULL,
        trader_score    DECIMAL(5,1)    DEFAULT NULL,
        coach_score     DECIMAL(5,1)    DEFAULT NULL,
        analyst_score   DECIMAL(5,1)    DEFAULT NULL,
        postmortem_score DECIMAL(5,1)   DEFAULT NULL,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        total_trades    INT             DEFAULT 0,
        total_pnl       DECIMAL(15,2)   DEFAULT 0.00,
        win_rate        DECIMAL(5,2)    DEFAULT 0.00,
        UNIQUE KEY idx_rss_account_date (account_id, date),
        KEY idx_rss_date (date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── system_config ──
    """CREATE TABLE IF NOT EXISTS system_config (
        config_key      VARCHAR(100)    PRIMARY KEY,
        config_value    TEXT            NOT NULL,
        description     TEXT,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── user_preferences ──
    """CREATE TABLE IF NOT EXISTS user_preferences (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        pref_key        VARCHAR(100)    NOT NULL,
        pref_value      TEXT,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_pref_key (pref_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── managed_sources ──
    """CREATE TABLE IF NOT EXISTS managed_sources (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        source_type     ENUM('tradingview','rss','telegram','discord','mt5','manual') NOT NULL,
        name            VARCHAR(200)    NOT NULL,
        category        VARCHAR(50)     DEFAULT 'news',
        config          JSON            DEFAULT NULL,
        enabled         TINYINT(1)      DEFAULT 1,
        trigger_mode    ENUM('immediate','confidence') DEFAULT 'confidence',
        score           FLOAT           DEFAULT 0,
        total_signals   INT             DEFAULT 0,
        winning_signals INT             DEFAULT 0,
        total_pnl       FLOAT           DEFAULT 0,
        download_media  TINYINT(1)      DEFAULT 0,
        created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_source (source_type, name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── ea_signals (required for trades; CEO report and DB layer expect trades) ──
    """CREATE TABLE IF NOT EXISTS ea_signals (
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
        hypothetical_entry  DECIMAL(15,5)   DEFAULT NULL,
        hypothetical_exit   DECIMAL(15,5)   DEFAULT NULL,
        hypothetical_pnl    DECIMAL(15,2)   DEFAULT NULL,
        hypothetical_closed BOOLEAN         NOT NULL DEFAULT FALSE,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_signals_account_ts (account_id, timestamp),
        INDEX idx_signals_symbol (symbol),
        INDEX idx_signals_status (status),
        INDEX idx_signals_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── ai_decisions (required for trades FK) ──
    """CREATE TABLE IF NOT EXISTS ai_decisions (
        id                              INT AUTO_INCREMENT PRIMARY KEY,
        signal_id                       INT             NOT NULL,
        account_id                      VARCHAR(50)     NOT NULL,
        timestamp                       DATETIME(3)     NOT NULL,
        model_used                      VARCHAR(50)     NOT NULL,
        decision                        ENUM('Approve','Veto') NOT NULL,
        confidence                      INT             NOT NULL,
        trader_reasoning                TEXT            DEFAULT NULL,
        coach_assessment                TEXT            DEFAULT NULL,
        coach_challenges                TEXT            DEFAULT NULL,
        trader_response_to_challenges   TEXT            DEFAULT NULL,
        full_dossier                    MEDIUMTEXT      DEFAULT NULL,
        full_dialogue                   MEDIUMTEXT      DEFAULT NULL,
        suggested_lot_modifier          DECIMAL(3,2)    DEFAULT 1.00,
        suggested_tp_levels            JSON            DEFAULT NULL,
        suggested_sl_level              DECIMAL(15,5)   DEFAULT NULL,
        is_free_trade                   BOOLEAN         NOT NULL DEFAULT FALSE,
        risk_mode                       VARCHAR(30)     DEFAULT NULL,
        token_count_input               INT             DEFAULT 0,
        token_count_output             INT             DEFAULT 0,
        api_cost_usd                    DECIMAL(10,6)   DEFAULT 0,
        latency_ms                      INT             DEFAULT 0,
        created_at                      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (signal_id) REFERENCES ea_signals(id) ON DELETE CASCADE,
        INDEX idx_decisions_account (account_id),
        INDEX idx_decisions_signal (signal_id),
        INDEX idx_decisions_decision (decision),
        INDEX idx_decisions_confidence (confidence)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── trades (CEO daily report and database layer; depends on ea_signals, ai_decisions) ──
    """CREATE TABLE IF NOT EXISTS trades (
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
        be_triggered            BOOLEAN         NOT NULL DEFAULT FALSE,
        be_trigger_time         DATETIME(3)     DEFAULT NULL,
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
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── position_events (FK trades) ──
    """CREATE TABLE IF NOT EXISTS position_events (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        trade_id        INT             NOT NULL,
        account_id      VARCHAR(50)     NOT NULL,
        timestamp       DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
        event_type      VARCHAR(50)     NOT NULL,
        old_value       VARCHAR(100)    DEFAULT NULL,
        new_value       VARCHAR(100)    DEFAULT NULL,
        description     TEXT            DEFAULT NULL,
        current_price   DECIMAL(15,5)   DEFAULT NULL,
        current_pnl     DECIMAL(15,2)   DEFAULT NULL,
        FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE,
        INDEX idx_events_trade (trade_id),
        INDEX idx_events_account_ts (account_id, timestamp)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── trade_lessons (FK trades, ea_signals, trade_dossiers) ──
    """CREATE TABLE IF NOT EXISTS trade_lessons (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        trade_id                INT             DEFAULT NULL,
        dossier_id              INT             DEFAULT NULL,
        signal_id               INT             DEFAULT NULL,
        symbol                  VARCHAR(20)     DEFAULT NULL,
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
        root_cause              TEXT            DEFAULT NULL,
        optimal_trade_summary   TEXT            DEFAULT NULL,
        confidence_calibration  TEXT            DEFAULT NULL,
        full_post_mortem        MEDIUMTEXT      DEFAULT NULL,
        technical_factors       JSON            DEFAULT NULL,
        timing_factors          JSON            DEFAULT NULL,
        fundamental_factors     JSON            DEFAULT NULL,
        decision_quality        JSON            DEFAULT NULL,
        execution_factors       JSON            DEFAULT NULL,
        qdrant_vector_id        VARCHAR(100)    DEFAULT NULL,
        token_count             INT             DEFAULT 0,
        api_cost_usd            DECIMAL(10,6)   DEFAULT 0,
        FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE CASCADE,
        FOREIGN KEY (signal_id) REFERENCES parsed_signals(id) ON DELETE SET NULL,
        INDEX idx_lessons_account (account_id),
        INDEX idx_lessons_outcome (outcome),
        INDEX idx_lessons_trade (trade_id),
        INDEX idx_lessons_dossier (dossier_id),
        INDEX idx_lessons_symbol (symbol)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── daily_performance ──
    """CREATE TABLE IF NOT EXISTS daily_performance (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        account_id              VARCHAR(50)     NOT NULL,
        date                    DATE            NOT NULL,
        total_signals           INT             NOT NULL DEFAULT 0,
        signals_approved        INT             NOT NULL DEFAULT 0,
        signals_vetoed          INT             NOT NULL DEFAULT 0,
        signals_rejected_risk  INT             NOT NULL DEFAULT 0,
        total_trades            INT             NOT NULL DEFAULT 0,
        wins                    INT             NOT NULL DEFAULT 0,
        losses                  INT             NOT NULL DEFAULT 0,
        breakevens              INT             NOT NULL DEFAULT 0,
        win_rate                DECIMAL(5,2)    DEFAULT NULL,
        total_pnl               DECIMAL(15,2)   NOT NULL DEFAULT 0,
        ea_hypothetical_pnl     DECIMAL(15,2)   NOT NULL DEFAULT 0,
        ai_alpha                DECIMAL(15,2)   NOT NULL DEFAULT 0,
        free_trades_count       INT             NOT NULL DEFAULT 0,
        free_trades_pnl         DECIMAL(15,2)   NOT NULL DEFAULT 0,
        avg_confidence          DECIMAL(5,2)    DEFAULT NULL,
        avg_confidence_winners DECIMAL(5,2)    DEFAULT NULL,
        avg_confidence_losers   DECIMAL(5,2)    DEFAULT NULL,
        max_drawdown            DECIMAL(15,2)   DEFAULT 0,
        max_drawdown_pct        DECIMAL(5,2)    DEFAULT 0,
        total_api_cost          DECIMAL(10,4)   NOT NULL DEFAULT 0,
        total_token_input       INT             NOT NULL DEFAULT 0,
        total_token_output      INT             NOT NULL DEFAULT 0,
        maturity_phase          INT             NOT NULL DEFAULT 1,
        daily_review_summary    TEXT            DEFAULT NULL,
        balance_eod             DECIMAL(15,2)   DEFAULT NULL,
        balance_sod             DECIMAL(15,2)   DEFAULT NULL,
        created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE INDEX idx_daily_account_date (account_id, date),
        INDEX idx_daily_date (date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── config ──
    """CREATE TABLE IF NOT EXISTS config (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'global',
        config_key      VARCHAR(100)    NOT NULL,
        config_value    TEXT            DEFAULT NULL,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        updated_by      VARCHAR(50)     NOT NULL DEFAULT 'user',
        UNIQUE INDEX idx_config_account_key (account_id, config_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── self_discovered_patterns ──
    """CREATE TABLE IF NOT EXISTS self_discovered_patterns (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        account_id          VARCHAR(50)     NOT NULL DEFAULT 'collective',
        discovered_date     DATE            NOT NULL,
        pattern_description TEXT            NOT NULL,
        conditions          JSON            DEFAULT NULL,
        sample_size         INT             NOT NULL DEFAULT 0,
        win_rate            DECIMAL(5,2)    DEFAULT NULL,
        avg_pnl             DECIMAL(15,2)   DEFAULT NULL,
        avg_confidence      DECIMAL(5,2)    DEFAULT NULL,
        confidence_level    ENUM('Low','Medium','High','Very High') NOT NULL DEFAULT 'Low',
        is_active           TINYINT(1)      NOT NULL DEFAULT 1,
        last_validated      DATE            DEFAULT NULL,
        validation_notes    TEXT            DEFAULT NULL,
        qdrant_vector_id    VARCHAR(100)    DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_patterns_account (account_id),
        INDEX idx_patterns_active (is_active),
        INDEX idx_patterns_confidence (confidence_level)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── ai_api_log (must exist for cost logging and bootstrap ALTERs) ──
    """CREATE TABLE IF NOT EXISTS ai_api_log (
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
        actual_cost_usd         DECIMAL(12,6)   DEFAULT NULL COMMENT 'Real cost from OpenRouter usage.total_cost',
        latency_ms              INT             NOT NULL DEFAULT 0,
        success                 BOOLEAN         NOT NULL DEFAULT TRUE,
        error_message           TEXT            DEFAULT NULL,
        created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_api_log_account (account_id),
        INDEX idx_api_log_ts (timestamp),
        INDEX idx_api_log_provider (provider, model)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── news_items (must exist before parsed_signals; collectors and alpha feed) ──
    """CREATE TABLE IF NOT EXISTS news_items (
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
        author_name     VARCHAR(200)    DEFAULT NULL,
        is_valid        TINYINT(1)      DEFAULT 1,
        UNIQUE KEY uk_hash (hash_key),
        INDEX idx_news_source (source),
        INDEX idx_news_collected (collected_at),
        INDEX idx_news_category (category),
        INDEX idx_news_sentiment (sentiment),
        INDEX idx_news_author (author),
        INDEX idx_news_source_detail (source_detail),
        INDEX idx_news_external_id (external_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── alpha_summaries ──
    """CREATE TABLE IF NOT EXISTS alpha_summaries (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        account_id          VARCHAR(50)     NOT NULL DEFAULT 'default',
        pane_id             VARCHAR(20)     NOT NULL,
        timeframe_minutes   INT             NOT NULL,
        summary_text        MEDIUMTEXT      NOT NULL,
        news_count          INT             DEFAULT 0,
        symbols_mentioned   TEXT            DEFAULT NULL,
        sentiment_overall   VARCHAR(20)     DEFAULT 'neutral',
        model_used          VARCHAR(50)     DEFAULT NULL,
        prompt_version_id   INT             DEFAULT NULL,
        model_provider      VARCHAR(30)     DEFAULT NULL,
        token_count         INT             DEFAULT 0,
        cost_usd            DECIMAL(10,6)   DEFAULT 0,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_summary_pane (pane_id, created_at),
        INDEX idx_summary_tf (timeframe_minutes, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── alpha_filter_prefs ──
    """CREATE TABLE IF NOT EXISTS alpha_filter_prefs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'default',
        filter_type     ENUM('view', 'ai_submit') NOT NULL,
        source          VARCHAR(100)    NOT NULL,
        sub_source      VARCHAR(100)    DEFAULT NULL,
        sub_sub_source  VARCHAR(100)    DEFAULT NULL,
        user_name       VARCHAR(100)    DEFAULT NULL,
        enabled         TINYINT(1)      NOT NULL DEFAULT 1,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_filter (account_id, filter_type, source, sub_source, sub_sub_source, user_name),
        INDEX idx_filter_type (filter_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── alpha_market_filter_prefs ──
    """CREATE TABLE IF NOT EXISTS alpha_market_filter_prefs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'default',
        filter_type     ENUM('view', 'ai_submit') NOT NULL,
        symbol          VARCHAR(20)     NOT NULL,
        enabled         TINYINT(1)      NOT NULL DEFAULT 1,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_market_filter (account_id, filter_type, symbol),
        INDEX idx_market_type (filter_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── tradingview_ideas_tracking ──
    """CREATE TABLE IF NOT EXISTS tradingview_ideas_tracking (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        idea_id             VARCHAR(50)     NOT NULL,
        news_item_id        INT             DEFAULT NULL,
        symbol              VARCHAR(20)     NOT NULL,
        direction           VARCHAR(10)     DEFAULT NULL,
        chart_timeframe     VARCHAR(10)     DEFAULT NULL,
        author              VARCHAR(100)    DEFAULT NULL,
        entry_price         DECIMAL(15,5)   DEFAULT NULL,
        target_price        DECIMAL(15,5)   DEFAULT NULL,
        stop_price          DECIMAL(15,5)   DEFAULT NULL,
        estimated_expiry    DATETIME        DEFAULT NULL,
        status              ENUM('tracking','hit_target','hit_stop','expired','stopped','unknown') NOT NULL DEFAULT 'tracking',
        initial_chart_url   VARCHAR(500)    DEFAULT NULL,
        latest_screenshot   VARCHAR(500)    DEFAULT NULL,
        ai_initial_analysis MEDIUMTEXT      DEFAULT NULL,
        ai_latest_analysis  MEDIUMTEXT      DEFAULT NULL,
        ai_score            INT             DEFAULT NULL,
        ai_end_date         DATETIME        DEFAULT NULL,
        check_count         INT             DEFAULT 0,
        last_checked_at     DATETIME        DEFAULT NULL,
        idea_url            VARCHAR(500)    DEFAULT NULL,
        check_interval_minutes INT         DEFAULT NULL,
        next_check_at       DATETIME        DEFAULT NULL,
        monitoring_end_at   DATETIME        DEFAULT NULL,
        outcome             VARCHAR(30)     DEFAULT NULL,
        outcome_score       DECIMAL(5,2)    DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_idea (idea_id),
        INDEX idx_tracking_status (status),
        INDEX idx_tracking_symbol (symbol),
        INDEX idx_tracking_expiry (estimated_expiry),
        INDEX idx_tracking_next_check (next_check_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── idea_snapshots ──
    """CREATE TABLE IF NOT EXISTS idea_snapshots (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        tracking_id         INT             NOT NULL,
        idea_id             VARCHAR(50)     NOT NULL,
        check_number        INT             NOT NULL DEFAULT 0,
        check_interval_label VARCHAR(50)    DEFAULT NULL,
        screenshot_path     VARCHAR(500)    DEFAULT NULL,
        ai_analysis         MEDIUMTEXT      DEFAULT NULL,
        ai_score            DECIMAL(5,2)    DEFAULT NULL,
        ai_opinion_changed  TINYINT(1)      DEFAULT 0,
        comment_count       INT             DEFAULT 0,
        new_comments_json   MEDIUMTEXT      DEFAULT NULL,
        status              VARCHAR(20)     DEFAULT 'tracking',
        snapshot_data       JSON            DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_snapshot_idea (idea_id),
        INDEX idx_snapshot_tracking (tracking_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── idea_comments ──
    """CREATE TABLE IF NOT EXISTS idea_comments (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        idea_id     VARCHAR(50)     NOT NULL,
        comment_id  VARCHAR(50)     NOT NULL,
        author      VARCHAR(100)    DEFAULT NULL,
        text        MEDIUMTEXT      DEFAULT NULL,
        likes       INT             DEFAULT 0,
        parent_id   VARCHAR(50)     DEFAULT NULL,
        posted_at   DATETIME        DEFAULT NULL,
        fetched_at  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_comment (idea_id, comment_id),
        INDEX idx_comment_idea (idea_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── role_daily_costs ──
    """CREATE TABLE IF NOT EXISTS role_daily_costs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_id      VARCHAR(50)     NOT NULL DEFAULT 'global',
        date            DATE            NOT NULL,
        role            VARCHAR(30)     NOT NULL,
        model           VARCHAR(80)     DEFAULT NULL,
        provider        VARCHAR(30)     DEFAULT NULL,
        api_calls       INT             NOT NULL DEFAULT 0,
        input_tokens    BIGINT          NOT NULL DEFAULT 0,
        output_tokens   BIGINT          NOT NULL DEFAULT 0,
        cost_usd        DECIMAL(10,6)   NOT NULL DEFAULT 0,
        signals_processed INT           NOT NULL DEFAULT 0,
        trades_contributed INT          NOT NULL DEFAULT 0,
        pnl_contribution DECIMAL(15,2)  NOT NULL DEFAULT 0.00,
        avg_latency_ms  INT             NOT NULL DEFAULT 0,
        max_latency_ms  INT             NOT NULL DEFAULT 0,
        error_count     INT             NOT NULL DEFAULT 0,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_rdc_account_date_role (account_id, date, role),
        INDEX idx_rdc_date (date),
        INDEX idx_rdc_role (role)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── Signal AI tables ──
    """CREATE TABLE IF NOT EXISTS parsed_signals (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        news_item_id    INT             NOT NULL,
        source          VARCHAR(100)    DEFAULT NULL,
        source_detail   VARCHAR(200)    DEFAULT NULL,
        author          VARCHAR(200)    DEFAULT NULL,
        author_badge    VARCHAR(50)     DEFAULT NULL,
        symbol          VARCHAR(20)     DEFAULT NULL,
        direction       VARCHAR(10)     DEFAULT NULL,
        order_type      VARCHAR(20)     DEFAULT 'limit',
        entry_price     DECIMAL(15,5)   DEFAULT NULL,
        entry_price_max DECIMAL(15,5)   DEFAULT 0,
        stop_loss       DECIMAL(15,5)   DEFAULT NULL,
        sl_source       VARCHAR(20)     DEFAULT 'human' COMMENT 'human, ai_generated, or policy',
        take_profit_1   DECIMAL(15,5)   DEFAULT NULL,
        take_profit_2   DECIMAL(15,5)   DEFAULT NULL,
        take_profit_3   DECIMAL(15,5)   DEFAULT NULL,
        take_profit_4   DECIMAL(15,5)   DEFAULT NULL,
        take_profit_5   DECIMAL(15,5)   DEFAULT NULL,
        take_profit_6   DECIMAL(15,5)   DEFAULT NULL,
        tp_source       VARCHAR(20)     DEFAULT 'human' COMMENT 'human, ai_generated, or policy',
        timeframe       VARCHAR(10)     DEFAULT NULL,
        confidence      INT             DEFAULT 0,
        signal_type     VARCHAR(30)     DEFAULT 'entry',
        tier            VARCHAR(20)     DEFAULT 'full',
        risk_reward     DECIMAL(5,2)    DEFAULT 0,
        raw_text        TEXT            DEFAULT NULL,
        ai_reasoning    TEXT            DEFAULT NULL,
        parsed_by       VARCHAR(50)     DEFAULT 'signal_ai',
        entry_distance_pct DECIMAL(8,2)  DEFAULT NULL,
        sentiment_score DECIMAL(3,2)    DEFAULT NULL,
        conviction_score DECIMAL(3,2)   DEFAULT NULL,
        trader_view     VARCHAR(500)    DEFAULT NULL,
        source_media    VARCHAR(20)     DEFAULT 'text',
        reasoning       TEXT            DEFAULT NULL,
        status          VARCHAR(20)     DEFAULT 'pending',
        entry_hit_at    DATETIME(3)     DEFAULT NULL,
        entry_actual    DECIMAL(15,5)   DEFAULT 0,
        sl_hit_at       DATETIME(3)     DEFAULT NULL,
        tp1_hit_at      DATETIME(3)     DEFAULT NULL,
        tp2_hit_at      DATETIME(3)     DEFAULT NULL,
        tp3_hit_at      DATETIME(3)     DEFAULT NULL,
        tp4_hit_at      DATETIME(3)     DEFAULT NULL,
        tp5_hit_at      DATETIME(3)     DEFAULT NULL,
        tp6_hit_at      DATETIME(3)     DEFAULT NULL,
        highest_price   DECIMAL(15,5)   DEFAULT 0,
        lowest_price    DECIMAL(15,5)   DEFAULT 0,
        max_favorable   DECIMAL(10,1)   DEFAULT 0,
        max_adverse     DECIMAL(10,1)   DEFAULT 0,
        outcome         VARCHAR(20)     DEFAULT NULL,
        outcome_pips    DECIMAL(10,2)   DEFAULT NULL,
        outcome_rr      DECIMAL(5,2)    DEFAULT 0,
        outcome_tp_hit  INT             DEFAULT NULL COMMENT 'Which TP was hit (1-6)',
        outcome_notes   TEXT            DEFAULT NULL,
        resolution_method VARCHAR(50)   DEFAULT NULL,
        post_mortem     TEXT            DEFAULT NULL,
        lesson          TEXT            DEFAULT NULL,
        model_used      VARCHAR(80)     DEFAULT NULL,
        prompt_version_id INT           DEFAULT NULL,
        cost_usd        DECIMAL(10,6)   DEFAULT 0,
        parent_signal_id INT            DEFAULT NULL,
        expires_at      DATETIME(3)     DEFAULT NULL,
        is_valid        TINYINT(1)      DEFAULT 1,
        signal_source   VARCHAR(20)     DEFAULT 'human' COMMENT 'human or jarvis',
        trader_confidence DECIMAL(5,4)  DEFAULT NULL COMMENT 'Trader confidence 0-1',
        ai_confidence   DECIMAL(5,4)    DEFAULT NULL COMMENT 'AI confidence in trade 0-1',
        trader_rationale TEXT           DEFAULT NULL,
        ai_rationale    TEXT            DEFAULT NULL,
        trade_type      VARCHAR(20)     DEFAULT NULL COMMENT 'swing, scalp, degen, position',
        parsed_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        resolved_at     DATETIME        DEFAULT NULL,
        updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ps_symbol (symbol),
        INDEX idx_ps_status (status),
        INDEX idx_ps_source (source),
        INDEX idx_ps_parsed (parsed_at),
        INDEX idx_ps_news (news_item_id),
        INDEX idx_ps_signal_source (signal_source)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS signal_provider_scores (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        source          VARCHAR(100)    NOT NULL,
        source_detail   VARCHAR(200)    NOT NULL,
        author          VARCHAR(100)    DEFAULT NULL,
        total_signals   INT             DEFAULT 0,
        valid_signals   INT             DEFAULT 0,
        entry_hit       INT             DEFAULT 0,
        tp1_hit         INT             DEFAULT 0,
        tp2_hit         INT             DEFAULT 0,
        tp3_hit         INT             DEFAULT 0,
        tp4_hit         INT             DEFAULT 0,
        tp5_hit         INT             DEFAULT 0,
        tp6_hit         INT             DEFAULT 0,
        sl_hit          INT             DEFAULT 0,
        missed          INT             DEFAULT 0,
        expired         INT             DEFAULT 0,
        total_pips      DECIMAL(10,1)   DEFAULT 0.0,
        avg_pips        DECIMAL(10,1)   DEFAULT 0.0,
        win_rate        DECIMAL(5,2)    DEFAULT 0.00,
        avg_rr          DECIMAL(5,2)    DEFAULT 0.00,
        trust_score     DECIMAL(5,2)    DEFAULT 0.00,
        streak          INT             DEFAULT 0,
        best_streak     INT             DEFAULT 0,
        worst_streak    INT             DEFAULT 0,
        last_signal_at  DATETIME        DEFAULT NULL,
        created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
        updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_provider (source, source_detail, author),
        INDEX idx_trust (trust_score)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS signal_update_tracker (
        id                      INT PRIMARY KEY,
        last_checked_news_id    INT             DEFAULT 0,
        last_checked_at         DATETIME        DEFAULT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS signal_updates (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        signal_id       INT             NOT NULL,
        parsed_signal_id INT            NOT NULL,
        news_item_id    INT             DEFAULT NULL,
        update_type     VARCHAR(50)     NOT NULL,
        matched_symbol  VARCHAR(20)     DEFAULT NULL,
        old_value       TEXT            DEFAULT NULL,
        new_value       TEXT            DEFAULT NULL,
        pips_mentioned  DECIMAL(10,1)   DEFAULT NULL,
        new_sl          DECIMAL(15,5)   DEFAULT NULL,
        new_tp          DECIMAL(15,5)   DEFAULT NULL,
        should_close    TINYINT(1)      DEFAULT 0,
        close_portion   VARCHAR(20)     DEFAULT NULL,
        ai_reasoning    TEXT            DEFAULT NULL,
        model_used      VARCHAR(80)     DEFAULT NULL,
        cost_usd        DECIMAL(10,6)   DEFAULT 0,
        reasoning       TEXT            DEFAULT NULL,
        created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_signal (signal_id),
        INDEX idx_news_item (news_item_id),
        INDEX idx_su_signal (parsed_signal_id),
        INDEX idx_su_type (update_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── source_model_assignments: which AI model to use per source x media type ──
    """CREATE TABLE IF NOT EXISTS source_model_assignments (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        source          VARCHAR(50)     NOT NULL,
        media_type      ENUM('text', 'image', 'voice', 'video') NOT NULL,
        model_id        VARCHAR(80)     NOT NULL,
        model_provider  VARCHAR(30)     NOT NULL,
        is_enabled      TINYINT(1)      NOT NULL DEFAULT 1,
        updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_source_media (source, media_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── media_jobs: background queue for voice/video processing ──
    """CREATE TABLE IF NOT EXISTS media_jobs (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        news_item_id    INT             DEFAULT NULL,
        source          VARCHAR(50)     NOT NULL,
        source_detail   VARCHAR(200)    DEFAULT NULL,
        author          VARCHAR(200)    DEFAULT NULL,
        media_type      ENUM('voice', 'video') NOT NULL,
        media_url       TEXT            NOT NULL,
        local_path      VARCHAR(500)    DEFAULT NULL,
        status          ENUM('queued','downloading','transcribing','extracting_frames',
                             'analyzing','complete','failed') NOT NULL DEFAULT 'queued',
        progress_pct    INT             DEFAULT 0,
        error_message   TEXT            DEFAULT NULL,
        transcript_text MEDIUMTEXT      DEFAULT NULL,
        keyframe_paths  JSON            DEFAULT NULL,
        ai_analysis     MEDIUMTEXT      DEFAULT NULL,
        total_cost_usd  DECIMAL(10,6)   DEFAULT 0,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at    DATETIME        DEFAULT NULL,
        INDEX idx_mj_status (status),
        INDEX idx_mj_news (news_item_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── notifications: lightweight notification system ──
    """CREATE TABLE IF NOT EXISTS notifications (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        type            VARCHAR(50)     NOT NULL DEFAULT 'system',
        title           VARCHAR(300)    NOT NULL,
        detail          MEDIUMTEXT      DEFAULT NULL,
        is_read         TINYINT(1)      NOT NULL DEFAULT 0,
        created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_notif_read (is_read),
        INDEX idx_notif_type (type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── mt5_accounts: MT5 accounts from Configuration UI (v8) ──
    """CREATE TABLE IF NOT EXISTS mt5_accounts (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        account_name    VARCHAR(100)    NOT NULL COMMENT 'Friendly name e.g. RoboForex Live',
        mt5_login       VARCHAR(50)     NOT NULL COMMENT 'MT5 account number',
        mt5_password    VARCHAR(200)    DEFAULT '' COMMENT 'Trading password',
        mt5_server      VARCHAR(100)    DEFAULT '' COMMENT 'MT5 server e.g. RoboForex-ECN',
        account_type    ENUM('live','demo') DEFAULT 'demo',
        base_currency   VARCHAR(10)     DEFAULT 'USD',
        max_risk_pct    DECIMAL(5,2)    DEFAULT 1.00 COMMENT 'Max risk % per trade',
        is_active       TINYINT(1)      DEFAULT 1,
        created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
        updated_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_mt5_login (mt5_login)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── agent_profiles (Agentic Company — 22 AI agents with hierarchy, souls, models) ──
    """CREATE TABLE IF NOT EXISTS agent_profiles (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        agent_id            VARCHAR(30)     NOT NULL UNIQUE
            COMMENT 'Unique slug: atlas, elon, apex, etc.',
        display_name        VARCHAR(100)    NOT NULL,
        emoji               VARCHAR(10)     DEFAULT NULL,
        title               VARCHAR(100)    DEFAULT NULL
            COMMENT 'Job title: COO, CTO, Lead Developer, etc.',
        department          VARCHAR(30)     DEFAULT NULL
            COMMENT 'executive, engineering, trading, compliance',
        soul                MEDIUMTEXT      DEFAULT NULL
            COMMENT 'Agent personality and philosophy (Muddy-OS style)',
        identity_prompt     MEDIUMTEXT      DEFAULT NULL
            COMMENT 'Operational identity: how the agent introduces itself in conversations',
        reports_to          VARCHAR(30)     DEFAULT NULL
            COMMENT 'FK to another agent_id — defines hierarchy',
        delegation_policy   ENUM('none','propose','execute') NOT NULL DEFAULT 'none'
            COMMENT 'none=worker, propose=suggests delegating, execute=delegates autonomously',
        can_delegate_to     JSON            DEFAULT NULL
            COMMENT 'List of agent_ids this agent can delegate work to',
        expertise_tags      JSON            DEFAULT NULL
            COMMENT 'Semantic tags for intelligent routing: ["trading","gold","scalping"]',
        tools               JSON            DEFAULT NULL
            COMMENT 'List of tool names this agent has access to',
        free_model_primary  VARCHAR(120)    DEFAULT NULL,
        free_model_fallback VARCHAR(120)    DEFAULT NULL,
        paid_model_primary  VARCHAR(120)    DEFAULT NULL,
        paid_model_fallback VARCHAR(120)    DEFAULT NULL,
        paid_provider_primary  VARCHAR(50) DEFAULT 'openrouter',
        paid_provider_fallback VARCHAR(50) DEFAULT 'openrouter',
        free_provider_primary  VARCHAR(50) DEFAULT 'openrouter',
        free_provider_fallback VARCHAR(50) DEFAULT 'openrouter',
        model_mode          ENUM('free','paid') NOT NULL DEFAULT 'free',
        autonomy            ENUM('propose','execute') NOT NULL DEFAULT 'propose'
            COMMENT 'propose=agent suggests, you approve. execute=agent acts.',
        has_internet_access TINYINT(1)      DEFAULT 0,
        can_propose_changes TINYINT(1)      DEFAULT 1,
        is_active           TINYINT(1)      NOT NULL DEFAULT 1,
        prompt_role         VARCHAR(50)     DEFAULT NULL
            COMMENT 'Maps to prompt_versions.role if agent wraps an existing cognitive role',
        agent_role          VARCHAR(30)     DEFAULT NULL
            COMMENT 'Functional role: quant, trader, scout, tracker, auditor, data_scientist, executive, support',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ap_department (department),
        INDEX idx_ap_reports_to (reports_to),
        INDEX idx_ap_active (is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── scheduled_tasks (cron jobs, heartbeats, meetings — managed by agents) ──
    """CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        task_name           VARCHAR(100)    NOT NULL UNIQUE,
        task_type           ENUM('cron','heartbeat','meeting','one_time') NOT NULL DEFAULT 'cron',
        description         TEXT            DEFAULT NULL,
        cron_schedule       VARCHAR(50)     DEFAULT NULL
            COMMENT 'Cron expression e.g. 0 8 * * * for 8am daily',
        interval_seconds    INT             DEFAULT NULL
            COMMENT 'For heartbeat type: run every N seconds',
        target_agent_id     VARCHAR(30)     DEFAULT NULL
            COMMENT 'Agent responsible for executing this task',
        department          VARCHAR(30)     DEFAULT NULL,
        payload             JSON            DEFAULT NULL
            COMMENT 'Task-specific config passed to the agent',
        is_active           TINYINT(1)      NOT NULL DEFAULT 1,
        created_by          VARCHAR(50)     DEFAULT 'system',
        last_run_at         DATETIME        DEFAULT NULL,
        last_result         TEXT            DEFAULT NULL,
        next_run_at         DATETIME        DEFAULT NULL,
        run_count           INT             DEFAULT 0,
        error_count         INT             DEFAULT 0,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_st_active (is_active),
        INDEX idx_st_next_run (next_run_at),
        INDEX idx_st_agent (target_agent_id),
        INDEX idx_st_type (task_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── action_points (tasks created by agents, require human approval) ──
    """CREATE TABLE IF NOT EXISTS action_points (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        description         TEXT            NOT NULL,
        detail              MEDIUMTEXT      DEFAULT NULL,
        responsible_agent_id VARCHAR(30)    DEFAULT NULL,
        status              ENUM('open','in_progress','pending_approval','done','cancelled')
                            NOT NULL DEFAULT 'open',
        priority            ENUM('low','medium','high','critical') NOT NULL DEFAULT 'medium',
        origin_type         VARCHAR(30)     DEFAULT NULL
            COMMENT 'Where this came from: meeting, proposal, cron, chat, manual',
        origin_id           INT             DEFAULT NULL
            COMMENT 'FK to source record (conversation_id, proposal_id, etc.)',
        created_by          VARCHAR(50)     DEFAULT 'system',
        approved_by         VARCHAR(50)     DEFAULT NULL,
        due_date            DATE            DEFAULT NULL,
        completed_at        DATETIME        DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_ap2_status (status),
        INDEX idx_ap2_agent (responsible_agent_id),
        INDEX idx_ap2_priority (priority),
        INDEX idx_ap2_due (due_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── department_charters (goals/objectives per department — reviewed by Justice) ──
    """CREATE TABLE IF NOT EXISTS department_charters (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        department          VARCHAR(30)     NOT NULL UNIQUE
            COMMENT 'Matches agent_profiles.department',
        charter_name        VARCHAR(200)    NOT NULL,
        charter_text        MEDIUMTEXT      NOT NULL
            COMMENT 'Goals, objectives, mission — the departments soul',
        reviewed_by         VARCHAR(30)     DEFAULT 'justice',
        last_reviewed_at    DATETIME        DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── company_policies (rules all agents must follow) ──
    """CREATE TABLE IF NOT EXISTS company_policies (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        policy_key          VARCHAR(80)     NOT NULL UNIQUE
            COMMENT 'Unique identifier: max_daily_drawdown, require_confluence, etc.',
        policy_value        TEXT            NOT NULL,
        description         TEXT            DEFAULT NULL,
        category            VARCHAR(30)     DEFAULT 'general'
            COMMENT 'risk, trading, engineering, compliance, general',
        enforced_by         VARCHAR(30)     DEFAULT NULL
            COMMENT 'Agent responsible for enforcement',
        is_active           TINYINT(1)      NOT NULL DEFAULT 1,
        created_by          VARCHAR(50)     DEFAULT 'system',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_cp_category (category),
        INDEX idx_cp_active (is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── vectorization_queue (items waiting to be embedded and stored in vector DB) ──
    """CREATE TABLE IF NOT EXISTS vectorization_queue (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        source_table        VARCHAR(50)     NOT NULL,
        source_id           INT             NOT NULL,
        collection_name     VARCHAR(50)     NOT NULL,
        text_content        MEDIUMTEXT      NOT NULL,
        metadata_json       JSON            DEFAULT NULL,
        status              ENUM('pending','processing','done','error')
                            NOT NULL DEFAULT 'pending',
        embedding_provider  VARCHAR(20)     DEFAULT NULL
            COMMENT 'local or openai — which embedder produced the vector',
        vector_id           VARCHAR(50)     DEFAULT NULL
            COMMENT 'UUID of the point in the vector store',
        attempts            INT             NOT NULL DEFAULT 0,
        error_message       TEXT            DEFAULT NULL,
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        processed_at        DATETIME        DEFAULT NULL,
        INDEX idx_vq_status_created (status, created_at),
        INDEX idx_vq_source (source_table, source_id),
        INDEX idx_vq_collection (collection_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── source_lineage (provenance: what data fed into what output) ──
    """CREATE TABLE IF NOT EXISTS source_lineage (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        output_table        VARCHAR(50)     NOT NULL,
        output_id           INT             NOT NULL,
        input_table         VARCHAR(50)     NOT NULL,
        input_id            INT             NOT NULL,
        relationship        VARCHAR(30)     NOT NULL DEFAULT 'informed_by'
            COMMENT 'analyzed_from, signal_derived_from, context_for, informed_by, delegated_from',
        agent_id            VARCHAR(30)     DEFAULT NULL
            COMMENT 'Which agent created this link (NULL for system)',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_sl_output (output_table, output_id),
        INDEX idx_sl_input (input_table, input_id),
        INDEX idx_sl_agent (agent_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── agent_activity_log (full accountability: who thought/said/did what, and why) ──
    """CREATE TABLE IF NOT EXISTS agent_activity_log (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        agent_id            VARCHAR(30)     NOT NULL
            COMMENT 'FK to agent_profiles.agent_id',
        activity_type       ENUM('thought','decision','action','query','delegation',
                                 'meeting','proposal','error') NOT NULL,
        summary             VARCHAR(300)    NOT NULL
            COMMENT 'One-liner for display',
        detail              MEDIUMTEXT      DEFAULT NULL
            COMMENT 'Full reasoning chain, inputs seen, outputs produced',
        input_refs          JSON            DEFAULT NULL
            COMMENT 'List of {table, id} that fed into this activity',
        output_refs         JSON            DEFAULT NULL
            COMMENT 'List of {table, id} this activity produced',
        rag_queries         JSON            DEFAULT NULL
            COMMENT 'Each: {query, motivation, reasoning, trigger, results_count, collections_searched, timestamp}',
        model_used          VARCHAR(120)    DEFAULT NULL,
        token_count         INT             DEFAULT 0,
        cost_usd            DECIMAL(10,6)   DEFAULT 0,
        parent_activity_id  INT             DEFAULT NULL
            COMMENT 'Threading: this thought led to this decision',
        session_id          VARCHAR(50)     DEFAULT NULL
            COMMENT 'Groups related activities in a meeting or conversation',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_aal_agent (agent_id),
        INDEX idx_aal_type (activity_type),
        INDEX idx_aal_session (session_id),
        INDEX idx_aal_created (created_at),
        INDEX idx_aal_parent (parent_activity_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── agent_conversations (chat threads — human-agent, agent-agent, meetings) ──
    """CREATE TABLE IF NOT EXISTS agent_conversations (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        conversation_type       ENUM('chat','huddle','break_room','meeting','debrief')
                                NOT NULL DEFAULT 'chat',
        topic                   VARCHAR(300)    DEFAULT NULL,
        participants            JSON            NOT NULL
            COMMENT 'List of speaker ids: ["human_ceo","atlas","quant"]',
        status                  ENUM('active','paused','completed','archived')
                                NOT NULL DEFAULT 'active',
        chain_id                VARCHAR(50)     DEFAULT NULL
            COMMENT 'Groups related conversations across time — same chain = same thread of thought',
        parent_conversation_id  INT             DEFAULT NULL
            COMMENT 'Direct link to the conversation this continues from',
        context_snapshot_id     INT             DEFAULT NULL
            COMMENT 'Which snapshot was loaded as initial context for this conversation',
        turn_count              INT             NOT NULL DEFAULT 0,
        total_tokens            INT             NOT NULL DEFAULT 0,
        initiator               VARCHAR(30)     DEFAULT 'human_ceo'
            COMMENT 'Who started: human_ceo, atlas, scheduler, curiosity, etc.',
        created_at              DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at                DATETIME        DEFAULT NULL,
        INDEX idx_ac_chain (chain_id),
        INDEX idx_ac_parent (parent_conversation_id),
        INDEX idx_ac_status (status),
        INDEX idx_ac_type (conversation_type),
        INDEX idx_ac_initiator (initiator),
        INDEX idx_ac_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── conversation_turns (every message in every conversation — permanent) ──
    """CREATE TABLE IF NOT EXISTS conversation_turns (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        conversation_id     INT             NOT NULL
            COMMENT 'FK to agent_conversations.id',
        turn_number         INT             NOT NULL,
        speaker_id          VARCHAR(30)     NOT NULL
            COMMENT 'agent_id or human_ceo',
        message_content     MEDIUMTEXT      NOT NULL,
        tool_calls          JSON            DEFAULT NULL
            COMMENT 'Any tools the agent invoked during this turn',
        citations           JSON            DEFAULT NULL
            COMMENT 'Source references: [{table, id, snippet, score}]',
        delegation_request  JSON            DEFAULT NULL
            COMMENT 'If agent delegated to another: {target_agent, task, status, response}',
        parent_turn_id      INT             DEFAULT NULL
            COMMENT 'Direct reply chain within a conversation',
        reasoning_chain     JSON            DEFAULT NULL
            COMMENT 'Full internal thinking: [{step, thought, conclusion}]',
        context_used        JSON            DEFAULT NULL
            COMMENT 'What RAG results/memories were consulted: [{query, results_summary, source_ids}]',
        token_count         INT             DEFAULT 0,
        vector_id           VARCHAR(50)     DEFAULT NULL
            COMMENT 'UUID of this turn in the conversations vector collection',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_ct_conv (conversation_id),
        INDEX idx_ct_speaker (speaker_id),
        INDEX idx_ct_parent (parent_turn_id),
        INDEX idx_ct_created (created_at),
        INDEX idx_ct_vector (vector_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── context_snapshots (periodic compressed state saves — never lose context) ──
    """CREATE TABLE IF NOT EXISTS context_snapshots (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        conversation_id     INT             NOT NULL
            COMMENT 'FK to agent_conversations.id',
        chain_id            VARCHAR(50)     DEFAULT NULL
            COMMENT 'Matches the chain for cross-conversation continuity',
        snapshot_type       ENUM('periodic','end_of_conversation','manual','context_overflow')
                            NOT NULL DEFAULT 'periodic',
        from_turn           INT             NOT NULL
            COMMENT 'First turn_number covered by this snapshot',
        to_turn             INT             NOT NULL
            COMMENT 'Last turn_number covered by this snapshot',
        summary_text        MEDIUMTEXT      NOT NULL
            COMMENT 'AI-compressed summary preserving all decisions, who said what, action items',
        key_decisions       JSON            DEFAULT NULL
            COMMENT 'Extracted decisions/conclusions: [{decision, by_whom, context}]',
        key_entities        JSON            DEFAULT NULL
            COMMENT 'Symbols, agents, traders mentioned: {symbols:[], agents:[], traders:[]}',
        participant_positions JSON           DEFAULT NULL
            COMMENT 'Where each participant stands: [{speaker, position, open_questions}]',
        token_count         INT             DEFAULT 0,
        model_used          VARCHAR(120)    DEFAULT NULL,
        cost_usd            DECIMAL(10,6)   DEFAULT 0,
        vector_id           VARCHAR(50)     DEFAULT NULL
            COMMENT 'UUID of this snapshot in the conversations vector collection',
        created_at          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_cs_conv (conversation_id),
        INDEX idx_cs_chain (chain_id),
        INDEX idx_cs_type (snapshot_type),
        INDEX idx_cs_turns (from_turn, to_turn),
        INDEX idx_cs_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ══════════════════════════════════════════════════════════════════
    # AUDIT & ACCOUNTABILITY ENGINE
    # ══════════════════════════════════════════════════════════════════

    """CREATE TABLE IF NOT EXISTS audit_reports (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        report_type         ENUM('trade_postmortem','daily_summary','weekly_review')
                            NOT NULL,
        dossier_id          INT NULL,
        symbol              VARCHAR(20),
        trade_direction     VARCHAR(10),
        trade_outcome       ENUM('won','lost','abandoned','expired') NULL,
        pnl_amount          DECIMAL(12,4) NULL,
        pnl_pct             DECIMAL(8,4) NULL,
        entry_price         DECIMAL(16,6) NULL,
        exit_price          DECIMAL(16,6) NULL,
        stop_loss           DECIMAL(16,6) NULL,
        trade_opened_at     DATETIME NULL,
        trade_closed_at     DATETIME NULL,

        root_cause          TEXT,
        blame_assignment    JSON,
        severity            ENUM('critical','major','minor','no_fault')
                            NOT NULL DEFAULT 'minor',
        auditor_summary     TEXT,

        evidence_json       LONGTEXT,
        stage1_snapshot     LONGTEXT,
        stage2_snapshot     LONGTEXT,
        tracker_snapshot    LONGTEXT,
        fresh_ta_report     LONGTEXT,
        candles_during_trade JSON,
        geo_macro_during     JSON,
        companion_during     JSON,

        interview_transcript LONGTEXT,
        interview_count      INT DEFAULT 0,

        report_date         DATE NULL,
        trades_reviewed     INT NULL,
        wins_count          INT NULL,
        losses_count        INT NULL,
        patterns_identified JSON,
        systemic_recs       TEXT,

        model_used          VARCHAR(100),
        total_cost          DECIMAL(8,4) DEFAULT 0,
        total_tokens        INT DEFAULT 0,
        duo_id              VARCHAR(50) DEFAULT NULL,
        cross_duo_analysis  TEXT DEFAULT NULL COMMENT 'If another duo traded same symbol, comparison notes',
        status              ENUM('pending','in_progress','completed','failed')
                            DEFAULT 'pending',
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at        TIMESTAMP NULL,

        INDEX idx_ar_type (report_type),
        INDEX idx_ar_dossier (dossier_id),
        INDEX idx_ar_date (report_date),
        INDEX idx_ar_outcome (trade_outcome),
        INDEX idx_ar_status (status),
        INDEX idx_ar_duo (duo_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS compliance_note_influence (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        dossier_id        INT NOT NULL,
        note_report_id    INT NOT NULL,
        trade_outcome     ENUM('won','lost') DEFAULT NULL,
        created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_cni_dossier (dossier_id),
        INDEX idx_cni_report (note_report_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS audit_interviews (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        audit_report_id     INT NOT NULL,
        round_number        INT NOT NULL,
        phase               ENUM('defense','cross_examination','self_improvement',
                                 'rebuttal','data_review','source_verification')
                            NOT NULL,
        interviewer         VARCHAR(50) NOT NULL DEFAULT 'ledger',
        interviewee         VARCHAR(50) NOT NULL,

        question            TEXT NOT NULL,
        response            TEXT NOT NULL,
        auditor_assessment  TEXT,
        credibility_score   INT,

        model_used          VARCHAR(100),
        tokens_used         INT,
        cost                DECIMAL(8,4),
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_ai_report (audit_report_id),
        INDEX idx_ai_interviewee (interviewee),
        INDEX idx_ai_phase (phase)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS audit_recommendations (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        audit_report_id     INT NOT NULL,
        category            ENUM('prompt_change','soul_change','identity_change',
                                 'parameter_change','data_source_change',
                                 'threshold_change','tool_change','process_change')
                            NOT NULL,
        target_agent        VARCHAR(50),
        target_field        VARCHAR(200),
        target_table        VARCHAR(100),

        current_value       LONGTEXT,
        proposed_value      LONGTEXT,
        diff_summary        TEXT,

        evidence            TEXT NOT NULL,
        business_justification TEXT NOT NULL,
        expected_impact     TEXT,

        severity            ENUM('critical','important','suggestion')
                            NOT NULL DEFAULT 'suggestion',
        priority            INT DEFAULT 5,

        status              ENUM('pending','approved','rejected','applied','rolled_back')
                            DEFAULT 'pending',
        ceo_comments        TEXT,
        ceo_decision_at     TIMESTAMP NULL,

        applied_at          TIMESTAMP NULL,
        applied_by          VARCHAR(50),
        rollback_value      LONGTEXT,
        rolled_back_at      TIMESTAMP NULL,
        rolled_back_by      VARCHAR(50),

        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_arec_status (status),
        INDEX idx_arec_report (audit_report_id),
        INDEX idx_arec_agent (target_agent),
        INDEX idx_arec_severity (severity)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS ceo_daily_digest (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        digest_date         DATE NOT NULL,
        digest_type         VARCHAR(50) NOT NULL DEFAULT 'auditor_auto_apply',
        content             LONGTEXT,
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_digest_date_type (digest_date, digest_type)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS audit_learning_metrics (
        id                  INT AUTO_INCREMENT PRIMARY KEY,
        metric_date         DATE NOT NULL,

        total_trades        INT DEFAULT 0,
        wins                INT DEFAULT 0,
        losses              INT DEFAULT 0,
        abandoned           INT DEFAULT 0,
        win_rate            DECIMAL(5,2),
        profit_factor       DECIMAL(8,4),
        avg_win_pct         DECIMAL(8,4),
        avg_loss_pct        DECIMAL(8,4),
        total_pnl           DECIMAL(12,4),

        postmortems_run     INT DEFAULT 0,
        recs_made           INT DEFAULT 0,
        recs_approved       INT DEFAULT 0,
        recs_applied        INT DEFAULT 0,
        recs_rolled_back    INT DEFAULT 0,

        quant_blame_avg     DECIMAL(5,2),
        apex_blame_avg      DECIMAL(5,2),
        tracker_blame_avg   DECIMAL(5,2),
        data_blame_avg      DECIMAL(5,2),
        market_blame_avg    DECIMAL(5,2),
        geo_macro_blame_avg DECIMAL(5,2),

        conditions_total    INT DEFAULT 0,
        conditions_accurate INT DEFAULT 0,
        condition_accuracy  DECIMAL(5,2),

        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE INDEX idx_alm_date (metric_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    """CREATE TABLE IF NOT EXISTS system_trust_score (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        score_date              DATE NOT NULL,
        overall_score           DECIMAL(5,2) DEFAULT 0 COMMENT '0-100 composite trust score',
        confidence_accuracy     DECIMAL(5,2) DEFAULT 0 COMMENT 'How well confidence predictions match reality (Brier-like)',
        rec_success_rate        DECIMAL(5,2) DEFAULT 0 COMMENT 'Pct of applied recs that improved performance',
        profitability_score     DECIMAL(5,2) DEFAULT 0 COMMENT 'Rolling 30-day PnL normalised to 0-100',
        calibration_error       DECIMAL(5,2) DEFAULT 0 COMMENT 'Avg abs(assigned_conf - actual_WR) per bucket',
        win_rate_30d            DECIMAL(5,2) DEFAULT 0,
        total_pnl_30d           DECIMAL(12,4) DEFAULT 0,
        trades_30d              INT DEFAULT 0,
        recs_applied_30d        INT DEFAULT 0,
        recs_successful_30d     INT DEFAULT 0,
        autonomy_eligible       VARCHAR(20) DEFAULT 'manual' COMMENT 'Highest autonomy level justified by this score',
        created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE INDEX idx_sts_date (score_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── threshold_history (tracks every threshold change for sweet-spot analysis) ──
    """CREATE TABLE IF NOT EXISTS threshold_history (
        id                INT AUTO_INCREMENT PRIMARY KEY,
        config_key        VARCHAR(100)  NOT NULL COMMENT 'e.g. min_confidence_for_trade',
        old_value         VARCHAR(50)   DEFAULT NULL,
        new_value         VARCHAR(50)   NOT NULL,
        changed_by        VARCHAR(50)   DEFAULT 'user' COMMENT 'user, auditor, system',
        changed_at        DATETIME      DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_th_key (config_key),
        INDEX idx_th_date (changed_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── apex_shadow_trades (counterfactual tracking for do_not_trade decisions) ──
    """CREATE TABLE IF NOT EXISTS apex_shadow_trades (
        id                      INT AUTO_INCREMENT PRIMARY KEY,
        symbol                  VARCHAR(20)     NOT NULL,
        direction               ENUM('BUY','SELL') DEFAULT NULL,
        entry_price             DECIMAL(15,5)   DEFAULT NULL,
        stop_loss               DECIMAL(15,5)   DEFAULT NULL,
        take_profit_1           DECIMAL(15,5)   DEFAULT NULL,
        take_profit_2           DECIMAL(15,5)   DEFAULT NULL,
        take_profit_3           DECIMAL(15,5)   DEFAULT NULL,
        confidence_score        INT             DEFAULT NULL,
        rationale               TEXT            DEFAULT NULL,
        stage2_raw_response     MEDIUMTEXT      DEFAULT NULL,
        stage1_summary          TEXT            DEFAULT NULL,
        model_used              VARCHAR(100)    DEFAULT NULL,
        conditions_snapshot     JSON            DEFAULT NULL,
        asset_class             VARCHAR(30)     DEFAULT NULL,
        shadow_status           ENUM('pending','shadow_won','shadow_lost',
                                     'shadow_expired','no_levels') DEFAULT 'pending',
        entry_hit_at            DATETIME        DEFAULT NULL,
        exit_price              DECIMAL(15,5)   DEFAULT NULL,
        exit_at                 DATETIME        DEFAULT NULL,
        counterfactual_pnl      DECIMAL(15,2)   DEFAULT NULL,
        counterfactual_pnl_pct  DECIMAL(8,4)    DEFAULT NULL,
        exit_reason             VARCHAR(100)    DEFAULT NULL,
        lesson_text             TEXT            DEFAULT NULL,
        lesson_generated_at     DATETIME        DEFAULT NULL,
        rejected_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
        evaluated_at            DATETIME        DEFAULT NULL,
        INDEX idx_shadow_symbol (symbol),
        INDEX idx_shadow_status (shadow_status),
        INDEX idx_shadow_confidence (confidence_score),
        INDEX idx_shadow_rejected (rejected_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── shadow_queue (Shadow Trader Agent — execution queue state machine) ──
    """CREATE TABLE IF NOT EXISTS shadow_queue (
        id                   INT AUTO_INCREMENT PRIMARY KEY,
        shadow_trade_id      INT NOT NULL,
        symbol               VARCHAR(30) NOT NULL,
        exchange_symbol      VARCHAR(50) DEFAULT NULL,
        direction            ENUM('BUY','SELL') NOT NULL,
        entry_price          DECIMAL(20,8) NOT NULL,
        stop_loss            DECIMAL(20,8) NOT NULL,
        take_profit_1        DECIMAL(20,8) DEFAULT NULL,
        take_profit_2        DECIMAL(20,8) DEFAULT NULL,
        take_profit_3        DECIMAL(20,8) DEFAULT NULL,
        confidence_score     INT NOT NULL,
        model_used           VARCHAR(100) DEFAULT NULL,
        distance_pct         DECIMAL(8,4) DEFAULT NULL,
        current_price        DECIMAL(20,8) DEFAULT NULL,
        tradable_on_exchange TINYINT(1) DEFAULT 0,
        queue_status         ENUM('queued','placed','filled','expired','cancelled',
                                  'replaced','skipped','missed','closed','margin_wait') DEFAULT 'queued',
        order_id             VARCHAR(100) DEFAULT NULL,
        live_trade_id        INT DEFAULT NULL,
        margin_allocated     DECIMAL(12,2) DEFAULT NULL,
        leverage             INT DEFAULT NULL,
        position_size        DECIMAL(20,8) DEFAULT NULL,
        skip_reason          VARCHAR(200) DEFAULT NULL,
        priority_score       DECIMAL(8,4) DEFAULT NULL,
        tcs_score            DECIMAL(5,2) DEFAULT NULL,
        tcs_components       JSON DEFAULT NULL,
        added_at             DATETIME DEFAULT CURRENT_TIMESTAMP,
        placed_at            DATETIME DEFAULT NULL,
        filled_at            DATETIME DEFAULT NULL,
        expired_at           DATETIME DEFAULT NULL,
        cancelled_at         DATETIME DEFAULT NULL,
        INDEX idx_sq_shadow   (shadow_trade_id),
        INDEX idx_sq_status   (queue_status),
        INDEX idx_sq_symbol   (symbol),
        INDEX idx_sq_priority (priority_score DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── shadow_config (Shadow Trader Agent — persistent dashboard settings) ──
    """CREATE TABLE IF NOT EXISTS shadow_config (
        config_key   VARCHAR(100) PRIMARY KEY,
        config_value TEXT NOT NULL,
        updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── market_regime_history (Market Regime Service — regime score history) ──
    """CREATE TABLE IF NOT EXISTS market_regime_history (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        symbol       VARCHAR(50)  NOT NULL,
        regime_score DECIMAL(5,2) NOT NULL,
        regime_label VARCHAR(50)  NOT NULL,
        btc_price    DECIMAL(20,8) DEFAULT NULL,
        components   JSON,
        source       ENUM('full','pulse') DEFAULT 'full',
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_mrh_symbol_time (symbol, created_at),
        INDEX idx_mrh_source_time (source, created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",

    # ── shadow_activity_log (Shadow Trader Agent — event feed) ──
    """CREATE TABLE IF NOT EXISTS shadow_activity_log (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        event_type  VARCHAR(30) NOT NULL,
        severity    ENUM('INFO','WARN','ERROR') DEFAULT 'INFO',
        symbol      VARCHAR(30) DEFAULT NULL,
        message     TEXT NOT NULL,
        details     JSON DEFAULT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_sal_type    (event_type),
        INDEX idx_sal_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
]


# ─────────────────────────────────────────────────────────────────────
# Seed Data
# ─────────────────────────────────────────────────────────────────────

SEED_KNOWN_MODELS = """
INSERT IGNORE INTO known_models
    (provider, model_id, display_name, supports_vision, supports_function_calling, context_window, is_available, is_recommended)
VALUES
    ('openrouter', 'openai/gpt-4.1',      'GPT-4.1',      1, 1, 1047576, 1, 1),
    ('openrouter', 'openai/gpt-4.1-mini', 'GPT-4.1 Mini', 1, 1, 1047576, 1, 1),
    ('openrouter', 'openai/gpt-4.1-nano', 'GPT-4.1 Nano', 0, 1, 1047576, 1, 0),
    ('openrouter', 'google/gemini-2.5-flash', 'Gemini 2.5 Flash', 1, 1, 1000000, 1, 1),
    ('anthropic', 'claude-sonnet-4-20250514', 'Claude Sonnet 4', 1, 1, 200000, 1, 1),
    ('anthropic', 'claude-opus-4-20250514', 'Claude Opus 4', 1, 1, 200000, 1, 0),
    ('google', 'gemini-2.5-pro', 'Gemini 2.5 Pro', 1, 1, 1000000, 0, 0),
    ('google', 'gemini-2.5-flash', 'Gemini 2.5 Flash', 1, 1, 1000000, 0, 0),
    ('manus', 'manus-1', 'Manus 1 (Browser Agent)', 0, 0, 128000, 1, 0),
    ('openrouter', 'mistralai/mistral-small-3.1-24b-instruct', 'Mistral Small 3.1 24B', 1, 1, 131072, 1, 1)
"""

SEED_SOURCE_MODEL_ASSIGNMENTS = """
INSERT IGNORE INTO source_model_assignments (source, media_type, model_id, model_provider) VALUES
    -- Discord: GPT-5-nano for text/voice, Mistral-small-3.1 for chart images (44% WR, 27x cheaper than Gemini), Gemini for video
    ('discord',           'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('discord',           'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    ('discord',           'voice', 'openai/gpt-5-nano',                       'openrouter'),
    ('discord',           'video', 'gemini-3-flash-preview',                  'google'),
    -- Telegram: GPT-5-nano for text/voice, Mistral-small-3.1 for images, Gemini for video
    ('telegram',          'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('telegram',          'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    ('telegram',          'voice', 'openai/gpt-5-nano',                       'openrouter'),
    ('telegram',          'video', 'gemini-3-flash-preview',                  'google'),
    -- TradingView Ideas: GPT-5-nano for text, Mistral-small-3.1 for chart images
    ('tradingview_ideas', 'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('tradingview_ideas', 'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    -- News: GPT-5-nano for text/voice, Mistral-small-3.1 for images, Gemini for video
    ('news',              'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('news',              'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    ('news',              'voice', 'openai/gpt-5-nano',                       'openrouter'),
    ('news',              'video', 'gemini-3-flash-preview',                  'google'),
    -- Twitter: GPT-5-nano for text/voice, Mistral-small-3.1 for images, Gemini for video
    ('twitter',           'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('twitter',           'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    ('twitter',           'voice', 'openai/gpt-5-nano',                       'openrouter'),
    ('twitter',           'video', 'gemini-3-flash-preview',                  'google'),
    -- YouTube: GPT-5-nano for text/voice, Mistral-small-3.1 for images, Gemini for video
    ('youtube',           'text',  'openai/gpt-5-nano',                       'openrouter'),
    ('youtube',           'image', 'mistralai/mistral-small-3.1-24b-instruct','openrouter'),
    ('youtube',           'voice', 'openai/gpt-5-nano',                       'openrouter'),
    ('youtube',           'video', 'gemini-3-flash-preview',                  'google')
"""

SEED_SYSTEM_CONFIG = """
INSERT INTO system_config (config_key, config_value, description) VALUES
    ('owner_approval_required', 'true', 'Legacy: When ON, roles propose changes and owner approves. When OFF, fully autonomous.'),
    ('ceo_model', 'openai/gpt-4.1-mini', 'AI model used for CEO reports'),
    ('ceo_model_provider', 'openrouter', 'Provider for CEO model'),
    ('ceo_autonomy_level', 'autonomous', 'CEO autonomy: manual|assisted|supervised|autonomous. Controls how aggressively Ledger recs are auto-applied.')
ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)
"""

SEED_AGENT_TUNING_CONFIG = """
INSERT INTO system_config (config_key, config_value, description) VALUES
    ('bear_debate_enabled', 'true', 'Enable/disable pre-trade Bull/Bear debate. Ledger can toggle.'),
    ('bull_debate_enabled', 'true', 'Enable/disable the Bull advocacy step in pre-trade debate.'),
    ('bear_confidence_soft_threshold', '55', 'Bear confidence above this triggers a soft confidence reduction on Apex. Adjustable by CEO.'),
    ('bear_confidence_hard_threshold', '75', 'Bear confidence above this triggers STRONG_OBJECTION reduction. Adjustable by CEO.'),
    ('bear_confidence_reduction_soft', '8', 'Confidence points removed from Apex when bear exceeds soft threshold.'),
    ('bear_confidence_reduction_hard', '15', 'Confidence points removed from Apex when bear exceeds hard threshold.'),
    ('experience_library_enabled', 'true', 'Enable/disable few-shot Experience Library injection into Stage 2.'),
    ('experience_library_max_tokens', '2000', 'Max token budget for Experience Library exemplars in Stage 2 prompt.'),
    ('experience_library_max_exemplars', '3', 'Max number of past trade exemplars to inject as few-shot examples.'),
    ('chart_generation_enabled', 'true', 'Enable/disable BillNye fallback chart generation when no external charts available.'),
    ('rag_weight_similarity', '0.50', 'RAG scoring weight for vector similarity (0.0-1.0). Sum with recency+importance should = 1.0.'),
    ('rag_weight_recency', '0.20', 'RAG scoring weight for recency (0.0-1.0).'),
    ('rag_weight_importance', '0.30', 'RAG scoring weight for PnL-weighted importance (0.0-1.0).'),
    ('calibration_deviation_threshold', '25', 'Confidence deviation (points) that triggers a calibration flag. Used by post-hoc calibration enforcement.'),
    ('tracker_top_losses_count', '5', 'Number of most recent losses to surface for Tracker before each update cycle.'),
    ('stage1_system_identity', 'You are Quant, the Factual Analyst at JarvAIs. You have three data sources: (1) raw OHLCV candle data across multiple timeframes, (2) BillNye (your Data Scientist) who provides computed TA-Lib indicators — RSI, EMA, MACD, Bollinger, ATR, Fibonacci, Order Blocks, FVGs, session levels, volume profile, and more, and (3) geopolitical and macroeconomic context.\n\nYour job: synthesise ALL this data into a comprehensive, DIRECTION-AGNOSTIC technical analysis.\n\nCRITICAL: You MUST present BOTH the LONG case AND the SHORT case with EQUAL rigour. Structure your output as:\n1. KEY LEVELS & STRUCTURES — Support/resistance, BOS/CHoCH, liquidity zones\n2. THE LONG CASE — All evidence supporting a bullish move (levels, confluences, indicators)\n3. THE SHORT CASE — All evidence supporting a bearish move (levels, confluences, indicators)\n4. CONFLUENCE ASSESSMENT — Where do indicators agree? Which case has more supporting evidence?\n5. RISK FACTORS — Geo/macro events, upcoming catalysts, divergences\n\nYou NEVER recommend a direction. You present evidence for BOTH sides with equal analytical depth. If 8 indicators point long and 2 point short, you still present both cases fully — the 2 bearish signals may be the ones that matter. Facts over feelings. Numbers over narratives. Let Apex decide.', 'System identity for Stage 1 TA (Quant). Direction-agnostic dual-case output. Editable by CEO/Ledger.'),
    ('stage2_system_identity', 'You are Apex, the Trader at JarvAIs. You make trade decisions using Smart Money Concepts (BOS, CHoCH, order blocks, liquidity grabs, AMD cycles, session levels, Fibonacci OTE) alongside dossiers from Quant, geopolitical context from Geo, and macro data from Macro. You specify entry, stop loss (placed beyond liquidity pools), take profits, confidence score, and position size. You wait for confluence. You are authorized to take scalp trades on M1-M15 when high-probability setups present themselves. You actively look for range-bound conditions and trade within ranges using higher-timeframe directional bias.', 'System identity prompt for Stage 2 decision LLM call. Maps to Apex agent. Editable by CEO/Ledger.'),
    ('postmortem_system_identity', 'You are a post-mortem investigator at JarvAIs, working under Ledger (Chief Auditor). You analyse completed trades to determine what went right, what went wrong, and what Apex should learn. You are brutally honest but constructive. You examine the original hypothesis, actual price action, entry/exit timing, stop loss placement, and whether the trade thesis was sound. You surface actionable lessons that improve future trading.', 'System identity prompt for post-mortem LLM call. Maps to Ledger/auditor role. Editable by CEO/Ledger.'),
    ('alpha_briefing_identity', 'You are JarvAIs Alpha Intelligence. Provide concise trading-relevant briefings. Max 3 sentences. Focus on key market movers, sentiment direction, and actionable signals for XAUUSD, EURUSD, GBPUSD, NAS100.', 'System identity for Alpha Intelligence news briefing widget. Editable by CEO.'),
    ('ceo_report_identity', 'You are the CEO of JarvAIs, an autonomous AI trading company. Be frank, professional, constructive. Celebrate wins. Address issues with solutions. Analyse trading performance, costs, and agent effectiveness.', 'System identity for CEO daily/day-so-far reports. Editable by CEO.'),
    ('proposal_analyst_identity', 'You are a trading systems analyst at JarvAIs. Generate evidence-based proposals in JSON format.', 'Identity prompt for ProposalEngine analyst role. Editable by CEO.'),
    ('proposal_coach_identity', 'You are the Coach at JarvAIs. Generate constructive proposals in JSON format.', 'Identity prompt for ProposalEngine coach proposals. Editable by CEO.'),
    ('proposal_ceo_identity', 'You are the CEO of JarvAIs. Generate strategic proposals in JSON format.', 'Identity prompt for ProposalEngine CEO-level strategic proposals. Editable by CEO.'),
    ('news_classifier_identity', 'You are Scribe, the Text Analyst at JarvAIs. You parse ALL incoming text messages to extract structured intelligence: trade signals, market opinions, news headlines, and sentiment. Classify each item by category (signal, alpha, news, market_update, macro, geo, general). Extract symbols mentioned, sentiment (bullish/bearish/neutral), and urgency (high/medium/low). Accuracy is paramount — misreading a sell as a buy costs real money.', 'Identity prompt for news enrichment/classification (Scribe agent). Editable by CEO.'),
    ('symbol_resolver_identity', 'You are a financial symbol resolver. Given a ticker, name, or abbreviation, return the canonical exchange ticker. Rules: Crypto=USDT perp pair, Commodities=forex pair (XAUUSD), Indices=standard code (NAS100), Stocks=primary ticker (AAPL). Return ONLY the ticker or UNKNOWN.', 'Identity prompt for LLM-based symbol resolution in market_symbols.py. Editable by CEO.'),
    ('signal_level_calc_identity', 'You are a trading level calculator. Respond ONLY with valid JSON.', 'Identity prompt for AI-generated SL/TP levels in signal_ai. Editable by CEO.'),
    ('escalation_max_turns', '1', 'Maximum Tracker→Apex dialogue turns per escalation. 1=single review (current). Future: increase for multi-turn negotiation.'),
    ('escalation_context_depth', '20', 'Number of most recent conversation entries retained in tracker_conversation column. Controls context window size.'),
    ('tracker_log_max_chars', '10000', 'Maximum character length for tracker_log column. Older entries truncated beyond this limit.'),
    ('auditor_defense_agents', 'quant,apex,tracker,geo,macro', 'Comma-separated list of agents interviewed in Round 1 (Defense) of Ledger post-mortem audit. Editable by CEO.'),
    ('auditor_cross_exam_agents', 'apex,quant', 'Comma-separated list of agents cross-examined in Round 2 of Ledger post-mortem audit. Editable by CEO.'),
    ('auditor_self_improvement_agents', 'quant,apex,tracker', 'Comma-separated list of agents asked self-improvement questions in Round 3 of Ledger post-mortem audit. Editable by CEO.'),
    ('signal_postmortem_identity', '', 'Identity prompt for signal post-mortem analyst (large prompt — code fallback used when empty). Editable by CEO.'),
    ('strategy_analyst_prompt', '', 'System prompt for strategy chat analyst (large prompt — edit via Settings > Prompts or directly in system_config). Code fallback used when empty.'),
    ('strategy_documenter_prompt', '', 'System prompt for strategy documenter/finalizer (large prompt — edit via Settings > Prompts). Code fallback used when empty.'),
    ('symbol_classification_identity', 'You are a financial markets classification expert. Return ONLY valid JSON array. No markdown, no explanation.', 'System identity for AI symbol classification. Editable by CEO.'),
    ('bull_max_tokens', '2000', 'Max tokens for Bull agent LLM call. Editable by CEO/Ledger.'),
    ('bull_temperature', '0.4', 'Temperature for Bull agent LLM call. Higher = more creative advocacy.'),
    ('bear_max_tokens', '2000', 'Max tokens for Bear agent LLM call. Editable by CEO/Ledger.'),
    ('bear_temperature', '0.4', 'Temperature for Bear agent LLM call. Higher = more creative challenges.'),
    ('postmortem_max_tokens', '6000', 'Max tokens for post-mortem LLM call.'),
    ('postmortem_temperature', '0.3', 'Temperature for post-mortem LLM call.'),
    ('ab_test_sample_rate', '0.20', 'Fraction of dossiers that use challenger prompts (0.0-1.0). 0.20 = 20%%.'),
    ('tracker_max_tokens', '2000', 'Max tokens for Tracker conversation LLM call.'),
    ('tracker_temperature', '0.2', 'Temperature for Tracker conversation LLM call.'),
    ('comparison_max_tokens', '200', 'Max tokens for dossier comparison LLM call.'),
    ('comparison_temperature', '0.1', 'Temperature for dossier comparison LLM call. Low = more deterministic.'),
    ('escalation_max_tokens', '500', 'Max tokens for Apex escalation review LLM call.'),
    ('escalation_temperature', '0.2', 'Temperature for Apex escalation review LLM call.'),
    ('trust_weight_confidence', '0.40', 'Trust score weight for confidence accuracy (0.0-1.0). Sum of 3 weights should = 1.0.'),
    ('trust_weight_recs', '0.25', 'Trust score weight for recommendation success rate (0.0-1.0).'),
    ('trust_weight_profitability', '0.35', 'Trust score weight for profitability (0.0-1.0).'),
    ('trust_autonomous_threshold', '85', 'Trust score above which autonomy level = autonomous. Requires 100+ trades.'),
    ('trust_supervised_threshold', '70', 'Trust score above which autonomy level = supervised. Requires 50+ trades.'),
    ('trust_assisted_threshold', '55', 'Trust score above which autonomy level = assisted. Requires 30+ trades.'),
    ('apex_comparison_prompt', 'You compare two trade dossiers for the same symbol and decide the best action. You are Apex, the senior trader at JarvAIs. Consider entry price overlap, R:R quality, confidence scores, and freshness of analysis. Reply with EXACTLY one line: KEEP_BOTH, SUPERSEDE, or ABANDON_NEW followed by a brief reason.', 'System prompt for Apex dossier comparison when duplicate/overlapping dossiers are detected. Editable by CEO/Ledger.'),
    ('apex_escalation_prompt', 'You are Apex, the senior day trader at JarvAIs. Your Tracker analyst has escalated dossier #{dossier_id} for review.\n\nYOUR ORIGINAL DECISION was: **{original_decision}** for {symbol} {direction}\nEntry: {entry} | SL: {sl} | TP1: {tp1}\n\n## CONTEXT\nWe are LIVE TRADING with real capital. Every entry risks real money. A missed trade costs nothing; a bad trade costs real capital. Only enter when the setup is genuinely strong.\n\n## YOUR OPTIONS\n1. **HOLD** — Keep the dossier alive. Conditions are still developing.\n2. **ENTER_NOW** — The majority of conditions are met and the setup is genuinely compelling. Use this only when conviction is high.\n3. **ABANDON** — The trade thesis is broken, SL would have been hit, or a fundamental shift has occurred.\n\nQUALITY OVER QUANTITY: Only recommend ENTER_NOW if 60%%+ of conditions are met AND the risk:reward is favourable. When in doubt, HOLD. Protecting capital is more important than catching every move.', 'Apex escalation review system prompt template with {placeholders}. Runtime vars: dossier_id, original_decision, symbol, direction, entry, sl, tp1. Editable by CEO/Ledger.'),
    ('auditor_system_identity', 'You are Ledger, Chief Auditor at JarvAIs. You conduct post-mortem investigations on completed trades, interview agents, deliver verdicts with blame assignment, and synthesise daily recommendations for the CEO. You are thorough, evidence-driven, and brutally honest. You always respond with valid JSON when instructed to do so. You hold every agent accountable — including yourself.', 'System identity prompt for all Ledger/Auditor LLM calls. Editable by CEO.'),
    ('auditor_max_tokens', '8192', 'Default max_tokens for auditor LLM calls. Overridable per-call.'),
    ('auditor_temperature', '0.2', 'Default temperature for auditor LLM calls. Overridable per-call.'),
    ('shadow_trade_tracking_enabled', 'true', 'Enable/disable capturing rejected (do_not_trade) decisions as shadow trades for counterfactual analysis. No LLM cost — just saves data.'),
    ('shadow_evaluation_interval_hours', '6', 'How often BillNye evaluates pending shadow trades against candle data (hours).'),
    ('shadow_expiry_hours', '24', 'Hours after rejection before an unevaluated shadow trade expires (entry never hit).'),
    ('shadow_lesson_enabled', 'true', 'Enable/disable Ledger generating lessons from evaluated shadow trades (one LLM call per would-have-won shadow).'),
    ('shadow_max_lessons_in_prompt', '5', 'Max shadow lessons injected into Stage 2 prompt per symbol. Recent first.'),
    ('shadow_confidence_band_window_days', '14', 'Lookback days for shadow confidence band analysis (win rate by confidence range).'),
    ('mentor_level_override_enabled', 'true', 'When true, mentor signal levels overwrite Stage 2 hallucinated levels. When false, Stage 2 levels are used. Toggle in Settings.'),
    ('regime_injection_enabled', 'true', 'When true, market regime data is injected into Apex dossier (Section 0). When false, regime section omitted.'),
    ('tracker_system_prompt', 'You are the Tracker AI monitoring dossier #{dossier_id} for {symbol}. Direction: {direction} | Entry: {entry_price} | SL: {stop_loss} | TP1: {take_profit_1}\n\n## Your Role — ANALYST, NOT DECISION MAKER\nYou are Apex''s field analyst. Your job is to REPORT conditions, NOT to make trade decisions. Apex (the senior trader) made the original trade decision and ONLY Apex can cancel it.\n\nYour responsibilities:\n1. Report current price action and technical conditions accurately\n2. Update condition statuses based on live data\n3. Flag anything that has changed since last update\n4. Recommend HOLD_POSITION if the setup is still developing normally\n5. Recommend ENTER_NOW if conditions have improved and entry is optimal\n6. Recommend ESCALATE_TO_APEX ONLY if the trade thesis is clearly broken (e.g. price blew through SL level, fundamental news invalidated the setup). This triggers an Apex review — do NOT use it lightly.\n\nCRITICAL RULES:\n- You do NOT have authority to cancel trades. Never recommend cancellation.\n- Uncertainty is NORMAL in trading. Do not escalate just because some conditions aren''t met yet — that''s why they''re called CONDITIONS.\n- This is PAPER TRADING for data collection. We NEED trades to execute so we can measure performance. Lean towards HOLD over ESCALATE.\n- A probability below 50%% does NOT mean cancel. It means conditions are still developing. Report accurately and let Apex decide.\n\n## Smart Money Reporting\nWhen reporting price action, always note:\n- Current AMD phase (Accumulation/Manipulation/Distribution)\n- Any liquidity grabs or stop hunts since last update\n- PDH/PDL levels and whether they''ve been swept\n- Session ORB status (broken? which direction? failed?)\n- Any session sweeps (Asia swept by London, London swept by NY)\nThese observations help Apex make informed decisions on escalation.\n\n## BillNye (Data Scientist) Protocol\nBillNye computes TA indicators from raw candle data. If your analysis is hampered by missing data, or you need additional confirmation for a specific timeframe (scalp on M5/M15, swing on H4/D1), REQUEST it via ta_requests. BillNye will fulfil it and deliver results next cycle.\nAvailable indicators:\n{indicator_manifest}\n\nIMPORTANT: Sentiment is NOT a computable indicator. Do not request ''sentiment_score''. Use Geo/Macro data from the companion feed.\n\nIf any condition cannot be verified due to missing data, REQUEST the specific indicator and timeframe that would resolve it.', 'Full Tracker system prompt template with {placeholders}. Runtime vars: dossier_id, symbol, direction, entry_price, stop_loss, take_profit_1, indicator_manifest. Editable by CEO/Ledger.')
ON DUPLICATE KEY UPDATE description = VALUES(description)
"""

SEED_AGENT_TUNING_CONFIG_WAVE3 = """
INSERT INTO system_config (config_key, config_value, description) VALUES
    ('symbol_blacklist', '["ADAUSDT","ARC","GUNUSDT"]', 'JSON array of symbols blocked from main pipeline (negative EV). Auto-updated by shadow_queue.update_blacklist_from_ev(). Editable by CEO.'),
    ('short_min_confidence', '75', 'Minimum confidence score required for SELL/SHORT trades (vs 65 for longs). Addresses directional edge gap. Editable by CEO.')
ON DUPLICATE KEY UPDATE config_value = VALUES(config_value), description = VALUES(description)
"""

SEED_SCHEDULED_TASKS = """
INSERT IGNORE INTO scheduled_tasks
    (task_name, task_type, description, cron_schedule, interval_seconds,
     target_agent_id, department, is_active, created_by)
VALUES
    ('morning_brief',     'cron',      'Atlas generates overnight summary, key metrics, pending action points',
     '0 8 * * *',   NULL,  'atlas',   'executive',  1, 'system'),
    ('heartbeat',         'heartbeat', 'System health check, pending tasks, proactive work trigger',
     NULL,           1800,  NULL,      NULL,         1, 'system'),
    ('curiosity_rounds',  'cron',      'Curiosity asks agents questions, proposes improvements',
     '0 */4 * * *', NULL,  'curiosity','executive',  1, 'system'),
    ('signal_scan',       'cron',      'Parse new signals and monitor active ones',
     '*/5 * * * *', NULL,  'signal',  'trading',    1, 'system'),
    ('nightly_review',    'cron',      'Performance review, propose prompt/model/soul changes',
     '0 22 * * *',  NULL,  'atlas',   'executive',  1, 'system'),
    ('surprise_feature',  'cron',      'Elon delegates a small improvement to Anvil or Pixel',
     '0 4 * * *',   NULL,  'elon',    'engineering', 0, 'system'),
    ('security_audit',    'cron',      'Cipher audits API keys, permissions, dependencies',
     '0 3 * * 0',   NULL,  'cipher',  'engineering', 1, 'system'),
    ('compliance_review', 'cron',      'Justice reviews autonomous decisions from the past 24h',
     '0 23 * * *',  NULL,  'justice',  'compliance', 1, 'system'),
    ('coach_debrief',     'cron',      'Mentor reviews trading day, generates lessons learned',
     '0 21 * * 1-5', NULL, 'mentor',  'trading',    1, 'system'),
    ('model_sync',        'cron',      'Sync models from all providers, update pricing',
     '0 6 * * *',   NULL,  NULL,      NULL,         1, 'system')
"""

SEED_COMPANY_POLICIES = """
INSERT IGNORE INTO company_policies
    (policy_key, policy_value, description, category, enforced_by, is_active, created_by)
VALUES
    ('max_daily_drawdown_pct', '10',
     'Maximum allowed daily drawdown as percentage of account balance',
     'risk', 'justice', 1, 'system'),
    ('max_position_size_pct', '2',
     'Maximum risk per trade as percentage of account balance',
     'risk', 'justice', 1, 'system'),
    ('require_confluence', 'true',
     'Trader must wait for multiple confirming signals before entry',
     'trading', 'warren', 1, 'system'),
    ('min_risk_reward', '2.0',
     'Minimum risk-reward ratio for any trade',
     'trading', 'warren', 1, 'system'),
    ('agent_autonomy_default', 'propose',
     'Default autonomy mode for new agents (propose=suggest, execute=act)',
     'general', 'atlas', 1, 'system'),
    ('model_mode_company', 'free',
     'Company-wide model mode: free or paid',
     'general', 'atlas', 1, 'system'),
    ('max_internet_searches_per_hour', '5',
     'Rate limit for internet searches per agent per hour',
     'engineering', 'cipher', 1, 'system'),
    ('require_approval_for_soul_changes', 'true',
     'Soul/identity changes must be approved by CEO before applying',
     'compliance', 'justice', 1, 'system')
"""

SEED_DEPARTMENT_CHARTERS = """
INSERT IGNORE INTO department_charters (department, charter_name, charter_text, reviewed_by) VALUES
    ('executive', 'Executive Office Charter',
     'Mission: Coordinate all departments, manage CEO communications, maintain company-wide alignment.\n\nObjectives:\n1. Route all work to the right specialist — never accumulate\n2. Maintain situational awareness across all departments\n3. Track and follow up on all action points\n4. Ensure CEO has full transparency\n5. Proactively surface risks and opportunities',
     'justice'),
    ('data_analytics', 'Data Analytics Charter',
     'Mission: Process, analyze and interpret all incoming data across every media type to support every department.\n\nObjectives:\n1. Analyze text, images, charts, audio and video from all sources\n2. Extract actionable intelligence from raw data\n3. Support Trading with market analysis, Engineering with metrics, Compliance with audit trails\n4. Maintain data quality and consistency\n5. Serve as the factual backbone for all AI decision-making',
     'justice'),
    ('engineering', 'Engineering Charter',
     'Mission: Build, maintain and secure the JarvAIs platform infrastructure.\n\nObjectives:\n1. Maintain code quality, security and reliability\n2. Optimize prompts, models and system performance\n3. Build and improve the dashboard and agent tools\n4. Conduct security audits and manage API keys\n5. Support all departments with technical solutions',
     'justice'),
    ('trading', 'Trading Charter',
     'Mission: Generate consistent trading profits through signal analysis, trade execution and risk management.\n\nObjectives:\n1. Parse and validate trading signals from all sources\n2. Execute trades within risk parameters\n3. Track signal accuracy and provider reliability\n4. Coach and improve trading strategies based on post-mortems\n5. Never exceed maximum drawdown or position size limits',
     'justice'),
    ('compliance', 'Compliance Charter',
     'Mission: Ensure all agent behavior aligns with company policies, charters and CEO directives.\n\nObjectives:\n1. Review all autonomous decisions for policy compliance\n2. Audit trails for every trade, decision and action\n3. Recommend charter amendments when patterns emerge\n4. Enforce approval workflows for soul/identity changes\n5. Report non-compliance to CEO immediately',
     'justice')
"""

RESTRUCTURE_DEPARTMENTS_SQL = [
    "UPDATE agent_profiles SET department = 'data_analytics', reports_to = 'quant' WHERE agent_id IN ('lens', 'reel', 'vox', 'scribe')",
    "UPDATE agent_profiles SET department = 'data_analytics', reports_to = 'warren', title = 'Head of Data Analytics', delegation_policy = 'propose' WHERE agent_id = 'quant'",
    "UPDATE agent_profiles SET department = 'executive', reports_to = 'atlas' WHERE agent_id = 'echo'",
    "UPDATE agent_profiles SET department = 'executive', reports_to = 'atlas' WHERE agent_id = 'curiosity'",
    "UPDATE agent_profiles SET model_mode = 'paid', paid_model_primary = 'gpt-4.1-mini', paid_model_fallback = 'gpt-4.1-nano' WHERE paid_model_primary IS NULL OR paid_model_primary = ''",
    "UPDATE company_policies SET policy_value = 'paid' WHERE policy_key = 'model_mode_company'",
]


def bootstrap_database(db):
    """
    Create all tables and seed initial data.
    Called from run_dashboard.py on every startup.
    Fully idempotent — safe to call repeatedly.
    """
    created = 0
    errors = 0

    # 1. Create all tables
    for ddl in TABLES:
        try:
            db.execute(ddl)
            created += 1
        except Exception as e:
            err = str(e)
            if 'already exists' in err.lower():
                created += 1  # Table exists, that's fine
            else:
                logger.warning(f"Table creation error: {e}")
                errors += 1

    logger.info(f"Bootstrap: {created} tables ensured, {errors} errors")

    # 2. Seed known_models (INSERT IGNORE — skips if already present)
    try:
        db.execute(SEED_KNOWN_MODELS)
        logger.info("Bootstrap: known_models seeded")
    except Exception as e:
        logger.warning(f"Bootstrap: known_models seed error: {e}")

    # 3. Seed system_config
    try:
        db.execute(SEED_SYSTEM_CONFIG)
        db.execute(
            "INSERT IGNORE INTO system_config (config_key, config_value, description) "
            "VALUES ('stats_epoch_date', '2026-04-01', "
            "'Dashboard stats start date — hides all statistics/costs/P&L before this date')"
        )
        logger.info("Bootstrap: system_config seeded")
    except Exception as e:
        logger.warning(f"Bootstrap: system_config seed error: {e}")

    # 3-agent-tuning. Seed agent tuning config (bear/bull debate, experience library, RAG weights)
    try:
        db.execute(SEED_AGENT_TUNING_CONFIG)
        logger.info("Bootstrap: agent_tuning_config seeded (bear/bull, experience library, RAG weights)")
    except Exception as e:
        logger.warning(f"Bootstrap: agent_tuning_config seed error: {e}")

    try:
        db.execute(SEED_AGENT_TUNING_CONFIG_WAVE3)
        logger.info("Bootstrap: wave3 tuning config seeded (symbol_blacklist, short_min_confidence)")
    except Exception as e:
        logger.warning(f"Bootstrap: wave3 tuning config seed error: {e}")

    # 3a. Seed paper_balance (only if not present — do not overwrite user reset)
    try:
        db.execute("""
            INSERT INTO system_config (config_key, config_value, description)
            SELECT 'paper_balance', '2000', 'Simulated paper trading balance; updated by realised P&L on close; user can reset'
            WHERE NOT EXISTS (SELECT 1 FROM system_config WHERE config_key = 'paper_balance')
        """)
        logger.info("Bootstrap: paper_balance ensured in system_config")
    except Exception as e:
        logger.debug(f"Bootstrap: paper_balance seed: {e}")

    # 3a-ii. Seed vector_prune_days (INSERT WHERE NOT EXISTS — preserves user changes)
    try:
        db.execute("""
            INSERT INTO system_config (config_key, config_value, description)
            SELECT 'vector_prune_days', '90', 'Days to keep vectors before pruning. Set to 0 to disable pruning.'
            WHERE NOT EXISTS (SELECT 1 FROM system_config WHERE config_key = 'vector_prune_days')
        """)
        logger.info("Bootstrap: vector_prune_days ensured in system_config")
    except Exception as e:
        logger.debug(f"Bootstrap: vector_prune_days seed: {e}")

    # 3a-iii. Seed media processing toggles (INSERT WHERE NOT EXISTS — preserves user changes)
    for _mk, _mv, _md in [
        ("media_process_images", "true", "Process chart images and screenshots from all sources"),
        ("media_process_text", "true", "Process text messages for trade signal extraction"),
        ("media_process_video", "false", "Download and analyze videos via yt-dlp + Whisper"),
        ("media_process_voice", "false", "Transcribe voice/audio via OpenAI Whisper"),
    ]:
        try:
            db.execute(
                "INSERT INTO system_config (config_key, config_value, description) "
                "SELECT %s, %s, %s WHERE NOT EXISTS "
                "(SELECT 1 FROM system_config WHERE config_key = %s)",
                (_mk, _mv, _md, _mk))
        except Exception:
            pass
    logger.info("Bootstrap: media processing toggles ensured")

    # 3a-iv. Migrate prompt key rename: dossier_tracker_prompt -> tracker_system_prompt
    # Idempotent — only touches rows that still use the old key name.
    try:
        db.execute(
            "UPDATE system_config "
            "SET config_key = REPLACE(config_key, 'dossier_tracker_prompt', 'tracker_system_prompt') "
            "WHERE config_key LIKE 'dossier_tracker_prompt%%'"
        )
        logger.info("Bootstrap: tracker prompt key migration ensured")
    except Exception as e:
        logger.debug(f"Bootstrap: tracker prompt key migration: {e}")

    # 3b. Seed source_model_assignments (INSERT IGNORE — skips if already present)
    try:
        db.execute(SEED_SOURCE_MODEL_ASSIGNMENTS)
        logger.info("Bootstrap: source_model_assignments seeded")
    except Exception as e:
        logger.warning(f"Bootstrap: source_model_assignments seed error: {e}")

    # 3b2. Migrate chart/image models to Mistral Small 3.1 (44% WR, 27x cheaper than Gemini)
    # Only images — Mistral doesn't support video, Gemini stays for video
    try:
        db.execute(
            "UPDATE source_model_assignments "
            "SET model_id = 'mistralai/mistral-small-3.1-24b-instruct', model_provider = 'openrouter' "
            "WHERE media_type = 'image' "
            "AND model_id IN ('gemini-3-flash-preview', 'openai/gpt-5-nano')")
        logger.info("Bootstrap: migrated image models to Mistral Small 3.1")
    except Exception as e:
        logger.debug(f"Bootstrap: model migration: {e}")

    # 3c. Pre-populate Telegram credentials (from telegram_config.json → system_config)
    #     This ensures credentials survive DB resets and are always available in the dashboard.
    try:
        import json as _json
        tg_config_path = Path(__file__).parent.parent / "data" / "telegram_config.json"
        if tg_config_path.exists():
            with open(tg_config_path, 'r') as _f:
                tg_cfg = _json.load(_f)
            tg_creds = {
                'TELEGRAM_API_ID': str(tg_cfg.get('api_id', '')),
                'TELEGRAM_API_HASH': str(tg_cfg.get('api_hash', '')),
                'TELEGRAM_PHONE': str(tg_cfg.get('phone', '')),
            }
            for key, val in tg_creds.items():
                if val:
                    db.execute("""
                        INSERT INTO system_config (config_key, config_value) VALUES (%s, %s)
                        ON DUPLICATE KEY UPDATE config_value = IF(config_value = '' OR config_value IS NULL, %s, config_value)
                    """, (key, val, val))
            logger.info("Bootstrap: Telegram credentials synced to system_config")
    except Exception as e:
        logger.debug(f"Bootstrap: Telegram cred sync: {e}")

    # 4. Seed prompt_versions from SQL file (only if table is empty)
    try:
        row = db.fetch_one("SELECT COUNT(*) as cnt FROM prompt_versions")
        count = row.get('cnt', 0) if row else 0
        if count == 0:
            _seed_prompts_from_sql(db)
        else:
            logger.info(f"Bootstrap: prompt_versions already has {count} rows, skipping seed")
    except Exception as e:
        logger.warning(f"Bootstrap: prompt_versions seed check error: {e}")
        # Table might have just been created — try seeding anyway
        try:
            _seed_prompts_from_sql(db)
        except Exception as e2:
            logger.error(f"Bootstrap: prompt_versions seed failed: {e2}")

    # 4b. Ensure new source roles exist (INSERT IGNORE — safe on existing DBs)
    _seed_missing_source_roles(db)

    # 4c. Ensure alpha analysis prompt roles exist in prompt_versions
    _seed_alpha_analysis_roles(db)

    # 5. Create market_symbols table, seed from existing signals, reclassify unknowns
    try:
        from db.market_symbols import (ensure_market_symbols_table, seed_from_existing_signals,
                                       reclassify_unknown_symbols, normalize_and_resolve_all_symbols,
                                       normalize_dossier_symbols, backfill_exchange_tickers)
        ensure_market_symbols_table(db)
        new_count = seed_from_existing_signals(db)
        if new_count > 0:
            logger.info(f"Bootstrap: {new_count} new market symbols seeded from existing signals")
        reclassified = reclassify_unknown_symbols(db)
        if reclassified > 0:
            logger.info(f"Bootstrap: {reclassified} symbols reclassified from 'unknown'")
        # Normalize crypto symbols to USDT and resolve exchange tickers
        try:
            norm_report = normalize_and_resolve_all_symbols(db)
            logger.info(f"Bootstrap: symbol normalization — "
                        f"{norm_report.get('canonical_fixed', 0)} canonical fixed, "
                        f"{norm_report.get('tickers_resolved', 0)} tickers resolved")
        except Exception as ne:
            logger.debug(f"Bootstrap: symbol normalization error: {ne}")
        # Normalize dossier symbols (BTC -> BTCUSDT, etc.)
        try:
            dossier_report = normalize_dossier_symbols(db)
            if dossier_report.get("normalized", 0) > 0:
                logger.info(f"Bootstrap: {dossier_report['normalized']} dossier symbols normalized")
        except Exception as de:
            logger.debug(f"Bootstrap: dossier normalization error: {de}")
        # Backfill exchange tickers for any crypto symbols still missing them
        try:
            bf = backfill_exchange_tickers(db)
            if bf.get("resolved", 0) > 0:
                logger.info(f"Bootstrap: backfilled exchange tickers for "
                            f"{bf['resolved']}/{bf['total']} symbols")
        except Exception as be:
            logger.debug(f"Bootstrap: ticker backfill error: {be}")
    except Exception as e:
        logger.warning(f"Bootstrap: market_symbols setup error: {e}")

    # 6. Ensure context column exists in ai_api_log (migration)
    try:
        db.execute(
            "ALTER TABLE ai_api_log ADD COLUMN context VARCHAR(100) DEFAULT NULL AFTER role"
        )
        logger.info("Bootstrap: added context column to ai_api_log")
    except Exception as e:
        if 'Duplicate column' in str(e) or 'duplicate' in str(e).lower():
            pass  # Column already exists
        else:
            logger.warning(f"Bootstrap: ai_api_log context column error: {e}")

    # 6b. Add granular cost tracking columns to ai_api_log (source, source_detail, author, news_item_id, media_type)
    ai_api_log_columns = [
        ("source",        "ALTER TABLE ai_api_log ADD COLUMN source VARCHAR(50) DEFAULT NULL AFTER context"),
        ("source_detail", "ALTER TABLE ai_api_log ADD COLUMN source_detail VARCHAR(200) DEFAULT NULL AFTER source"),
        ("author",        "ALTER TABLE ai_api_log ADD COLUMN author VARCHAR(200) DEFAULT NULL AFTER source_detail"),
        ("news_item_id",  "ALTER TABLE ai_api_log ADD COLUMN news_item_id INT DEFAULT NULL AFTER author"),
        ("media_type",    "ALTER TABLE ai_api_log ADD COLUMN media_type VARCHAR(20) DEFAULT NULL AFTER news_item_id"),
    ]
    for col_name, alter_sql in ai_api_log_columns:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col_name} column to ai_api_log")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: ai_api_log {col_name} column: {e}")

    # 6c. Add dossier_id for cost-per-dossier tracking
    try:
        db.execute(
            "ALTER TABLE ai_api_log ADD COLUMN dossier_id INT DEFAULT NULL AFTER trade_id"
        )
        logger.info("Bootstrap: added dossier_id column to ai_api_log")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: ai_api_log dossier_id column: {e}")

    # 6d. Add cost_source to track whether cost is actual (from OpenRouter) or estimated
    try:
        db.execute(
            "ALTER TABLE ai_api_log ADD COLUMN cost_source VARCHAR(30) DEFAULT 'estimated' AFTER cost_usd"
        )
        logger.info("Bootstrap: added cost_source column to ai_api_log")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: ai_api_log cost_source column: {e}")

    # 7. Ensure managed_sources has all required columns (migration for existing tables)
    managed_sources_columns = [
        ("score", "ALTER TABLE managed_sources ADD COLUMN score FLOAT DEFAULT 0"),
        ("total_signals", "ALTER TABLE managed_sources ADD COLUMN total_signals INT DEFAULT 0"),
        ("winning_signals", "ALTER TABLE managed_sources ADD COLUMN winning_signals INT DEFAULT 0"),
        ("total_pnl", "ALTER TABLE managed_sources ADD COLUMN total_pnl FLOAT DEFAULT 0"),
        ("last_collected_id", "ALTER TABLE managed_sources ADD COLUMN last_collected_id VARCHAR(100) DEFAULT NULL"),
        ("last_collected_at", "ALTER TABLE managed_sources ADD COLUMN last_collected_at DATETIME DEFAULT NULL"),
    ]
    for col_name, alter_sql in managed_sources_columns:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col_name} column to managed_sources")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: managed_sources {col_name} column: {e}")

    # 8. Sync hardcoded RSS feeds and TradingView symbols to managed_sources
    try:
        _sync_collector_sources_to_managed(db)
    except Exception as e:
        logger.warning(f"Bootstrap: collector source sync error: {e}")

    # 9. Disable AI and Signals for unknown/unclassified symbols (they must be manually enabled)
    try:
        result = db.execute(
            "UPDATE market_symbols SET submit_to_ai = 0, detect_signals = 0 "
            "WHERE asset_class = 'unknown' AND (submit_to_ai = 1 OR detect_signals = 1)"
        )
        if result and result > 0:
            logger.info(f"Bootstrap: disabled AI/Signals for {result} unknown symbols")
    except Exception as e:
        logger.debug(f"Bootstrap: unknown symbols update: {e}")

    # 10. Add is_valid column to news_items and parsed_signals (migration for existing databases)
    for tbl, col, alter_sql in [
        ("news_items", "is_valid", "ALTER TABLE news_items ADD COLUMN is_valid TINYINT(1) DEFAULT 1"),
        ("parsed_signals", "is_valid", "ALTER TABLE parsed_signals ADD COLUMN is_valid TINYINT(1) DEFAULT 1"),
        ("news_items", "media_url", "ALTER TABLE news_items ADD COLUMN media_url TEXT DEFAULT NULL"),
        ("news_items", "media_type", "ALTER TABLE news_items ADD COLUMN media_type VARCHAR(50) DEFAULT NULL"),
        # Alpha Analysis: new columns for sentiment and conviction tracking
        ("parsed_signals", "sentiment_score", "ALTER TABLE parsed_signals ADD COLUMN sentiment_score DECIMAL(3,2) DEFAULT NULL"),
        ("parsed_signals", "conviction_score", "ALTER TABLE parsed_signals ADD COLUMN conviction_score DECIMAL(3,2) DEFAULT NULL"),
        ("parsed_signals", "trader_view", "ALTER TABLE parsed_signals ADD COLUMN trader_view VARCHAR(500) DEFAULT NULL"),
        ("parsed_signals", "source_media", "ALTER TABLE parsed_signals ADD COLUMN source_media VARCHAR(20) DEFAULT 'text'"),
        # v14: SL/TP source tracking
        ("parsed_signals", "sl_source", "ALTER TABLE parsed_signals ADD COLUMN sl_source VARCHAR(20) DEFAULT 'human' AFTER stop_loss"),
        ("parsed_signals", "tp_source", "ALTER TABLE parsed_signals ADD COLUMN tp_source VARCHAR(20) DEFAULT 'human' AFTER take_profit_6"),
    ]:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col} column to {tbl}")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: {tbl} {col} column: {e}")

    # 11. Following system tables (v9 migration)
    _bootstrap_following_tables(db)

    # 12. Auto-refresh model availability based on API keys in environment
    try:
        from core.model_interface import get_model_interface
        mi = get_model_interface()
        mi.auto_refresh_model_availability()
    except Exception as e:
        logger.debug(f"Bootstrap: model availability refresh: {e}")

    # 13. Discover new models from provider APIs (Layer 1 — automatic)
    try:
        from services.model_upgrade import ModelUpgradeService
        svc = ModelUpgradeService(db=db)
        discovered = svc.discover_new_models()
        if discovered:
            logger.info(f"Bootstrap: Discovered {len(discovered)} new models from provider APIs")
    except Exception as e:
        logger.debug(f"Bootstrap: model discovery: {e}")

    # 14. Normalize provider labels — detect provider from model name pattern
    #     gpt-*, o1-*, o3-*, o4-*, chatgpt-*, openai/* → openrouter
    #     whisper-* → openai (direct, for transcription)
    #     claude-* → anthropic
    #     gemini-* → google
    try:
        provider_rules = [
            ("openrouter", ["gpt-%", "o1-%", "o3-%", "o4-%", "chatgpt-%", "openai/%"]),
            ("openai",     ["whisper-%"]),
            ("anthropic",  ["claude-%"]),
            ("google",     ["gemini-%"]),
        ]
        for table, model_col, prov_col in [
            ("prompt_versions",         "model",    "model_provider"),
            ("source_model_assignments","model_id", "model_provider"),
            ("known_models",            "model_id", "provider"),
        ]:
            for correct_provider, patterns in provider_rules:
                for pat in patterns:
                    db.execute(
                        f"UPDATE {table} SET {prov_col} = %s "
                        f"WHERE {model_col} LIKE %s AND ({prov_col} IS NULL OR {prov_col} = '' "
                        f"OR {prov_col} = 'openai' OR {prov_col} = 'unknown')",
                        (correct_provider, pat)
                    )
    except Exception as e:
        logger.debug(f"Bootstrap: provider normalize: {e}")

    # 14b. Add new columns to known_models if they don't exist
    try:
        new_columns = [
            ("known_models", "max_output_tokens", "ALTER TABLE known_models ADD COLUMN max_output_tokens INT DEFAULT NULL COMMENT 'Max output tokens'"),
            ("known_models", "max_tokens_param", "ALTER TABLE known_models ADD COLUMN max_tokens_param VARCHAR(30) DEFAULT 'max_tokens'"),
            ("known_models", "temperature_max", "ALTER TABLE known_models ADD COLUMN temperature_max DECIMAL(3,1) DEFAULT 2.0"),
            ("known_models", "supports_audio", "ALTER TABLE known_models ADD COLUMN supports_audio TINYINT(1) DEFAULT 0"),
            ("known_models", "supports_video", "ALTER TABLE known_models ADD COLUMN supports_video TINYINT(1) DEFAULT 0"),
        ]
        for table, col, sql in new_columns:
            try:
                existing = db.fetch_all(f"SHOW COLUMNS FROM {table} LIKE %s", (col,))
                if not existing:
                    db.execute(sql)
                    logger.info(f"Bootstrap: Added column {table}.{col}")
            except Exception as col_err:
                logger.debug(f"Bootstrap: column {col} check: {col_err}")
    except Exception as e:
        logger.debug(f"Bootstrap: known_models column migration: {e}")

    # 14c. Add new columns to parsed_signals for extended TP levels
    try:
        signal_columns = [
            ("parsed_signals", "take_profit_4", "ALTER TABLE parsed_signals ADD COLUMN take_profit_4 DECIMAL(15,5) DEFAULT NULL"),
            ("parsed_signals", "take_profit_5", "ALTER TABLE parsed_signals ADD COLUMN take_profit_5 DECIMAL(15,5) DEFAULT NULL"),
            ("parsed_signals", "take_profit_6", "ALTER TABLE parsed_signals ADD COLUMN take_profit_6 DECIMAL(15,5) DEFAULT NULL"),
            ("parsed_signals", "outcome_tp_hit", "ALTER TABLE parsed_signals ADD COLUMN outcome_tp_hit INT DEFAULT NULL COMMENT 'Which TP was hit (1-6)'"),
        ]
        for table, col, sql in signal_columns:
            try:
                existing = db.fetch_all(f"SHOW COLUMNS FROM {table} LIKE %s", (col,))
                if not existing:
                    db.execute(sql)
                    logger.info(f"Bootstrap: Added column {table}.{col}")
            except Exception as col_err:
                logger.debug(f"Bootstrap: column {col} check: {col_err}")
    except Exception as e:
        logger.debug(f"Bootstrap: parsed_signals column migration: {e}")

    # 14c-fix. Convert parsed_signals.status from ENUM to VARCHAR(20) if still ENUM
    # (old schema had ENUM missing tp4_hit/tp5_hit/tp6_hit causing Data truncated errors)
    try:
        col_info = db.fetch_one("SHOW COLUMNS FROM parsed_signals WHERE Field = 'status'")
        if col_info and 'enum' in str(col_info.get('Type', '')).lower():
            db.execute(
                "ALTER TABLE parsed_signals MODIFY COLUMN status VARCHAR(20) DEFAULT %s",
                ("pending",))
            logger.info("Bootstrap: converted parsed_signals.status from ENUM to VARCHAR(20)")
    except Exception as e:
        logger.debug(f"Bootstrap: parsed_signals.status migration: {e}")

    # 14d. Add new columns to parsed_signals for dual analysis (human vs jarvis)
    try:
        dual_analysis_cols = [
            ("parsed_signals", "signal_source", "ALTER TABLE parsed_signals ADD COLUMN signal_source VARCHAR(20) DEFAULT 'human'"),
            ("parsed_signals", "trader_confidence", "ALTER TABLE parsed_signals ADD COLUMN trader_confidence DECIMAL(5,4) DEFAULT NULL"),
            ("parsed_signals", "ai_confidence", "ALTER TABLE parsed_signals ADD COLUMN ai_confidence DECIMAL(5,4) DEFAULT NULL"),
            ("parsed_signals", "trader_rationale", "ALTER TABLE parsed_signals ADD COLUMN trader_rationale TEXT DEFAULT NULL"),
            ("parsed_signals", "ai_rationale", "ALTER TABLE parsed_signals ADD COLUMN ai_rationale TEXT DEFAULT NULL"),
            ("parsed_signals", "trade_type", "ALTER TABLE parsed_signals ADD COLUMN trade_type VARCHAR(20) DEFAULT NULL"),
        ]
        for table, col, sql in dual_analysis_cols:
            try:
                existing = db.fetch_all(f"SHOW COLUMNS FROM {table} LIKE %s", (col,))
                if not existing:
                    db.execute(sql)
                    logger.info(f"Bootstrap: Added column {table}.{col}")
            except Exception as col_err:
                logger.debug(f"Bootstrap: column {col} check: {col_err}")
        try:
            db.execute("ALTER TABLE parsed_signals ADD INDEX idx_ps_signal_source (signal_source)")
        except Exception:
            pass
    except Exception as e:
        logger.debug(f"Bootstrap: parsed_signals dual analysis migration: {e}")

    # 15. Run quick model discovery (just mark available models, skip full pricing sync)
    # Full model sync with AI matching runs once daily on CEO schedule
    try:
        from services.model_upgrade import ModelUpgradeService
        svc = ModelUpgradeService(db=db)
        discovered = svc.discover_new_models()
        if discovered:
            logger.info(f"Bootstrap: Discovered {len(discovered)} new models")
        # Count available models
        avail = db.fetch_one("SELECT COUNT(*) AS c FROM known_models WHERE is_available = 1")
        logger.info(f"Bootstrap: {avail['c']} models available")
    except Exception as e:
        logger.warning(f"Bootstrap: model discovery error: {e}")

    # 16. Model availability is now set by model sync above
    #     No hardcoded whitelist - if a model is returned by a provider API, it's available.
    #     This ensures dropdowns always show exactly what your API keys can access.
    try:
        # Count currently available models (set by discover_new_models in step 13)
        avail = db.fetch_one("SELECT COUNT(*) AS c FROM known_models WHERE is_available = 1")
        logger.info(f"Bootstrap: {avail['c']} models available (discovered from your API keys)")
    except Exception as e:
        logger.debug(f"Bootstrap: model availability count: {e}")

    # 16. Sync model pricing from LiteLLM community database (factual pricing)
    try:
        from services.model_upgrade import ModelUpgradeService
        svc = ModelUpgradeService(db=db)
        pricing_result = svc.sync_model_pricing(auto_apply=True, notify=True)
        logger.info(
            f"Bootstrap: Pricing sync — {pricing_result['total_updated']} updated, "
            f"{pricing_result['total_new']} new, "
            f"{len(pricing_result.get('active_model_changes', []))} active price changes"
        )
    except Exception as e:
        logger.warning(f"Bootstrap: pricing sync: {e}")

    # Create trade_dossiers table for the Trading Floor system
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS trade_dossiers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                status ENUM('draft','proposed','monitoring','open_order','live',
                            'ready','executed',
                            'won','lost','expired','abandoned') DEFAULT 'draft',
                trade_decision ENUM('trade_now','wait_for_conditions','do_not_trade') DEFAULT NULL,
                direction ENUM('BUY','SELL') DEFAULT NULL,
                entry_price DECIMAL(15,5) DEFAULT NULL,
                stop_loss DECIMAL(15,5) DEFAULT NULL,
                take_profit_1 DECIMAL(15,5) DEFAULT NULL,
                take_profit_2 DECIMAL(15,5) DEFAULT NULL,
                take_profit_3 DECIMAL(15,5) DEFAULT NULL,
                confidence_score INT DEFAULT NULL,
                time_horizon_hours INT DEFAULT 24,

                dossier_sections JSON COMMENT 'All 8 dossier sections as structured data',
                stage1_ta_output MEDIUMTEXT COMMENT 'Unbiased TA from cheap model',
                stage2_full_prompt MEDIUMTEXT COMMENT 'Full prompt sent to premium model',
                stage2_hypothesis JSON COMMENT 'Entry, SL, TPs, direction, confidence, rationale, conditions',
                stage2_model_used VARCHAR(100) DEFAULT NULL,
                stage2_raw_response MEDIUMTEXT COMMENT 'Premium model raw response',

                conditions_for_entry JSON COMMENT 'Conditions premium model says must be true',
                conditions_met JSON COMMENT 'Tracked conditions status over time',
                probability_history JSON COMMENT 'Array of timestamp+probability+reason snapshots',
                tracker_log MEDIUMTEXT COMMENT 'Trackers monitoring observations',

                linked_signal_id INT DEFAULT NULL COMMENT 'Paper trade signal in parsed_signals',
                limit_order_active TINYINT(1) DEFAULT 0,
                limit_order_price DECIMAL(15,5) DEFAULT NULL,
                limit_order_placed_at DATETIME DEFAULT NULL,

                postmortem_output MEDIUMTEXT COMMENT 'Post-mortem analysis from premium model',
                postmortem_at DATETIME DEFAULT NULL,
                liquidation_grab_detected TINYINT(1) DEFAULT 0,
                lessons_learned TEXT,

                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                expires_at DATETIME DEFAULT NULL,

                INDEX idx_symbol (symbol),
                INDEX idx_status (status),
                INDEX idx_created (created_at),
                INDEX idx_symbol_status (symbol, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: trade_dossiers: {e}")

    # Add tracker_conversation column for Phase 3 conversational tracker
    for alter_sql in [
        "ALTER TABLE trade_dossiers ADD COLUMN tracker_conversation JSON COMMENT 'Persistent two-way LLM conversation history'",
    ]:
        try:
            db.execute(alter_sql)
            logger.info("Bootstrap: added tracker_conversation column to trade_dossiers")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: tracker_conversation column: {e}")

    # 3-tier model tracking columns
    for alter_sql in [
        "ALTER TABLE trade_dossiers ADD COLUMN stage1_model_used VARCHAR(100) DEFAULT NULL COMMENT 'Model that produced Stage 1 TA'",
        "ALTER TABLE trade_dossiers ADD COLUMN model_tier VARCHAR(20) DEFAULT NULL COMMENT 'Which tier was active: primary, secondary, free'",
    ]:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added model tracking column to trade_dossiers")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: model tracking column: {e}")

    # P&L tracking columns
    pnl_alters = [
        "ALTER TABLE trade_dossiers ADD COLUMN margin_usd DECIMAL(12,2) DEFAULT 20.00 COMMENT 'Margin per position in USD'",
        "ALTER TABLE trade_dossiers ADD COLUMN leverage INT DEFAULT 5 COMMENT 'Leverage multiplier'",
        "ALTER TABLE trade_dossiers ADD COLUMN current_price DECIMAL(15,5) DEFAULT NULL COMMENT 'Latest observed price'",
        "ALTER TABLE trade_dossiers ADD COLUMN current_price_at DATETIME DEFAULT NULL COMMENT 'When current_price was last updated'",
        "ALTER TABLE trade_dossiers ADD COLUMN unrealised_pnl DECIMAL(12,4) DEFAULT NULL COMMENT 'Live P&L in USD'",
        "ALTER TABLE trade_dossiers ADD COLUMN unrealised_pnl_pct DECIMAL(8,4) DEFAULT NULL COMMENT 'Live P&L as pct of margin'",
        "ALTER TABLE trade_dossiers ADD COLUMN realised_pnl DECIMAL(12,4) DEFAULT NULL COMMENT 'Final P&L if closed'",
        "ALTER TABLE trade_dossiers ADD COLUMN realised_pnl_pct DECIMAL(8,4) DEFAULT NULL COMMENT 'Final P&L pct if closed'",
        "ALTER TABLE trade_dossiers ADD COLUMN executed_at DATETIME DEFAULT NULL COMMENT 'When the trade went live'",
    ]
    for alter_sql in pnl_alters:
        try:
            db.execute(alter_sql)
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: P&L column: {e}")

    # ── Status rename: ready → open_order, executed → live ──
    try:
        db.execute("""
            ALTER TABLE trade_dossiers
            MODIFY COLUMN status ENUM('draft','proposed','monitoring',
                'open_order','live','ready','executed',
                'won','lost','expired','abandoned') DEFAULT 'draft'
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: status ENUM expand: {e}")

    try:
        db.execute("UPDATE trade_dossiers SET status='open_order' WHERE status='ready'")
        db.execute("UPDATE trade_dossiers SET status='live' WHERE status='executed'")
    except Exception as e:
        logger.debug(f"Bootstrap: status migration: {e}")

    # TP progress tracking + extra TP columns for dossiers
    try:
        db.execute("""
            ALTER TABLE trade_dossiers ADD COLUMN tp_progress
            VARCHAR(10) DEFAULT 'none' COMMENT 'Furthest TP level reached (tp1_hit..tp6_hit)'
        """)
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: tp_progress column: {e}")

    # Widen tp_progress from ENUM to VARCHAR if it was created as ENUM
    try:
        db.execute("""
            ALTER TABLE trade_dossiers MODIFY COLUMN tp_progress
            VARCHAR(10) DEFAULT 'none' COMMENT 'Furthest TP level reached (tp1_hit..tp6_hit)'
        """)
    except Exception:
        pass

    for col in ["tp1_hit_at", "tp2_hit_at", "tp3_hit_at",
                "tp4_hit_at", "tp5_hit_at", "tp6_hit_at", "sl_hit_at"]:
        try:
            db.execute(f"ALTER TABLE trade_dossiers ADD COLUMN {col} DATETIME(3) DEFAULT NULL")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: {col}: {e}")

    for col in ["take_profit_4", "take_profit_5", "take_profit_6"]:
        try:
            db.execute(f"ALTER TABLE trade_dossiers ADD COLUMN {col} DECIMAL(15,5) DEFAULT NULL")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: {col}: {e}")

    # Dossier intelligence: stores mentor context, source alpha, reasoning
    try:
        db.execute("""
            ALTER TABLE trade_dossiers ADD COLUMN dossier_intelligence MEDIUMTEXT DEFAULT NULL
            COMMENT 'Rich context: mentor setup, source alpha, AI analysis'
        """)
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: dossier_intelligence: {e}")

    # Mentor: track all symbols (bypass detect_signals gate)
    try:
        db.execute("""
            ALTER TABLE user_profiles ADD COLUMN track_all_symbols TINYINT(1) DEFAULT 0
            COMMENT 'When 1, signals from this mentor bypass the market_symbols detect_signals gate'
        """)
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: track_all_symbols: {e}")

    try:
        db.execute("""
            ALTER TABLE user_profiles ADD COLUMN mentor_trading_enabled TINYINT(1) DEFAULT 1
            COMMENT 'When 1, mentor signals are sent to Apex/trading floor. When 0, alpha still collected but no trades.'
        """)
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: mentor_trading_enabled: {e}")

    for col_sql in [
        "ALTER TABLE trade_dossiers ADD COLUMN mentor_source VARCHAR(100) DEFAULT NULL "
        "COMMENT 'mentor author if this dossier originated from a mentor signal'",
        "ALTER TABLE trade_dossiers ADD COLUMN mentor_type ENUM('apex_assessed','mentor_mirror') DEFAULT NULL "
        "COMMENT 'apex_assessed = Apex independent review, mentor_mirror = copy of mentor levels'",
        "ALTER TABLE trade_dossiers ADD COLUMN opp_score DECIMAL(5,2) DEFAULT NULL "
        "COMMENT 'Composite Opportunity Score 0-100 for ranking best trades'",
    ]:
        try:
            db.execute(col_sql)
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: mentor dossier cols: {e}")

    # Prompt version tracking for A/B testing
    try:
        db.execute(
            "ALTER TABLE trade_dossiers ADD COLUMN prompt_version_id INT DEFAULT NULL "
            "COMMENT 'Links to prompt_versions.id for A/B testing and prompt evolution'"
        )
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: prompt_version_id: {e}")

    # Original stop loss: preserved before any SL trailing, critical for accurate retest
    try:
        db.execute(
            "ALTER TABLE trade_dossiers ADD COLUMN original_stop_loss DECIMAL(15,5) DEFAULT NULL "
            "COMMENT 'SL at entry time, before any TP-triggered trailing. Used by retest.'"
        )
        logger.info("Bootstrap: added original_stop_loss column to trade_dossiers")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: original_stop_loss: {e}")

    # Backfill original_stop_loss for existing dossiers (one-time migration)
    try:
        # Dossiers with no TP trailing: current stop_loss IS the original
        db.execute("""
            UPDATE trade_dossiers
            SET original_stop_loss = stop_loss
            WHERE original_stop_loss IS NULL
              AND stop_loss IS NOT NULL
              AND (tp_progress IS NULL OR tp_progress = 'none')
        """)
        # Dossiers WITH TP trailing: try to recover original SL from stage2_hypothesis JSON
        trailed = db.fetch_all("""
            SELECT id, stage2_hypothesis, entry_price, direction
            FROM trade_dossiers
            WHERE original_stop_loss IS NULL
              AND stop_loss IS NOT NULL
              AND tp_progress IS NOT NULL AND tp_progress != 'none'
        """)
        import json as _json
        for row in (trailed or []):
            orig_sl = None
            hyp = row.get("stage2_hypothesis")
            if hyp:
                try:
                    if isinstance(hyp, str):
                        hyp = _json.loads(hyp)
                    if isinstance(hyp, dict):
                        orig_sl = hyp.get("stop_loss") or hyp.get("sl")
                        if orig_sl:
                            orig_sl = float(orig_sl)
                except Exception:
                    pass
            if orig_sl and orig_sl > 0:
                db.execute(
                    "UPDATE trade_dossiers SET original_stop_loss = %s WHERE id = %s",
                    (round(orig_sl, 5), row["id"]))
        logger.info("Bootstrap: backfilled original_stop_loss for existing dossiers")
    except Exception as e:
        logger.debug(f"Bootstrap: original_stop_loss backfill: {e}")

    # raw_symbol: preserve original signal name before normalization
    try:
        db.execute(
            "ALTER TABLE trade_dossiers ADD COLUMN raw_symbol VARCHAR(30) DEFAULT NULL "
            "COMMENT 'Original symbol from signal/mentor before USDT normalization'"
        )
        logger.info("Bootstrap: added raw_symbol column to trade_dossiers")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: raw_symbol: {e}")

    # Paper Trade Realism: fee/funding columns for trade_dossiers
    paper_realism_dossier_cols = [
        "ALTER TABLE trade_dossiers ADD COLUMN actual_entry_price DECIMAL(15,5) DEFAULT NULL COMMENT 'Real fill price vs entry_price target'",
        "ALTER TABLE trade_dossiers ADD COLUMN actual_exit_price DECIMAL(15,5) DEFAULT NULL COMMENT 'Real exit price'",
        "ALTER TABLE trade_dossiers ADD COLUMN entry_fill_source VARCHAR(20) DEFAULT NULL COMMENT 'wick|trade_now_market|live_stream|live_fallback'",
        "ALTER TABLE trade_dossiers ADD COLUMN entry_fee DECIMAL(12,6) DEFAULT NULL COMMENT 'USD fee on entry'",
        "ALTER TABLE trade_dossiers ADD COLUMN exit_fee DECIMAL(12,6) DEFAULT NULL COMMENT 'USD fee on exit'",
        "ALTER TABLE trade_dossiers ADD COLUMN accrued_funding DECIMAL(12,6) DEFAULT NULL COMMENT 'Cumulative funding rate cost'",
        "ALTER TABLE trade_dossiers ADD COLUMN last_funding_at DATETIME DEFAULT NULL COMMENT 'When funding was last applied'",
        "ALTER TABLE trade_dossiers ADD COLUMN total_fees DECIMAL(12,6) DEFAULT NULL COMMENT 'entry_fee + exit_fee + accrued_funding'",
        "ALTER TABLE trade_dossiers ADD COLUMN resolved_exchange VARCHAR(20) DEFAULT NULL COMMENT 'Exchange symbol resolves on (bybit/blofin) or NULL if no exchange'",
        "ALTER TABLE trade_dossiers ADD COLUMN waterfall_resolved TINYINT(1) DEFAULT NULL COMMENT '1=symbol found on waterfall accounts, 0=not found, NULL=not checked'",
        "ALTER TABLE trade_dossiers ADD COLUMN waterfall_account VARCHAR(100) DEFAULT NULL COMMENT 'First waterfall account_id that matched this symbol'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_probability INT DEFAULT NULL COMMENT 'Tracker probability when dossier moved to open_order'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_conditions_met INT DEFAULT NULL COMMENT 'Conditions met count at trigger time'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_conditions_total INT DEFAULT NULL COMMENT 'Total conditions at trigger time'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_threshold_execute INT DEFAULT NULL COMMENT 'condition_threshold_execute setting at trigger time'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_threshold_limit INT DEFAULT NULL COMMENT 'condition_threshold_limit_order setting at trigger time'",
        "ALTER TABLE trade_dossiers ADD COLUMN trigger_min_confidence INT DEFAULT NULL COMMENT 'min_confidence_for_trade setting at trigger time'",
    ]
    for alter_sql in paper_realism_dossier_cols:
        try:
            db.execute(alter_sql)
            logger.info("Bootstrap: added paper realism column to trade_dossiers")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: paper realism trade_dossiers: {e}")

    # Risk mode: pct or fixed amount per trade (live account settings)
    for alter_sql in [
        "ALTER TABLE trading_accounts ADD COLUMN risk_mode ENUM('pct','fixed') DEFAULT 'pct' "
        "COMMENT 'pct=%% of balance, fixed=fixed USD per trade'",
        "ALTER TABLE trading_accounts ADD COLUMN risk_fixed_usd DECIMAL(12,2) DEFAULT 20.00 "
        "COMMENT 'Fixed margin per trade when risk_mode=fixed'",
    ]:
        try:
            db.execute(alter_sql)
            logger.info("Bootstrap: added risk_mode columns to trading_accounts")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: risk_mode trading_accounts: {e}")

    # Apex seen items tracking (for efficient alpha delivery — only new items as full detail)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS apex_seen_items (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                symbol          VARCHAR(50)     NOT NULL,
                news_item_id    INT             NOT NULL,
                dossier_id      INT             NOT NULL,
                seen_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_apex_seen_symbol (symbol, seen_at),
                INDEX idx_apex_seen_ni (news_item_id),
                UNIQUE KEY uq_apex_seen (symbol, news_item_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception:
        pass

    # ── paper_reason: why a dossier couldn't be placed on an exchange ──
    try:
        db.execute(
            "ALTER TABLE trade_dossiers ADD COLUMN paper_reason VARCHAR(120) DEFAULT NULL "
            "COMMENT 'Why this dossier is paper-only: paper_margin_full, paper_no_exchange, "
            "paper_no_eligible_accounts, no_live_accounts, mentor_not_whitelisted'")
        logger.info("Bootstrap: added paper_reason column to trade_dossiers")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: paper_reason: {e}")

    # ── trade_lessons: add missing columns for lesson categorisation ──
    for col_name, alter_sql in [
        ("lesson_type",     "ALTER TABLE trade_lessons ADD COLUMN lesson_type VARCHAR(50) DEFAULT 'general'"),
        ("source_model",    "ALTER TABLE trade_lessons ADD COLUMN source_model VARCHAR(100) DEFAULT NULL"),
        ("source_trade_id", "ALTER TABLE trade_lessons ADD COLUMN source_trade_id INT DEFAULT NULL"),
    ]:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col_name} column to trade_lessons")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: trade_lessons.{col_name}: {e}")

    # ── One-time repair: fix stale total_trades/total_pnl in prompt_versions ──
    try:
        from services.trade_scorer import TradeScorer
        ts = TradeScorer()
        result = ts.repair_prompt_stats()
        if result.get("repaired", 0) > 0:
            logger.info(f"Bootstrap: repaired {result['repaired']} prompt_versions rows")
    except Exception as e:
        logger.debug(f"Bootstrap: repair_prompt_stats: {e}")

    logger.info("Bootstrap complete")


def _bootstrap_following_tables(db):
    """Create Following system tables if they don't exist (v9 migration).
    Tables: user_groups, user_profiles, user_profile_links, alpha_assessments, identity_suggestions.
    Also adds download_media column to managed_sources and telegram_channels.
    """
    # Add download_media columns (safe - ignores if already exists)
    for alter_sql in [
        "ALTER TABLE managed_sources ADD COLUMN download_media TINYINT(1) DEFAULT 0",
        "ALTER TABLE telegram_channels ADD COLUMN download_media TINYINT(1) DEFAULT 0",
    ]:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added download_media column")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: download_media column: {e}")

    # Create user_groups table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_groups (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_group_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_groups: {e}")

    # Create user_profiles table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INT AUTO_INCREMENT PRIMARY KEY,
                display_name VARCHAR(200) NOT NULL,
                notes TEXT,
                group_id INT DEFAULT NULL,
                avatar_url VARCHAR(500),
                ai_summary TEXT,
                ai_rating DECIMAL(5,2) DEFAULT NULL,
                ai_last_analyzed_at DATETIME DEFAULT NULL,
                total_alpha INT DEFAULT 0,
                total_signals INT DEFAULT 0,
                wins INT DEFAULT 0,
                losses INT DEFAULT 0,
                total_pips DECIMAL(10,1) DEFAULT 0,
                win_rate DECIMAL(5,2) DEFAULT 0,
                avg_rr DECIMAL(5,2) DEFAULT 0,
                sharpe_ratio DECIMAL(5,2) DEFAULT NULL,
                trust_score DECIMAL(5,2) DEFAULT 0,
                tier VARCHAR(10) DEFAULT 'T1',
                tier_per_symbol JSON DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_up_group (group_id),
                INDEX idx_up_trust (trust_score DESC),
                INDEX idx_up_winrate (win_rate DESC)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_profiles: {e}")

    # Create user_profile_links table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_links (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_profile_id INT NOT NULL,
                source VARCHAR(100) NOT NULL,
                source_detail VARCHAR(200),
                source_username VARCHAR(200),
                track_signals TINYINT(1) DEFAULT 1,
                download_media TINYINT(1) DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_link (source, source_detail, source_username),
                INDEX idx_upl_profile (user_profile_id),
                INDEX idx_upl_source (source, source_detail)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_profile_links: {e}")

    # Create alpha_assessments table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alpha_assessments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                news_item_id INT NOT NULL,
                author VARCHAR(100),
                source VARCHAR(100),
                source_detail VARCHAR(200),
                user_profile_id INT DEFAULT NULL,
                claim_type ENUM('bullish','bearish','neutral','signal','news','opinion','mixed') DEFAULT 'neutral',
                symbols_mentioned JSON,
                market_direction_at_post VARCHAR(10),
                market_direction_1h VARCHAR(10),
                market_direction_4h VARCHAR(10),
                market_direction_1d VARCHAR(10),
                market_direction_1w VARCHAR(10),
                accuracy_score DECIMAL(5,2) DEFAULT NULL,
                assessed_by VARCHAR(50) DEFAULT 'alpha_analyst',
                assessment_text TEXT,
                assessed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_aa_news (news_item_id),
                INDEX idx_aa_author (author),
                INDEX idx_aa_profile (user_profile_id),
                INDEX idx_aa_accuracy (accuracy_score DESC)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: alpha_assessments: {e}")

    # Create identity_suggestions table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS identity_suggestions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                source_a VARCHAR(100) NOT NULL,
                source_detail_a VARCHAR(200),
                username_a VARCHAR(200) NOT NULL,
                source_b VARCHAR(100) NOT NULL,
                source_detail_b VARCHAR(200),
                username_b VARCHAR(200) NOT NULL,
                confidence DECIMAL(5,2) DEFAULT 0,
                reasoning TEXT,
                status ENUM('pending','accepted','dismissed') DEFAULT 'pending',
                user_profile_id INT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_is_status (status),
                INDEX idx_is_confidence (confidence DESC)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: identity_suggestions: {e}")

    # Create user_follow_preferences table (v10)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_follow_preferences (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                source          VARCHAR(100)    NOT NULL,
                source_detail   VARCHAR(200)    DEFAULT '',
                author          VARCHAR(200)    NOT NULL,
                follow_in_feed  TINYINT(1)      DEFAULT 0,
                watch_signals   TINYINT(1)      DEFAULT 0,
                track_alpha     TINYINT(1)      DEFAULT 0,
                copy_trades     TINYINT(1)      DEFAULT 0,
                download_media  TINYINT(1)      DEFAULT 0,
                trade_mode      VARCHAR(10)     DEFAULT 'off',
                created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_user_pref (source, source_detail, author),
                INDEX idx_ufp_follow (follow_in_feed),
                INDEX idx_ufp_watch (watch_signals),
                INDEX idx_ufp_track (track_alpha)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_follow_preferences: {e}")

    # Create user_symbol_mentions table (Command Center: tracks which symbols each user talks about)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_symbol_mentions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                author VARCHAR(200) NOT NULL,
                source VARCHAR(100) NOT NULL,
                source_detail VARCHAR(200) DEFAULT '',
                symbol VARCHAR(50) NOT NULL,
                mention_count INT DEFAULT 1,
                last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_author_symbol (author, source, source_detail, symbol),
                INDEX idx_symbol (symbol),
                INDEX idx_author (author, source)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_symbol_mentions: {e}")

    # Create tier_change_log table (audit trail for promotions/demotions)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS tier_change_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                author VARCHAR(200) NOT NULL,
                source VARCHAR(100) NOT NULL,
                source_detail VARCHAR(200) DEFAULT '',
                old_tier VARCHAR(5) NOT NULL,
                new_tier VARCHAR(5) NOT NULL,
                symbol VARCHAR(50) DEFAULT NULL,
                reason TEXT,
                changed_by VARCHAR(50) DEFAULT 'system',
                evidence TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_author (author, source)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: tier_change_log: {e}")

    # Add mentor columns to user_profiles (safe ALTER — ignores if already exists)
    for col_sql in [
        "ALTER TABLE user_profiles ADD COLUMN is_mentor TINYINT(1) NOT NULL DEFAULT 0 AFTER trust_score",
        "ALTER TABLE user_profiles ADD COLUMN mentor_notes TEXT AFTER is_mentor",
        "ALTER TABLE user_profiles ADD COLUMN mentor_style_summary TEXT AFTER mentor_notes",
        "ALTER TABLE user_profiles ADD COLUMN mentor_assigned_at DATETIME DEFAULT NULL AFTER mentor_style_summary",
        "ALTER TABLE user_profiles ADD INDEX idx_up_mentor (is_mentor)",
    ]:
        try:
            db.execute(col_sql)
        except Exception:
            pass  # column/index already exists

    # Create mentor_learnings table (AI-generated insights from mentor content)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS mentor_learnings (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_profile_id INT NOT NULL,
                news_item_id INT DEFAULT NULL,
                symbol VARCHAR(50) DEFAULT NULL,
                content_type ENUM('text','chart','video','audio','voicenote','signal') DEFAULT 'text',
                learning_category ENUM(
                    'trading_style','entry_pattern','exit_strategy',
                    'risk_management','session_preference','setup_type',
                    'market_read','psychology','general'
                ) DEFAULT 'general',
                insight_title VARCHAR(300) NOT NULL,
                insight_detail TEXT NOT NULL,
                confidence DECIMAL(5,2) DEFAULT 50.00,
                learned_by VARCHAR(50) DEFAULT 'apex',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_ml_profile (user_profile_id),
                INDEX idx_ml_symbol (symbol),
                INDEX idx_ml_category (learning_category),
                INDEX idx_ml_news (news_item_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: mentor_learnings: {e}")

    # Create user_selected_symbols table (Command Center: user's selected symbols)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS user_selected_symbols (
                id INT AUTO_INCREMENT PRIMARY KEY,
                author VARCHAR(200) NOT NULL,
                source VARCHAR(100) NOT NULL,
                source_detail VARCHAR(200) DEFAULT '',
                symbol VARCHAR(50) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_user_sym (author, source, source_detail, symbol),
                INDEX idx_symbol (symbol)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: user_selected_symbols: {e}")

    # Create market_symbols table (symbol registry)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS market_symbols (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL UNIQUE,
                display_name VARCHAR(100) DEFAULT NULL,
                asset_class ENUM('forex','commodity','index','cryptocurrency','stock','etf','unknown') DEFAULT 'unknown',
                category VARCHAR(50) DEFAULT 'other',
                yahoo_ticker VARCHAR(30) DEFAULT NULL,
                mt5_symbol VARCHAR(30) DEFAULT NULL,
                first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                first_seen_source VARCHAR(100) DEFAULT NULL,
                signal_count INT DEFAULT 0,
                last_signal_at DATETIME DEFAULT NULL,
                show_in_alpha TINYINT(1) DEFAULT 1,
                submit_to_ai TINYINT(1) DEFAULT 1,
                detect_signals TINYINT(1) DEFAULT 1,
                tradable TINYINT(1) DEFAULT 0,
                notes TEXT DEFAULT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                bybit_ticker VARCHAR(30) DEFAULT NULL,
                blofin_ticker VARCHAR(30) DEFAULT NULL,
                bitget_ticker VARCHAR(30) DEFAULT NULL,
                preferred_exchange VARCHAR(20) DEFAULT 'bybit',
                fallback_exchange VARCHAR(20) DEFAULT 'blofin',
                INDEX idx_asset_class (asset_class),
                INDEX idx_category (category),
                INDEX idx_tradable (tradable)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: market_symbols: {e}")

    # Add CCXT exchange ticker columns to market_symbols (migration for existing tables)
    for col_name, alter_sql in [
        ("bybit_ticker", "ALTER TABLE market_symbols ADD COLUMN bybit_ticker VARCHAR(30) DEFAULT NULL"),
        ("blofin_ticker", "ALTER TABLE market_symbols ADD COLUMN blofin_ticker VARCHAR(30) DEFAULT NULL"),
        ("bitget_ticker", "ALTER TABLE market_symbols ADD COLUMN bitget_ticker VARCHAR(30) DEFAULT NULL"),
        ("preferred_exchange", "ALTER TABLE market_symbols ADD COLUMN preferred_exchange VARCHAR(20) DEFAULT 'bybit'"),
        ("fallback_exchange", "ALTER TABLE market_symbols ADD COLUMN fallback_exchange VARCHAR(20) DEFAULT 'blofin'"),
    ]:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col_name} column to market_symbols")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: market_symbols {col_name} column: {e}")

    # Upgrade system_config.config_value to MEDIUMTEXT for large prompts
    try:
        db.execute("ALTER TABLE system_config MODIFY COLUMN config_value MEDIUMTEXT NOT NULL")
        logger.info("Bootstrap: upgraded system_config.config_value to MEDIUMTEXT")
    except Exception as e:
        logger.debug(f"Bootstrap: system_config config_value upgrade: {e}")

    # Fix: switch source_detail columns to utf8mb4_bin so emoji-containing
    # channel names (📈 vs 📉 vs 📊) are distinguished correctly.
    # utf8mb4_unicode_ci treats all emojis as identical, causing cross-channel
    # contamination in user_follow_preferences and related tables.
    _collation_fixes = [
        ("user_follow_preferences", "source_detail", 200),
        ("user_follow_preferences", "author",         200),
        ("user_symbol_mentions",    "source_detail", 200),
        ("user_selected_symbols",   "source_detail", 200),
        ("news_items",              "source_detail", 100),
    ]
    for tbl, col, sz in _collation_fixes:
        try:
            db.execute(f"ALTER TABLE {tbl} MODIFY COLUMN {col} VARCHAR({sz}) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            logger.info(f"Bootstrap: switched {tbl}.{col} to utf8mb4_bin collation")
        except Exception as e:
            if "Unknown table" not in str(e):
                logger.debug(f"Bootstrap: {tbl}.{col} collation fix: {e}")

    # Clean up orphaned user_follow_preferences where source_detail
    # no longer matches any news_items row (stale emoji variants).
    try:
        orphaned = db.fetch_all("""
            SELECT ufp.id FROM user_follow_preferences ufp
            LEFT JOIN (SELECT DISTINCT source, source_detail FROM news_items) ni
              ON ufp.source = ni.source
             AND ufp.source_detail COLLATE utf8mb4_bin = ni.source_detail COLLATE utf8mb4_bin
            WHERE ni.source IS NULL AND ufp.source = 'discord'
        """)
        if orphaned:
            ids = [str(r['id']) for r in orphaned]
            db.execute(f"DELETE FROM user_follow_preferences WHERE id IN ({','.join(ids)})")
            logger.info(f"Bootstrap: cleaned {len(ids)} orphaned discord preference rows (emoji collation fix)")
    except Exception as e:
        logger.debug(f"Bootstrap: orphan pref cleanup: {e}")

    # Create telegram_channels table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS telegram_channels (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                account_id      VARCHAR(50)     NOT NULL DEFAULT 'default',
                channel_id      BIGINT          NOT NULL,
                channel_name    VARCHAR(200)    NOT NULL DEFAULT '',
                channel_type    VARCHAR(20)     DEFAULT 'channel',
                enabled         BOOLEAN         NOT NULL DEFAULT TRUE,
                last_message_id BIGINT          DEFAULT 0,
                download_media  TINYINT(1)      DEFAULT 0,
                created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_channel (account_id, channel_id),
                INDEX idx_tg_enabled (enabled)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: telegram_channels: {e}")

    # Create memory_audit table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS memory_audit (
                id                      INT AUTO_INCREMENT PRIMARY KEY,
                collection              VARCHAR(50)     NOT NULL,
                role                    VARCHAR(30)     NOT NULL DEFAULT 'all',
                text_preview            VARCHAR(2000)   DEFAULT NULL,
                metadata_json           TEXT            DEFAULT NULL,
                stored_at               DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
                storage_backend         VARCHAR(30)     NOT NULL DEFAULT 'mem0',
                INDEX idx_ma_collection (collection),
                INDEX idx_ma_role (role),
                INDEX idx_ma_stored (stored_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: memory_audit: {e}")

    # Create memory_fallback table
    try:
        db.execute("""
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
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: memory_fallback: {e}")

    # Create candles table (price data)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                candle_time DATETIME NOT NULL,
                open DECIMAL(15,5) NOT NULL,
                high DECIMAL(15,5) NOT NULL,
                low DECIMAL(15,5) NOT NULL,
                close DECIMAL(15,5) NOT NULL,
                volume BIGINT DEFAULT 0,
                source VARCHAR(20) DEFAULT 'unknown',
                fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uk_candle (symbol, timeframe, candle_time),
                INDEX idx_symbol_tf (symbol, timeframe),
                INDEX idx_candle_time (candle_time)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: candles: {e}")

    # Create candle_coverage table (tracks data coverage per symbol)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS candle_coverage (
                id INT AUTO_INCREMENT PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                timeframe VARCHAR(5) NOT NULL,
                earliest_candle DATETIME DEFAULT NULL,
                latest_candle DATETIME DEFAULT NULL,
                total_candles INT DEFAULT 0,
                gaps_detected INT DEFAULT 0,
                last_fetched_at DATETIME DEFAULT NULL,
                source VARCHAR(20) DEFAULT 'unknown',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_symbol_tf (symbol, timeframe),
                INDEX idx_last_fetched (last_fetched_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: candle_coverage: {e}")

    # Create data_source_config table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS data_source_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                source_name VARCHAR(30) NOT NULL UNIQUE,
                display_name VARCHAR(50) NOT NULL,
                is_enabled TINYINT(1) DEFAULT 1,
                priority INT DEFAULT 1,
                api_key VARCHAR(200) DEFAULT '',
                api_secret VARCHAR(200) DEFAULT '',
                config_json TEXT,
                supports_forex TINYINT(1) DEFAULT 1,
                supports_crypto TINYINT(1) DEFAULT 0,
                supports_stocks TINYINT(1) DEFAULT 0,
                supports_indices TINYINT(1) DEFAULT 1,
                supports_commodities TINYINT(1) DEFAULT 1,
                last_success_at DATETIME DEFAULT NULL,
                last_error TEXT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        # Seed default data sources
        db.execute("""
            INSERT INTO data_source_config (source_name, display_name, is_enabled, priority, supports_forex, supports_crypto, supports_stocks, supports_indices, supports_commodities) VALUES
                ('mt5', 'MetaTrader 5', 1, 1, 1, 0, 0, 1, 1),
                ('yahoo', 'Yahoo Finance', 1, 2, 1, 1, 1, 1, 1),
                ('twelvedata', 'Twelve Data', 0, 3, 1, 1, 1, 1, 1),
                ('capital', 'Capital.com', 0, 4, 1, 0, 1, 1, 1)
            ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)
        """)
    except Exception as e:
        logger.debug(f"Bootstrap: data_source_config: {e}")

    # ── Trading Strategies ──
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS trading_strategies (
                id                  INT AUTO_INCREMENT PRIMARY KEY,
                name                VARCHAR(100) NOT NULL,
                description         TEXT,
                status              ENUM('draft','active','archived') DEFAULT 'draft',
                comprehension_score INT DEFAULT 0,
                structured_rules    LONGTEXT,
                step_count          INT DEFAULT 0,
                tags                JSON,
                qdrant_vector_id    VARCHAR(100) DEFAULT NULL,
                total_trades        INT DEFAULT 0,
                total_wins          INT DEFAULT 0,
                total_pnl           DECIMAL(12,4) DEFAULT 0,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_strat_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS trade_patterns (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                pattern_name    VARCHAR(200) NOT NULL,
                pattern_type    ENUM('winning','losing','neutral') DEFAULT 'neutral',
                symbol          VARCHAR(30) DEFAULT NULL,
                market_class    VARCHAR(30) DEFAULT NULL,
                timeframe       VARCHAR(10) DEFAULT NULL,
                session         VARCHAR(20) DEFAULT NULL,
                direction       VARCHAR(10) DEFAULT NULL,
                setup_type      VARCHAR(100) DEFAULT NULL,
                description     TEXT,
                lesson          TEXT,
                evidence_ids    JSON,
                occurrences     INT DEFAULT 1,
                win_count       INT DEFAULT 0,
                loss_count      INT DEFAULT 0,
                avg_pnl         DECIMAL(12,4) DEFAULT 0,
                avg_confidence  DECIMAL(5,2) DEFAULT 0,
                win_rate        DECIMAL(5,2) DEFAULT 0,
                maturity_score  INT DEFAULT 0,
                first_seen      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_reviewed   TIMESTAMP NULL DEFAULT NULL,
                status          ENUM('emerging','maturing','mature','stale') DEFAULT 'emerging',
                INDEX idx_tp_symbol (symbol),
                INDEX idx_tp_type (pattern_type),
                INDEX idx_tp_maturity (maturity_score),
                INDEX idx_tp_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS strategy_messages (
                id                  INT AUTO_INCREMENT PRIMARY KEY,
                strategy_id         INT NOT NULL,
                role                ENUM('user','assistant') NOT NULL,
                content             TEXT NOT NULL,
                images              JSON,
                comprehension_score INT DEFAULT NULL,
                model_used          VARCHAR(100) DEFAULT NULL,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_sm_strategy (strategy_id),
                FOREIGN KEY (strategy_id) REFERENCES trading_strategies(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        db.execute("ALTER TABLE strategy_messages MODIFY COLUMN content MEDIUMTEXT NOT NULL")
    except Exception as e:
        logger.debug(f"Bootstrap: trading_strategies: {e}")

    # ── trading_accounts (live exchange accounts for order execution) ──
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS trading_accounts (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                account_id      VARCHAR(50) NOT NULL UNIQUE,
                name            VARCHAR(100) NOT NULL,
                exchange        VARCHAR(20) NOT NULL,
                account_type    ENUM('live','demo','competition') DEFAULT 'demo',
                api_key         VARCHAR(200) DEFAULT '',
                api_secret      VARCHAR(200) DEFAULT '',
                api_passphrase  VARCHAR(200) DEFAULT '',
                testnet         TINYINT(1) DEFAULT 0,
                enabled         TINYINT(1) DEFAULT 1,
                live_trading    TINYINT(1) DEFAULT 0,
                risk_per_trade_pct    DECIMAL(5,2) DEFAULT 1.00,
                leverage_mode   ENUM('max_before_sl','fixed','sliding') DEFAULT 'max_before_sl',
                leverage_value  INT DEFAULT NULL,
                margin_cap_pct  DECIMAL(5,2) DEFAULT 50.00,
                max_trades_per_symbol INT DEFAULT 1,
                max_open_trades INT DEFAULT 5,
                sl_to_be_enabled      TINYINT(1) DEFAULT 1,
                sl_to_be_trigger      VARCHAR(10) DEFAULT 'tp1',
                tp1_close_pct         INT DEFAULT 50,
                tp2_close_pct         INT DEFAULT 25,
                tp3_close_pct         INT DEFAULT 25,
                free_trade_mode       TINYINT(1) DEFAULT 0,
                leverage_safety_buffer INT DEFAULT 3,
                entry_threshold_pct   DECIMAL(5,2) DEFAULT 2.00,
                min_sl_pct            DECIMAL(5,2) DEFAULT 1.50
                    COMMENT 'Minimum SL distance from entry as %. Trades with tighter SL are blocked.',
                position_expiry_hours INT DEFAULT NULL,
                apex_enabled    TINYINT(1) DEFAULT 1,
                mentor_enabled  TINYINT(1) DEFAULT 0,
                mentor_ids      JSON DEFAULT NULL,
                mentor_modes    JSON DEFAULT NULL,
                mentor_mode     ENUM('copy','enhance','independent') DEFAULT 'copy',
                mentor_priority ENUM('mentor','apex') DEFAULT 'mentor',
                receive_dossiers TINYINT(1) DEFAULT 1,
                cached_balance  DECIMAL(15,2) DEFAULT NULL,
                cached_balance_at DATETIME DEFAULT NULL,
                cached_pnl      DECIMAL(15,4) DEFAULT NULL,
                notes           TEXT DEFAULT NULL,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: trading_accounts table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: trading_accounts: {e}")

    # ── live_trades (real exchange orders/positions linked to dossiers) ──
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS live_trades (
                id                INT AUTO_INCREMENT PRIMARY KEY,
                account_id        VARCHAR(50) NOT NULL,
                dossier_id        INT DEFAULT NULL,
                exchange          VARCHAR(20) NOT NULL,
                symbol            VARCHAR(30) NOT NULL,
                exchange_symbol   VARCHAR(50) NOT NULL,
                direction         ENUM('BUY','SELL') NOT NULL,
                order_id          VARCHAR(100) DEFAULT NULL,
                order_type        VARCHAR(20) DEFAULT 'limit',
                entry_price       DECIMAL(20,8) NOT NULL,
                position_size     DECIMAL(20,8) DEFAULT NULL,
                margin_usd        DECIMAL(12,2) DEFAULT NULL,
                leverage          INT DEFAULT NULL,
                stop_loss         DECIMAL(20,8) DEFAULT NULL,
                take_profit_1     DECIMAL(20,8) DEFAULT NULL,
                take_profit_2     DECIMAL(20,8) DEFAULT NULL,
                take_profit_3     DECIMAL(20,8) DEFAULT NULL,
                status            ENUM('pending','open','partial_closed','closed','cancelled','abandoned','expired') DEFAULT 'pending',
                actual_entry_price DECIMAL(20,8) DEFAULT NULL,
                actual_exit_price  DECIMAL(20,8) DEFAULT NULL,
                filled_at         DATETIME DEFAULT NULL,
                closed_at         DATETIME DEFAULT NULL,
                realised_pnl      DECIMAL(12,4) DEFAULT NULL,
                realised_pnl_pct  DECIMAL(8,4) DEFAULT NULL,
                unrealised_pnl    DECIMAL(12,4) DEFAULT NULL,
                unrealised_pnl_pct DECIMAL(8,4) DEFAULT NULL,
                current_price     DECIMAL(20,8) DEFAULT NULL,
                current_price_at  DATETIME DEFAULT NULL,
                tp_progress       VARCHAR(30) DEFAULT NULL,
                sl_moved_to_be    TINYINT(1) DEFAULT 0,
                trade_source      ENUM('apex','mentor','manual') DEFAULT 'apex',
                mentor_source     VARCHAR(100) DEFAULT NULL,
                close_comment     TEXT DEFAULT NULL,
                postmortem_id     INT DEFAULT NULL,
                created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_lt_account (account_id),
                INDEX idx_lt_status (status),
                INDEX idx_lt_dossier (dossier_id),
                INDEX idx_lt_symbol (symbol),
                INDEX idx_lt_exsymbol (exchange_symbol),
                UNIQUE INDEX idx_lt_dossier_account (dossier_id, account_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: live_trades table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: live_trades: {e}")

    # Widen tp_progress column if it was created at VARCHAR(10) previously
    try:
        db.execute("ALTER TABLE live_trades MODIFY COLUMN tp_progress VARCHAR(30) DEFAULT NULL")
    except Exception:
        pass

    # Ensure unique index on (dossier_id, account_id) to prevent duplicate orders
    try:
        db.execute("CREATE UNIQUE INDEX idx_lt_dossier_account ON live_trades(dossier_id, account_id)")
    except Exception:
        pass  # Already exists

    # Add 'expired' to live_trades status ENUM for existing DBs
    try:
        db.execute("""ALTER TABLE live_trades MODIFY COLUMN status
            ENUM('pending','open','partial_closed','closed','cancelled','abandoned','expired')
            DEFAULT 'pending'""")
    except Exception:
        pass

    # Add index on exchange_symbol for existing DBs
    try:
        db.execute("ALTER TABLE live_trades ADD INDEX idx_lt_exsymbol (exchange_symbol)")
    except Exception:
        pass

    # Orphan reopen query: account_id + exchange_symbol + status + updated_at (live_trade_monitor)
    try:
        db.execute(
            "CREATE INDEX idx_lt_orphan_reopen ON live_trades "
            "(account_id, exchange_symbol, status, updated_at)")
        logger.info("Bootstrap: idx_lt_orphan_reopen on live_trades")
    except Exception:
        pass

    # Paper Trade Realism: fee/funding columns for live_trades (actual_entry/exit already exist)
    paper_realism_lt_cols = [
        "ALTER TABLE live_trades ADD COLUMN entry_fee DECIMAL(12,6) DEFAULT NULL COMMENT 'USD fee on entry'",
        "ALTER TABLE live_trades ADD COLUMN exit_fee DECIMAL(12,6) DEFAULT NULL COMMENT 'USD fee on exit'",
        "ALTER TABLE live_trades ADD COLUMN accrued_funding DECIMAL(12,6) DEFAULT NULL COMMENT 'Cumulative funding rate cost'",
        "ALTER TABLE live_trades ADD COLUMN total_fees DECIMAL(12,6) DEFAULT NULL COMMENT 'entry_fee + exit_fee + accrued_funding'",
    ]
    for alter_sql in paper_realism_lt_cols:
        try:
            db.execute(alter_sql)
            logger.info("Bootstrap: added paper realism column to live_trades")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: paper realism live_trades: {e}")

    # Live price tracking columns for live_trades (so fallback API still shows prices)
    lt_price_cols = [
        "ALTER TABLE live_trades ADD COLUMN current_price DECIMAL(20,8) DEFAULT NULL COMMENT 'Latest mark/last price from exchange'",
        "ALTER TABLE live_trades ADD COLUMN current_price_at DATETIME DEFAULT NULL COMMENT 'When current_price was last updated'",
        "ALTER TABLE live_trades ADD COLUMN unrealised_pnl_pct DECIMAL(8,4) DEFAULT NULL COMMENT 'Unrealised P&L as percentage of margin'",
    ]
    for alter_sql in lt_price_cols:
        try:
            db.execute(alter_sql)
            logger.info("Bootstrap: added price tracking column to live_trades")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: live_trades price col: {e}")

    # Notes column for live_trades (used by LiveTradeMonitor to log
    # auto-abandonment reasons and manual close comments).
    try:
        db.execute("ALTER TABLE live_trades ADD COLUMN notes TEXT DEFAULT NULL")
        logger.info("Bootstrap: added notes column to live_trades")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: live_trades notes col: {e}")

    # Add safety_buffer column to trading_accounts if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN leverage_safety_buffer INT DEFAULT 3 AFTER leverage_value")
    except Exception:
        pass

    # Add entry_threshold_pct column to trading_accounts if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN entry_threshold_pct DECIMAL(5,2) DEFAULT 2.00 AFTER max_open_trades")
    except Exception:
        pass

    # Add position_expiry_hours column to trading_accounts if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN position_expiry_hours INT DEFAULT NULL AFTER entry_threshold_pct")
    except Exception:
        pass

    # ── apex_shadow_trades: ensure table exists for existing DBs ──
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS apex_shadow_trades (
            id                      INT AUTO_INCREMENT PRIMARY KEY,
            symbol                  VARCHAR(20)     NOT NULL,
            direction               ENUM('BUY','SELL') DEFAULT NULL,
            entry_price             DECIMAL(15,5)   DEFAULT NULL,
            stop_loss               DECIMAL(15,5)   DEFAULT NULL,
            take_profit_1           DECIMAL(15,5)   DEFAULT NULL,
            take_profit_2           DECIMAL(15,5)   DEFAULT NULL,
            take_profit_3           DECIMAL(15,5)   DEFAULT NULL,
            confidence_score        INT             DEFAULT NULL,
            rationale               TEXT            DEFAULT NULL,
            stage2_raw_response     MEDIUMTEXT      DEFAULT NULL,
            stage1_summary          TEXT            DEFAULT NULL,
            model_used              VARCHAR(100)    DEFAULT NULL,
            conditions_snapshot     JSON            DEFAULT NULL,
            asset_class             VARCHAR(30)     DEFAULT NULL,
            shadow_status           ENUM('pending','shadow_won','shadow_lost',
                                         'shadow_expired','no_levels') DEFAULT 'pending',
            entry_hit_at            DATETIME        DEFAULT NULL,
            exit_price              DECIMAL(15,5)   DEFAULT NULL,
            exit_at                 DATETIME        DEFAULT NULL,
            counterfactual_pnl      DECIMAL(15,2)   DEFAULT NULL,
            counterfactual_pnl_pct  DECIMAL(8,4)    DEFAULT NULL,
            exit_reason             VARCHAR(100)    DEFAULT NULL,
            lesson_text             TEXT            DEFAULT NULL,
            lesson_generated_at     DATETIME        DEFAULT NULL,
            rejected_at             DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
            evaluated_at            DATETIME        DEFAULT NULL,
            INDEX idx_shadow_symbol (symbol),
            INDEX idx_shadow_status (shadow_status),
            INDEX idx_shadow_confidence (confidence_score),
            INDEX idx_shadow_rejected (rejected_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        logger.info("Bootstrap: apex_shadow_trades table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: apex_shadow_trades: {e}")

    # ── trade_lessons: relax account_id for dossier-only lessons, add FK ──
    try:
        db.execute("ALTER TABLE trade_lessons MODIFY COLUMN account_id VARCHAR(50) DEFAULT 'jarvais'")
    except Exception:
        pass
    try:
        db.execute("""ALTER TABLE trade_lessons ADD CONSTRAINT fk_tl_dossier
                      FOREIGN KEY (dossier_id) REFERENCES trade_dossiers(id)
                      ON DELETE SET NULL""")
    except Exception:
        pass  # FK already exists or dossier_id column absent

    # ── Composite indices for trade_lessons and audit_reports ──
    for idx_sql in [
        "CREATE INDEX idx_lessons_symbol_outcome ON trade_lessons(symbol, outcome)",
        "CREATE INDEX idx_ar_type_status_created ON audit_reports(report_type, status, created_at)",
    ]:
        try:
            db.execute(idx_sql)
            logger.info(f"Bootstrap: created index {idx_sql.split()[2]}")
        except Exception:
            pass  # Index may already exist

    # ── Widen trade_dossiers.symbol to VARCHAR(50) ──
    try:
        db.execute("ALTER TABLE trade_dossiers MODIFY COLUMN symbol VARCHAR(50)")
        logger.info("Bootstrap: widened trade_dossiers.symbol to VARCHAR(50)")
    except Exception:
        pass  # May already be widened or column absent

    # ── live_trade_audit (API call log / audit trail) ──
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS live_trade_audit (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                account_id      VARCHAR(50) DEFAULT NULL,
                dossier_id      INT DEFAULT NULL,
                action          VARCHAR(50) NOT NULL,
                exchange        VARCHAR(20) DEFAULT NULL,
                symbol          VARCHAR(50) DEFAULT NULL,
                request_data    JSON DEFAULT NULL,
                response_data   JSON DEFAULT NULL,
                success         TINYINT(1) DEFAULT NULL,
                error_message   TEXT DEFAULT NULL,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_lta_account (account_id),
                INDEX idx_lta_action (action),
                INDEX idx_lta_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: live_trade_audit table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: live_trade_audit: {e}")

    # Add leverage_safety_buffer to trading_accounts if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN leverage_safety_buffer INT DEFAULT 3 AFTER free_trade_mode")
    except Exception:
        pass

    # Add mentor_modes JSON to trading_accounts if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN mentor_modes JSON DEFAULT NULL AFTER mentor_ids")
    except Exception:
        pass

    # Add entry_threshold_pct and position_expiry_hours if missing
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN entry_threshold_pct DECIMAL(5,2) DEFAULT 2.0 AFTER leverage_safety_buffer")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE trading_accounts ADD COLUMN position_expiry_hours INT DEFAULT NULL AFTER entry_threshold_pct")
    except Exception:
        pass

    # min_sl_pct: minimum SL distance from entry
    try:
        db.execute(
            "ALTER TABLE trading_accounts ADD COLUMN min_sl_pct DECIMAL(5,2) DEFAULT 1.50 "
            "COMMENT 'Minimum SL distance from entry as pct. Trades with tighter SL are blocked.' "
            "AFTER entry_threshold_pct")
    except Exception:
        pass

    # Waterfall trading: account priority for trade placement
    try:
        db.execute(
            "ALTER TABLE trading_accounts ADD COLUMN waterfall_priority INT DEFAULT 99 "
            "COMMENT 'Lower number = higher priority for waterfall trade placement'")
        logger.info("Bootstrap: added waterfall_priority column to trading_accounts")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: waterfall_priority: {e}")

    # Set default waterfall priorities for exchanges that still have the default 99
    try:
        pri = 1
        for exch in ("blofin", "bybit", "bitget"):
            rows = db.fetch_all(
                "SELECT account_id FROM trading_accounts WHERE exchange = %s "
                "AND waterfall_priority = 99 ORDER BY id", (exch,))
            for r in (rows or []):
                db.execute("UPDATE trading_accounts SET waterfall_priority = %s "
                           "WHERE account_id = %s", (pri, r["account_id"]))
                pri += 1
    except Exception:
        pass

    # Waterfall enabled config
    try:
        existing = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = 'waterfall_enabled'")
        if not existing:
            db.execute(
                "INSERT INTO system_config (config_key, config_value) "
                "VALUES ('waterfall_enabled', '1')")
            logger.info("Bootstrap: waterfall_enabled set to ON by default")
    except Exception:
        pass

    # ── live_trade_pnl_daily (daily P&L aggregation per account) ──
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS live_trade_pnl_daily (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                account_id      VARCHAR(50) NOT NULL,
                trade_date      DATE NOT NULL,
                realised_pnl    DECIMAL(12,4) DEFAULT 0,
                unrealised_pnl  DECIMAL(12,4) DEFAULT 0,
                balance         DECIMAL(15,2) DEFAULT NULL,
                trade_count     INT DEFAULT 0,
                apex_pnl        DECIMAL(12,4) DEFAULT 0,
                mentor_pnl      DECIMAL(12,4) DEFAULT 0,
                UNIQUE KEY uq_lt_account_date (account_id, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: live_trade_pnl_daily table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: live_trade_pnl_daily: {e}")

    logger.info("Bootstrap: Following system tables verified")

    # ── Phase: Multi-Duo tables ─────────────────────────────────────────
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS duos (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                duo_id          VARCHAR(50) NOT NULL UNIQUE,
                display_name    VARCHAR(100) NOT NULL,
                quant_name      VARCHAR(100) DEFAULT NULL,
                trader_name     VARCHAR(100) DEFAULT NULL,
                quant_agent_id  VARCHAR(30) DEFAULT NULL
                    COMMENT 'FK to agent_profiles.agent_id for the Quant agent',
                trader_agent_id VARCHAR(30) DEFAULT NULL
                    COMMENT 'FK to agent_profiles.agent_id for the Trader agent',
                is_active       TINYINT(1) DEFAULT 1,
                created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_duo_active (is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: duos table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: duos table: {e}")

    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS duo_config (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                duo_id          VARCHAR(50) NOT NULL,
                config_key      VARCHAR(200) NOT NULL,
                config_value    TEXT,
                description     VARCHAR(500) DEFAULT NULL,
                updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_duo_config (duo_id, config_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: duo_config table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: duo_config table: {e}")

    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS duo_accounts (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                duo_id          VARCHAR(50) NOT NULL,
                account_id      VARCHAR(100) NOT NULL,
                role            VARCHAR(50) DEFAULT 'primary',
                is_active       TINYINT(1) DEFAULT 1,
                UNIQUE KEY uq_duo_account (duo_id, account_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: duo_accounts table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: duo_accounts table: {e}")

    # Seed default 'apex' duo (idempotent)
    try:
        db.execute("""
            INSERT INTO duos (duo_id, display_name, quant_name, trader_name,
                              quant_agent_id, trader_agent_id, is_active)
            VALUES ('apex', 'Apex', 'Quant1', 'Apex', 'quant', 'apex', 1)
            ON DUPLICATE KEY UPDATE
                quant_agent_id = COALESCE(quant_agent_id, 'quant'),
                trader_agent_id = COALESCE(trader_agent_id, 'apex'),
                updated_at = NOW()
        """)
        logger.info("Bootstrap: apex duo seeded")
    except Exception as e:
        logger.debug(f"Bootstrap: apex duo seed: {e}")

    # Seed 'optimus' duo (idempotent)
    try:
        db.execute("""
            INSERT INTO duos (duo_id, display_name, quant_name, trader_name,
                              quant_agent_id, trader_agent_id, is_active)
            VALUES ('optimus', 'Optimus', 'Quant2', 'Optimus', 'quant2', 'optimus', 1)
            ON DUPLICATE KEY UPDATE
                quant_agent_id = COALESCE(quant_agent_id, 'quant2'),
                trader_agent_id = COALESCE(trader_agent_id, 'optimus'),
                updated_at = NOW()
        """)
        logger.info("Bootstrap: optimus duo seeded")
    except Exception as e:
        logger.debug(f"Bootstrap: optimus duo seed: {e}")

    # Seed 'shadow' pseudo-duo (mechanical executor, no LLM, disabled in duos table)
    try:
        db.execute("""
            INSERT INTO duos (duo_id, display_name, quant_name, trader_name,
                              quant_agent_id, trader_agent_id, is_active)
            VALUES ('shadow', 'Shadow Trader', '', '', NULL, NULL, 0)
            ON DUPLICATE KEY UPDATE
                display_name = 'Shadow Trader',
                updated_at = NOW()
        """)
        logger.info("Bootstrap: shadow pseudo-duo seeded")
    except Exception as e:
        logger.debug(f"Bootstrap: shadow pseudo-duo seed: {e}")

    # ── Shadow Trader: ALTER apex_shadow_trades for exchange actuals ──
    _shadow_alters = [
        ("duo_id",              "ALTER TABLE apex_shadow_trades ADD COLUMN duo_id VARCHAR(50) DEFAULT NULL AFTER id"),
        ("placed_on_exchange",  "ALTER TABLE apex_shadow_trades ADD COLUMN placed_on_exchange TINYINT(1) DEFAULT 0 AFTER lesson_generated_at"),
        ("exchange_order_id",   "ALTER TABLE apex_shadow_trades ADD COLUMN exchange_order_id VARCHAR(100) DEFAULT NULL AFTER placed_on_exchange"),
        ("exchange_account_id", "ALTER TABLE apex_shadow_trades ADD COLUMN exchange_account_id VARCHAR(50) DEFAULT NULL AFTER exchange_order_id"),
        ("actual_entry_price",  "ALTER TABLE apex_shadow_trades ADD COLUMN actual_entry_price DECIMAL(20,8) DEFAULT NULL AFTER exchange_account_id"),
        ("actual_exit_price",   "ALTER TABLE apex_shadow_trades ADD COLUMN actual_exit_price DECIMAL(20,8) DEFAULT NULL AFTER actual_entry_price"),
        ("actual_fees",         "ALTER TABLE apex_shadow_trades ADD COLUMN actual_fees DECIMAL(12,6) DEFAULT NULL AFTER actual_exit_price"),
        ("actual_funding",      "ALTER TABLE apex_shadow_trades ADD COLUMN actual_funding DECIMAL(12,6) DEFAULT NULL AFTER actual_fees"),
        ("actual_pnl",          "ALTER TABLE apex_shadow_trades ADD COLUMN actual_pnl DECIMAL(15,4) DEFAULT NULL AFTER actual_funding"),
        ("actual_pnl_pct",      "ALTER TABLE apex_shadow_trades ADD COLUMN actual_pnl_pct DECIMAL(8,4) DEFAULT NULL AFTER actual_pnl"),
        ("actual_entry_at",     "ALTER TABLE apex_shadow_trades ADD COLUMN actual_entry_at DATETIME DEFAULT NULL AFTER actual_pnl_pct"),
        ("actual_exit_at",      "ALTER TABLE apex_shadow_trades ADD COLUMN actual_exit_at DATETIME DEFAULT NULL AFTER actual_entry_at"),
        ("exchange_status",     "ALTER TABLE apex_shadow_trades ADD COLUMN exchange_status ENUM('not_placed','pending','open','closed','cancelled','missed') DEFAULT 'not_placed' AFTER actual_exit_at"),
        ("regime_sizing_multiplier", "ALTER TABLE apex_shadow_trades ADD COLUMN regime_sizing_multiplier DECIMAL(4,2) DEFAULT 1.00 AFTER exchange_status"),
    ]
    for col_name, alter_sql in _shadow_alters:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added {col_name} to apex_shadow_trades")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: apex_shadow_trades.{col_name}: {e}")

    # Shadow indexes on apex_shadow_trades
    for idx_name, idx_sql in [
        ("idx_shadow_exchange",  "CREATE INDEX idx_shadow_exchange ON apex_shadow_trades (exchange_status)"),
        ("idx_shadow_placed",    "CREATE INDEX idx_shadow_placed ON apex_shadow_trades (placed_on_exchange)"),
        ("idx_shadow_duo",       "CREATE INDEX idx_shadow_duo ON apex_shadow_trades (duo_id)"),
        ("idx_sq_shadow_trade_id", "CREATE INDEX idx_sq_shadow_trade_id ON shadow_queue (shadow_trade_id)"),
        ("idx_sq_symbol_dir_status", "CREATE INDEX idx_sq_symbol_dir_status ON shadow_queue (symbol, direction, queue_status)"),
    ]:
        try:
            db.execute(idx_sql)
        except Exception:
            pass

    # ── Shadow Trader: ALTER live_trades for shadow linking ──
    try:
        db.execute("ALTER TABLE live_trades ADD COLUMN shadow_trade_id INT DEFAULT NULL AFTER dossier_id")
        logger.info("Bootstrap: added shadow_trade_id to live_trades")
    except Exception as e:
        if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: live_trades.shadow_trade_id: {e}")

    try:
        db.execute("CREATE INDEX idx_lt_shadow ON live_trades (shadow_trade_id)")
    except Exception:
        pass

    # Extend trade_source ENUM to include 'shadow'
    try:
        db.execute("ALTER TABLE live_trades MODIFY COLUMN trade_source ENUM('apex','mentor','manual','shadow') DEFAULT 'apex'")
        logger.info("Bootstrap: extended trade_source ENUM with 'shadow'")
    except Exception as e:
        logger.debug(f"Bootstrap: trade_source ENUM extend: {e}")

    # ── Shadow Trader: extend shadow_queue ENUM with 'closed' ──
    try:
        db.execute("""
            ALTER TABLE shadow_queue MODIFY COLUMN queue_status
            ENUM('queued','placed','filled','expired','cancelled',
                 'replaced','skipped','missed','closed','margin_wait') DEFAULT 'queued'
        """)
        logger.info("Bootstrap: extended shadow_queue.queue_status ENUM with 'closed','margin_wait'")
    except Exception as e:
        logger.debug(f"Bootstrap: shadow_queue ENUM extend: {e}")

    # ── Shadow Trader: add TCE columns if missing ──
    for col_def in [
        "ADD COLUMN tcs_score DECIMAL(5,2) DEFAULT NULL AFTER priority_score",
        "ADD COLUMN tcs_components JSON DEFAULT NULL AFTER tcs_score",
    ]:
        try:
            db.execute(f"ALTER TABLE shadow_queue {col_def}")
            logger.info(f"Bootstrap: shadow_queue {col_def.split()[2]} column added")
        except Exception:
            pass

    # ── Shadow Trader: seed default shadow_config values ──
    _shadow_config_defaults = [
        ("enabled",                     "false"),
        ("min_confidence",              "70"),
        ("allowed_models",              '["google/gemini-3-flash-preview","x-ai/grok-4.20","manus-1"]'),
        ("direction_filter",            "both"),
        ("blacklist",                   '["ADAUSDT","ARC","GUNUSDT"]'),
        ("risk_per_trade_pct",          "0.5"),
        ("margin_cap_pct",              "33"),
        ("account_id",                  "ShadowDemo"),
        ("queue_refresh_seconds",       "10"),
        ("ui_refresh_seconds",          "5"),
        ("exchange_order_refresh_hours", "4"),
        ("max_queue_display",           "200"),
        ("cooloff_minutes",             "5"),
        ("max_orders_per_cycle",        "1"),
        ("order_expiry_hours",          "0"),
        ("shadow_exit_mode",            "tp1_full"),
        ("mentor_level_override_enabled", "true"),
        ("tce_enabled",                 "true"),
        ("tce_velocity_threshold",      "2.0"),
        ("tce_velocity_check_interval_sec", "60"),
        ("tce_velocity_enabled",        "true"),
        ("tce_atr_explosion_ratio",     "2.0"),
        ("tce_min_score_to_place",      "25"),
        ("tce_min_regime_for_buy",      "-30"),
        ("tce_max_regime_for_sell",     "30"),
        ("regime_daily_max_drawdown_pct", "5.0"),
        ("regime_injection_enabled",    "true"),
        ("regime_history_retention_days", "30"),
        ("protection_circuits_enabled", "true"),
        ("manus_intel_enabled",         "false"),
        ("manus_desk_brief_enabled",    "false"),
        ("manus_intel_max_age_minutes", "120"),
        ("manus_intel_timeout_seconds", "900"),
        ("shadow_waterfall_enabled",    "false"),
        # Smart Queue: mentor filter, R:R gate, dynamic margin, coin reputation
        ("shadow_mentor_blacklist",     '["thelordofentry","arabian.panda"]'),
        ("shadow_min_rr",              "1.5"),
        ("shadow_base_margin_pct",     "4.0"),
        ("shadow_max_margin_pct",      "8.0"),
        ("shadow_dynamic_margin_enabled", "false"),
        ("shadow_coin_memory",         "8"),
        ("shadow_coin_rep_enabled",    "true"),
    ]
    for cfg_key, cfg_val in _shadow_config_defaults:
        try:
            db.execute(
                "INSERT IGNORE INTO shadow_config (config_key, config_value) VALUES (%s, %s)",
                (cfg_key, cfg_val))
        except Exception:
            pass
    logger.info("Bootstrap: shadow_config defaults seeded")

    # ── Shadow Trader: seed trading account from .env ──
    try:
        import os
        s_key = os.getenv("BYBIT_DEMO_SHADDOW_API_KEY", "")
        s_secret = os.getenv("BYBIT_DEMO_SHADDOW_API_SECRET", "")
        if s_key and s_secret:
            existing = db.fetch_one(
                "SELECT account_id FROM trading_accounts WHERE account_id = 'ShadowDemo'")
            if not existing:
                db.execute("""
                    INSERT INTO trading_accounts
                    (account_id, name, exchange, account_type,
                     api_key, api_secret,
                     enabled, live_trading,
                     risk_per_trade_pct, margin_cap_pct, max_open_trades,
                     duo_allowed, waterfall_priority)
                    VALUES ('ShadowDemo', 'Shadow Demo (Bybit)', 'bybit', 'demo',
                            %s, %s,
                            1, 1,
                            0.50, 33.00, 66,
                            '["shadow"]', 99)
                """, (s_key, s_secret))
                logger.info("Bootstrap: ShadowDemo trading account seeded from .env")
            else:
                logger.info("Bootstrap: ShadowDemo account already exists")
    except Exception as e:
        logger.debug(f"Bootstrap: ShadowDemo account seed: {e}")

    # Add duo_id columns to existing tables
    _duo_id_alters = [
        ("trade_dossiers",    "ALTER TABLE trade_dossiers ADD COLUMN duo_id VARCHAR(50) DEFAULT 'apex' AFTER id"),
        ("live_trades",       "ALTER TABLE live_trades ADD COLUMN duo_id VARCHAR(50) DEFAULT 'apex' AFTER id"),
        ("ai_api_log",        "ALTER TABLE ai_api_log ADD COLUMN duo_id VARCHAR(50) DEFAULT NULL AFTER account_id"),
        ("live_trade_pnl_daily", "ALTER TABLE live_trade_pnl_daily ADD COLUMN duo_id VARCHAR(50) DEFAULT 'apex' AFTER account_id"),
        ("live_trade_audit",  "ALTER TABLE live_trade_audit ADD COLUMN duo_id VARCHAR(50) DEFAULT NULL AFTER account_id"),
        ("trading_accounts",  "ALTER TABLE trading_accounts ADD COLUMN duo_allowed JSON DEFAULT NULL COMMENT 'List of duo_ids allowed to trade; NULL=all'"),
    ]
    for table_name, alter_sql in _duo_id_alters:
        try:
            db.execute(alter_sql)
            logger.info(f"Bootstrap: added duo column to {table_name}")
        except Exception as e:
            if 'Duplicate column' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: duo column on {table_name}: {e}")

    # Indexes on duo_id for query performance
    _duo_indexes = [
        ("idx_td_duo_id",  "CREATE INDEX idx_td_duo_id ON trade_dossiers (duo_id)"),
        ("idx_lt_duo_id",  "CREATE INDEX idx_lt_duo_id ON live_trades (duo_id)"),
        ("idx_api_duo_id", "CREATE INDEX idx_api_duo_id ON ai_api_log (duo_id)"),
    ]
    for idx_name, idx_sql in _duo_indexes:
        try:
            db.execute(idx_sql)
            logger.info(f"Bootstrap: created index {idx_name}")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: index {idx_name}: {e}")

    # Performance indexes for heavy query patterns (Phase 1b optimization)
    _perf_indexes = [
        ("idx_api_duo_ts",      "CREATE INDEX idx_api_duo_ts ON ai_api_log (duo_id, timestamp)"),
        ("idx_api_ctx_ts",      "CREATE INDEX idx_api_ctx_ts ON ai_api_log (context, timestamp)"),
        ("idx_news_published",  "CREATE INDEX idx_news_published ON news_items (published_at)"),
        ("idx_td_status_upd",   "CREATE INDEX idx_td_status_upd ON trade_dossiers (status, updated_at)"),
    ]
    for idx_name, idx_sql in _perf_indexes:
        try:
            db.execute(idx_sql)
            logger.info(f"Bootstrap: created performance index {idx_name}")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: perf index {idx_name}: {e}")

    try:
        db.execute(
            "ALTER TABLE ai_api_log ADD COLUMN actual_cost_usd "
            "DECIMAL(12,6) DEFAULT NULL "
            "COMMENT 'Real cost from OpenRouter usage.total_cost' "
            "AFTER cost_usd"
        )
        logger.info("Bootstrap: added actual_cost_usd column to ai_api_log")
    except Exception as e:
        if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: actual_cost_usd: {e}")

    for _col, _sql in [
        ("duo_id", "ALTER TABLE audit_reports ADD COLUMN duo_id VARCHAR(50) DEFAULT NULL AFTER total_tokens"),
        ("cross_duo_analysis", "ALTER TABLE audit_reports ADD COLUMN cross_duo_analysis TEXT DEFAULT NULL AFTER duo_id"),
    ]:
        try:
            db.execute(_sql)
            logger.info(f"Bootstrap: added {_col} to audit_reports")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: audit_reports.{_col}: {e}")

    # duo_id on audit_recommendations (so Ledger recs target the right duo's prompts)
    try:
        db.execute(
            "ALTER TABLE audit_recommendations ADD COLUMN duo_id VARCHAR(50) DEFAULT NULL "
            "COMMENT 'Duo this recommendation applies to (from audit_reports)' "
            "AFTER audit_report_id"
        )
        logger.info("Bootstrap: added duo_id to audit_recommendations")
    except Exception as e:
        if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: audit_recommendations.duo_id: {e}")

    # agent_role column on agent_profiles (for dynamic duo assignment)
    try:
        db.execute(
            "ALTER TABLE agent_profiles ADD COLUMN agent_role VARCHAR(30) DEFAULT NULL "
            "COMMENT 'Functional role: quant, trader, scout, tracker, auditor, data_scientist, executive, support' "
            "AFTER prompt_role"
        )
        logger.info("Bootstrap: added agent_role to agent_profiles")
    except Exception as e:
        if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: agent_profiles.agent_role: {e}")

    # quant_agent_id, trader_agent_id columns on duos (FK to agent_profiles)
    for _col, _sql in [
        ("quant_agent_id",
         "ALTER TABLE duos ADD COLUMN quant_agent_id VARCHAR(30) DEFAULT NULL "
         "COMMENT 'FK to agent_profiles.agent_id for the Quant agent' AFTER trader_name"),
        ("trader_agent_id",
         "ALTER TABLE duos ADD COLUMN trader_agent_id VARCHAR(30) DEFAULT NULL "
         "COMMENT 'FK to agent_profiles.agent_id for the Trader agent' AFTER quant_agent_id"),
    ]:
        try:
            db.execute(_sql)
            logger.info(f"Bootstrap: added {_col} to duos")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: duos.{_col}: {e}")

    # Backfill existing apex duo with agent FK refs
    try:
        db.execute(
            "UPDATE duos SET quant_agent_id = 'quant', trader_agent_id = 'apex' "
            "WHERE duo_id = 'apex' AND quant_agent_id IS NULL"
        )
    except Exception:
        pass

    logger.info("Bootstrap: Multi-Duo tables and columns verified")
    # ── End Multi-Duo tables ────────────────────────────────────────────

    # ── Symbol Intelligence table (priority queue + verdict cache) ────
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS symbol_intel (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                symbol          VARCHAR(50) NOT NULL,
                duo_id          VARCHAR(50) NOT NULL,
                last_verdict    ENUM('no_setup','weak_setup','moderate_setup',
                                     'strong_setup','trade_proposed','gated')
                                DEFAULT 'no_setup',
                last_quant_score    SMALLINT        DEFAULT NULL
                    COMMENT 'Stage 1 OVERALL_SETUP_SCORE 0-100',
                last_confidence     SMALLINT        DEFAULT NULL
                    COMMENT 'Stage 2 confidence_score 0-100',
                last_decision       VARCHAR(30)     DEFAULT NULL
                    COMMENT 'Stage 2 trade_decision string',
                last_analyzed_at    DATETIME        DEFAULT NULL,
                price_at_analysis   DECIMAL(20,8)   DEFAULT NULL,
                volume_at_analysis  BIGINT          DEFAULT NULL,
                total_wins          INT             DEFAULT 0,
                total_losses        INT             DEFAULT 0,
                total_expired       INT             DEFAULT 0,
                total_builds        INT             DEFAULT 0
                    COMMENT 'Lifetime dossier builds for this symbol+duo',
                realized_pnl_sum    DECIMAL(15,4)   DEFAULT 0,
                avg_win_pct         DECIMAL(8,4)    DEFAULT NULL,
                avg_loss_pct        DECIMAL(8,4)    DEFAULT NULL,
                best_source         VARCHAR(30)     DEFAULT NULL
                    COMMENT 'Source that last triggered a build (watchlist/alpha/mentor)',
                priority_rank       DECIMAL(10,4)   DEFAULT 0
                    COMMENT 'Computed composite priority score',
                consecutive_skips   INT             DEFAULT 0,
                skip_reason         VARCHAR(200)    DEFAULT NULL,
                lesson_count        INT             DEFAULT 0
                    COMMENT 'Number of trade_lessons for this symbol',
                created_at          DATETIME        DEFAULT CURRENT_TIMESTAMP,
                updated_at          DATETIME        DEFAULT CURRENT_TIMESTAMP
                                    ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_symbol_duo (symbol, duo_id),
                INDEX idx_si_analyzed (duo_id, last_analyzed_at),
                INDEX idx_si_verdict (duo_id, last_verdict)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: symbol_intel table verified")
    except Exception as e:
        logger.debug(f"Bootstrap: symbol_intel: {e}")

    # ── End Symbol Intelligence table ────────────────────────────────────

    # ── Desk Curator columns on symbol_intel ──────────────────────────
    for col_sql in [
        "ALTER TABLE symbol_intel ADD COLUMN chart_tradeability_score DECIMAL(5,1) DEFAULT NULL "
        "COMMENT 'CTS 0-100 from DataScientist math layer' AFTER priority_rank",
        "ALTER TABLE symbol_intel ADD COLUMN cts_grade CHAR(2) DEFAULT NULL "
        "COMMENT 'CTS letter grade A+ through F' AFTER chart_tradeability_score",
        "ALTER TABLE symbol_intel ADD COLUMN avg_chart_quality DECIMAL(5,2) DEFAULT NULL "
        "COMMENT 'Running avg of Stage 2 chart_quality feedback 1-10' AFTER cts_grade",
        "ALTER TABLE symbol_intel ADD COLUMN chart_quality_samples INT DEFAULT 0 "
        "COMMENT 'Count of chart_quality ratings received' AFTER avg_chart_quality",
        "ALTER TABLE symbol_intel ADD COLUMN desk_brief_rank INT DEFAULT NULL "
        "COMMENT 'Rank from last Desk Brief LLM call (1=best)' AFTER chart_quality_samples",
        "ALTER TABLE symbol_intel ADD COLUMN last_desk_brief_at DATETIME DEFAULT NULL "
        "COMMENT 'When this symbol was last included in a Desk Brief' AFTER desk_brief_rank",
    ]:
        try:
            db.execute(col_sql)
            logger.info(f"Bootstrap: symbol_intel column added")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: symbol_intel col: {e}")

    # Grok Desk Brief trade setup columns — stores the Head Quant's
    # independent trade assessment (direction, entry, SL, TP1-3, confidence).
    # Tracked as shadow predictions to measure Grok's accuracy over time.
    grok_setup_cols = [
        "ALTER TABLE symbol_intel ADD COLUMN grok_direction VARCHAR(4) DEFAULT NULL "
        "COMMENT 'BUY or SELL from Grok Desk Brief' AFTER last_desk_brief_at",
        "ALTER TABLE symbol_intel ADD COLUMN grok_confidence INT DEFAULT NULL "
        "COMMENT 'Grok confidence 0-100' AFTER grok_direction",
        "ALTER TABLE symbol_intel ADD COLUMN grok_entry DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Grok suggested entry price' AFTER grok_confidence",
        "ALTER TABLE symbol_intel ADD COLUMN grok_stop_loss DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Grok suggested SL' AFTER grok_entry",
        "ALTER TABLE symbol_intel ADD COLUMN grok_tp1 DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Grok TP1' AFTER grok_stop_loss",
        "ALTER TABLE symbol_intel ADD COLUMN grok_tp2 DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Grok TP2' AFTER grok_tp1",
        "ALTER TABLE symbol_intel ADD COLUMN grok_tp3 DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Grok TP3' AFTER grok_tp2",
        "ALTER TABLE symbol_intel ADD COLUMN grok_reasoning TEXT DEFAULT NULL "
        "COMMENT 'Grok reasoning summary for the trade' AFTER grok_tp3",
        "ALTER TABLE symbol_intel ADD COLUMN grok_setup_at DATETIME DEFAULT NULL "
        "COMMENT 'Timestamp of Grok setup' AFTER grok_reasoning",
        "ALTER TABLE symbol_intel ADD COLUMN grok_setup_outcome VARCHAR(20) DEFAULT NULL "
        "COMMENT 'pending/tp1_hit/tp2_hit/tp3_hit/sl_hit/expired' AFTER grok_setup_at",
        "ALTER TABLE symbol_intel ADD COLUMN grok_setup_resolved_at DATETIME DEFAULT NULL "
        "COMMENT 'When the Grok prediction resolved' AFTER grok_setup_outcome",
    ]
    for col_sql in grok_setup_cols:
        try:
            db.execute(col_sql)
            logger.info("Bootstrap: symbol_intel Grok setup column added")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: symbol_intel grok col: {e}")

    # ── Seed desk_curator prompt in prompt_versions ─────────────────
    try:
        db.execute(
            """INSERT IGNORE INTO prompt_versions
               (role, category, version, prompt_name, description, system_prompt,
                is_active, activated_at, changed_by)
               VALUES ('desk_curator', 'scout', 1, 'Desk Curator',
                'Head Quant Trader persona that ranks top tradeable charts in a single batch LLM call',
                %s, 1, NOW(), 'system_bootstrap')""",
            (
                "You are the Head Quant Trader at JarvAIs, a professional crypto trading firm.\n"
                "Your job is to review the Desk Brief — a batch of candidate trading symbols — "
                "and rank them by tradeability.\n\n"
                "TRADEABILITY means:\n"
                "1. Chart structure is clean: respects Fibonacci levels, EMAs are aligned, "
                "swing highs/lows are orderly, BOS/CHoCH events are present\n"
                "2. Confluence: multiple independent signals agree (RSI, MACD, Bollinger, volume)\n"
                "3. Historical reliability: coins that have previously respected levels and "
                "produced winning trades are preferred\n"
                "4. Volume confirms the move: relative volume is healthy, not dying\n"
                "5. Risk/reward potential: clear entry, stop loss, and take profit zones exist\n\n"
                "You are NOT looking for:\n"
                "- Coins that are just volatile (chaos is not tradeability)\n"
                "- Coins with no structure (random chop)\n"
                "- Low-volume coins where slippage is a risk\n\n"
                "Respond with ONLY a JSON array of symbols in your ranked order, "
                "best first. Example: [\"BTCUSDT\", \"ETHUSDT\", \"SOLUSDT\"]\n"
                "No explanations, no markdown, just the JSON array.",
            ))
        logger.info("Bootstrap: desk_curator prompt seeded")
    except Exception as e:
        if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: desk_curator prompt: {e}")

    # ── Manus setup + market intel columns on symbol_intel ──────────
    manus_setup_cols = [
        "ALTER TABLE symbol_intel ADD COLUMN manus_direction VARCHAR(4) DEFAULT NULL "
        "COMMENT 'BUY or SELL from Manus Desk Brief' AFTER grok_setup_resolved_at",
        "ALTER TABLE symbol_intel ADD COLUMN manus_confidence INT DEFAULT NULL "
        "COMMENT 'Manus confidence 0-100' AFTER manus_direction",
        "ALTER TABLE symbol_intel ADD COLUMN manus_entry DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Manus suggested entry price' AFTER manus_confidence",
        "ALTER TABLE symbol_intel ADD COLUMN manus_stop_loss DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Manus suggested SL' AFTER manus_entry",
        "ALTER TABLE symbol_intel ADD COLUMN manus_tp1 DECIMAL(20,8) DEFAULT NULL "
        "COMMENT 'Manus TP1' AFTER manus_stop_loss",
        "ALTER TABLE symbol_intel ADD COLUMN manus_reasoning TEXT DEFAULT NULL "
        "COMMENT 'Manus reasoning summary' AFTER manus_tp1",
        "ALTER TABLE symbol_intel ADD COLUMN manus_regime_alignment VARCHAR(10) DEFAULT NULL "
        "COMMENT 'ALIGNED/COUNTER/NEUTRAL from Manus' AFTER manus_reasoning",
        "ALTER TABLE symbol_intel ADD COLUMN manus_risk_flags TEXT DEFAULT NULL "
        "COMMENT 'JSON array of risk flags from Manus' AFTER manus_regime_alignment",
        "ALTER TABLE symbol_intel ADD COLUMN manus_setup_at DATETIME DEFAULT NULL "
        "COMMENT 'Timestamp of Manus review' AFTER manus_risk_flags",
        "ALTER TABLE symbol_intel ADD COLUMN manus_rsi_1h FLOAT DEFAULT NULL "
        "COMMENT 'Manus-extracted RSI 1h from CoinMarketCap' AFTER manus_setup_at",
        "ALTER TABLE symbol_intel ADD COLUMN manus_rsi_4h FLOAT DEFAULT NULL "
        "COMMENT 'Manus-extracted RSI 4h' AFTER manus_rsi_1h",
        "ALTER TABLE symbol_intel ADD COLUMN manus_rsi_24h FLOAT DEFAULT NULL "
        "COMMENT 'Manus-extracted RSI 24h' AFTER manus_rsi_4h",
        "ALTER TABLE symbol_intel ADD COLUMN manus_funding_rate_binance FLOAT DEFAULT NULL "
        "COMMENT 'Funding rate from Binance via Manus' AFTER manus_rsi_24h",
        "ALTER TABLE symbol_intel ADD COLUMN manus_funding_rate_bybit FLOAT DEFAULT NULL "
        "COMMENT 'Funding rate from Bybit via Manus' AFTER manus_funding_rate_binance",
        "ALTER TABLE symbol_intel ADD COLUMN manus_oi_change_24h FLOAT DEFAULT NULL "
        "COMMENT '24h OI change pct from Coinglass via Manus' AFTER manus_funding_rate_bybit",
        "ALTER TABLE symbol_intel ADD COLUMN manus_last_updated DATETIME DEFAULT NULL "
        "COMMENT 'When Manus data was last updated' AFTER manus_oi_change_24h",
        "ALTER TABLE symbol_intel ADD COLUMN llm_consensus TINYINT(1) DEFAULT 0 "
        "COMMENT 'True if Grok and Manus agree on direction' AFTER manus_last_updated",
    ]
    for col_sql in manus_setup_cols:
        try:
            db.execute(col_sql)
            logger.info("Bootstrap: symbol_intel Manus column added")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: symbol_intel manus col: {e}")

    # ── market_regime_intel table (Manus web extractions) ─────────
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS market_regime_intel (
                id                          INT AUTO_INCREMENT PRIMARY KEY,
                timestamp                   DATETIME NOT NULL,
                average_crypto_rsi          FLOAT DEFAULT NULL,
                percent_overbought          FLOAT DEFAULT NULL,
                percent_oversold            FLOAT DEFAULT NULL,
                btc_oi_weighted_funding_rate FLOAT DEFAULT NULL,
                hyperliquid_long_traders     INT DEFAULT NULL,
                hyperliquid_short_traders    INT DEFAULT NULL,
                hyperliquid_ls_ratio        FLOAT DEFAULT NULL,
                btc_liquidation_cluster_bias VARCHAR(20) DEFAULT NULL,
                manus_regime_assessment     TEXT DEFAULT NULL,
                INDEX idx_mri_timestamp (timestamp)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        logger.info("Bootstrap: market_regime_intel table ensured")
    except Exception as e:
        logger.debug(f"Bootstrap: market_regime_intel table: {e}")

    # ── trade_source column on shadow_queue ─────────────────────
    # (live_trades already has trade_source from Phase 1)
    try:
        db.execute(
            "ALTER TABLE shadow_queue ADD COLUMN trade_source VARCHAR(20) DEFAULT 'apex' "
            "COMMENT 'Origin: apex, grok, manus' AFTER queue_status"
        )
        logger.info("Bootstrap: shadow_queue.trade_source column added")
    except Exception as e:
        if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
            logger.debug(f"Bootstrap: shadow_queue.trade_source: {e}")

    # ── shadow_queue: add account_id + exchange_name for waterfall tracking ──
    for _sq_col, _sq_def in [
        ("account_id", "ALTER TABLE shadow_queue ADD COLUMN account_id VARCHAR(100) DEFAULT NULL "
         "COMMENT 'Target waterfall account for this queued trade' AFTER exchange_symbol"),
        ("exchange_name", "ALTER TABLE shadow_queue ADD COLUMN exchange_name VARCHAR(20) DEFAULT NULL "
         "COMMENT 'Exchange name (bybit/blofin/bitget) for display' AFTER account_id"),
        ("regime_sizing_multiplier", "ALTER TABLE shadow_queue ADD COLUMN regime_sizing_multiplier DECIMAL(4,2) DEFAULT 1.00 "
         "COMMENT 'Position size multiplier from market regime (0.25-1.5x)' AFTER exchange_name"),
    ]:
        try:
            db.execute(_sq_def)
            logger.info(f"Bootstrap: shadow_queue.{_sq_col} column added")
        except Exception as e:
            if 'Duplicate' not in str(e) and 'duplicate' not in str(e).lower():
                logger.debug(f"Bootstrap: shadow_queue.{_sq_col}: {e}")

    try:
        db.execute("CREATE INDEX idx_sq_account ON shadow_queue (account_id)")
        logger.info("Bootstrap: shadow_queue.account_id index created")
    except Exception:
        pass

    # ── End Manus schema additions ────────────────────────────────

    # 16. Seed agent_profiles (new agents get full soul; existing keep DB souls)
    try:
        from db.seed_agents import seed_agent_profiles, migrate_agent_souls
        count = seed_agent_profiles(db)
        logger.info(f"Bootstrap: agent_profiles processed {count} agents "
                     "(new agents inserted, existing souls preserved)")
        migrated = migrate_agent_souls(db)
        if migrated:
            logger.info(f"Bootstrap: {migrated} agent soul migrations applied")
    except ImportError:
        logger.info("Bootstrap: db/seed_agents.py not found yet — agent seeding skipped")
    except Exception as e:
        logger.warning(f"Bootstrap: agent_profiles seed error: {e}")

    # 17. Seed scheduled_tasks (cron jobs for the agentic company)
    try:
        db.execute(SEED_SCHEDULED_TASKS)
        logger.info("Bootstrap: scheduled_tasks seeded")
    except Exception as e:
        if 'Duplicate' not in str(e):
            logger.debug(f"Bootstrap: scheduled_tasks seed: {e}")

    # 18. Seed company_policies
    try:
        db.execute(SEED_COMPANY_POLICIES)
        logger.info("Bootstrap: company_policies seeded")
    except Exception as e:
        if 'Duplicate' not in str(e):
            logger.debug(f"Bootstrap: company_policies seed: {e}")

    # 19. Seed department_charters
    try:
        db.execute(SEED_DEPARTMENT_CHARTERS)
        logger.info("Bootstrap: department_charters seeded")
    except Exception as e:
        if 'Duplicate' not in str(e):
            logger.debug(f"Bootstrap: department_charters seed: {e}")

    # 20. Restructure departments and switch to paid models (idempotent UPDATE statements)
    try:
        for sql in RESTRUCTURE_DEPARTMENTS_SQL:
            db.execute(sql)
        logger.info("Bootstrap: department restructure + paid model switch applied")
    except Exception as e:
        logger.warning(f"Bootstrap: department restructure: {e}")

    # 21. Seed/update dossier prompts in system_config (DB-driven, UI-editable)
    try:
        from db.seed_dossier_prompts import seed_dossier_prompts
        seed_dossier_prompts(db)
    except ImportError:
        logger.debug("Bootstrap: seed_dossier_prompts.py not found, skipping")
    except Exception as e:
        logger.warning(f"Bootstrap: dossier prompt seed: {e}")


def _sync_collector_sources_to_managed(db):
    """
    Sync hardcoded RSS feeds and TradingView symbols from collectors INTO managed_sources
    so the UI shows what the collectors are actually using.
    """
    import json

    # ── RSS Feeds from NewsCollector ──
    RSS_FEEDS = [
        {"name": "CNBC World Markets", "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html", "category": "news"},
        {"name": "CNBC Economy", "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html", "category": "news"},
        {"name": "CNBC Finance", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "category": "news"},
        {"name": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex", "category": "news"},
        {"name": "Google News: Forex & Gold", "url": "https://news.google.com/rss/search?q=forex+gold+XAUUSD+trading&hl=en-US&gl=US&ceid=US:en", "category": "news"},
        {"name": "Google News: Fed & Rates", "url": "https://news.google.com/rss/search?q=federal+reserve+interest+rate+inflation&hl=en-US&gl=US&ceid=US:en", "category": "news"},
        {"name": "Google News: Gold Price", "url": "https://news.google.com/rss/search?q=gold+price+precious+metals&hl=en-US&gl=US&ceid=US:en", "category": "news"},
        {"name": "Forex Factory Calendar", "url": "https://www.forexfactory.com/calendar.php?do=getCalendarEvents", "category": "news"},
        {"name": "FXStreet News", "url": "https://www.fxstreet.com/rss/news", "category": "news"},
        {"name": "Investing.com News", "url": "https://www.investing.com/rss/news.rss", "category": "news"},
        {"name": "MQL5 News", "url": "https://www.mql5.com/en/news/rss", "category": "news"},
        {"name": "MyFXBook Calendar", "url": "https://www.myfxbook.com/rss/forex-economic-calendar-events", "category": "news"},
    ]

    rss_added = 0
    for feed in RSS_FEEDS:
        try:
            existing = db.fetch_one(
                "SELECT id FROM managed_sources WHERE source_type = 'rss' AND name = %s",
                (feed["name"],)
            )
            if not existing:
                config = json.dumps({"url": feed["url"], "type": "rss"})
                db.execute(
                    """INSERT INTO managed_sources (source_type, name, category, config, enabled, trigger_mode)
                       VALUES ('rss', %s, %s, %s, 1, 'confidence')""",
                    (feed["name"], feed["category"], config)
                )
                rss_added += 1
        except Exception as e:
            if "Duplicate" not in str(e):
                logger.debug(f"Bootstrap: RSS feed sync error for {feed['name']}: {e}")

    if rss_added > 0:
        logger.info(f"Bootstrap: Synced {rss_added} RSS feeds to managed_sources")

    # ── TradingView Symbols (core/default — user manages extras via Alpha Sources UI) ──
    TV_SYMBOLS = [
        {"symbol": "XAUUSD", "tv_id": "CAPITALCOM:GOLD", "aliases": ["GOLD", "XAUUSD", "XAU"]},
        {"symbol": "BTCUSD", "tv_id": "CAPITALCOM:BTCUSD", "aliases": ["BITCOIN", "BTCUSD", "BTC"]},
    ]

    tv_added = 0
    for sym in TV_SYMBOLS:
        try:
            existing = db.fetch_one(
                "SELECT id FROM managed_sources WHERE source_type = 'tradingview' AND name = %s",
                (sym["symbol"],)
            )
            if not existing:
                config = json.dumps({"tv_id": sym["tv_id"], "aliases": sym["aliases"]})
                db.execute(
                    """INSERT INTO managed_sources (source_type, name, category, config, enabled, trigger_mode)
                       VALUES ('tradingview', %s, 'tv_idea', %s, 1, 'confidence')""",
                    (sym["symbol"], config)
                )
                tv_added += 1
        except Exception as e:
            if "Duplicate" not in str(e):
                logger.debug(f"Bootstrap: TradingView symbol sync error for {sym['symbol']}: {e}")

    if tv_added > 0:
        logger.info(f"Bootstrap: Synced {tv_added} TradingView symbols to managed_sources")


def _seed_missing_source_roles(db):
    """Ensure new source roles (twitter, youtube) exist in prompt_versions.

    Uses INSERT IGNORE so it's safe to run on every startup — only adds rows for roles
    that don't already exist. source_tradingview_minds was consolidated into source_tradingview.
    """
    NEW_SOURCE_ROLES = ["source_twitter", "source_youtube"]

    # Deactivate the consolidated source_tradingview_minds (merged into source_tradingview)
    try:
        db.execute(
            "UPDATE prompt_versions SET is_active = 0 WHERE role = 'source_tradingview_minds' AND is_active = 1"
        )
    except Exception:
        pass
    try:
        from db.seed_prompts import PROMPTS
        added = 0
        for role_key in NEW_SOURCE_ROLES:
            if role_key not in PROMPTS:
                logger.warning(f"Bootstrap: {role_key} not found in seed_prompts.py — skipping")
                continue
            config = PROMPTS[role_key]
            try:
                db.execute(
                    """INSERT IGNORE INTO prompt_versions
                       (role, category, version, prompt_name, description, system_prompt,
                        is_active, activated_at, changed_by)
                       VALUES (%s, %s, 1, %s, %s, %s, 1, NOW(), 'system_bootstrap')""",
                    (role_key, config["category"], config["name"],
                     config["description"], config["prompt"])
                )
                added += 1
            except Exception as e:
                logger.warning(f"Bootstrap: seed source role {role_key} error: {e}")
        if added > 0:
            logger.info(f"Bootstrap: Ensured {added} new source roles in prompt_versions")
    except Exception as e:
        logger.warning(f"Bootstrap: _seed_missing_source_roles error: {e}")


def _seed_alpha_analysis_roles(db):
    """Seed alpha analysis prompts into prompt_versions with the FULL prompt text.
    Imports the prompt constants from alpha_analysis.py. These are the initial seeds —
    once in the DB, they're editable via Prompt Engineer and the DB version takes priority."""
    try:
        from services.alpha_analysis import (
            PROMPT_CHART_ANALYSIS, PROMPT_TEXT_ANALYSIS,
            PROMPT_VOICE_ANALYSIS, PROMPT_VIDEO_ANALYSIS, PROMPT_ASSIMILATION
        )
    except ImportError as e:
        logger.warning(f"Bootstrap: Could not import alpha prompts for seeding: {e}")
        return

    ALPHA_ROLES = [
        ("alpha_chart_analysis", "alpha", "Alpha Chart Analysis",
         "Comprehensive chart/image analysis — extracts human trades AND generates JarvAIs independent trades with entry/SL/TP1-6. Used for all visual media.",
         PROMPT_CHART_ANALYSIS),
        ("alpha_text_analysis", "alpha", "Alpha Text Analysis",
         "Extracts human trade setups from text messages. Extraction only — no AI trade generation (text alone doesn't warrant independent AI opinion).",
         PROMPT_TEXT_ANALYSIS),
        ("alpha_voice_analysis", "alpha", "Alpha Voice Analysis",
         "Extracts human trade setups from voice/audio transcripts with tone and emphasis detection. Extraction only — no AI trade generation.",
         PROMPT_VOICE_ANALYSIS),
        ("alpha_video_analysis", "alpha", "Alpha Video Analysis",
         "Extracts human trade setups from video transcripts with timestamp correlation to chart screenshots. Visual frames trigger chart analysis.",
         PROMPT_VIDEO_ANALYSIS),
        ("alpha_assimilation", "alpha", "Alpha Assimilation",
         "Merges all per-media extractions into the unified v2.0 dual-analysis dossier. Model is auto-selected as best available vision model (override via model dropdown).",
         PROMPT_ASSIMILATION),
    ]
    try:
        added = 0
        for role, category, name, description, system_prompt in ALPHA_ROLES:
            try:
                existing = db.fetch_one(
                    "SELECT id FROM prompt_versions WHERE role = %s LIMIT 1", (role,)
                )
                if not existing:
                    db.execute(
                        """INSERT INTO prompt_versions
                           (role, category, version, prompt_name, description, system_prompt,
                            is_active, activated_at, changed_by)
                           VALUES (%s, %s, 1, %s, %s, %s, 1, NOW(), 'system_bootstrap')""",
                        (role, category, name, description, system_prompt)
                    )
                    added += 1
                    logger.info(f"Bootstrap: Seeded alpha prompt '{role}' ({len(system_prompt)} chars)")
            except Exception as e:
                logger.debug(f"Bootstrap: alpha role {role}: {e}")
        if added:
            logger.info(f"Bootstrap: Registered {added} alpha analysis prompt roles in prompt_versions")
    except Exception as e:
        logger.warning(f"Bootstrap: _seed_alpha_analysis_roles error: {e}")


def _seed_prompts_from_sql(db):
    """Seed prompt_versions from the db/seed_data.sql file."""
    seed_file = Path(__file__).parent / "seed_data.sql"
    if not seed_file.exists():
        logger.warning(f"Bootstrap: seed_data.sql not found at {seed_file}, trying seed_prompts.py fallback")
        _seed_prompts_fallback(db)
        return

    logger.info(f"Bootstrap: Seeding prompt_versions from {seed_file}")
    with open(seed_file, "r", encoding="utf-8-sig") as f:
        content = f.read()

    # Find each INSERT statement by regex (can't split on ; because prompts contain semicolons)
    import re
    statements = re.findall(r'INSERT\s+IGNORE\s+INTO\s+.*?(?=INSERT\s+IGNORE\s+INTO|\Z)', content, re.DOTALL | re.IGNORECASE)

    executed = 0
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        # Remove trailing semicolons and whitespace
        stmt = stmt.rstrip().rstrip(';').rstrip()
        if not stmt:
            continue
        try:
            db.execute(stmt)
            executed += 1
            logger.info(f"Bootstrap: seed statement executed ({len(stmt)} chars)")
        except Exception as e:
            logger.warning(f"Bootstrap: seed statement error ({len(stmt)} chars): {e}")

    logger.info(f"Bootstrap: Executed {executed} seed statements from seed_data.sql")


def _seed_prompts_fallback(db):
    """Fallback: seed prompts using seed_prompts.py if SQL file is missing."""
    try:
        from db.seed_prompts import PROMPTS
        for role_key, config in PROMPTS.items():
            try:
                db.execute(
                    """INSERT IGNORE INTO prompt_versions
                       (role, category, version, prompt_name, description, system_prompt,
                        is_active, activated_at, changed_by)
                       VALUES (%s, %s, 1, %s, %s, %s, 1, NOW(), 'human_owner')""",
                    (role_key, config["category"], config["name"],
                     config["description"], config["prompt"])
                )
            except Exception as e:
                logger.warning(f"Bootstrap: seed prompt {role_key} error: {e}")
        logger.info(f"Bootstrap: Seeded {len(PROMPTS)} prompts from seed_prompts.py")

        # 15. Ensure alpha_analysis_enabled config exists
        try:
            existing = db.fetch_one(
                "SELECT config_value FROM config WHERE config_key = 'alpha_analysis_enabled'"
            )
            if not existing:
                db.execute(
                    """INSERT INTO config (account_id, config_key, config_value, updated_by)
                       VALUES ('system', 'alpha_analysis_enabled', 'true', 'bootstrap')"""
                )
                logger.info("Bootstrap: Added alpha_analysis_enabled config")
        except Exception as e:
            logger.debug(f"Bootstrap: alpha_analysis_enabled config check: {e}")
    except Exception as e:
        logger.error(f"Bootstrap: seed_prompts.py fallback failed: {e}")

    # ── Auto-seed trading accounts from .env keys if none exist ──
    try:
        import os
        acct_count = db.fetch_one("SELECT COUNT(*) as cnt FROM trading_accounts")
        if acct_count and int(acct_count["cnt"]) == 0:
            env_accounts = [
                {
                    "account_id": "bybit_main",
                    "name": "Bybit Main",
                    "exchange": "bybit",
                    "account_type": "live",
                    "env_key": "BYBIT_API_KEY",
                    "env_secret": "BYBIT_SECRET",
                    "env_pass": "BYBIT_PASSPHRASE",
                },
                {
                    "account_id": "blofin_main",
                    "name": "Blofin Main",
                    "exchange": "blofin",
                    "account_type": "live",
                    "env_key": "BLOFIN_API_KEY",
                    "env_secret": "BLOFIN_API_SECRET",
                    "env_pass": "BLOFIN_API_PHRASE",
                },
                {
                    "account_id": "bitget_main",
                    "name": "Bitget Main",
                    "exchange": "bitget",
                    "account_type": "live",
                    "env_key": "BITGET_API_KEY",
                    "env_secret": "BITGET_API_SECRET",
                    "env_pass": "BITGET_API_PHASE",
                },
            ]
            for ea in env_accounts:
                api_key = os.getenv(ea["env_key"], "")
                api_secret = os.getenv(ea["env_secret"], "")
                if not api_key:
                    continue
                try:
                    db.execute("""
                        INSERT INTO trading_accounts
                        (account_id, name, exchange, account_type,
                         api_key, api_secret, api_passphrase,
                         enabled, live_trading, risk_per_trade_pct,
                         leverage_mode, margin_cap_pct,
                         max_trades_per_symbol, max_open_trades,
                         apex_enabled, mentor_enabled, receive_dossiers)
                        VALUES (%s, %s, %s, %s, %s, %s, %s,
                                1, 0, 1.0, 'max_before_sl', 50,
                                1, 5, 1, 1, 1)
                    """, (ea["account_id"], ea["name"], ea["exchange"],
                          ea["account_type"], api_key, api_secret,
                          os.getenv(ea["env_pass"], "")))
                    logger.info(f"Bootstrap: Seeded trading account '{ea['account_id']}' "
                                f"from .env ({ea['exchange']})")
                except Exception as ex:
                    logger.debug(f"Bootstrap: Seed account {ea['account_id']}: {ex}")
        else:
            # Update existing accounts that have blank keys with .env values
            env_map = {
                "bybit": ("BYBIT_API_KEY", "BYBIT_SECRET", "BYBIT_PASSPHRASE"),
                "blofin": ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_API_PHRASE"),
                "bitget": ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PHASE"),
            }
            blank_accts = db.fetch_all(
                "SELECT account_id, exchange FROM trading_accounts "
                "WHERE (api_key IS NULL OR api_key = '')")
            for acct in (blank_accts or []):
                exch = acct["exchange"]
                if exch not in env_map:
                    continue
                k, s, p = env_map[exch]
                api_key = os.getenv(k, "")
                api_secret = os.getenv(s, "")
                if api_key:
                    db.execute(
                        "UPDATE trading_accounts SET api_key = %s, api_secret = %s, "
                        "api_passphrase = %s WHERE account_id = %s",
                        (api_key, api_secret, os.getenv(p, ""), acct["account_id"]))
                    logger.info(f"Bootstrap: Populated .env keys for account "
                                f"'{acct['account_id']}' ({exch})")
        # Fix: ensure passphrase is populated from correct env var for exchanges that use one
        _pass_env = {"blofin": "BLOFIN_API_PHRASE", "bitget": "BITGET_API_PHASE"}
        for exch, env_var in _pass_env.items():
            try:
                empty_accts = db.fetch_all(
                    "SELECT account_id FROM trading_accounts "
                    "WHERE exchange = %s AND (api_passphrase IS NULL OR api_passphrase = '')",
                    (exch,))
                phrase = os.getenv(env_var, "")
                if phrase:
                    for acct in (empty_accts or []):
                        db.execute(
                            "UPDATE trading_accounts SET api_passphrase = %s "
                            "WHERE account_id = %s",
                            (phrase, acct["account_id"]))
                        logger.info(f"Bootstrap: Set {exch} passphrase for '{acct['account_id']}'")
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Bootstrap: Trading account env-seed: {e}")

    # ── One-time P&L data cleanup (Phase 1 of P&L Accuracy Overhaul) ──
    try:
        # 1a. Reclassify cancelled live_trades that actually filled on exchanges
        filled = db.execute(
            "UPDATE live_trades SET status = 'closed' "
            "WHERE status = 'cancelled' AND filled_at IS NOT NULL "
            "AND actual_entry_price IS NOT NULL AND actual_exit_price IS NOT NULL")
        if filled:
            logger.info(f"Bootstrap: Reclassified {filled} filled-but-cancelled trades to 'closed'")

        # 1b. Zero P&L on cancelled/abandoned trades that never filled
        zeroed = db.execute(
            "UPDATE live_trades SET realised_pnl = NULL, unrealised_pnl = NULL, "
            "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL "
            "WHERE status IN ('cancelled', 'abandoned') AND filled_at IS NULL "
            "AND (realised_pnl IS NOT NULL OR unrealised_pnl IS NOT NULL)")
        if zeroed:
            logger.info(f"Bootstrap: Zeroed phantom P&L on {zeroed} unfilled cancelled/abandoned trades")

        # 1d. Normalize paper_reason values
        db.execute(
            "UPDATE trade_dossiers SET paper_reason = TRIM(paper_reason) "
            "WHERE paper_reason LIKE ' %%'")
        db.execute(
            "UPDATE trade_dossiers SET paper_reason = REPLACE(paper_reason, "
            "'waterfall_fail', 'waterfall_failed') "
            "WHERE paper_reason LIKE '%%waterfall_fail' "
            "AND paper_reason NOT LIKE '%%waterfall_failed%%'")
        db.execute(
            "UPDATE trade_dossiers SET paper_reason = REPLACE(paper_reason, "
            "'waterfall_failed waterfall_failed', 'waterfall_failed') "
            "WHERE paper_reason LIKE '%%waterfall_failed waterfall_failed%%'")
        logger.info("Bootstrap: Paper reason normalization complete")
    except Exception as e:
        logger.debug(f"Bootstrap: P&L data cleanup: {e}")
