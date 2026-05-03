-- ============================================================================
-- Phase Y.1 — ROLLBACK: feed_views
-- ============================================================================
DROP VIEW IF EXISTS v_memory_feed_30d;
DROP VIEW IF EXISTS v_memory_feed_14d;
DROP VIEW IF EXISTS v_memory_feed_7d;

SELECT 'Phase Y.1 feed_views rollback complete' AS status;
