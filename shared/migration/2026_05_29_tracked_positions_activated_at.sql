-- Phase 3 (2026-05-29): add activated_at to tracked_positions.
--
-- WHY: the Entry Radar wants to show "JUST FILLED" setups (positions that
-- transitioned pending -> open in the last ~30 min). We could not detect this
-- before because:
--   * updated_at changes every minute (price refresh) -> useless as a fill clock
--   * created_at is the SIGNAL time, not the fill time (a setup can sit pending
--     for over an hour before a real candle touches the entry)
-- So we add a dedicated activated_at stamp, set exactly once at the
-- pending -> open transition (to the candle timestamp that touched entry).
--
-- NULL means: never activated (still pending, or cancelled/expired pre-entry).
-- Existing open rows stay NULL (they filled hours ago and should NOT show as
-- "just filled"); no backfill is required. New activations populate it going
-- forward via position_monitor._activate_pending_positions.

ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;

COMMENT ON COLUMN public.tracked_positions.activated_at IS
    'When the position transitioned pending->open (the 1m candle timestamp that '
    'first traded through entry). NULL = never activated. Set once by '
    'position_monitor._activate_pending_positions. Powers the Entry Radar '
    '"JUST FILLED" view.';

-- Partial index: the radar only ever asks "what activated recently?", so index
-- just the non-null rows.
CREATE INDEX IF NOT EXISTS idx_tracked_positions_activated_at
    ON public.tracked_positions (activated_at)
    WHERE activated_at IS NOT NULL;
