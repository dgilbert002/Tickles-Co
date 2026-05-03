-- ============================================================================
-- Phase 2 — Schema Additions ROLLBACK (per-company)
-- Reverses 2026_04_29_phase2_schema_additions.sql
-- WARNING: Drops position_postmortems table (data lost).
--          Removes columns from signal_interpretations and tracked_positions
--          (data in those columns is lost).
-- ============================================================================

-- 1. Drop position_postmortems
DROP TABLE IF EXISTS public.position_postmortems CASCADE;

-- 2. Drop freeze trigger + function
DROP TRIGGER IF EXISTS trg_tp_freeze_entry_reasons ON public.tracked_positions;
DROP FUNCTION IF EXISTS public.fn_freeze_entry_reasons();

-- 3. Drop new indexes on tracked_positions
DROP INDEX IF EXISTS idx_tp_actor_type;
DROP INDEX IF EXISTS idx_tp_actor_id;
DROP INDEX IF EXISTS idx_tp_postmortem;
DROP INDEX IF EXISTS idx_tp_closed_at;
DROP INDEX IF EXISTS idx_tp_correlation_id;

-- 4. Drop new columns from tracked_positions
ALTER TABLE public.tracked_positions
    DROP COLUMN IF EXISTS actor_type,
    DROP COLUMN IF EXISTS actor_id,
    DROP COLUMN IF EXISTS department,
    DROP COLUMN IF EXISTS position_kind,
    DROP COLUMN IF EXISTS asset_class,
    DROP COLUMN IF EXISTS venue,
    DROP COLUMN IF EXISTS legs,
    DROP COLUMN IF EXISTS sl_history,
    DROP COLUMN IF EXISTS partial_closes,
    DROP COLUMN IF EXISTS entry_reason_trader,
    DROP COLUMN IF EXISTS entry_reason_llm,
    DROP COLUMN IF EXISTS entry_reason_agent,
    DROP COLUMN IF EXISTS entry_reason_frozen_at,
    DROP COLUMN IF EXISTS exit_reason_trader,
    DROP COLUMN IF EXISTS exit_reason_llm,
    DROP COLUMN IF EXISTS exit_reason_system,
    DROP COLUMN IF EXISTS closed_at,
    DROP COLUMN IF EXISTS realized_pnl_usd_final,
    DROP COLUMN IF EXISTS postmortem_status,
    DROP COLUMN IF EXISTS correlation_id;

-- Restore the 'jarvais' default (only if column exists and has no default)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tracked_positions'
          AND table_schema = 'public'
          AND column_name = 'company_id'
    ) THEN
        ALTER TABLE public.tracked_positions
            ALTER COLUMN company_id SET DEFAULT 'jarvais';
    END IF;
END $$;

-- 5. Drop new indexes on signal_interpretations
DROP INDEX IF EXISTS idx_si_prompt_version;
DROP INDEX IF EXISTS idx_si_pattern_tags;
DROP INDEX IF EXISTS idx_si_setup_tags;
DROP INDEX IF EXISTS idx_si_regime_tags;
DROP INDEX IF EXISTS idx_si_session_tags;
DROP INDEX IF EXISTS idx_si_symbol_norm;
DROP INDEX IF EXISTS idx_si_exchange;
DROP INDEX IF EXISTS idx_si_correlation_id;

-- 6. Drop new columns from signal_interpretations
ALTER TABLE public.signal_interpretations
    DROP COLUMN IF EXISTS prefilter_provider,
    DROP COLUMN IF EXISTS prefilter_model,
    DROP COLUMN IF EXISTS prefilter_temperature,
    DROP COLUMN IF EXISTS prefilter_result,
    DROP COLUMN IF EXISTS prefilter_cost_usd,
    DROP COLUMN IF EXISTS vision_provider,
    DROP COLUMN IF EXISTS vision_model_requested,
    DROP COLUMN IF EXISTS vision_model_resolved,
    DROP COLUMN IF EXISTS vision_temperature,
    DROP COLUMN IF EXISTS prompt_version,
    DROP COLUMN IF EXISTS prompt_hash,
    DROP COLUMN IF EXISTS llm_raw_request_path,
    DROP COLUMN IF EXISTS llm_raw_response_path,
    DROP COLUMN IF EXISTS trader_stated_thesis,
    DROP COLUMN IF EXISTS llm_inferred_thesis,
    DROP COLUMN IF EXISTS reason_agreement_score,
    DROP COLUMN IF EXISTS pattern_tags,
    DROP COLUMN IF EXISTS setup_tags,
    DROP COLUMN IF EXISTS regime_tags,
    DROP COLUMN IF EXISTS session_tags,
    DROP COLUMN IF EXISTS instrument_symbol_normalised,
    DROP COLUMN IF EXISTS instrument_exchange,
    DROP COLUMN IF EXISTS correlation_id;

-- ============================================================================
-- Done
-- ============================================================================
SELECT 'Phase 2 per-company rollback complete' AS status;
