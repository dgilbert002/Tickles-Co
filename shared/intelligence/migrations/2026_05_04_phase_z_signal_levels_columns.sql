-- ===========================================================================
-- Phase Z — Signal levels mirrored into dedicated columns
-- ===========================================================================
-- Until now ``signal_interpretations.llm_levels`` (JSONB) was the only home
-- for entry/SL/TP values extracted by the LLM. That makes:
--   * the Signals dashboard table unable to render levels without parsing
--     JSONB on every row,
--   * downstream live/actual-vs-signal tracking expensive (every consumer
--     re-parses JSON), and
--   * indexed range queries (e.g. "signals with entry above current price")
--     impossible without a btree on a generated column.
--
-- This migration adds dedicated NUMERIC columns mirrored from ``llm_levels``
-- at INSERT time by ``interpretation_service.py``. The JSONB stays as the
-- canonical write-time payload; the columns are a denormalised read model.
--
-- Columns:
--   entry_price       — primary entry trigger
--   stop_loss         — protective stop
--   take_profit_1..6  — laddered TP targets
--
-- All NULL-able because pre-existing rows have no level data and because
-- the LLM may legitimately omit a level. Backfill is performed by a single
-- UPDATE that reads the existing JSONB.
--
-- Forward-only — see paired _ROLLBACK.sql.
-- ===========================================================================

ALTER TABLE public.signal_interpretations
    ADD COLUMN IF NOT EXISTS entry_price       NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS stop_loss         NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_1     NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_2     NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_3     NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_4     NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_5     NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS take_profit_6     NUMERIC(20, 8);

-- Index for "signals with non-null entry" queries used by the Signals tab.
CREATE INDEX IF NOT EXISTS idx_interp_entry_price
    ON public.signal_interpretations (entry_price)
    WHERE entry_price IS NOT NULL;

-- ---------------------------------------------------------------------------
-- One-shot backfill: hydrate the new columns from existing llm_levels JSONB.
--
-- ``llm_levels`` is a moving target — the LLM has historically emitted:
--   * pure JSON numbers (12345.67),
--   * decimal strings ("12345.67"),
--   * formatted strings with currency / units ("$12,345.67"),
--   * ranges ("0.2104-0.2110"),
--   * comma-separated lists ("12345.67, 12400.00").
--
-- A naive ``REGEXP_REPLACE(..., '[^0-9.\-]', '', 'g')::NUMERIC`` cast blows
-- up on ranges/lists because the resulting string still contains a hyphen
-- or a stray dot. To stay safe we extract the FIRST signed decimal token
-- via ``SUBSTRING(... FROM '...regex...')`` and only then cast. Anything
-- that doesn't match the regex resolves to NULL, which is the correct
-- "no data" outcome.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION pg_temp.phase_z_first_numeric(txt TEXT)
RETURNS NUMERIC
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    token TEXT;
    val NUMERIC;
BEGIN
    IF txt IS NULL OR txt = '' THEN
        RETURN NULL;
    END IF;
    -- Match: optional leading minus, digits, optional ``.digits``.
    token := SUBSTRING(txt FROM '-?[0-9]+(?:\.[0-9]+)?');
    IF token IS NULL OR token = '' OR token = '-' THEN
        RETURN NULL;
    END IF;
    BEGIN
        val := token::NUMERIC;
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
    IF val = 0 THEN
        RETURN NULL;
    END IF;
    RETURN val;
END
$$;

UPDATE public.signal_interpretations si
SET
    entry_price   = pg_temp.phase_z_first_numeric(si.llm_levels->>'entry'),
    stop_loss     = pg_temp.phase_z_first_numeric(si.llm_levels->>'stop_loss'),
    take_profit_1 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_1'),
    take_profit_2 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_2'),
    take_profit_3 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_3'),
    take_profit_4 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_4'),
    take_profit_5 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_5'),
    take_profit_6 = pg_temp.phase_z_first_numeric(si.llm_levels->>'take_profit_6')
WHERE si.llm_levels IS NOT NULL
  AND si.entry_price IS NULL
  AND si.stop_loss IS NULL
  AND si.take_profit_1 IS NULL;

COMMENT ON COLUMN public.signal_interpretations.entry_price
    IS 'Phase Z: denormalised entry trigger mirrored from llm_levels.entry at INSERT.';
COMMENT ON COLUMN public.signal_interpretations.stop_loss
    IS 'Phase Z: denormalised stop-loss mirrored from llm_levels.stop_loss at INSERT.';
COMMENT ON COLUMN public.signal_interpretations.take_profit_1
    IS 'Phase Z: denormalised TP1 mirrored from llm_levels.take_profit_1 at INSERT.';
