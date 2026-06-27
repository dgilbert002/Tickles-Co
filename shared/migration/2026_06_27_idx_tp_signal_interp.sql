-- Speed up dashboard /api/unified-signals by indexing the join from
-- signal_interpretations to tracked_positions.
CREATE INDEX IF NOT EXISTS idx_tp_signal_interp
ON public.tracked_positions (signal_interpretation_id);
