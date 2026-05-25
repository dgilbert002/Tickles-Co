-- ============================================================================
-- Round 10 (2026-05-24) — Vision-model picker.
--
-- Adds an audit trail for changes to the chart_hacker.model.* config rows
-- in public.system_config. The actual model values themselves live in the
-- pre-existing public.system_config table (already created by Phase 0).
-- We only need a NEW table for the audit history.
--
-- Why an audit table?
--   The Settings panel has no auth (private server), so we can't capture
--   "who" changed it, but we can capture "when" and "what". This gives us
--   a forensic trail when extraction quality changes mysteriously: we can
--   correlate a quality dip with the moment a model was swapped.
--
-- Idempotent — safe to re-run.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.model_config_audit (
    id          BIGSERIAL PRIMARY KEY,
    -- One of the slot identifiers from
    -- shared/intelligence/model_config.py: 'primary' | 'fallback' | 'prefilter'.
    slot        VARCHAR(32) NOT NULL,
    -- Previous OpenRouter model id (NULL on first ever set for this slot).
    model_old   TEXT,
    -- New OpenRouter model id.
    model_new   TEXT NOT NULL,
    -- Free-form actor label. We don't have user auth on the panel so this
    -- is typically 'dashboard-anon' or similar. Kept as TEXT for forward
    -- compatibility (when we add auth later we can stuff a session id here).
    actor_label VARCHAR(128),
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT model_config_audit_slot_chk
      CHECK (slot IN ('primary', 'fallback', 'prefilter'))
);

CREATE INDEX IF NOT EXISTS idx_model_config_audit_changed_at
    ON public.model_config_audit (changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_model_config_audit_slot_changed
    ON public.model_config_audit (slot, changed_at DESC);

COMMENT ON TABLE public.model_config_audit IS
    'Round 10 (2026-05-24): audit trail of vision-model swaps via the dashboard Settings panel. One row per change. Read by /api/settings/vision-model-history.';

-- Note: we deliberately do NOT pre-seed system_config rows here. The
-- model_config module resolves DB → env → code-default, so a missing row
-- just means "use the code default", which on Round 10 is qwen3-vl-32b /
-- claude-sonnet-4 / gemini-2.5-flash. Operators who want to override that
-- pick a model in the Settings dropdown and the row appears.
