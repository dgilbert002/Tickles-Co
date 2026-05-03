-- ============================================================================
-- Phase Y.1 — ROLLBACK: skill_views
-- ============================================================================
-- Drops the three skill views and the compute_skill_score function.
-- Safe to re-run.
-- ============================================================================

DROP VIEW IF EXISTS v_actor_skill_30d;
DROP VIEW IF EXISTS v_actor_skill_14d;
DROP VIEW IF EXISTS v_actor_skill_7d;
DROP FUNCTION IF EXISTS compute_skill_score(TEXT, TEXT, INT);

SELECT 'Phase Y.1 skill_views rollback complete' AS status;
