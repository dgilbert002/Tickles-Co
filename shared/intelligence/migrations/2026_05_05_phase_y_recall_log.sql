-- ============================================================================
-- Phase Y.1 — mem0_recall_log table
-- ============================================================================
-- Date:    2026-05-05
-- Plan:    shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.4
-- Target:  tickles_<company> (per-company)
--
-- Records every mem0 query made at decision time and what came back.
-- Tiered match flags are populated retroactively by postmortem_service.py
-- one row at a time when the corresponding tracked_position closes.
--
-- Used by C4 of compute_skill_score (in 2026_05_05_phase_y_skill_views.sql).
-- This migration MUST be applied BEFORE that one is exercised against real data.
-- ============================================================================

CREATE TABLE IF NOT EXISTS mem0_recall_log (
    id                BIGSERIAL    PRIMARY KEY,

    -- Who/what asked
    actor_id          TEXT         NOT NULL,
    company_id        TEXT         NOT NULL,
    correlation_id    TEXT,                              -- joins api_cost_log + payload_store

    -- Query shape
    query_summary     TEXT         NOT NULL,             -- raw signal text passed to mem.search()
    query_dimension   TEXT,                              -- 'lesson' / 'warning' / 'postmortem' / etc
    query_symbol      TEXT,                              -- slash form, e.g. 'BTC/USDT'

    -- Result shape
    returned_count    INT          NOT NULL DEFAULT 0,
    top_k_ids         JSONB        NOT NULL DEFAULT '[]'::jsonb,
    top_k_metadata    JSONB        NOT NULL DEFAULT '[]'::jsonb,

    -- Tiered match flags — populated retroactively when position closes.
    -- Each is the OR over top_k results: TRUE if ANY returned memory matched on that axis.
    match_outcome     BOOLEAN,                           -- ANY top_k.metadata.outcome = pos.outcome
    match_dim         BOOLEAN,                           -- ANY top_k.metadata.dimension = query_dimension
    match_symbol      BOOLEAN,                           -- ANY top_k.metadata.symbol = pos.instrument_symbol

    -- Cross-references
    position_id       BIGINT,                            -- tracked_positions.id (NULL until linked)
    matched_at        TIMESTAMPTZ,                       -- when retroactive UPDATE landed
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Sanity: matched_at and the match_* flags are set together by
    -- postmortem_service.py — either all-NULL ("pending") or all-non-NULL
    -- ("resolved"). This rules out the nonsense state of TRUE flags with
    -- NULL matched_at (and vice versa) which would silently break the
    -- C4 query's "WHERE match_outcome IS NOT NULL" pending-filter.
    CONSTRAINT ck_mem0_recall_match_consistency CHECK (
        (matched_at IS NULL AND match_outcome IS NULL
            AND match_dim IS NULL AND match_symbol IS NULL)
        OR
        (matched_at IS NOT NULL AND match_outcome IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_mem0_recall_actor_window
    ON mem0_recall_log (actor_id, company_id, created_at DESC);

-- Partial UNIQUE index: a single resolved recall row per (actor, position).
-- Pending rows (position_id IS NULL) are unconstrained. Prevents duplicate
-- retroactive matches if postmortem_service.py is retried after a partial
-- failure — without this, a duplicate would silently inflate C4.
--
-- Bug-hunt note (Y.2): the constraint must include actor_id, NOT position_id
-- alone. C4 of compute_skill_score aggregates per (actor_id, company_id) over
-- a time window — it does NOT filter by position_id. In multi-agent scenarios
-- (agent A and agent B both recall mem0 for the same position 999) each
-- actor needs its own resolved recall row to get credit in their own C4
-- average. A bare UNIQUE (position_id) would silently drop every recall
-- after the first, regardless of actor.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mem0_recall_position
    ON mem0_recall_log (actor_id, position_id) WHERE position_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_mem0_recall_correlation
    ON mem0_recall_log (correlation_id) WHERE correlation_id IS NOT NULL;

COMMENT ON TABLE mem0_recall_log IS
    'Phase Y.1 §3.4 — every mem0 recall at decision time. C4 of compute_skill_score reads this.';
COMMENT ON COLUMN mem0_recall_log.match_outcome IS
    'TRUE if ANY top_k result.metadata.outcome equals the closed position outcome. NULL = pending close.';
COMMENT ON COLUMN mem0_recall_log.match_dim IS
    'TRUE if ANY top_k result.metadata.dimension equals query_dimension.';
COMMENT ON COLUMN mem0_recall_log.match_symbol IS
    'TRUE if ANY top_k result.metadata.symbol equals the closed position instrument_symbol.';

-- ----------------------------------------------------------------------------
-- Done
-- ----------------------------------------------------------------------------
SELECT 'Phase Y.1 mem0_recall_log migration complete' AS status,
       (SELECT COUNT(*) FROM pg_tables WHERE tablename = 'mem0_recall_log') AS table_exists;
