-- =============================================================================
-- Migration: 2026_05_05_phase_y0_insight_kinds_check.sql
-- Database:  memu  (cross-company institutional memory)
-- Purpose:   Phase Y.0 — retrofit a CHECK constraint on memu.insights.kind
--            so the DB enforces the canonical insight-kind enum that
--            shared/memu/insight_kinds.py declares as the single source of
--            truth (BroadcastPayload + MCP tool schemas + this DDL).
--
-- Pre-existing deployments did not have this constraint (the DDL in
-- shared/memu/client.py originally created the column with no CHECK).
-- This migration is idempotent: it drops the constraint by name first,
-- so re-running with an updated kind set is safe.
--
-- Canonical kind set (sorted): anomaly, lesson, playbook, postmortem,
-- regime_shift, warning. Keep in sync with INSIGHT_KINDS in
-- shared/memu/insight_kinds.py.
-- =============================================================================

BEGIN;

-- 1. Audit: any rows that would violate the new constraint? Surface them
--    loudly before we add the constraint, so an operator can decide to
--    delete / re-classify them rather than have the ALTER TABLE fail.
DO $$
DECLARE
    bad_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO bad_count
    FROM insights
    WHERE kind NOT IN (
        'anomaly', 'lesson', 'playbook', 'postmortem', 'regime_shift', 'warning'
    );
    IF bad_count > 0 THEN
        RAISE EXCEPTION
            'Phase Y.0 migration aborted: % insight rows have kinds outside the canonical set. '
            'Inspect with: SELECT DISTINCT kind FROM insights WHERE kind NOT IN ('
            '''anomaly'',''lesson'',''playbook'',''postmortem'',''regime_shift'',''warning'');',
            bad_count;
    END IF;
END
$$;

-- 2. Drop the constraint if a previous version exists (idempotent re-runs).
ALTER TABLE insights
    DROP CONSTRAINT IF EXISTS ck_insights_kind_enum;

-- 3. Add the canonical CHECK constraint.
ALTER TABLE insights
    ADD CONSTRAINT ck_insights_kind_enum
    CHECK (kind IN (
        'anomaly', 'lesson', 'playbook', 'postmortem', 'regime_shift', 'warning'
    ));

COMMIT;

-- Verification query (run manually after migration):
--   SELECT conname, pg_get_constraintdef(oid)
--   FROM pg_constraint
--   WHERE conrelid = 'insights'::regclass
--     AND conname = 'ck_insights_kind_enum';
