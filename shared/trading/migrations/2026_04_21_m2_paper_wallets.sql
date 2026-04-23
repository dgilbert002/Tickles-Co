-- Phase M2: Paper wallets for paper trading
--
-- A paper wallet is a virtual account with a starting balance that agents
-- can use for paper trading. When created, a corresponding banker_balances
-- row is inserted so Treasury can see the available capital.
--
-- One wallet per (company_id, agent_id, exchange) — creating again resets
-- the starting balance and re-seeds banker_balances.
--
-- Rollback at the bottom.
BEGIN;

CREATE TABLE IF NOT EXISTS public.paper_wallets (
    id                   BIGSERIAL    PRIMARY KEY,
    company_id           TEXT         NOT NULL,
    agent_id             TEXT         NOT NULL,
    exchange             TEXT         NOT NULL DEFAULT 'bybit',
    account_id_external  TEXT         NOT NULL,
    starting_balance_usd NUMERIC(20,8) NOT NULL,
    currency             TEXT         NOT NULL DEFAULT 'USD',
    account_type         TEXT         NOT NULL DEFAULT 'paper',
    contest_id           TEXT,
    is_active            BOOLEAN      NOT NULL DEFAULT true,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    metadata             JSONB        NOT NULL DEFAULT '{}'::JSONB,

    CONSTRAINT uq_paper_wallets_owner UNIQUE (company_id, agent_id, exchange)
);

CREATE INDEX IF NOT EXISTS paper_wallets_company_idx
    ON public.paper_wallets (company_id);

CREATE INDEX IF NOT EXISTS paper_wallets_active_idx
    ON public.paper_wallets (company_id, is_active)
    WHERE is_active = true;

COMMIT;

-- =====================================================================
-- ROLLBACK (uncomment + run if you need to drop M2 tables):
-- BEGIN;
-- DROP TABLE IF EXISTS public.paper_wallets;
-- COMMIT;
