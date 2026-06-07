-- shared/intelligence/migrations/2026_05_24_copy_agent_state.sql
--
-- Persistent state for the LiveCopyTradeMonitor daemon.
--
-- Background:
--   The 7 copy-trade competition agents (copy_spot_seq, copy_lev_parallel,
--   copy_lev_be_lock, copy_opt_spot_seq, copy_opt_lev_parallel,
--   copy_opt_lev_be_lock, copy_ch_ai_vision) lived entirely in
--   `LiveCopyTradeMonitor._agents` (a Python dict). Every restart wiped
--   the dict and re-initialised every agent to balance=1000, trades=0,
--   open_positions=[], so the dashboard's "balance / equity / realized /
--   unrealized / win-rate / trade-count" panels froze at the round-defaults
--   the moment the service restarted (2026-05-24 02:53 CEST).
--
-- This migration adds the persistence layer.
--
-- Schema decisions:
--   - JSONB `open_positions` and `entered_position_ids` rather than child
--     tables: the volume is small (max ~20 paper positions per agent in
--     leveraged-parallel mode, ~50 entered_position_ids before pruning),
--     atomicity is per-agent UPSERT, and read/write happens on every
--     30-second tick. JSONB keeps it one round-trip per agent per save.
--   - PRIMARY KEY (agent_id) — one row per agent. The existing 7 agent
--     IDs come from the NAME_TO_ID dict in copy_trade_monitor.py.
--   - Every numeric column has a sane DEFAULT so a missing row falls
--     back to the round-defaults (1000 balance, 0 P&L, etc.) — same
--     behaviour as today, just persistent once the row is written.

CREATE TABLE IF NOT EXISTS public.copy_agent_state (
    agent_id              TEXT        PRIMARY KEY,
    balance               NUMERIC     NOT NULL DEFAULT 1000,
    starting_balance      NUMERIC     NOT NULL DEFAULT 1000,
    total_pnl             NUMERIC     NOT NULL DEFAULT 0,
    total_fees            NUMERIC     NOT NULL DEFAULT 0,
    wins                  INTEGER     NOT NULL DEFAULT 0,
    losses                INTEGER     NOT NULL DEFAULT 0,
    trades                INTEGER     NOT NULL DEFAULT 0,
    open_positions        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    entered_position_ids  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for "show me agents updated in the last N seconds" queries the
-- dashboard / cron-canary may run.
CREATE INDEX IF NOT EXISTS idx_copy_agent_state_updated_at
    ON public.copy_agent_state (updated_at DESC);

-- Seed the 7 known agents idempotently. ON CONFLICT DO NOTHING so a
-- replay of the migration after live data has been written doesn't
-- clobber the running balances.
INSERT INTO public.copy_agent_state (agent_id, balance, starting_balance) VALUES
  ('copy_spot_seq',          1000, 1000),
  ('copy_lev_parallel',      1000, 1000),
  ('copy_lev_be_lock',       1000, 1000),
  ('copy_opt_spot_seq',      1000, 1000),
  ('copy_opt_lev_parallel',  1000, 1000),
  ('copy_opt_lev_be_lock',   1000, 1000),
  ('copy_charthacker',       1000, 1000)  -- renamed from copy_ch_ai_vision 2026-05-29
ON CONFLICT (agent_id) DO NOTHING;
