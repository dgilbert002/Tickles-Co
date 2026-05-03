-- ============================================================================
-- Phase 11 — Platform-Agnostic Edge Score + actor_performance + CoachService
-- ============================================================================
-- Date: 2026-05-03
-- Author: tickles-intelligence
--
-- [BE] edge_score formula — available-component normalisation
-- [BF] Minimum-sample gate — closed_position_count >= 3 for leaderboard
-- [BG] CoachService prompt A/B ledger
-- [K] trader_performance reconciliation — dual-write during 90-day window
-- ============================================================================

-- ============================================================================
-- 1. actor_performance — Cross-actor performance rollup (replaces
--    trader_performance for new consumers; trader_performance kept for
--    90-day backwards compat per Phase 11 §E)
-- ============================================================================
CREATE TABLE IF NOT EXISTS actor_performance (
    id                      BIGSERIAL PRIMARY KEY,
    actor_type              TEXT NOT NULL,
    actor_id                TEXT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,                 -- exclusive
    closed_position_count   INT  NOT NULL,
    edge_score              NUMERIC(5,4) NOT NULL,         -- [0.0000, 1.0000]
    components_jsonb        JSONB NOT NULL,                -- per-component score + available flag
    weights_used_jsonb      JSONB NOT NULL,                -- weights AFTER renormalisation
    confidence_low          BOOLEAN NOT NULL DEFAULT FALSE,
    formula_version         INT NOT NULL,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (actor_type, actor_id, period_start, period_end, formula_version)
);

CREATE INDEX IF NOT EXISTS idx_actor_perf_lookup
    ON actor_performance (actor_type, actor_id, period_end DESC);

CREATE INDEX IF NOT EXISTS idx_actor_perf_period
    ON actor_performance (period_start, period_end, edge_score DESC);

-- ============================================================================
-- 2. actor_leaderboard view — ranked, hides rows with < 3 closed positions
-- ============================================================================
CREATE OR REPLACE VIEW actor_leaderboard AS
SELECT
    actor_type, actor_id, period_start, period_end,
    closed_position_count, edge_score, confidence_low,
    components_jsonb, formula_version,
    RANK() OVER (PARTITION BY period_start, period_end ORDER BY edge_score DESC, closed_position_count DESC) AS rank
FROM actor_performance
WHERE closed_position_count >= 3;     -- BF: hide pure-noise rows entirely

-- ============================================================================
-- 3. edge_score_changes — audit trail for > 0.05 day-over-day delta
-- ============================================================================
CREATE TABLE IF NOT EXISTS edge_score_changes (
    id           BIGSERIAL PRIMARY KEY,
    actor_type   TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    period_end   DATE NOT NULL,
    score_before NUMERIC(5,4),
    score_after  NUMERIC(5,4) NOT NULL,
    delta        NUMERIC(6,4) NOT NULL,
    components_before JSONB,
    components_after  JSONB NOT NULL,
    note         TEXT NULL,              -- e.g. 'prompt_promoted'
    logged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_edge_score_changes_actor
    ON edge_score_changes (actor_type, actor_id, period_end DESC);

-- ============================================================================
-- 4. prompt_assignments — CoachService [BG] variant assignment ledger
-- ============================================================================
CREATE TABLE IF NOT EXISTS prompt_assignments (
    id           BIGSERIAL PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    assignment_day DATE NOT NULL,
    prompt_name  TEXT NOT NULL,           -- e.g. 'chart_analysis'
    variant      TEXT NOT NULL,           -- e.g. 'v1' or 'v2'
    prompt_hash  CHAR(16) NOT NULL,       -- references prompt_versions
    UNIQUE (actor_id, assignment_day, prompt_name)
);

CREATE INDEX IF NOT EXISTS idx_prompt_assignments_lookup
    ON prompt_assignments (actor_id, prompt_name, assignment_day DESC);

-- ============================================================================
-- 5. Deprecation note on trader_performance (Phase 11 §E)
-- ============================================================================
-- trader_performance is DEPRECATED as of 2026-05-03.
-- EdgeScorer dual-writes Discord-actor rows for 90-day backwards compat.
-- After 2026-08-01, dual-write stops and table is truncated (kept as empty
-- shell so \d and any forgotten consumer don't 500).
-- See shared/docs/TICKLES_INFRASTRUCTURE_ATLAS.md §3.2 for mapping.
-- ============================================================================

SELECT 'Phase 11 migration complete' AS status;
