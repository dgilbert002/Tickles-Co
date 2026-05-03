-- ============================================================================
-- Phase 2 — Schema Additions (per-company)
-- Target: tickles_<company>.public
-- ============================================================================
-- Adds columns to signal_interpretations, tracked_positions.
-- Creates position_postmortems table.
-- Installs reason-freeze trigger on tracked_positions.
-- All changes are additive — no data loss.
-- ============================================================================

-- ============================================================================
-- 1. signal_interpretations — new columns
-- ============================================================================

ALTER TABLE public.signal_interpretations
    ADD COLUMN IF NOT EXISTS prefilter_provider        VARCHAR(32),
    ADD COLUMN IF NOT EXISTS prefilter_model           VARCHAR(128),
    ADD COLUMN IF NOT EXISTS prefilter_temperature     NUMERIC(4,2),
    ADD COLUMN IF NOT EXISTS prefilter_result          VARCHAR(32),
    ADD COLUMN IF NOT EXISTS prefilter_cost_usd        NUMERIC(20,8) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS vision_provider           VARCHAR(32),
    ADD COLUMN IF NOT EXISTS vision_model_requested    VARCHAR(128),
    ADD COLUMN IF NOT EXISTS vision_model_resolved     VARCHAR(128),
    ADD COLUMN IF NOT EXISTS vision_temperature        NUMERIC(4,2),
    ADD COLUMN IF NOT EXISTS prompt_version            VARCHAR(32),
    ADD COLUMN IF NOT EXISTS prompt_hash               CHAR(16),
    ADD COLUMN IF NOT EXISTS llm_raw_request_path      TEXT,
    ADD COLUMN IF NOT EXISTS llm_raw_response_path     TEXT,
    ADD COLUMN IF NOT EXISTS trader_stated_thesis      TEXT,
    ADD COLUMN IF NOT EXISTS llm_inferred_thesis       TEXT,
    ADD COLUMN IF NOT EXISTS reason_agreement_score    NUMERIC(4,3),
    ADD COLUMN IF NOT EXISTS pattern_tags              JSONB,
    ADD COLUMN IF NOT EXISTS setup_tags                JSONB,
    ADD COLUMN IF NOT EXISTS regime_tags               JSONB,
    ADD COLUMN IF NOT EXISTS session_tags              JSONB,
    ADD COLUMN IF NOT EXISTS instrument_symbol_normalised VARCHAR(64),
    ADD COLUMN IF NOT EXISTS instrument_exchange       VARCHAR(32),
    ADD COLUMN IF NOT EXISTS correlation_id            VARCHAR(36);

-- Indexes for new columns
CREATE INDEX IF NOT EXISTS idx_si_prompt_version     ON public.signal_interpretations (prompt_version);
CREATE INDEX IF NOT EXISTS idx_si_pattern_tags       ON public.signal_interpretations USING GIN (pattern_tags);
CREATE INDEX IF NOT EXISTS idx_si_setup_tags         ON public.signal_interpretations USING GIN (setup_tags);
CREATE INDEX IF NOT EXISTS idx_si_regime_tags        ON public.signal_interpretations USING GIN (regime_tags);
CREATE INDEX IF NOT EXISTS idx_si_session_tags       ON public.signal_interpretations USING GIN (session_tags);
CREATE INDEX IF NOT EXISTS idx_si_symbol_norm        ON public.signal_interpretations (instrument_symbol_normalised);
CREATE INDEX IF NOT EXISTS idx_si_exchange           ON public.signal_interpretations (instrument_exchange);
CREATE INDEX IF NOT EXISTS idx_si_correlation_id     ON public.signal_interpretations (correlation_id) WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- 2. tracked_positions — new columns + drop jarvais default + freeze trigger
-- ============================================================================

ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS actor_type              VARCHAR(32),
    ADD COLUMN IF NOT EXISTS actor_id                VARCHAR(128),
    ADD COLUMN IF NOT EXISTS department              VARCHAR(64),
    ADD COLUMN IF NOT EXISTS position_kind           VARCHAR(32),
    ADD COLUMN IF NOT EXISTS asset_class             VARCHAR(32),
    ADD COLUMN IF NOT EXISTS venue                   VARCHAR(64),
    ADD COLUMN IF NOT EXISTS legs                    JSONB,
    ADD COLUMN IF NOT EXISTS sl_history              JSONB,
    ADD COLUMN IF NOT EXISTS partial_closes          JSONB,
    ADD COLUMN IF NOT EXISTS entry_reason_trader     TEXT,
    ADD COLUMN IF NOT EXISTS entry_reason_llm        TEXT,
    ADD COLUMN IF NOT EXISTS entry_reason_agent      TEXT,
    ADD COLUMN IF NOT EXISTS entry_reason_frozen_at  TIMESTAMPTZ(3),
    ADD COLUMN IF NOT EXISTS exit_reason_trader      TEXT,
    ADD COLUMN IF NOT EXISTS exit_reason_llm         TEXT,
    ADD COLUMN IF NOT EXISTS exit_reason_system      TEXT,
    ADD COLUMN IF NOT EXISTS closed_at               TIMESTAMPTZ(3),
    ADD COLUMN IF NOT EXISTS realized_pnl_usd_final  NUMERIC(20,8),
    ADD COLUMN IF NOT EXISTS postmortem_status       VARCHAR(32) DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS correlation_id          VARCHAR(36);

