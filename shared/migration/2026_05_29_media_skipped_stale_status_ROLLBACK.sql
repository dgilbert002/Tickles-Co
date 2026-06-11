-- ROLLBACK for 2026_05_29_media_skipped_stale_status.sql
-- Restores the prior CHECK constraint (without 'skipped_stale').
-- NOTE: any rows already set to 'skipped_stale' must be re-mapped first, or the
-- ADD CONSTRAINT will fail validation. We remap them to the generic 'skipped'.

UPDATE public.media_items
SET processing_status = 'skipped'
WHERE processing_status = 'skipped_stale';

ALTER TABLE public.media_items
    DROP CONSTRAINT IF EXISTS media_items_processing_status_check;

ALTER TABLE public.media_items
    ADD CONSTRAINT media_items_processing_status_check
    CHECK (processing_status IN (
        'pending','downloading','downloaded','analyzing','analyzed',
        'discarded','failed','skipped','skipped_vision_unavailable',
        'skipped_not_chart','skipped_unsupported_media','skipped_no_content'
    ));
