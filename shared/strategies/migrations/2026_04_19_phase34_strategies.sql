-- Phase 34: Strategy Composer.
--
-- The composer aggregates candidate trade signals from every upstream
-- producer (arb scanner, copy-trader, soul verdicts, future strategy
-- plugins) and turns them into a single canonical stream of
-- "intents". Each intent is audit-logged in public.strategy_intents
-- so operators can inspect exactly what the composer proposed, what
-- Treasury/Guardrails decided, and which execution order (if any) it
-- eventually produced.
--
-- Ownership: tickles_shared.

BEGIN;

-- ---------------------------------------------------------------- descriptors
CREATE TABLE IF NOT EXISTS public.strategy_descriptors (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL,               -- 'arb' | 'copy' | 'souls' | 'custom'
    description     TEXT NOT NULL DEFAULT '',
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    priority        INTEGER NOT NULL DEFAULT 100, -- higher = evaluated first
    config          JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS strategy_descriptors_unique_idx
    ON public.strategy_descriptors (
        kind, name, COALESCE(company_id, '')
    );

-- ------------------------------------------------------------------- intents
CREATE TABLE IF NOT EXISTS public.strategy_intents (
    id                BIGSERIAL PRIMARY KEY,
    company_id        TEXT,
    strategy_name     TEXT NOT NULL,            -- matches strategy_descriptors.name
    strategy_kind     TEXT NOT NULL,            -- arb | copy | souls | custom
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,            -- 'buy' | 'sell'
    venue             TEXT,                     -- optional preferred venue
    size_base         NUMERIC(20,10) NOT NULL,
    notional_usd      NUMERIC(20,4) NOT NULL DEFAULT 0,
    reference_price   NUMERIC(20,10),
    status            TEXT NOT NULL DEFAULT 'pending',
        -- pending | approved | rejected | submitted | filled | skipped | duplicate | failed
    decision_reason   TEXT,
    order_id          BIGINT,                   -- link into public.orders when submitted
    correlation_id    TEXT,
    source_ref        TEXT,                     -- opaque ref back to producer row
                                                 -- e.g. "arb_opportunities.id=17"
    priority_score    NUMERIC(12,4) NOT NULL DEFAULT 0,
    metadata          JSONB NOT NULL DEFAULT '{}'::JSONB,
    proposed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at        TIMESTAMPTZ,
    submitted_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS strategy_intents_status_idx
    ON public.strategy_intents (status, proposed_at DESC);
CREATE INDEX IF NOT EXISTS strategy_intents_strategy_idx
    ON public.strategy_intents (strategy_name, proposed_at DESC);
-- Dedupe belt-and-braces: one intent per (strategy, source_ref) when
-- source_ref is set. Composer also de-dupes in memory before insert.
CREATE UNIQUE INDEX IF NOT EXISTS strategy_intents_source_unique_idx
    ON public.strategy_intents (strategy_name, source_ref)
    WHERE source_ref IS NOT NULL;

-- ------------------------------------------------------------------- view
CREATE OR REPLACE VIEW public.strategy_intents_latest AS
SELECT DISTINCT ON (strategy_name, symbol, side) *
FROM public.strategy_intents
ORDER BY strategy_name, symbol, side, proposed_at DESC;

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW IF EXISTS public.strategy_intents_latest;
-- DROP TABLE IF EXISTS public.strategy_intents;
-- DROP TABLE IF EXISTS public.strategy_descriptors;
-- COMMIT;
