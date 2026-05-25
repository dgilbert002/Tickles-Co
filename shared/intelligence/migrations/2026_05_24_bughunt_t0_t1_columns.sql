-- ============================================================================
-- 2026-05-24 — Bug Hunt Tier 0/1 schema additions
-- ============================================================================
-- Author: tickles-intelligence (5-agent + 4-agent bug-hunt synthesis)
-- Phase: post-Bug-12 (after sibling-class hunt)
--
-- Findings addressed:
--   * C1 (Code Analyzer 2 §1.1) — `tracked_positions.deduped_at` column referenced
--     by `interpretation_service.py:2427` (UPDATE) and `dashboard/snapshot.py:363`
--     (KPI SELECT). Column was never defined in any migration. INSERT/UPDATE
--     would throw the moment Phase-8 dedup actually fires; KPI query was a
--     latent 500.
--
-- Apply:   psql -d tickles_shared -f 2026_05_24_bughunt_t0_t1_columns.sql
-- Rollback: 2026_05_24_bughunt_t0_t1_columns_ROLLBACK.sql
-- ============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- C1: tracked_positions.deduped_at — timestamp of the last Phase-8 dedup match.
--     NULL means the position has never been merged with a duplicate signal.
-- ----------------------------------------------------------------------------
ALTER TABLE public.tracked_positions
    ADD COLUMN IF NOT EXISTS deduped_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN public.tracked_positions.deduped_at IS
    'Timestamp of the most recent Phase-8 duplicate-signal merge. NULL when '
    'the position has not yet been deduped against a continuation signal. '
    'Used by dashboard snapshot KPI (positions deduped in last 24h).';

-- Partial index — `snapshot.py` filters on `WHERE deduped_at >= now() - interval '24 hours'`,
-- which is a hot KPI read. Partial keeps the index tiny since most rows are NULL.
CREATE INDEX IF NOT EXISTS idx_tracked_positions_deduped_at
    ON public.tracked_positions (deduped_at)
    WHERE deduped_at IS NOT NULL;

SELECT '2026-05-24 bughunt t0/t1 columns: applied' AS status;

COMMIT;
