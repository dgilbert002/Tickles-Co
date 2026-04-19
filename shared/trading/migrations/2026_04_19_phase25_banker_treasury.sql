-- Phase 25: Banker + Treasury + Capabilities
--
-- Lives in tickles_shared so that a treasury view can answer questions
-- across every company without cross-database joins. Each row carries a
-- company_id string (matching the existing tickles_{company} convention),
-- not a FK, because the authoritative company registry lives in a
-- later phase (Owner Dashboard / provisioning).
--
-- Additive and fully idempotent — safe to re-run.
BEGIN;

-- ---------------------------------------------------------------------------
-- 1. capabilities — "what is this company / strategy / agent allowed to do?"
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.capabilities (
    id                   bigserial    PRIMARY KEY,
    company_id           text         NOT NULL,
    scope_kind           text         NOT NULL,     -- company | strategy | agent | venue
    scope_id             text         NOT NULL,     -- 'global' for company scope, strategy name, etc.
    max_notional_usd     numeric(20,8),             -- per-order notional cap; NULL = unlimited
    max_leverage         integer,                   -- NULL = exchange-default
    max_daily_loss_usd   numeric(20,8),             -- NULL = no daily circuit-breaker
    max_open_positions   integer,                   -- NULL = unlimited concurrent positions
    allow_venues         text[]       NOT NULL DEFAULT '{}'::text[],
    deny_venues          text[]       NOT NULL DEFAULT '{}'::text[],
    allow_symbols        text[]       NOT NULL DEFAULT '{}'::text[],
    deny_symbols         text[]       NOT NULL DEFAULT '{}'::text[],
    allow_directions     text[]       NOT NULL DEFAULT '{long,short}'::text[],
    allow_order_types    text[]       NOT NULL DEFAULT '{market,limit}'::text[],
    active               boolean      NOT NULL DEFAULT true,
    notes                text         NOT NULL DEFAULT '',
    metadata             jsonb        NOT NULL DEFAULT '{}'::jsonb,
    created_at           timestamptz  NOT NULL DEFAULT now(),
    updated_at           timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT uq_capabilities_scope UNIQUE (company_id, scope_kind, scope_id)
);

CREATE INDEX IF NOT EXISTS idx_capabilities_company
    ON public.capabilities (company_id);

CREATE INDEX IF NOT EXISTS idx_capabilities_scope
    ON public.capabilities (scope_kind, scope_id);

-- ---------------------------------------------------------------------------
-- 2. banker_balances — append-only balance/equity snapshots per account
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.banker_balances (
    id                   bigserial    PRIMARY KEY,
    company_id           text         NOT NULL,
    exchange             text         NOT NULL,
    account_id_external  text         NOT NULL,
    account_type         text         NOT NULL DEFAULT 'demo',  -- demo | live | paper
    currency             text         NOT NULL DEFAULT 'USD',
    balance              numeric(20,8) NOT NULL,
    equity               numeric(20,8),
    margin_used          numeric(20,8),
    free_margin          numeric(20,8),
    unrealised_pnl       numeric(20,8),
    source               text         NOT NULL DEFAULT 'ccxt',
    ts                   timestamptz  NOT NULL DEFAULT now(),
    metadata             jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_banker_balances_company_ts
    ON public.banker_balances (company_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_banker_balances_account_ts
    ON public.banker_balances (company_id, exchange, account_id_external, ts DESC);

-- Convenience view: latest balance per (company, exchange, account, currency).
CREATE OR REPLACE VIEW public.banker_balances_latest AS
SELECT DISTINCT ON (company_id, exchange, account_id_external, currency)
    id,
    company_id,
    exchange,
    account_id_external,
    account_type,
    currency,
    balance,
    equity,
    margin_used,
    free_margin,
    unrealised_pnl,
    source,
    ts,
    metadata
FROM public.banker_balances
ORDER BY company_id, exchange, account_id_external, currency, ts DESC;

-- ---------------------------------------------------------------------------
-- 3. leverage_history — audit log of leverage changes (requested vs applied)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.leverage_history (
    id                   bigserial    PRIMARY KEY,
    company_id           text         NOT NULL,
    exchange             text         NOT NULL,
    symbol               text         NOT NULL,
    direction            text,                        -- long | short | both
    leverage_requested   integer      NOT NULL,
    leverage_applied     integer      NOT NULL,
    requested_by         text         NOT NULL DEFAULT 'system',
    ok                   boolean      NOT NULL DEFAULT true,
    reason               text,
    ts                   timestamptz  NOT NULL DEFAULT now(),
    metadata             jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_leverage_history_company_ts
    ON public.leverage_history (company_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_leverage_history_instrument_ts
    ON public.leverage_history (exchange, symbol, ts DESC);

-- ---------------------------------------------------------------------------
-- 4. treasury_decisions — audit log of every TreasuryDecision made
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.treasury_decisions (
    id                   bigserial    PRIMARY KEY,
    company_id           text         NOT NULL,
    strategy_id          text,
    agent_id             text,
    exchange             text         NOT NULL,
    symbol               text         NOT NULL,
    direction            text         NOT NULL,     -- long | short
    intent_hash          text         NOT NULL,
    approved             boolean      NOT NULL,
    reasons              text[]       NOT NULL DEFAULT '{}'::text[],
    capability_ids       bigint[]     NOT NULL DEFAULT '{}'::bigint[],
    requested_notional_usd numeric(20,8),
    approved_notional_usd  numeric(20,8),
    available_capital_usd  numeric(20,8),
    ts                   timestamptz  NOT NULL DEFAULT now(),
    metadata             jsonb        NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_treasury_decisions_company_ts
    ON public.treasury_decisions (company_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_treasury_decisions_intent_hash
    ON public.treasury_decisions (intent_hash);

COMMIT;
