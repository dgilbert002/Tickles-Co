-- Phase 23 enrichment migration
--
-- Adds enrichment columns to news_items so the pipeline can
-- write back without cramming everything into the existing
-- ``metadata`` jsonb blob.
--
-- Safe to run multiple times (uses IF NOT EXISTS).
-- Rollback: drop the enrichment column and the two indices.

BEGIN;

ALTER TABLE public.news_items
    ADD COLUMN IF NOT EXISTS enrichment   jsonb;
ALTER TABLE public.news_items
    ADD COLUMN IF NOT EXISTS enriched_at  timestamptz;

CREATE INDEX IF NOT EXISTS idx_news_enriched_at
    ON public.news_items (enriched_at);

CREATE INDEX IF NOT EXISTS idx_news_enrichment_gin
    ON public.news_items USING gin (enrichment);

-- Helper view: messages that still need enrichment.
CREATE OR REPLACE VIEW public.news_items_pending_enrichment AS
SELECT
    id,
    hash_key,
    source,
    headline,
    content,
    collected_at
FROM public.news_items
WHERE enriched_at IS NULL
ORDER BY collected_at ASC;

COMMIT;
