-- ============================================================================
-- Rollback for Phase 3A.1 — 2026-04-18
-- ============================================================================
-- WARNING: this drops collector_sources + media_items and removes the new
-- columns from news_items. Any data in those tables/columns is lost.
-- Only run if you are absolutely certain you want to revert.
-- ============================================================================

BEGIN;

-- Drop columns added to news_items (keep original 9-column layout)
ALTER TABLE public.news_items
    DROP COLUMN IF EXISTS media_count,
    DROP COLUMN IF EXISTS has_media,
    DROP COLUMN IF EXISTS metadata,
    DROP COLUMN IF EXISTS message_id,
    DROP COLUMN IF EXISTS author_id,
    DROP COLUMN IF EXISTS author,
    DROP COLUMN IF EXISTS channel_name,
    DROP COLUMN IF EXISTS source_id;

DROP TABLE IF EXISTS public.media_items;

DROP TRIGGER IF EXISTS trg_collector_sources_updated_at ON public.collector_sources;
DROP TABLE IF EXISTS public.collector_sources;

-- Note: trg_set_updated_at() stays — harmless generic helper.

COMMIT;
