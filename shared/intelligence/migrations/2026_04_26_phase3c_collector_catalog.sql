-- ============================================================================
-- Phase 3C — Collector Catalog + Position Tracking Schema (Postgres)
-- Date: 2026-04-26
-- Target DB: tickles_shared
-- Author: Roo — Discord Trader Monitoring Architecture
--
-- WHAT THIS DOES
--   1. collector_catalog — Registry of all data sources (Discord servers,
--      Telegram channels, RSS feeds, TradingView, etc.) with their metadata.
--   2. watched_channels — Per-channel configuration: what to collect
--      (text, images, charts, videos), which instruments to watch for,
--      and collection parameters.
--   3. watched_users — Per-user configuration within channels: track their
--      trades, commentary, chart analysis. Links to trader_profiles.
--   4. tracked_positions — The core trade tracking table. Every detected
--      trade signal gets a row with entry, SL, TP, current status, P&L.
--   5. position_updates — Time-series snapshots of position state for
--      continuous P&L tracking, distance-to-entry, time-in-trade.
--   6. agent_opinions — ChartHacker's parallel analysis: would it take the
--      trade, at what entry/SL/TP, and how it fares against the trader.
--
-- SAFETY
--   * Idempotent (IF NOT EXISTS everywhere).
--   * Uses existing set_updated_at() trigger from tickles_shared.
--   * All monetary values NUMERIC(20,8); timestamps TIMESTAMPTZ(3).
--   * Foreign keys to trader_profiles (soft, cross-DB aware).
-- ============================================================================

\c tickles_shared

-- ============================================================================
-- 1. collector_catalog — Source Registry
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.collector_catalog (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Identity
    source_type         VARCHAR(32)     NOT NULL
        CHECK (source_type IN ('discord','telegram','rss','tradingview','twitter','api','webhook')),
    source_name         VARCHAR(255)    NOT NULL,       -- human name, e.g. "ChartHackers"
    source_slug         VARCHAR(100)    NOT NULL,       -- machine id, e.g. "charthackers_discord"

    -- Connection / access config (stored as JSONB for flexibility)
    connection_config   JSONB           NOT NULL DEFAULT '{}',
    -- Example for Discord: {"server_id": "123", "server_name": "ChartHackers", "bot_token_env": "DISCORD_BOT_TOKEN"}
    -- Example for RSS: {"url": "https://...", "poll_interval_minutes": 5}

    -- Global enable/disable
    is_enabled          BOOLEAN         NOT NULL DEFAULT TRUE,
    priority            INT             NOT NULL DEFAULT 100,  -- lower = higher priority

    -- Classification
    primary_asset_class VARCHAR(32)
        CHECK (primary_asset_class IN ('crypto','cfd','stock','forex','commodity','index','mixed')),
    notes               TEXT,

    -- Metadata
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_collector_slug UNIQUE (source_slug)
);

CREATE INDEX IF NOT EXISTS idx_collector_type    ON public.collector_catalog (source_type);
CREATE INDEX IF NOT EXISTS idx_collector_enabled ON public.collector_catalog (is_enabled);

-- ============================================================================
-- 2. watched_channels — Channel-level Collection Configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.watched_channels (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Link to collector_catalog
    collector_id        BIGINT          NOT NULL REFERENCES public.collector_catalog(id) ON DELETE CASCADE,

    -- Channel identity (platform-specific)
    platform_channel_id VARCHAR(100)    NOT NULL,       -- Discord channel ID, Telegram chat ID, etc.
    channel_name        VARCHAR(255)    NOT NULL,       -- human name, e.g. "panda-trades"
    channel_slug        VARCHAR(100)    NOT NULL,       -- machine id, e.g. "charthackers_panda_trades"

    -- What to collect
    collect_text        BOOLEAN         NOT NULL DEFAULT TRUE,
    collect_images      BOOLEAN         NOT NULL DEFAULT TRUE,
    collect_charts      BOOLEAN         NOT NULL DEFAULT TRUE,   -- images that look like charts
    collect_videos      BOOLEAN         NOT NULL DEFAULT FALSE,
    collect_voice       BOOLEAN         NOT NULL DEFAULT FALSE,

    -- Content filtering
    min_confidence      NUMERIC(3,2)    NOT NULL DEFAULT 0.0,    -- for ML-based filtering (future)
    allowed_keywords    TEXT[],                      -- only collect messages matching these
    blocked_keywords    TEXT[],                      -- skip messages matching these

    -- Instrument detection (what markets this channel covers)
    instrument_patterns JSONB           NOT NULL DEFAULT '[]',   -- [{"pattern": "BTC", "symbol": "BTCUSDT", "exchange": "bybit"}, ...]
    default_instrument  VARCHAR(50),                    -- fallback when no instrument detected
    default_exchange    VARCHAR(50),                    -- fallback exchange

    -- Collection parameters
    max_messages_per_run    INT         NOT NULL DEFAULT 200,
    group_window_seconds    INT         NOT NULL DEFAULT 60,     -- message grouping window
    download_media          BOOLEAN     NOT NULL DEFAULT TRUE,
    media_retention_days    INT         NOT NULL DEFAULT 30,

    -- Enable/disable
    is_enabled          BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Metadata
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_channel_collector_platform UNIQUE (collector_id, platform_channel_id)
);

