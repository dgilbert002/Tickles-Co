-- ============================================================================
-- Phase 2 — Schema Additions ROLLBACK (shared DB)
-- Reverses 2026_04_29_phase2_schema_additions_shared.sql
-- WARNING: Drops tracked_positions from tickles_shared (data lost).
-- ============================================================================

-- 1. Drop freeze trigger + function
DROP TRIGGER IF EXISTS trg_tp_freeze_entry_reasons ON public.tracked_positions;
DROP FUNCTION IF EXISTS public.fn_freeze_entry_reasons();

-- 2. Drop tracked_positions table (and all indexes + triggers with it)
DROP TABLE IF EXISTS public.tracked_positions CASCADE;

-- ============================================================================
-- Done
-- ============================================================================
SELECT 'Phase 2 shared rollback complete' AS status;
