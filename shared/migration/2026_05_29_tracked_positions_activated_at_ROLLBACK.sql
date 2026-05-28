-- Rollback for 2026_05_29_tracked_positions_activated_at.sql
DROP INDEX IF EXISTS idx_tracked_positions_activated_at;
ALTER TABLE public.tracked_positions DROP COLUMN IF EXISTS activated_at;
