-- ============================================================================
-- Migration: phase_slice3_tracked_positions
-- Date: 2026-05-04
-- Purpose: Slice 3 — Position Tracking. Adds entry_price_source provenance
--          column, free-form metadata jsonb, and a partial index for fast
--          open-position lookup by (company, symbol, direction).
-- Target:  tickles_shared.public.tracked_positions
-- Notes:   Idempotent (IF NOT EXISTS). Safe to re-run.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Provenance column for entry-price resolution.
--    Allowed values are populated by interpretation_service._resolve_entry_price:
--      'trader'      — explicit trader-provided entry
--      'llm'         — LLM-extracted entry level
--      'live_price'  — fallback to live price probe
--      'last_candle' — fallback to last candle close
--      'none'        — no price resolved (only stored when ALLOW_NULL_ENTRY_PRICE=1)
-- ---------------------------------------------------------------------------
ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS entry_price_source varchar(16);

-- ---------------------------------------------------------------------------
-- 2. Free-form metadata jsonb for forward-bridge sources (e.g. Surgeon)
--    and other agents to record details that don't justify a column.
-- ---------------------------------------------------------------------------
ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- 3. GIN index on metadata for containment queries.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tracked_pos_metadata_gin
    ON public.tracked_positions
    USING gin (metadata);

-- ---------------------------------------------------------------------------
-- 4. Partial index for open-position lookup by company / symbol / direction.
--    This accelerates the de-dupe and merge logic in
--    snapshot.aggregate_open_positions and the surgeon bridge idempotency
--    check. Existing idx_tracked_pos_open is keyed on (status, trader_profile_id)
--    which does not cover this access pattern.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tracked_pos_open_by_company_symbol
    ON public.tracked_positions
        (company_id, instrument_symbol_normalised, direction, status)
    WHERE status = 'open';

COMMIT;
