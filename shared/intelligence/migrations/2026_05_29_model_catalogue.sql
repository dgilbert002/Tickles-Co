-- ============================================================================
-- Round 14 (2026-05-29) — Model Catalogue + Provider-aware slot picker.
--
-- 1. public.model_catalogue — a DB-backed cache of the models AND routing
--    policies available from each provider (OpenRouter + Requesty). Synced on
--    dashboard startup, on a daily timer, and via a manual "Refresh" button.
--    The dashboard Settings picker reads this table so the operator scrolls a
--    LIVE list (with cost + context + vision flag) instead of a hand-typed JSON.
--
-- 2. Relax the model_config_audit slot CHECK so the new text-service slots
--    (postmortem/guru/mcp/text_extract/chart_hacker_opinion) can be audited
--    too. Round 10 limited it to the 3 vision slots.
--
-- Idempotent — safe to re-run.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.model_catalogue (
    id                    BIGSERIAL PRIMARY KEY,
    -- 'openrouter' | 'requesty'
    provider              VARCHAR(32)  NOT NULL,
    -- The id passed verbatim to /chat/completions
    -- (e.g. 'anthropic/claude-sonnet-4' or 'policy/tickles-vision').
    model_id              TEXT         NOT NULL,
    -- Human-friendly name for the dropdown.
    label                 TEXT,
    -- True for Requesty routing policies (id starts 'policy/'). The UI pins
    -- these to the top of the list.
    is_policy             BOOLEAN      NOT NULL DEFAULT FALSE,
    -- True if the model can read images. Vision slots filter to these.
    is_vision             BOOLEAN      NOT NULL DEFAULT FALSE,
    -- Max context window in tokens (NULL if unknown).
    context_length        INTEGER,
    -- Normalised pricing: USD per 1,000,000 tokens. NULL for policies (cost
    -- depends on the underlying model the policy selects at inference time).
    input_cost_per_mtok   NUMERIC,
    output_cost_per_mtok  NUMERIC,
    -- Full raw provider record for forward-compatibility / debugging.
    raw                   JSONB,
    synced_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT model_catalogue_provider_chk
      CHECK (provider IN ('openrouter', 'requesty')),
    CONSTRAINT model_catalogue_uniq UNIQUE (provider, model_id)
);

CREATE INDEX IF NOT EXISTS idx_model_catalogue_provider
    ON public.model_catalogue (provider);
CREATE INDEX IF NOT EXISTS idx_model_catalogue_vision
    ON public.model_catalogue (is_vision);
CREATE INDEX IF NOT EXISTS idx_model_catalogue_policy
    ON public.model_catalogue (is_policy);

COMMENT ON TABLE public.model_catalogue IS
    'Round 14 (2026-05-29): synced cache of OpenRouter + Requesty models and routing policies. Powers the dashboard Settings model picker. Refreshed on startup / daily / manual.';

-- ---------------------------------------------------------------------------
-- Relax the Round-10 audit CHECK so all picker slots can be audited.
-- ---------------------------------------------------------------------------
ALTER TABLE public.model_config_audit
    DROP CONSTRAINT IF EXISTS model_config_audit_slot_chk;

ALTER TABLE public.model_config_audit
    ADD CONSTRAINT model_config_audit_slot_chk
    CHECK (slot IN (
        'primary', 'fallback', 'prefilter',
        'postmortem', 'guru', 'mcp', 'text_extract', 'chart_hacker_opinion'
    ));
