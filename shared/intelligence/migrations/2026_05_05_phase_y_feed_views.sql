-- ============================================================================
-- Phase Y.1 — Memory Feed views (7d / 14d / 30d)
-- ============================================================================
-- Date:    2026-05-05
-- Plan:    shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md §4.1
-- Target:  tickles_<company> (per-company)
--
-- Three windowed UNION-ALL views over the Postgres-native event sources for
-- the dashboard's Memory Feed tab.
--
-- Sources merged here (Postgres-only):
--   - position_postmortems lessons_for_actor   → 'postmortem_actor' rows
--   - position_postmortems lessons_for_company → 'postmortem_company' rows
--   - edge_score_changes                       → 'promotion' / 'edge_change' rows
--
-- NOT merged here (not in Postgres — handled in Python by the snapshot provider):
--   - agent_events  (lives in ClickHouse — see shared/migration/clickhouse_schema.sql)
--   - mem0          (lives in Qdrant)
--   - MemU          (lives in the memu Postgres DB, separate connection)
--   - memu_outbox   (lives in the memu Postgres DB, separate connection)
--
-- Per-window views (instead of a single parameterised function) so the planner
-- can use indexes on `closed_at`/`logged_at` per window.
--
-- ----------------------------------------------------------------------------
-- DEVIATIONS from the plan SQL (§4.1) verified against real DDL:
--   1. position_postmortems has NO actor_instance / company_id columns.
--      Must JOIN to tracked_positions on position_id.
--   2. position_postmortems.lessons_for_actor and .lessons_for_company are
--      TEXT, not JSONB arrays. Each non-NULL/non-empty TEXT becomes ONE feed
--      row (no jsonb_array_elements expansion). Phase Y.next may upgrade to
--      JSONB-array if the postmortem prompt starts emitting structured lists.
--   3. edge_score_changes columns are (actor_type, actor_id, period_end,
--      score_before, score_after, delta, components_before, components_after,
--      note, logged_at). The plan's (event_type, old_score, new_score,
--      metadata, created_at, reason) do not exist. Adapted accordingly.
--   4. edge_score_changes has NO company_id; the table is per-company so we
--      synthesise the value from tracked_positions in a JOIN — but actor_id
--      may not be in tracked_positions for system-level rows, so we use a
--      LEFT JOIN to derive a best-effort company_id, falling back to
--      current_database() string for orphan rows.
--   5. agent_events lane DROPPED entirely (ClickHouse). Snapshot provider
--      will UNION it in Python; see shared/intelligence/feed_snapshot.py
--      (Phase Y.3).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Helper: derive company_id for edge_score_changes (which lacks the column).
-- We pick the modal company_id of that actor's tracked_positions; if the actor
-- has no positions yet we fall back to the database name (ASCII-trimmed).
-- This is acceptable for a dashboard view — accuracy isn't life-critical.
-- ----------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_memory_feed_7d AS
WITH events AS (
    -- ----- Postmortem lessons-for-actor -----
    SELECT 'postmortem_actor'::text   AS source_kind,
           'tier1'::text                AS tier,
           tp.actor_id                  AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_actor         AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':actor') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '7 days'
       AND pp.lessons_for_actor IS NOT NULL
       AND length(trim(pp.lessons_for_actor)) > 0

    UNION ALL

    -- ----- Postmortem lessons-for-company -----
    SELECT 'postmortem_company'::text  AS source_kind,
           'tier2'::text                AS tier,
           'shared'::text               AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_company       AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':company') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '7 days'
       AND pp.lessons_for_company IS NOT NULL
       AND length(trim(pp.lessons_for_company)) > 0

    UNION ALL

    -- ----- Edge-score changes / prompt promotions -----
    SELECT CASE WHEN esc.note = 'prompt_promoted' THEN 'promotion' ELSE 'edge_change' END
                                        AS source_kind,
           'tier2'::text                AS tier,
           esc.actor_id                 AS actor,
           COALESCE(
               actor_co.company_id,
               regexp_replace(current_database(), '^tickles_', '')
           )                            AS company,
           COALESCE(esc.note, 'edge_change') AS dimension,
           format(
               'edge %s → %s (Δ%s%s)',
               COALESCE(esc.score_before::text, 'null'),
               esc.score_after::text,
               esc.delta::text,
               CASE WHEN esc.note IS NOT NULL THEN ' — ' || esc.note ELSE '' END
           )                            AS body,
           jsonb_build_object(
               'actor_type',            esc.actor_type,
               'period_end',            esc.period_end,
               'score_before',          esc.score_before,
               'score_after',           esc.score_after,
               'delta',                 esc.delta,
               'components_before',     esc.components_before,
               'components_after',      esc.components_after,
               'note',                  esc.note
           )                            AS raw,
           esc.logged_at                AS ts,
           ('esc:' || esc.id::text)     AS source_id,
           NULL::text                   AS correlation_id
      FROM edge_score_changes esc
      LEFT JOIN LATERAL (
          SELECT company_id
            FROM tracked_positions
           WHERE actor_id   = esc.actor_id
             AND actor_type = esc.actor_type
             AND company_id IS NOT NULL
           GROUP BY company_id
           ORDER BY count(*) DESC
           LIMIT 1
      ) actor_co ON TRUE
     WHERE esc.logged_at > now() - interval '7 days'
)
SELECT * FROM events;

