-- ============================================================================
-- ROLLBACK for 2026_05_29_model_catalogue.sql
-- ============================================================================

DROP TABLE IF EXISTS public.model_catalogue;

-- Restore the Round-10 vision-only slot CHECK.
ALTER TABLE public.model_config_audit
    DROP CONSTRAINT IF EXISTS model_config_audit_slot_chk;

ALTER TABLE public.model_config_audit
    ADD CONSTRAINT model_config_audit_slot_chk
    CHECK (slot IN ('primary', 'fallback', 'prefilter'));