-- Drop the hardcoded 'jarvais' default — every row must explicitly declare its company
ALTER TABLE public.tracked_positions
    ALTER COLUMN company_id DROP DEFAULT;

-- Indexes for new columns
CREATE INDEX IF NOT EXISTS idx_tp_actor_type         ON public.tracked_positions (actor_type);
CREATE INDEX IF NOT EXISTS idx_tp_actor_id           ON public.tracked_positions (actor_id);
CREATE INDEX IF NOT EXISTS idx_tp_postmortem         ON public.tracked_positions (postmortem_status) WHERE postmortem_status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tp_closed_at          ON public.tracked_positions (closed_at) WHERE closed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tp_correlation_id     ON public.tracked_positions (correlation_id) WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- [AG] Reason-freeze trigger — hindsight-bias guard
-- ============================================================================
-- Once entry_reason_frozen_at is set, no UPDATE may change entry_reason_*
-- columns.  The only escape hatch is ALTER TABLE ... DISABLE TRIGGER,
-- which leaves an audit trail in pg_trigger.

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
-- 3. position_postmortems — new table
-- ============================================================================
-- Stores LLM post-trade analysis.  Lives in per-company DB.
-- position_id is a soft FK to tracked_positions (same DB, same schema).

CREATE TABLE IF NOT EXISTS public.position_postmortems (
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

-- Indexes
CREATE INDEX IF NOT EXISTS idx_pm_position_id        ON public.position_postmortems (position_id);
CREATE INDEX IF NOT EXISTS idx_pm_postmortem_version ON public.position_postmortems (postmortem_version);
CREATE INDEX IF NOT EXISTS idx_pm_param_hash         ON public.position_postmortems (param_hash);
CREATE INDEX IF NOT EXISTS idx_pm_correlation_id     ON public.position_postmortems (correlation_id) WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- [AF] Soft-FK note (documented in migration comment)
-- position_postmortems.position_id references tracked_positions.id
-- conceptually.  Postgres enforces this within the same DB via the
-- application layer (PostMortemService).  A nightly orphan check cron
-- flags any position_id missing from tracked_positions.
-- ============================================================================

-- ============================================================================
-- Done
-- ============================================================================
SELECT 'Phase 2 per-company migration complete' AS status,
       (SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'signal_interpretations'
          AND table_schema = 'public') AS signal_interp_columns,
       (SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'tracked_positions'
          AND table_schema = 'public') AS tracked_pos_columns,
       (SELECT COUNT(*) FROM pg_tables
        WHERE tablename = 'position_postmortems'
          AND schemaname = 'public') AS postmortem_table_exists;