COMMENT ON VIEW v_memory_feed_7d IS
    'Phase Y.1 §4.1 — 7d Postgres-native memory-feed UNION. mem0/MemU/agent_events merged in Python.';

CREATE OR REPLACE VIEW v_memory_feed_14d AS
WITH events AS (
    SELECT 'postmortem_actor'::text   AS source_kind,
           'tier1'::text                AS tier,
           tp.actor_id                  AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_actor         AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':actor') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '14 days'
       AND pp.lessons_for_actor IS NOT NULL
       AND length(trim(pp.lessons_for_actor)) > 0
    UNION ALL
    SELECT 'postmortem_company'::text  AS source_kind,
           'tier2'::text                AS tier,
           'shared'::text               AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_company       AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':company') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '14 days'
       AND pp.lessons_for_company IS NOT NULL
       AND length(trim(pp.lessons_for_company)) > 0
    UNION ALL
    SELECT CASE WHEN esc.note = 'prompt_promoted' THEN 'promotion' ELSE 'edge_change' END
                                        AS source_kind,
           'tier2'::text                AS tier,
           esc.actor_id                 AS actor,
           COALESCE(
               actor_co.company_id,
               regexp_replace(current_database(), '^tickles_', '')
           )                            AS company,
           COALESCE(esc.note, 'edge_change') AS dimension,
           format(
               'edge %s → %s (Δ%s%s)',
               COALESCE(esc.score_before::text, 'null'),
               esc.score_after::text,
               esc.delta::text,
               CASE WHEN esc.note IS NOT NULL THEN ' — ' || esc.note ELSE '' END
           )                            AS body,
           jsonb_build_object(
               'actor_type',            esc.actor_type,
               'period_end',            esc.period_end,
               'score_before',          esc.score_before,
               'score_after',           esc.score_after,
               'delta',                 esc.delta,
               'components_before',     esc.components_before,
               'components_after',      esc.components_after,
               'note',                  esc.note
           )                            AS raw,
           esc.logged_at                AS ts,
           ('esc:' || esc.id::text)     AS source_id,
           NULL::text                   AS correlation_id
      FROM edge_score_changes esc
      LEFT JOIN LATERAL (
          SELECT company_id
            FROM tracked_positions
           WHERE actor_id   = esc.actor_id
             AND actor_type = esc.actor_type
             AND company_id IS NOT NULL
           GROUP BY company_id
           ORDER BY count(*) DESC
           LIMIT 1
      ) actor_co ON TRUE
     WHERE esc.logged_at > now() - interval '14 days'
)
SELECT * FROM events;

