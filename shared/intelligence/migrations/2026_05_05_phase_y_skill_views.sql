-- ============================================================================
-- Phase Y.1 — Skill-vs-Luck Composite (compute_skill_score + 3 views)
-- ============================================================================
-- Date:    2026-05-05
-- Plan:    shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.2
-- Target:  tickles_<company> (per-company; runs once per company DB)
--
-- Creates:
--   FUNCTION compute_skill_score(actor_id TEXT, company_id TEXT, window_days INT) RETURNS NUMERIC
--   VIEW     v_actor_skill_7d
--   VIEW     v_actor_skill_14d
--   VIEW     v_actor_skill_30d
--
-- Composition (weights MUST sum to 1.0):
--   C1 reasoning clarity   0.30
--   C2 consistency         0.25
--   C3 reason→outcome      0.20
--   C4 mem0 recall         0.15  (tiered, see §3.4)
--   C5 prompt lift         0.10
--
-- Returns NULL when n_trades < 5 ("insufficient data" — dashboard shows em-dash).
--
-- ----------------------------------------------------------------------------
-- DEVIATIONS from the plan SQL (verified against real DDL, 2026-05-05):
--   1. position_postmortems.pattern_confirmed is JSONB, not BOOLEAN.
--      C3 uses jsonb_typeof(...) = 'array' AND jsonb_array_length(...) > 0
--      to detect "patterns were confirmed".
--   2. edge_score_changes columns are (actor_type, actor_id, period_end,
--      score_before, score_after, delta, components_before, components_after,
--      note, logged_at) — NOT (event_type, old_score, new_score, metadata,
--      created_at). C5 uses note='prompt_promoted' as the promotion marker
--      and (score_after - score_before) for the lift.
--   3. edge_score_changes has no company_id column; the table lives in the
--      per-company DB so all rows are implicitly scoped. p_company_id is
--      accepted in the function signature for API symmetry but ignored in
--      the C5 query (with a comment).
--   4. mem0_recall_log is created in a sibling migration
--      (2026_05_05_phase_y_recall_log.sql) which MUST be applied before this
--      function is called. The function references it; until that migration
--      runs C4 will fail at execution. The CREATE OR REPLACE here does not
--      verify referenced tables exist.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- compute_skill_score — single source of truth for the formula
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION compute_skill_score(
    p_actor_id     TEXT,
    p_company_id   TEXT,
    p_window_days  INT
) RETURNS NUMERIC AS $$
DECLARE
    v_clarity        NUMERIC;
    v_consistency    NUMERIC;
    v_reason_corr    NUMERIC;
    v_recall_hit     NUMERIC;
    v_prompt_lift    NUMERIC;
    v_n_trades       INT;
    v_score          NUMERIC;
