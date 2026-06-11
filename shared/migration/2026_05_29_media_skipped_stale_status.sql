-- 2026_05_29_media_skipped_stale_status.sql
-- A2 fix: add 'skipped_stale' as an allowed terminal processing_status for
-- media_items. Image media whose parent news_item is older than the
-- interpretation freshness window (max_age_hours) can never be claimed, so the
-- daemon's hygiene sweep (cleanup_stale_downloaded_backlog) moves them here
-- instead of letting them pile up forever in 'downloaded'.
--
-- Widens the existing CHECK constraint. Idempotent: drops + re-adds.
--
-- IMPORTANT: use the `processing_status IN (...)` form (NOT an explicit ARRAY
-- cast). Because the column is `character varying`, Postgres rewrites this into
-- `((ARRAY['…'::character varying, …])::text[])`, which is EXACTLY the form
-- pg_dump emits and the canonical snapshot
-- (shared/scripts/snapshots/tickles_shared.snapshot.sql) records — so the
-- schema-drift CI gate stays green. (This mirrors 2026_05_04_phase_slice4.)

ALTER TABLE public.media_items
    DROP CONSTRAINT IF EXISTS media_items_processing_status_check;

ALTER TABLE public.media_items
    ADD CONSTRAINT media_items_processing_status_check
    CHECK (processing_status IN (
        'pending','downloading','downloaded','analyzing','analyzed',
        'discarded','failed','skipped','skipped_vision_unavailable',
        'skipped_not_chart','skipped_unsupported_media','skipped_no_content',
        'skipped_stale'
    ));
