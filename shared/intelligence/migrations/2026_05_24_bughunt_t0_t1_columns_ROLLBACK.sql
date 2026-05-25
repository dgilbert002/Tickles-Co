-- Rollback for 2026_05_24_bughunt_t0_t1_columns.sql
BEGIN;
DROP INDEX IF EXISTS public.idx_tracked_positions_deduped_at;
ALTER TABLE public.tracked_positions DROP COLUMN IF EXISTS deduped_at;
COMMIT;
