-- Phase 26: Execution Layer on NautilusTrader
--
-- Every approved Treasury decision (see Phase 25) that we actually
-- want to *place* is converted into an ExecutionIntent and dispatched
-- through an ExecutionAdapter (paper / ccxt / nautilus). The adapter
-- reports back with fills, which we persist here as immutable rows.
--
-- Ownership: tickles_shared. All tables are append-only except
-- positions (materialised view from fills).
--
-- Rollback script is at the bottom of this file (commented out).

BEGIN;

-- --------------------------------------------------------------------
-- orders: one row per ExecutionIntent that we attempted to place.
-- Append-only. "Status" is the adapter's best-known status at the time
-- of the most recent update. For a full audit trail, see order_events.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.orders (
    id                          BIGSERIAL PRIMARY KEY,
    company_id                  TEXT NOT NULL,
    strategy_id                 TEXT,
    agent_id                    TEXT,
    intent_hash                 TEXT NOT NULL,
    treasury_decision_id        BIGINT REFERENCES public.treasury_decisions(id),
    adapter                     TEXT NOT NULL,           -- paper / ccxt / nautilus
    exchange                    TEXT NOT NULL,
    account_id_external         TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    direction                   TEXT NOT NULL,           -- long / short
    order_type                  TEXT NOT NULL,           -- market / limit / stop / stop_limit
    quantity                    NUMERIC(20, 8) NOT NULL,
    requested_notional_usd      NUMERIC(20, 8),
    requested_price             NUMERIC(20, 8),
    time_in_force               TEXT,                    -- gtc / ioc / fok / gtd
    client_order_id             TEXT NOT NULL,           -- idempotency key
    external_order_id           TEXT,                    -- exchange's id once known
    status                      TEXT NOT NULL,           -- new / accepted / partially_filled / filled / canceled / rejected / expired / pending_cancel
    filled_quantity             NUMERIC(20, 8) NOT NULL DEFAULT 0,
    average_fill_price          NUMERIC(20, 8),
    fees_paid_usd               NUMERIC(20, 8) NOT NULL DEFAULT 0,
    reason                      TEXT,                    -- adapter-provided reject / last update reason
    submitted_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata                    JSONB NOT NULL DEFAULT '{}'::JSONB,
    UNIQUE (adapter, client_order_id)
);

CREATE INDEX IF NOT EXISTS orders_company_submitted_idx
    ON public.orders (company_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS orders_intent_hash_idx
    ON public.orders (intent_hash);
CREATE INDEX IF NOT EXISTS orders_status_idx
    ON public.orders (status) WHERE status IN ('new', 'accepted', 'partially_filled');
CREATE INDEX IF NOT EXISTS orders_external_id_idx
    ON public.orders (adapter, exchange, external_order_id)
    WHERE external_order_id IS NOT NULL;

-- --------------------------------------------------------------------
-- order_events: append-only log of every state transition / message
-- the adapter emits for an order.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.order_events (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES public.orders(id),
    event_type      TEXT NOT NULL,             -- submitted / accepted / partial_fill / fill / cancel / reject / expire / update
    severity        TEXT NOT NULL DEFAULT 'info',  -- info / warning / error
    message         TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::JSONB,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS order_events_order_ts_idx
    ON public.order_events (order_id, ts);
CREATE INDEX IF NOT EXISTS order_events_type_idx
    ON public.order_events (event_type, ts DESC);

-- --------------------------------------------------------------------
-- fills: one row per executed quantity slice. Append-only. This is
-- the "cash" side of Phase 25's Banker: any realized P&L and fee
-- accounting is derived from fills, not orders.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.fills (
    id                      BIGSERIAL PRIMARY KEY,
    order_id                BIGINT NOT NULL REFERENCES public.orders(id),
    company_id              TEXT NOT NULL,
    adapter                 TEXT NOT NULL,
    exchange                TEXT NOT NULL,
    account_id_external     TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL,         -- long / short (matches the order)
    quantity                NUMERIC(20, 8) NOT NULL,
    price                   NUMERIC(20, 8) NOT NULL,
    notional_usd            NUMERIC(20, 8) NOT NULL,
    fee_usd                 NUMERIC(20, 8) NOT NULL DEFAULT 0,
    fee_currency            TEXT,
    is_maker                BOOLEAN,
    liquidity               TEXT,                  -- maker / taker / unknown
    realized_pnl_usd        NUMERIC(20, 8),        -- set when closing leg, else NULL
    external_fill_id        TEXT,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata                JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE INDEX IF NOT EXISTS fills_order_idx           ON public.fills (order_id);
CREATE INDEX IF NOT EXISTS fills_company_ts_idx      ON public.fills (company_id, ts DESC);
CREATE INDEX IF NOT EXISTS fills_symbol_ts_idx       ON public.fills (exchange, symbol, ts DESC);

-- --------------------------------------------------------------------
-- positions: append-only *snapshots* of positions. The "current"
-- position is the most recent row per key; the view below computes it
-- for consumers that don't want to stitch fills together themselves.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.position_snapshots (
    id                      BIGSERIAL PRIMARY KEY,
    company_id              TEXT NOT NULL,
    adapter                 TEXT NOT NULL,
    exchange                TEXT NOT NULL,
    account_id_external     TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    direction               TEXT NOT NULL,                -- long / short / flat
    quantity                NUMERIC(20, 8) NOT NULL,
    average_entry_price     NUMERIC(20, 8),
    notional_usd            NUMERIC(20, 8),
    unrealised_pnl_usd      NUMERIC(20, 8),
    realized_pnl_usd        NUMERIC(20, 8) NOT NULL DEFAULT 0,
    leverage                INTEGER NOT NULL DEFAULT 1,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source                  TEXT NOT NULL,                -- adapter / sync / reconciliation
    metadata                JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE INDEX IF NOT EXISTS position_snapshots_key_ts_idx
    ON public.position_snapshots
    (company_id, adapter, exchange, account_id_external, symbol, ts DESC);

CREATE OR REPLACE VIEW public.positions_current AS
SELECT DISTINCT ON (company_id, adapter, exchange, account_id_external, symbol)
    id, company_id, adapter, exchange, account_id_external, symbol,
    direction, quantity, average_entry_price, notional_usd,
    unrealised_pnl_usd, realized_pnl_usd, leverage, ts, source, metadata
FROM public.position_snapshots
ORDER BY company_id, adapter, exchange, account_id_external, symbol, ts DESC;

COMMIT;

-- =====================================================================
-- ROLLBACK (uncomment + run if you need to drop Phase 26 tables):
-- BEGIN;
-- DROP VIEW  IF EXISTS public.positions_current;
-- DROP TABLE IF EXISTS public.position_snapshots;
-- DROP TABLE IF EXISTS public.fills;
-- DROP TABLE IF EXISTS public.order_events;
-- DROP TABLE IF EXISTS public.orders;
-- COMMIT;