COMMENT ON VIEW v_memory_feed_14d IS
    'Phase Y.1 §4.1 — 14d Postgres-native memory-feed UNION.';

CREATE OR REPLACE VIEW v_memory_feed_30d AS
WITH events AS (
    SELECT 'postmortem_actor'::text   AS source_kind,
           'tier1'::text                AS tier,
           tp.actor_id                  AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_actor         AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':actor') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '30 days'
       AND pp.lessons_for_actor IS NOT NULL
       AND length(trim(pp.lessons_for_actor)) > 0
    UNION ALL
    SELECT 'postmortem_company'::text  AS source_kind,
           'tier2'::text                AS tier,
           'shared'::text               AS actor,
           tp.company_id                AS company,
           'lesson'::text               AS dimension,
           pp.lessons_for_company       AS body,
           jsonb_build_object(
               'postmortem_id',         pp.id,
               'position_id',           pp.position_id,
               'instrument_symbol',     tp.instrument_symbol,
               'outcome',               tp.outcome,
               'prompt_version',        pp.prompt_version
           )                            AS raw,
           pp.created_at                AS ts,
           ('pm:' || pp.id::text || ':company') AS source_id,
           pp.correlation_id            AS correlation_id
      FROM position_postmortems pp
      JOIN tracked_positions tp ON tp.id = pp.position_id
     WHERE pp.created_at > now() - interval '30 days'
       AND pp.lessons_for_company IS NOT NULL
       AND length(trim(pp.lessons_for_company)) > 0
    UNION ALL
    SELECT CASE WHEN esc.note = 'prompt_promoted' THEN 'promotion' ELSE 'edge_change' END
                                        AS source_kind,
           'tier2'::text                AS tier,
           esc.actor_id                 AS actor,
           COALESCE(
               actor_co.company_id,
               regexp_replace(current_database(), '^tickles_', '')
           )                            AS company,
           COALESCE(esc.note, 'edge_change') AS dimension,
           format(
               'edge %s → %s (Δ%s%s)',
               COALESCE(esc.score_before::text, 'null'),
               esc.score_after::text,
               esc.delta::text,
               CASE WHEN esc.note IS NOT NULL THEN ' — ' || esc.note ELSE '' END
           )                            AS body,
           jsonb_build_object(
               'actor_type',            esc.actor_type,
               'period_end',            esc.period_end,
               'score_before',          esc.score_before,
               'score_after',           esc.score_after,
               'delta',                 esc.delta,
               'components_before',     esc.components_before,
               'components_after',      esc.components_after,
               'note',                  esc.note
           )                            AS raw,
           esc.logged_at                AS ts,
           ('esc:' || esc.id::text)     AS source_id,
           NULL::text                   AS correlation_id
      FROM edge_score_changes esc
      LEFT JOIN LATERAL (
          SELECT company_id
            FROM tracked_positions
           WHERE actor_id   = esc.actor_id
             AND actor_type = esc.actor_type
             AND company_id IS NOT NULL
           GROUP BY company_id
           ORDER BY count(*) DESC
           LIMIT 1
      ) actor_co ON TRUE
     WHERE esc.logged_at > now() - interval '30 days'
)
SELECT * FROM events;

COMMENT ON VIEW v_memory_feed_30d IS
    'Phase Y.1 §4.1 — 30d Postgres-native memory-feed UNION. Surfaced as "1M" in dashboard.';

-- ----------------------------------------------------------------------------
-- Done
-- ----------------------------------------------------------------------------
SELECT 'Phase Y.1 feed_views migration complete' AS status,
       (SELECT COUNT(*) FROM pg_views WHERE viewname IN
            ('v_memory_feed_7d', 'v_memory_feed_14d', 'v_memory_feed_30d')) AS feed_views_count;
