-- Phase 32: Scout / Curiosity / Optimiser / RegimeWatcher helpers.
-- Scout proposes symbols, Curiosity picks experiments, Optimiser
-- tracks parameter candidates, RegimeWatcher records transitions
-- between regime labels. Each soul continues to log verdicts to
-- Phase 31's agent_decisions table; the helpers below are opt-in
-- persistence surfaces for their specific outputs.
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.scout_candidates (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    universe        TEXT,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    score           NUMERIC(10,4) NOT NULL DEFAULT 0,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed',      -- proposed / accepted / rejected
    correlation_id  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS scout_candidates_unique_idx
    ON public.scout_candidates (
        exchange, symbol,
        COALESCE(universe, ''),
        COALESCE(company_id, '')
    );
CREATE INDEX IF NOT EXISTS scout_candidates_status_idx
    ON public.scout_candidates (status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.optimiser_candidates (
    id              BIGSERIAL PRIMARY KEY,
    strategy        TEXT NOT NULL,
    company_id      TEXT,
    params          JSONB NOT NULL,
    score           NUMERIC(12,6),
    status          TEXT NOT NULL DEFAULT 'pending',       -- pending / running / done / failed
    correlation_id  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS optimiser_strategy_idx
    ON public.optimiser_candidates (strategy, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.regime_transitions (
    id              BIGSERIAL PRIMARY KEY,
    universe        TEXT,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    from_regime     TEXT,
    to_regime       TEXT NOT NULL,
    transitioned_at TIMESTAMPTZ NOT NULL,
    confidence      NUMERIC(6,4) NOT NULL DEFAULT 0,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS regime_transitions_symbol_idx
    ON public.regime_transitions (exchange, symbol, timeframe, transitioned_at DESC);

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP TABLE IF EXISTS public.regime_transitions;
-- DROP TABLE IF EXISTS public.optimiser_candidates;
-- DROP TABLE IF EXISTS public.scout_candidates;
-- COMMIT;
