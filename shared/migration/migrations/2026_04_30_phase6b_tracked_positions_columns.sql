-- ============================================================================
-- Phase 6b — Add missing Phase 6 columns to tracked_positions
-- ============================================================================
-- instrument_symbol_normalised and reason_agreement_score were referenced
-- in the Phase 6 plan §A but not present in the base schema.
-- ============================================================================

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS instrument_symbol_normalised VARCHAR(64);

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS reason_agreement_score NUMERIC(4,3);

-- Index for symbol-normalised lookups (used by MemU broadcast, postmortem queries)
CREATE INDEX IF NOT EXISTS idx_tp_symbol_norm
  ON public.tracked_positions (instrument_symbol_normalised)
  WHERE instrument_symbol_normalised IS NOT NULL;
