-- ============================================================================
-- Migration: demo_orders_table
-- Date: 2026-05-29
-- Purpose: Capture the canonical schema for public.demo_orders — the table the
--          demo_bridge daemon writes when it mirrors a paper competition signal
--          onto a real demo exchange account, and that the dashboard
--          "Paper vs Demo" tab reads to compare paper vs demo execution.
--
--          This table already existed on the live tickles_shared DB but had NO
--          migration file (schema-drift risk flagged in the 2026-05-29 Phase 1
--          audit). This migration documents and reproduces it exactly so a
--          fresh environment matches production. It is IDEMPOTENT (IF NOT
--          EXISTS) and therefore SAFE to run against the live DB — it will be
--          a no-op there.
--
-- Target:  tickles_shared.public
-- Depends: tracked_positions, competition_trades (both must already exist)
-- Notes:   Idempotent. Safe to re-run.
--
-- ROLLBACK: see 2026_05_29_demo_orders_table_ROLLBACK.sql
--           (drops the table — destructive, only for fresh/dev environments).
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- demo_orders — one row per (signal, demo account) mirror attempt.
--
--   tracked_position_id  → the upstream signal (tracked_positions.id). This is
--                          the JOIN KEY used by the dashboard to line up the
--                          paper leg (competition_trades) with the demo leg.
--   competition_trade_id → optional back-link to the paper trade once known.
--   status               → 'pending' (limit resting) → 'filled' → 'closed',
--                          or 'rejected' (exchange refused the order).
--   paper_*              → the levels copied from the signal (what we asked for)
--   demo_entry/exit      → what the exchange actually gave us (for slippage)
--   slippage_*           → demo vs paper price difference (entry & exit)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.demo_orders (
    id                   BIGSERIAL PRIMARY KEY,
    tracked_position_id  BIGINT NOT NULL,                 -- JOIN KEY → tracked_positions.id
    competition_trade_id BIGINT,                          -- optional → competition_trades.id
    exchange             VARCHAR(20) NOT NULL,            -- 'bybit', 'bitget', ...
    account_name         VARCHAR(50) NOT NULL,            -- 'TicklesCo3', ...
    exchange_order_id    VARCHAR(100),                    -- broker-side order id
    agent_id             VARCHAR(50),                     -- competition agent that owns the mirror
    symbol               VARCHAR(50) NOT NULL,
    direction            VARCHAR(10) NOT NULL,            -- 'long' | 'short'
    paper_entry          NUMERIC(18,8),                   -- requested entry (from signal)
    demo_entry           NUMERIC(18,8),                   -- actual fill price (set on fill)
    paper_sl             NUMERIC(18,8),
    paper_tp             NUMERIC(18,8),
    leverage             INTEGER,
    quantity             NUMERIC(18,8),
    notional_usd         NUMERIC(14,2),
    status               VARCHAR(20) DEFAULT 'pending',   -- pending|filled|closed|rejected|cancelled
    ordered_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    filled_at            TIMESTAMPTZ,
    closed_at            TIMESTAMPTZ,
    exit_price           NUMERIC(18,8),
    paper_pnl            NUMERIC(14,4),
    demo_pnl             NUMERIC(14,4),
    slippage_entry       NUMERIC(10,6),                   -- (demo_entry - paper_entry)/paper_entry
    slippage_exit        NUMERIC(10,6),
    error_message        TEXT,                            -- exchange rejection reason
    metadata             JSONB DEFAULT '{}',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_demo_orders_tp
    ON public.demo_orders (tracked_position_id);
CREATE INDEX IF NOT EXISTS idx_demo_orders_status
    ON public.demo_orders (status);
CREATE INDEX IF NOT EXISTS idx_demo_orders_ct
    ON public.demo_orders (competition_trade_id);

COMMIT;