BEGIN
    -- Floor: need at least 5 closed trades in window or return NULL.
    SELECT count(*)
      INTO v_n_trades
      FROM tracked_positions tp
     WHERE tp.actor_id = p_actor_id
       AND tp.company_id = p_company_id
       AND tp.closed_at IS NOT NULL
       AND tp.closed_at > now() - make_interval(days => p_window_days)
       AND tp.realized_pnl_usd_final IS NOT NULL;

    IF v_n_trades IS NULL OR v_n_trades < 5 THEN
        RETURN NULL;
    END IF;

    -- C1: reasoning clarity = avg(min(1.0, len(what_happened) / 400)).
    SELECT COALESCE(
               avg(LEAST(1.0, length(pp.what_happened)::numeric / 400.0)),
               0
           )
      INTO v_clarity
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE tp.actor_id = p_actor_id
       AND tp.company_id = p_company_id
       AND tp.closed_at IS NOT NULL
       AND tp.closed_at > now() - make_interval(days => p_window_days);

    -- C2: consistency = inverse coefficient of variation of pnl_pct.
    SELECT
        CASE
            WHEN abs(avg(realized_pnl_pct)) < 0.0001 THEN 0
            WHEN stddev_pop(realized_pnl_pct) IS NULL  THEN 0
            ELSE LEAST(
                1.0,
                1.0 / (1.0 + stddev_pop(realized_pnl_pct) / abs(avg(realized_pnl_pct)))
            )
        END
      INTO v_consistency
      FROM tracked_positions
     WHERE actor_id   = p_actor_id
       AND company_id = p_company_id
       AND closed_at  IS NOT NULL
       AND closed_at  > now() - make_interval(days => p_window_days)
       AND realized_pnl_pct IS NOT NULL;

    -- C3: reason→outcome correlation.
    -- pattern_confirmed is JSONB (array) — non-empty array = patterns confirmed.
    -- Filter requires entry_reason_trader present AND reason-frozen (so we know the
    -- reason wasn't edited after the fact).
    SELECT COALESCE(
               avg(
                   CASE
                       WHEN pp.pattern_confirmed IS NOT NULL
                            AND jsonb_typeof(pp.pattern_confirmed) = 'array'
                            AND jsonb_array_length(pp.pattern_confirmed) > 0
                       THEN 1.0
                       ELSE 0.0
                   END
               ),
               0
           )
      INTO v_reason_corr
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE tp.actor_id = p_actor_id
       AND tp.company_id = p_company_id
       AND tp.closed_at IS NOT NULL
       AND tp.closed_at > now() - make_interval(days => p_window_days)
       AND tp.realized_pnl_pct IS NOT NULL
       AND tp.entry_reason_trader IS NOT NULL
       AND tp.entry_reason_frozen_at IS NOT NULL;

    -- C4: mem0 recall match-quality (tiered, per §11 Q2).
    -- match_outcome is set retroactively by postmortem_service.py one row per
    -- position close. Rows whose match_outcome IS NULL are excluded (still pending).
    SELECT COALESCE(
               avg(
                   CASE
                       WHEN match_dim AND match_outcome AND match_symbol THEN 1.0
                       WHEN match_dim AND match_outcome                  THEN 0.6
                       WHEN match_outcome                                 THEN 0.3
                       ELSE 0.0
                   END
               ),
               0
           )
      INTO v_recall_hit
      FROM mem0_recall_log
     WHERE actor_id      = p_actor_id
       AND company_id    = p_company_id
       AND created_at    > now() - make_interval(days => p_window_days)
       AND match_outcome IS NOT NULL;

    -- C5: prompt lift, sigmoid-clipped on (score_after - score_before).
    -- NOTE: edge_score_changes has no company_id column; the table is per-company
    -- so the scope is implicit. p_company_id is unused here for that reason.
    SELECT COALESCE(
               avg(
                   1.0 / (
                       1.0 + exp(
                           -greatest(-10, least(10, (esc.score_after - COALESCE(esc.score_before, 0)) * 5))
                       )
                   )
               ),
               0
           )
      INTO v_prompt_lift
      FROM edge_score_changes esc
     WHERE esc.actor_id  = p_actor_id
       AND esc.note      = 'prompt_promoted'
       AND esc.logged_at > now() - make_interval(days => p_window_days);

    -- Composite (fixed weights; component dropout / renormalisation lives in the
    -- Python mirror shared/intelligence/skill_scorer.py for callers that have
    -- per-component nullity information).
    v_score :=
          0.30 * COALESCE(v_clarity,     0)
        + 0.25 * COALESCE(v_consistency, 0)
        + 0.20 * COALESCE(v_reason_corr, 0)
        + 0.15 * COALESCE(v_recall_hit,  0)
        + 0.10 * COALESCE(v_prompt_lift, 0);

    RETURN GREATEST(0, LEAST(1, v_score));
END;
$$ LANGUAGE plpgsql STABLE;

COMMENT ON FUNCTION compute_skill_score(TEXT, TEXT, INT) IS
    'Phase Y.1 skill-vs-luck composite. Returns NULL when n_trades<5. '
    'Weights 0.30/0.25/0.20/0.15/0.10 (C1..C5). See PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.2.';

-- ----------------------------------------------------------------------------
-- Three convenience views — one per window
-- ----------------------------------------------------------------------------
-- The base set is DISTINCT (actor_id, company_id) over tracked_positions,
-- restricted to non-NULL actor_id (some legacy rows have actor_id NULL).

CREATE OR REPLACE VIEW v_actor_skill_7d AS
SELECT actors.actor_id,
       actors.company_id,
       compute_skill_score(actors.actor_id, actors.company_id, 7) AS skill_score,
       7 AS window_days
  FROM (
      SELECT DISTINCT actor_id, company_id
        FROM tracked_positions
       WHERE actor_id IS NOT NULL
         AND company_id IS NOT NULL
  ) actors;

COMMENT ON VIEW v_actor_skill_7d IS
    'Phase Y.1 — skill_score over trailing 7 days per actor. NULL = insufficient data.';

CREATE OR REPLACE VIEW v_actor_skill_14d AS
SELECT actors.actor_id,
       actors.company_id,
       compute_skill_score(actors.actor_id, actors.company_id, 14) AS skill_score,
       14 AS window_days
  FROM (
      SELECT DISTINCT actor_id, company_id
        FROM tracked_positions
       WHERE actor_id IS NOT NULL
         AND company_id IS NOT NULL
  ) actors;

COMMENT ON VIEW v_actor_skill_14d IS
    'Phase Y.1 — skill_score over trailing 14 days per actor. NULL = insufficient data.';

CREATE OR REPLACE VIEW v_actor_skill_30d AS
SELECT actors.actor_id,
       actors.company_id,
       compute_skill_score(actors.actor_id, actors.company_id, 30) AS skill_score,
       30 AS window_days
  FROM (
      SELECT DISTINCT actor_id, company_id
        FROM tracked_positions
       WHERE actor_id IS NOT NULL
         AND company_id IS NOT NULL
  ) actors;

COMMENT ON VIEW v_actor_skill_30d IS
    'Phase Y.1 — skill_score over trailing 30 days per actor. NULL = insufficient data. '
    'Surfaced in the dashboard with the cosmetic label "1M".';

-- ----------------------------------------------------------------------------
-- Done
-- ----------------------------------------------------------------------------
SELECT 'Phase Y.1 skill_views migration complete' AS status,
       (SELECT COUNT(*) FROM pg_proc WHERE proname = 'compute_skill_score') AS function_exists,
       (SELECT COUNT(*) FROM pg_views WHERE viewname IN
            ('v_actor_skill_7d', 'v_actor_skill_14d', 'v_actor_skill_30d')) AS skill_views_count;
