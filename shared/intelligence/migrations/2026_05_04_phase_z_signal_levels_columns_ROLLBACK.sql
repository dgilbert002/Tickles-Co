-- ===========================================================================
-- ROLLBACK: Phase Z — Signal levels mirrored into dedicated columns
-- ===========================================================================
-- Drops the dedicated entry/SL/TP columns added by the forward migration
-- (``2026_05_04_phase_z_signal_levels_columns.sql``). The canonical
-- ``llm_levels`` JSONB column is left untouched — no data is lost because
-- the dropped columns are denormalised mirrors.
-- ===========================================================================

DROP INDEX IF EXISTS idx_interp_entry_price;

ALTER TABLE public.signal_interpretations
    DROP COLUMN IF EXISTS entry_price,
    DROP COLUMN IF EXISTS stop_loss,
    DROP COLUMN IF EXISTS take_profit_1,
    DROP COLUMN IF EXISTS take_profit_2,
    DROP COLUMN IF EXISTS take_profit_3,
    DROP COLUMN IF EXISTS take_profit_4,
    DROP COLUMN IF EXISTS take_profit_5,
    DROP COLUMN IF EXISTS take_profit_6;
