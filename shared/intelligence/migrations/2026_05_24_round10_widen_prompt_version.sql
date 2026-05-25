-- ============================================================================
-- Round 10 (2026-05-24) — Widen prompt_version columns from VARCHAR(32) to VARCHAR(64).
--
-- Why this exists
-- ---------------
-- Round 9 (2026-05-24) bumped the chart-analysis prompt to version
--   '2026.05.24-trader-explicit-only-v1'   (34 characters).
-- The signal_interpretations.prompt_version column is VARCHAR(32). asyncpg's
-- COPY/BIND raises StringDataRightTruncationError on every INSERT, which the
-- daemon catches and logs as 'failed', losing the interpretation result.
--
-- Net effect: every successful LLM call since Round 9 deployed has been
-- silently dropped. About 80 calls, mostly to Claude Sonnet 4 (~$2.12 wasted)
-- before Round 10 caught it.
--
-- Fix: widen the column to 64 chars. Round 11+ won't have the same issue
-- without first bumping the schema.
--
-- Idempotent: ALTER COLUMN ... TYPE is a no-op if the column is already 64.
-- ============================================================================

ALTER TABLE public.signal_interpretations
    ALTER COLUMN prompt_version TYPE VARCHAR(64);

-- Same risk in any sister tables that copy this column shape. Apply
-- defensively even if currently empty.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='signal_interpretations'
           AND column_name='prefilter_provider'
           AND character_maximum_length = 32
    ) THEN
        ALTER TABLE public.signal_interpretations
            ALTER COLUMN prefilter_provider TYPE VARCHAR(64),
            ALTER COLUMN prefilter_result   TYPE VARCHAR(64),
            ALTER COLUMN vision_provider    TYPE VARCHAR(64);
    END IF;
END
$$;

COMMENT ON COLUMN public.signal_interpretations.prompt_version IS
    'Round 10 (2026-05-24) widened from 32 to 64 chars to fit the Round 9 prompt version 2026.05.24-trader-explicit-only-v1.';
