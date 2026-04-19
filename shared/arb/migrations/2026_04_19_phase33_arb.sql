-- Phase 33: Cross-exchange arbitrage scanner.
--
-- The arb scanner polls N venues for quotes on the same symbol and
-- emits opportunities when (best_bid_venue_A - best_ask_venue_B) is
-- wide enough to pay fees/slippage and still leave a profit. Every
-- opportunity is audit-logged in public.arb_opportunities so the
-- strategy composer (Phase 34) can replay, grade and eventually act
-- on them via the Phase 26 ExecutionRouter.
--
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.arb_venues (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    name            TEXT NOT NULL,                 -- e.g. "binance"
    kind            TEXT NOT NULL DEFAULT 'spot',  -- spot / perp / margin
    taker_fee_bps   NUMERIC(8,4) NOT NULL DEFAULT 10,
    maker_fee_bps   NUMERIC(8,4) NOT NULL DEFAULT 2,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS arb_venues_unique_idx
    ON public.arb_venues (
        name, kind, COALESCE(company_id, '')
    );

CREATE TABLE IF NOT EXISTS public.arb_opportunities (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    symbol          TEXT NOT NULL,
    buy_venue       TEXT NOT NULL,
    sell_venue      TEXT NOT NULL,
    buy_ask         NUMERIC(20,10) NOT NULL,
    sell_bid        NUMERIC(20,10) NOT NULL,
    size_base       NUMERIC(20,10) NOT NULL DEFAULT 0,
    gross_bps       NUMERIC(12,4) NOT NULL,
    net_bps         NUMERIC(12,4) NOT NULL,
    est_profit_usd  NUMERIC(16,4) NOT NULL DEFAULT 0,
    fees_bps        NUMERIC(10,4) NOT NULL DEFAULT 0,
    correlation_id  TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (net_bps >= 0)
);
CREATE INDEX IF NOT EXISTS arb_opp_symbol_idx
    ON public.arb_opportunities (symbol, observed_at DESC);
CREATE INDEX IF NOT EXISTS arb_opp_score_idx
    ON public.arb_opportunities (net_bps DESC, observed_at DESC);

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP TABLE IF EXISTS public.arb_opportunities;
-- DROP TABLE IF EXISTS public.arb_venues;
-- COMMIT;
