-- Phase 33: Copy-Trader.
--
-- A source (public wallet, another account, a Telegram signal feed,
-- a LiquidityBot-style PnL tracker) is registered in public.copy_sources.
-- The copy service polls each enabled source for new fills and maps
-- them into mirrored trades (public.copy_trades) with a per-source
-- sizing rule. Mirrored trades are audit-logged; actual execution
-- still runs through Phase 26 ExecutionRouter when the strategy
-- composer (Phase 34) promotes them to orders.
--
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.copy_sources (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          TEXT,
    name                TEXT NOT NULL,                -- friendly label
    kind                TEXT NOT NULL,                -- 'ccxt_account' | 'wallet' | 'feed' | 'static'
    venue               TEXT,
    identifier          TEXT NOT NULL,                -- account-id / address / feed-id
    size_mode           TEXT NOT NULL DEFAULT 'ratio', -- ratio / fixed_notional_usd / replicate
    size_value          NUMERIC(20,6) NOT NULL DEFAULT 0.1,
    max_notional_usd    NUMERIC(20,4),
    symbol_whitelist    JSONB NOT NULL DEFAULT '[]'::JSONB,
    symbol_blacklist    JSONB NOT NULL DEFAULT '[]'::JSONB,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    last_checked_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS copy_sources_unique_idx
    ON public.copy_sources (
        kind, COALESCE(venue, ''), identifier, COALESCE(company_id, '')
    );

CREATE TABLE IF NOT EXISTS public.copy_trades (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           BIGINT NOT NULL REFERENCES public.copy_sources(id) ON DELETE CASCADE,
    company_id          TEXT,
    source_fill_id      TEXT NOT NULL,               -- external fill id (idempotency)
    source_trade_ts     TIMESTAMPTZ NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,               -- 'buy' | 'sell'
    source_price        NUMERIC(20,10),
    source_qty_base     NUMERIC(20,10),
    source_notional_usd NUMERIC(20,4),
    mapped_qty_base     NUMERIC(20,10) NOT NULL,
    mapped_notional_usd NUMERIC(20,4) NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | submitted | filled | skipped | rejected
    skip_reason         TEXT,
    correlation_id      TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, source_fill_id)
);
CREATE INDEX IF NOT EXISTS copy_trades_status_idx
    ON public.copy_trades (status, created_at DESC);

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP TABLE IF EXISTS public.copy_trades;
-- DROP TABLE IF EXISTS public.copy_sources;
-- COMMIT;
