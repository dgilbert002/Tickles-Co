-- ============================================================================
-- Round 10 (2026-05-24) ROLLBACK — drop the model_config_audit table.
--
-- Reversing this migration ALSO requires:
--   1. Reverting shared/intelligence/model_config.py (or pinning the code
--      defaults back to claude-sonnet-4 primary).
--   2. Reverting shared/intelligence/interpretation_service.py to read
--      PRIMARY_MODEL / FALLBACK_MODEL / PREFILTER_MODEL constants directly.
--   3. Removing /api/settings/* routes from shared/dashboard/server.py.
--   4. Reverting the Settings tab in shared/dashboard/web/index.html and
--      shared/dashboard/static/app.js.
--
-- The DELETE on system_config below removes any operator-stored model
-- choices, returning the system to env-var / code-default resolution.
-- ============================================================================

DROP INDEX IF EXISTS public.idx_model_config_audit_slot_changed;
DROP INDEX IF EXISTS public.idx_model_config_audit_changed_at;
DROP TABLE IF EXISTS public.model_config_audit;

-- Strip operator-set model rows so the system falls back to env / defaults.
DELETE FROM public.system_config
 WHERE namespace = 'chart_hacker'
   AND config_key IN ('model.primary', 'model.fallback', 'model.prefilter');
