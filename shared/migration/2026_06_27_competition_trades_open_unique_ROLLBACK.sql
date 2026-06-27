-- ROLLBACK for 2026_06_27_competition_trades_open_unique.sql
-- Drops the partial unique index guarding against duplicate open paper trades.
DROP INDEX IF EXISTS public.uq_comp_trades_open_agent_tp;
