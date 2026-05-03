-- ============================================================================
-- Phase Y.1 — ROLLBACK: mem0_recall_log
-- ============================================================================
-- WARNING: dropping mem0_recall_log destroys the C4 component history for
-- compute_skill_score. Skill scores will fall back to the dropped-component
-- renormalisation path in the Python mirror (shared/intelligence/skill_scorer.py).
-- ============================================================================

DROP TABLE IF EXISTS mem0_recall_log CASCADE;

SELECT 'Phase Y.1 mem0_recall_log rollback complete' AS status;
