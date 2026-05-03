-- ============================================================================
-- Phase Y.1 — skill_weight_recommendations table ("Ask AI" audit trail)
-- ============================================================================
-- Date:    2026-05-05
-- Plan:    shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.5 + §11 Q1
-- Target:  tickles_<company> (per-company)
--
-- One row per click of the "Ask AI" button on the skill-score panel.
-- Stores the LLM's proposed reweighting + audit metadata.
-- Applying the recommendation is a separate manual Architect-mode step
-- (the row's `applied` flag is just a marker, NOT a trigger).
-- ============================================================================

CREATE TABLE IF NOT EXISTS skill_weight_recommendations (
    id                  BIGSERIAL    PRIMARY KEY,

    -- Scope
    company_id          TEXT         NOT NULL,
    requested_by        TEXT         NOT NULL,           -- user/session id from manage panel auth
    window_days         INT          NOT NULL
        CHECK (window_days IN (7, 14, 30)),

    -- Sample size at request time
    n_actors            INT          NOT NULL,           -- |actors with skill_score not NULL|
    n_trades            INT          NOT NULL,           -- |closed trades in window|

    -- Frozen current weights (before this recommendation)
    current_weights     JSONB        NOT NULL,           -- {"clarity":0.30,"consistency":0.25,...}

    -- LLM-proposed weights + per-component CI
    proposed_weights    JSONB        NOT NULL,           -- {"clarity":0.28, ...} — must sum to ~1.0
    confidence_band     JSONB        NOT NULL,           -- {"clarity":[0.24,0.32], ...}
    rationale           TEXT,                            -- LLM-emitted human-readable summary

    -- Provenance
    model_provider      TEXT         NOT NULL,           -- 'openrouter' / 'openai' / 'anthropic'
    model_name          TEXT         NOT NULL,
    correlation_id      TEXT,                            -- joins api_cost_log
    cost_usd            NUMERIC(10,6),

    -- Adoption marker (NOT a trigger — Architect must manually edit the SQL)
    applied             BOOLEAN      NOT NULL DEFAULT FALSE,
    applied_at          TIMESTAMPTZ,
    applied_by          TEXT,                            -- who flipped the flag

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Sanity: applied_at must be set when applied=TRUE
    CONSTRAINT ck_swr_applied_consistency CHECK (
        (applied = FALSE AND applied_at IS NULL)
        OR (applied = TRUE AND applied_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_skill_weight_rec_company_recent
    ON skill_weight_recommendations (company_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_skill_weight_rec_applied
    ON skill_weight_recommendations (company_id, applied_at DESC)
    WHERE applied = TRUE;

CREATE INDEX IF NOT EXISTS idx_skill_weight_rec_correlation
    ON skill_weight_recommendations (correlation_id) WHERE correlation_id IS NOT NULL;

COMMENT ON TABLE skill_weight_recommendations IS
    'Phase Y.1 §3.5 — audit trail for "Ask AI" weight-recalibration suggestions. '
    'Applying does NOT mutate compute_skill_score(); a follow-up Architect migration must.';
COMMENT ON COLUMN skill_weight_recommendations.applied IS
    'Marker only. Setting TRUE does not change the live weights — that requires a manual SQL migration.';

-- ----------------------------------------------------------------------------
-- Done
-- ----------------------------------------------------------------------------
SELECT 'Phase Y.1 skill_weight_recommendations migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE tablename = 'skill_weight_recommendations') AS table_exists;
