-- ============================================================================
-- Phase 3B — Intelligence Pipeline Schema ROLLBACK
-- Date: 2026-04-26
-- Reverses 2026_04_26_phase3b_intelligence.sql
-- WARNING: Drops trader_profiles, signal_interpretations, trader_performance.
--          Any data in those tables is lost.
-- ============================================================================

\c tickles_shared

-- 1. Revert media_items.processing_status CHECK constraint
DO $$
DECLARE
    con_name TEXT;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'public.media_items'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%processing_status%'
    LIMIT 1;

    IF con_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE public.media_items DROP CONSTRAINT %I', con_name);
    END IF;

    ALTER TABLE public.media_items
        ADD CONSTRAINT media_items_processing_status_check
        CHECK (processing_status IN (
            'pending','downloading','downloaded','analyzing',
            'analyzed','discarded','failed','skipped'
        ));
END $$;

-- 2. Drop trader_profiles
DROP TABLE IF EXISTS public.trader_profiles CASCADE;

-- 3. Remove intelligence system_config entries
DELETE FROM public.system_config WHERE namespace = 'intelligence';

\c tickles_COMPANY_NAME

-- 4. Drop signal_interpretations
DROP TABLE IF EXISTS public.signal_interpretations CASCADE;

-- 5. Drop trader_performance
DROP TABLE IF EXISTS public.trader_performance CASCADE;

SELECT 'Phase 3B rollback complete' AS status;
