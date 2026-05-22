-- ============================================================================
-- Phase Slice 4 — Feed Hygiene (Pending News/Media Cleanup)
-- Date: 2026-05-04
-- Target DB: tickles_shared
-- Author: Roo — Code mode
--
-- WHAT THIS DOES
--   1. Extends media_items.processing_status CHECK constraint with three new
--      terminal states used by the interpretation daemon's hygiene pass:
--
--        * 'skipped_not_chart'         — pre-filter classified the image as
--                                        commentary / meme / unclear; no
--                                        expensive vision call was made.
--        * 'skipped_unsupported_media' — non-image media (video, audio, …)
--                                        which the vision LLM cannot process.
--        * 'skipped_no_content'        — text-only news that produced no
--                                        signal and aged out of the queue.
--
--   2. (No change to news_items.) The cleanup of stale text-only news items
--      writes to news_items.enrichment_status = 'skipped_no_content'. That
--      column is plain TEXT with no CHECK constraint, so no schema change
--      is required — listing the value here for traceability only.
--
-- SAFETY
--   * Idempotent: drops + recreates the existing CHECK constraint by name.
--   * Preserves every status value already in the constraint, including the
--     'skipped_vision_unavailable' state added by Phase 3B.
--   * Read-only on data: CHECK validation runs against existing rows; if any
--     existing row had a value outside the new set the migration would fail
--     fast — that is the desired behaviour.
--
-- ROLLBACK
--   ALTER TABLE public.media_items DROP CONSTRAINT media_items_processing_status_check;
--   ALTER TABLE public.media_items
--       ADD CONSTRAINT media_items_processing_status_check
--       CHECK (processing_status IN (
--           'pending','downloading','downloaded','analyzing',
--           'analyzed','discarded','failed','skipped',
--           'skipped_vision_unavailable'
--       ));
-- ============================================================================

\c tickles_shared

BEGIN;

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
            'analyzed','discarded','failed','skipped',
            'skipped_vision_unavailable',
            'skipped_not_chart',
            'skipped_unsupported_media',
            'skipped_no_content'
        ));
END $$;

COMMIT;

-- ============================================================================
-- SANITY (run manually after COMMIT)
-- ============================================================================
-- SELECT pg_get_constraintdef(oid)
--   FROM pg_constraint
--  WHERE conrelid = 'public.media_items'::regclass
--    AND contype  = 'c';
--
-- Expected output to contain:
--   'skipped_not_chart', 'skipped_unsupported_media', 'skipped_no_content'
-- ============================================================================
