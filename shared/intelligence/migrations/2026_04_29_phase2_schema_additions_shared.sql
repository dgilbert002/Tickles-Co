-- ============================================================================
-- Phase 2 — Schema Additions (shared DB)
-- Target: tickles_shared.public
-- ============================================================================
-- Creates tracked_positions in tickles_shared for new companies.
-- Existing companies already have tracked_positions in their per-company DBs
-- (migrated via the per-company Phase 2 migration).
-- ============================================================================

-- ============================================================================
-- tracked_positions — shared ledger for new companies
-- ============================================================================
-- Full CREATE with all Phase 2 columns baked in.  This is the canonical
-- schema for any company provisioned after Phase 2 lands.

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
    status              VARCHAR(16)     NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','partial_exit','closed','expired','invalidated','cancelled')),
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

    -- Metadata
    created_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Deduplication
    CONSTRAINT uq_position_dedup UNIQUE (news_item_id, trader_profile_id, instrument_symbol, direction)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tracked_pos_trader      ON public.tracked_positions (trader_profile_id);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_status      ON public.tracked_positions (status);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_symbol      ON public.tracked_positions (instrument_symbol, instrument_exchange);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_company     ON public.tracked_positions (company_id);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_signal_ts   ON public.tracked_positions (signal_timestamp);
CREATE INDEX IF NOT EXISTS idx_tracked_pos_open        ON public.tracked_positions (status, trader_profile_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_tp_actor_type           ON public.tracked_positions (actor_type);
CREATE INDEX IF NOT EXISTS idx_tp_actor_id             ON public.tracked_positions (actor_id);
CREATE INDEX IF NOT EXISTS idx_tp_postmortem           ON public.tracked_positions (postmortem_status) WHERE postmortem_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tp_closed_at            ON public.tracked_positions (closed_at) WHERE closed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tp_correlation_id       ON public.tracked_positions (correlation_id) WHERE correlation_id IS NOT NULL;

DROP TRIGGER IF EXISTS trg_tracked_positions_updated ON public.tracked_positions;
CREATE TRIGGER trg_tracked_positions_updated
    BEFORE UPDATE ON public.tracked_positions
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ============================================================================
-- [AG] Reason-freeze trigger — shared ledger version
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

DROP TRIGGER IF EXISTS trg_tp_freeze_entry_reasons ON public.tracked_positions;
CREATE TRIGGER trg_tp_freeze_entry_reasons
    BEFORE UPDATE ON public.tracked_positions
    FOR EACH ROW EXECUTE FUNCTION public.fn_freeze_entry_reasons();

-- ============================================================================
-- Done
-- ============================================================================
SELECT 'Phase 2 shared migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE tablename = 'tracked_positions' AND schemaname = 'public') AS tracked_pos_exists;