CREATE INDEX IF NOT EXISTS idx_watched_channel_collector ON public.watched_channels (collector_id);
CREATE INDEX IF NOT EXISTS idx_watched_channel_enabled   ON public.watched_channels (is_enabled);
CREATE INDEX IF NOT EXISTS idx_watched_channel_slug      ON public.watched_channels (channel_slug);

-- ============================================================================
-- 3. watched_users — User-level Tracking Configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.watched_users (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Link to watched_channels (a user can appear in multiple channels)
    channel_id          BIGINT          NOT NULL REFERENCES public.watched_channels(id) ON DELETE CASCADE,

    -- Link to trader_profiles (shared catalog)
    trader_profile_id   BIGINT,                         -- FK to public.trader_profiles (soft, not enforced)

    -- User identity (platform-specific)
    platform_user_id    VARCHAR(100)    NOT NULL,       -- Discord user ID, Telegram username, etc.
    username_raw        VARCHAR(255)    NOT NULL,       -- raw username
    username_normalized VARCHAR(255)    NOT NULL,       -- lowercased, stripped
    display_name        VARCHAR(255),                   -- human-readable name

    -- What to track for this user
    track_trades        BOOLEAN         NOT NULL DEFAULT TRUE,   -- detect and track their trade calls
    track_charts        BOOLEAN         NOT NULL DEFAULT TRUE,   -- analyze their chart images
    track_commentary    BOOLEAN         NOT NULL DEFAULT TRUE,   -- store their text commentary
    track_advice        BOOLEAN         NOT NULL DEFAULT TRUE,   -- extract trading advice/wisdom

    -- Trade detection parameters
    trade_detection_confidence  NUMERIC(3,2)    NOT NULL DEFAULT 0.7,
    -- How confident must the parser be that this message contains a trade signal

    -- Classification (overrides channel defaults)
    trader_type         VARCHAR(32)
        CHECK (trader_type IN ('pro','amateur','bot','news','unknown')),
    primary_asset_class VARCHAR(32)
        CHECK (primary_asset_class IN ('crypto','cfd','stock','forex','commodity','index')),
    primary_timeframe   VARCHAR(16)
        CHECK (primary_timeframe IN ('scalping','intraday','swing','position','unknown')),

    -- Scoring weights (how much this user's signals count)
    signal_weight       NUMERIC(5,4)    NOT NULL DEFAULT 1.0,

    -- Enable/disable
    is_enabled          BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Metadata
    first_seen_at       TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at        TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_watched_user_channel_platform UNIQUE (channel_id, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_watched_user_channel     ON public.watched_users (channel_id);
CREATE INDEX IF NOT EXISTS idx_watched_user_profile     ON public.watched_users (trader_profile_id);
CREATE INDEX IF NOT EXISTS idx_watched_user_enabled     ON public.watched_users (is_enabled);
CREATE INDEX IF NOT EXISTS idx_watched_user_normalized  ON public.watched_users (username_normalized);

-- ============================================================================
-- 4. tracked_positions — Core Trade Tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.tracked_positions (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Source references
    news_item_id        BIGINT          NOT NULL,       -- FK to public.news_items
    media_item_id       BIGINT,                         -- FK to public.media_items (nullable for text-only)
    trader_profile_id   BIGINT          NOT NULL,       -- FK to public.trader_profiles
    signal_interpretation_id BIGINT,                    -- FK to signal_interpretations (in company DB)

    -- Instrument (resolved from message + catalog)
    instrument_symbol   VARCHAR(50)     NOT NULL,
    instrument_exchange VARCHAR(50)     NOT NULL DEFAULT 'bybit',
    epic_code           VARCHAR(50),                    -- Capital.com epic for CFDs

    -- Trade parameters (extracted from message or chart analysis)
    direction           VARCHAR(8)      NOT NULL
        CHECK (direction IN ('long','short')),
    entry_price         NUMERIC(20,8),                  -- detected or inferred entry
    stop_loss           NUMERIC(20,8),                  -- detected SL
    take_profit_1       NUMERIC(20,8),                  -- detected TP1
    take_profit_2       NUMERIC(20,8),                  -- detected TP2 (optional)
    take_profit_3       NUMERIC(20,8),                  -- detected TP3 (optional)
    position_size       NUMERIC(20,8),                  -- detected size (if mentioned)
    leverage            NUMERIC(5,2),                   -- detected leverage (if mentioned)

    -- Detection metadata
    detection_method    VARCHAR(32)     NOT NULL DEFAULT 'manual'
        CHECK (detection_method IN ('manual','llm_vision','text_parser','quant_pattern','agent_override')),
    detection_confidence NUMERIC(5,4)   NOT NULL DEFAULT 0.0,
    raw_signal_text     TEXT,                           -- the message text that triggered this position
    signal_timestamp    TIMESTAMPTZ(3)  NOT NULL,       -- when the signal was posted

    -- Status lifecycle
    status              VARCHAR(16)     NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','partial_exit','closed','expired','invalidated','cancelled')),
    status_reason       VARCHAR(100),                   -- why status changed

    -- Current market state (updated by PositionMonitor daemon)
    current_price       NUMERIC(20,8),                  -- last known price
    price_updated_at    TIMESTAMPTZ(3),                 -- when current_price was last updated
    highest_price       NUMERIC(20,8),                  -- highest price since entry (for drawdown)
    lowest_price        NUMERIC(20,8),                  -- lowest price since entry

    -- P&L metrics (updated continuously)
    unrealized_pnl_pct  NUMERIC(10,4),                  -- current unrealized P&L %
    unrealized_pnl_usd  NUMERIC(20,8),                  -- current unrealized P&L in USD
    realized_pnl_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,  -- realized P&L % (from partial exits)
    realized_pnl_usd    NUMERIC(20,8)   NOT NULL DEFAULT 0,  -- realized P&L in USD
    max_drawdown_pct    NUMERIC(10,4)   NOT NULL DEFAULT 0,  -- max drawdown from entry
    max_profit_pct      NUMERIC(10,4)   NOT NULL DEFAULT 0,  -- max profit from entry

    -- Distance metrics
    distance_to_entry_pct NUMERIC(10,4),                  -- how far price is from entry (%)
    distance_to_sl_pct    NUMERIC(10,4),                  -- how far price is from SL (%)
    distance_to_tp1_pct   NUMERIC(10,4),                  -- how far price is from TP1 (%)
    risk_reward_ratio     NUMERIC(10,4),                  -- R:R at entry

    -- Time metrics
    time_in_trade_minutes INT             NOT NULL DEFAULT 0,
    time_to_tp1_minutes   INT,                            -- how long to hit TP1 (if hit)
    time_to_sl_minutes    INT,                            -- how long to hit SL (if hit)
    expiry_at             TIMESTAMPTZ(3),                 -- when this signal expires (if applicable)

    -- Outcome (filled when position closes)
    outcome               VARCHAR(16)
        CHECK (outcome IN ('tp1_hit','tp2_hit','tp3_hit','sl_hit','breakeven','expired','manual_close','invalidated')),
    exit_price            NUMERIC(20,8),                  -- actual exit price
    exit_timestamp        TIMESTAMPTZ(3),                 -- when position closed
    exit_reason           TEXT,                           -- why it closed

    -- Notional for P&L calc (from env or config)
    notional_usd          NUMERIC(20,8)   NOT NULL DEFAULT 1000.0,

    -- Company context (for multi-tenancy)
    company_id            VARCHAR(50)     NOT NULL DEFAULT 'jarvais',

    -- Metadata
    created_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication: one position per (news_item, trader, symbol, direction)
    CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction)
);

CREATE INDEX IF NOT EXISTS idx_tracked_pos_trader      ON public.tracked_positions (trader_profile_id);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_status      ON public.tracked_positions (status);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_symbol      ON public.tracked_positions (instrument_symbol, instrument_exchange);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_company     ON public.tracked_positions (company_id);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_signal_ts   ON public.tracked_positions (signal_timestamp);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_open        ON public.tracked_positions (status, trader_profile_id) WHERE status = 'open';

-- ============================================================================
-- 5. position_updates — Time-Series Snapshots for Continuous Tracking
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.position_updates (
    id                  BIGSERIAL       PRIMARY KEY,
    position_id         BIGINT          NOT NULL REFERENCES public.tracked_positions(id) ON DELETE CASCADE,

    -- Market state at this snapshot
    price               NUMERIC(20,8)   NOT NULL,
    timestamp           TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Computed metrics at this snapshot
    unrealized_pnl_pct  NUMERIC(10,4)   NOT NULL,
    unrealized_pnl_usd  NUMERIC(20,8)   NOT NULL,
    distance_to_entry_pct NUMERIC(10,4) NOT NULL,
    distance_to_sl_pct    NUMERIC(10,4),
    distance_to_tp1_pct   NUMERIC(10,4),
    time_in_trade_minutes INT             NOT NULL,

    -- Source of this update
    update_source       VARCHAR(32)     NOT NULL DEFAULT 'candle_poll'
        CHECK (update_source IN ('candle_poll','tick','manual','agent_review')),

    -- Metadata
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pos_update_position ON public.position_updates (position_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_pos_update_recent   ON public.position_updates (timestamp DESC) WHERE timestamp > NOW() - INTERVAL '7 days';

-- ============================================================================
-- 6. agent_opinions — ChartHacker's Parallel Analysis & Competition
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.agent_opinions (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Link to the position this opinion is about
    position_id         BIGINT          NOT NULL REFERENCES public.tracked_positions(id) ON DELETE CASCADE,

    -- Agent identity
    agent_name          VARCHAR(100)    NOT NULL DEFAULT 'ChartHacker',
    agent_version       VARCHAR(50)     NOT NULL DEFAULT '1.0',

    -- Agent's opinion on the trade
    would_take_trade    BOOLEAN         NOT NULL,       -- would the agent enter this trade?
    agent_direction     VARCHAR(8)
        CHECK (agent_direction IN ('long','short','neutral')),
    agent_entry_price   NUMERIC(20,8),                  -- agent's preferred entry
    agent_stop_loss     NUMERIC(20,8),                  -- agent's preferred SL
    agent_take_profit   NUMERIC(20,8),                  -- agent's preferred TP
    agent_confidence    NUMERIC(5,4)    NOT NULL DEFAULT 0.0,

    -- Agent's reasoning
    reasoning           TEXT,                           -- why it would/wouldn't take the trade
    pattern_detected    VARCHAR(255),                   -- what pattern the agent saw
    indicators_used     JSONB           NOT NULL DEFAULT '[]',  -- which indicators informed the opinion

    -- Competition tracking (how agent fares vs the trader)
    agent_pnl_pct       NUMERIC(10,4),                  -- P&L if agent had taken its own trade
    trader_pnl_pct      NUMERIC(10,4),                  -- actual trader P&L at time of opinion
    performance_delta   NUMERIC(10,4),                  -- agent_pnl - trader_pnl (positive = agent won)

    -- Learning metadata
    lessons_learned     TEXT,                           -- what the agent learned from this trade
    similar_trades_found INT,                         -- how many similar historical trades were found
    historical_win_rate NUMERIC(5,4),                   -- win rate of similar historical trades

    -- Source of the opinion
    opinion_source      VARCHAR(32)     NOT NULL DEFAULT 'daemon'
        CHECK (opinion_source IN ('daemon','mcp_tool','manual_review','scheduled')),

    -- Metadata
    created_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- One opinion per agent per position (updated as position evolves)
    CONSTRAINT uq_agent_opinion UNIQUE (position_id, agent_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_opinion_position ON public.agent_opinions (position_id);
CREATE INDEX IF NOT EXISTS idx_agent_opinion_agent     ON public.agent_opinions (agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_opinion_delta     ON public.agent_opinions (performance_delta) WHERE performance_delta IS NOT NULL;

-- ============================================================================
-- 7. Seed ChartHackers Discord Configuration
-- ============================================================================

INSERT INTO public.collector_catalog (source_type, source_name, source_slug, connection_config, is_enabled, priority, primary_asset_class, notes)
VALUES (
    'discord',
    'ChartHackers',
    'charthackers_discord',
    '{"server_name": "ChartHackers", "bot_token_env": "DISCORD_BOT_TOKEN"}'::jsonb,
    TRUE,
    10,
    'mixed',
    'Primary Discord server for crypto and CFD trade signals'
)
ON CONFLICT (source_slug) DO UPDATE SET
    connection_config = EXCLUDED.connection_config,
    is_enabled = EXCLUDED.is_enabled,
    updated_at = CURRENT_TIMESTAMP;

-- Get the collector_id for channel inserts
DO $$
DECLARE
    v_collector_id BIGINT;
BEGIN
    SELECT id INTO v_collector_id FROM public.collector_catalog WHERE source_slug = 'charthackers_discord';

    -- Insert watched channels
    INSERT INTO public.watched_channels (collector_id, platform_channel_id, channel_name, channel_slug, collect_text, collect_images, collect_charts, collect_videos, default_instrument, default_exchange, is_enabled)
    VALUES
        (v_collector_id, 'daily-market-updates',    'daily-market-updates',    'charthackers_daily_market',    TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'chats-and-setups',        'chats-and-setups',        'charthackers_chats',           TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'panda-trades',            'panda-trades',            'charthackers_panda',           TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'nagel-trades',            'nagel-trades',            'charthackers_nagel',           TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'trader-j-trades',         'trader-j-trades',         'charthackers_trader_j',        TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'stonks-setups',           'stonks-setups',           'charthackers_stonks',          TRUE, TRUE, TRUE, FALSE, NULL, 'capital', TRUE),
        (v_collector_id, 'alt-coin-trading-setups',  'alt-coin-trading-setups', 'charthackers_altcoin',         TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'wen-n-tree',              'wen-n-tree',              'charthackers_wen',             TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'metals-comodities',       'metals-comodities',       'charthackers_metals',          TRUE, TRUE, TRUE, FALSE, NULL, 'capital', TRUE),
        (v_collector_id, 'live-show-charts',        'live-show-charts',        'charthackers_live',            TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'zoom-charts',             'zoom-charts',             'charthackers_zoom',            TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE),
        (v_collector_id, 'blofin-trading-comp',     'blofin-trading-comp',     'charthackers_blofin',          TRUE, TRUE, TRUE, FALSE, NULL, 'bybit', TRUE)
    ON CONFLICT (collector_id, platform_channel_id) DO UPDATE SET
        channel_name = EXCLUDED.channel_name,
        is_enabled = EXCLUDED.is_enabled,
        updated_at = CURRENT_TIMESTAMP;
END $$;

-- ============================================================================
-- 8. Seed Watched Traders (linked to trader_profiles)
-- ============================================================================

-- First ensure trader_profiles exist for these users
INSERT INTO public.trader_profiles (platform, handle_raw, handle_normalized, display_name, trader_type, primary_asset_class, primary_timeframe)
VALUES
    ('discord', 'emutrading',      'emutrading',      'Trader J (EMU)',       'pro', 'crypto', 'intraday'),
    ('discord', 'degendavidd',     'degendavidd',     'DegenDavidD',          'pro', 'crypto', 'swing'),
    ('discord', 'its.chaos',       'its.chaos',       'Chaoss',               'pro', 'crypto', 'intraday'),
    ('discord', 'thelordofentry',  'thelordofentry',  'Dylan',                'pro', 'crypto', 'scalping'),
    ('discord', 'arabian.panda',   'arabian.panda',   'ArabianPanda',         'pro', 'crypto', 'swing'),
    ('discord', 'thenagel',        'thenagel',        'TheNagel',             'pro', 'crypto', 'intraday')
ON CONFLICT (platform, handle_normalized) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    trader_type = EXCLUDED.trader_type,
    last_seen_at = CURRENT_TIMESTAMP;

-- Link watched_users to channels (this requires knowing channel_ids, so we use a DO block)
DO $$
DECLARE
    v_channel_id BIGINT;
    v_trader_id BIGINT;
BEGIN
    -- Link ArabianPanda to panda-trades
    SELECT id INTO v_channel_id FROM public.watched_channels WHERE channel_slug = 'charthackers_panda';
    SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'arabian.panda' AND platform = 'discord';
    IF v_channel_id IS NOT NULL AND v_trader_id IS NOT NULL THEN
        INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
        VALUES (v_channel_id, v_trader_id, 'arabian.panda', 'arabian.panda', 'arabian.panda', 'ArabianPanda', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
        ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
    END IF;

    -- Link TheNagel to nagel-trades
    SELECT id INTO v_channel_id FROM public.watched_channels WHERE channel_slug = 'charthackers_nagel';
    SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'thenagel' AND platform = 'discord';
    IF v_channel_id IS NOT NULL AND v_trader_id IS NOT NULL THEN
        INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
        VALUES (v_channel_id, v_trader_id, 'thenagel', 'thenagel', 'thenagel', 'TheNagel', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
        ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
    END IF;

    -- Link Trader J to trader-j-trades
    SELECT id INTO v_channel_id FROM public.watched_channels WHERE channel_slug = 'charthackers_trader_j';
    SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'emutrading' AND platform = 'discord';
    IF v_channel_id IS NOT NULL AND v_trader_id IS NOT NULL THEN
        INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
        VALUES (v_channel_id, v_trader_id, 'emutrading', 'emutrading', 'emutrading', 'Trader J (EMU)', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
        ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
    END IF;

    -- Link DegenDavidD, Chaoss, Dylan to chats-and-setups (they post in general channels too)
    SELECT id INTO v_channel_id FROM public.watched_channels WHERE channel_slug = 'charthackers_chats';
    IF v_channel_id IS NOT NULL THEN
        -- DegenDavidD
        SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'degendavidd' AND platform = 'discord';
        IF v_trader_id IS NOT NULL THEN
            INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
            VALUES (v_channel_id, v_trader_id, 'degendavidd', 'degendavidd', 'degendavidd', 'DegenDavidD', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
            ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
        END IF;

        -- Chaoss
        SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'its.chaos' AND platform = 'discord';
        IF v_trader_id IS NOT NULL THEN
            INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
            VALUES (v_channel_id, v_trader_id, 'its.chaos', 'its.chaos', 'its.chaos', 'Chaoss', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
            ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
        END IF;

        -- Dylan
        SELECT id INTO v_trader_id FROM public.trader_profiles WHERE handle_normalized = 'thelordofentry' AND platform = 'discord';
        IF v_trader_id IS NOT NULL THEN
            INSERT INTO public.watched_users (channel_id, trader_profile_id, platform_user_id, username_raw, username_normalized, display_name, track_trades, track_charts, track_commentary, track_advice, trader_type, primary_asset_class)
            VALUES (v_channel_id, v_trader_id, 'thelordofentry', 'thelordofentry', 'thelordofentry', 'Dylan', TRUE, TRUE, TRUE, TRUE, 'pro', 'crypto')
            ON CONFLICT (channel_id, platform_user_id) DO NOTHING;
        END IF;
    END IF;
END $$;

-- ============================================================================
-- 9. System Config Entries for Position Monitor
-- ============================================================================

INSERT INTO public.system_config (namespace, config_key, config_value) VALUES
    ('position_monitor', 'poll_interval_seconds',    '60'),
    ('position_monitor', 'max_open_positions_per_trader', '20'),
    ('position_monitor', 'default_notional_usd',     '1000.0'),
    ('position_monitor', 'position_expiry_hours',    '72'),
    ('position_monitor', 'sl_buffer_pct',            '0.5'),     -- extra buffer beyond detected SL
    ('position_monitor', 'tp_buffer_pct',            '0.5'),     -- extra buffer beyond detected TP
    ('position_monitor', 'capital_com_enabled',      'true'),
    ('position_monitor', 'capital_com_epic_lookup',  'auto'),    -- auto-resolve epic from symbol
    ('charthacker_agent', 'review_interval_minutes', '30'),
    ('charthacker_agent', 'competition_mode',         'true'),    -- track agent vs trader P&L
    ('charthacker_agent', 'mcp_tools_enabled',        'true'),    -- allow agent to use MCP tools
    ('charthacker_agent', 'memory_enabled',           'true'),    -- store lessons in Mem0
    ('charthacker_agent', 'min_confidence_to_opine',  '0.6')
ON CONFLICT (namespace, config_key) DO NOTHING;
