-- ============================================================================
-- Phase Y.1 — ROLLBACK: skill_weight_recommendations
-- ============================================================================
-- WARNING: dropping skill_weight_recommendations destroys the audit trail for
-- past Ask-AI invocations. Adopted weight changes must be re-discovered from
-- git history of compute_skill_score().
-- ============================================================================

DROP TABLE IF EXISTS skill_weight_recommendations CASCADE;

SELECT 'Phase Y.1 skill_weight_recommendations rollback complete' AS status;
