-- ============================================================================
-- Migration: phase_demo_exchange_infrastructure
-- Date: 2026-05-28
-- Purpose: Exchange accounts, competitions, and agent-exchange assignments
--          for the paper → demo bridge. Enables multi-exchange competition
--          trading with isolated or cumulative balance modes.
-- Target:  tickles_shared.public
-- Depends: competition_trades table (must already exist)
-- Notes:   Idempotent (IF NOT EXISTS). Safe to re-run.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. exchange_accounts — API credentials + metadata per exchange account.
--    One row per (exchange, account_name) pair. API keys stored encrypted.
--    Supports dynamic adding of new exchanges via the dashboard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.exchange_accounts (
    id              SERIAL PRIMARY KEY,
    exchange        VARCHAR(20) NOT NULL,          -- 'bybit', 'blofin', 'bitget', 'capital.com'
    account_name    VARCHAR(50) NOT NULL,          -- 'DEMO', 'DEMO_SHADDOW', 'main', 'live'
    description     TEXT,                          -- human-readable label
    account_type    VARCHAR(20) NOT NULL DEFAULT 'demo',  -- 'demo', 'live', 'paper'
    api_key         TEXT,                          -- encrypted at rest (app-layer encrypt/decrypt)
    api_secret      TEXT,
    api_passphrase  TEXT,                          -- for Bitget / BloFin passphrase
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_tested_at  TIMESTAMPTZ,
    last_balance    NUMERIC(14,2),                 -- last known total equity (USDT)
    last_error      TEXT,                          -- last connectivity error
    metadata        JSONB DEFAULT '{}',            -- extra config (sandbox flag, etc.)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uk_exchange_account UNIQUE (exchange, account_name)
);

CREATE INDEX IF NOT EXISTS idx_exchange_accounts_exchange
    ON public.exchange_accounts (exchange);
CREATE INDEX IF NOT EXISTS idx_exchange_accounts_active
    ON public.exchange_accounts (is_active) WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- 2. competitions — named competition instances. Each competition has a
--    default balance mode (isolated or cumulative), risk percentage, and
--    starting capital. Agents inherit these unless they override.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.competitions (
    id                  VARCHAR(50) PRIMARY KEY,       -- 'home-lab', 'blofin-may-2026'
    name                VARCHAR(100) NOT NULL,
    description         TEXT,
    default_balance_mode VARCHAR(20) NOT NULL DEFAULT 'cumulative',  -- 'isolated' | 'cumulative'
    default_risk_pct    NUMERIC(5,4) NOT NULL DEFAULT 0.05,          -- 5% = 0.05
    default_starting_usd NUMERIC(12,2) NOT NULL DEFAULT 200,
    max_concurrent       INT DEFAULT 20,               -- max open positions per agent
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    config               JSONB DEFAULT '{}',           -- extra config
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- 3. competition_agent_exchanges — the bridge table.
--    One row per (competition, agent, exchange_account). An agent in a
--    competition can use multiple exchange accounts with priority ordering.
--    Mode/risk_pct/max_concurrent NULL = inherit from competition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.competition_agent_exchanges (
    id                  SERIAL PRIMARY KEY,
    competition_id      VARCHAR(50) NOT NULL REFERENCES public.competitions(id) ON DELETE CASCADE,
    agent_id            VARCHAR(50) NOT NULL,          -- 'copy_opt_lev_parallel', etc.
    exchange_account_id INT NOT NULL REFERENCES public.exchange_accounts(id) ON DELETE RESTRICT,
    priority            INT NOT NULL DEFAULT 0,        -- 0 = first choice, higher = fallback
    starting_capital_usd NUMERIC(12,2),                -- NULL = use competition default
    balance_mode        VARCHAR(20),                   -- NULL = use competition default ('isolated' | 'cumulative')
    risk_pct            NUMERIC(5,4),                  -- NULL = use competition default
    max_concurrent      INT,                           -- NULL = use competition default
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uk_agent_exchange_per_comp UNIQUE (competition_id, agent_id, exchange_account_id),
    CONSTRAINT chk_balance_mode CHECK (balance_mode IS NULL OR balance_mode IN ('isolated', 'cumulative'))
);

CREATE INDEX IF NOT EXISTS idx_cae_competition
    ON public.competition_agent_exchanges (competition_id);
CREATE INDEX IF NOT EXISTS idx_cae_agent
    ON public.competition_agent_exchanges (agent_id);
CREATE INDEX IF NOT EXISTS idx_cae_active
    ON public.competition_agent_exchanges (competition_id, agent_id) WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- 4. competition_trades — add exchange tracking columns.
--    These allow the bridge daemon to record the real-exchange order details
--    and the dashboard to compare paper P&L vs exchange P&L side-by-side.
-- ---------------------------------------------------------------------------
ALTER TABLE public.competition_trades
    ADD COLUMN IF NOT EXISTS exchange_order_id  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS exchange_name      VARCHAR(20),
    ADD COLUMN IF NOT EXISTS exchange_account   VARCHAR(50),
    ADD COLUMN IF NOT EXISTS exchange_pnl       NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS exchange_fee       NUMERIC(12,2),
    ADD COLUMN IF NOT EXISTS exchange_fill_price NUMERIC(18,8);

CREATE INDEX IF NOT EXISTS idx_ct_exchange_order
    ON public.competition_trades (exchange_order_id) WHERE exchange_order_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. Seed data — create the default "copy-trade-scenarios" competition and
--    assign all 12 current agents. This lets the bridge work immediately
--    once exchange accounts are added. All agents default to cumulative
--    mode, $200 each, using whatever exchange accounts are assigned.
-- ---------------------------------------------------------------------------
INSERT INTO public.competitions (id, name, description, default_balance_mode, default_risk_pct, default_starting_usd)
VALUES ('copy-trade-scenarios', 'Paper Competition (Original)', 
        'Default competition migrated from paper-only copy-trade contest. 12 agents.',
        'cumulative', 0.05, 200)
ON CONFLICT (id) DO NOTHING;

COMMIT;
