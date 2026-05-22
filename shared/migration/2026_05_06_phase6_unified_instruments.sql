-- ============================================================================
-- Migration: phase6_unified_instruments
-- Date: 2026-05-06
-- Purpose: Phase 6 — Unified Asset Register. Creates the public.unified_instruments
--          table that serves as the source of truth for all instruments across
--          all .env-configured exchanges (Bybit, BloFin, Bitget, Capital.com).
-- Target:  tickles_shared.public.unified_instruments
-- Notes:   Idempotent (IF NOT EXISTS). Safe to re-run.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Create the unified_instruments table.
--    Every instrument from every configured exchange is stored here with a
--    canonical asset identifier, enabling cross-exchange normalization.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.unified_instruments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_asset VARCHAR(20) NOT NULL,          -- e.g. 'BTC', 'ETH', 'XAU'
    asset_type VARCHAR(20) NOT NULL,               -- 'crypto', 'forex', 'commodity', 'index', 'stock'
    base_currency VARCHAR(10) NOT NULL,            -- e.g. 'BTC'
    quote_currency VARCHAR(10) NOT NULL,           -- e.g. 'USDT', 'USD'
    exchange VARCHAR(50) NOT NULL,                 -- e.g. 'bybit', 'capital.com'
    exchange_symbol VARCHAR(50) NOT NULL,          -- exchange-specific symbol
    canonical_symbol VARCHAR(50) NOT NULL,         -- normalized slash-form e.g. 'BTC/USDT'
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uk_exchange_symbol UNIQUE (exchange, exchange_symbol)
);

-- ---------------------------------------------------------------------------
-- 2. Indexes for common lookup patterns.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_unified_instruments_canonical_asset
    ON public.unified_instruments (canonical_asset);

CREATE INDEX IF NOT EXISTS idx_unified_instruments_canonical_symbol
    ON public.unified_instruments (canonical_symbol);

CREATE INDEX IF NOT EXISTS idx_unified_instruments_exchange
    ON public.unified_instruments (exchange);

-- ---------------------------------------------------------------------------
-- 3. CHECK constraint to ensure asset_type is one of the recognised values.
-- ---------------------------------------------------------------------------
ALTER TABLE public.unified_instruments
    ADD CONSTRAINT chk_unified_instruments_asset_type
    CHECK (asset_type IN ('crypto', 'forex', 'commodity', 'index', 'stock'));

COMMIT;
