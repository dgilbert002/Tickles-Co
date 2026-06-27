-- Phase 1 (Fix A): prevent paper-engine double-entry of the same tracked
-- position for the same agent while it is still open.
--
-- Context: copy_trade_monitor occasionally created two OPEN competition_trades
-- rows for the same (agent_id, tracked_position_id) when a backfill/restart ran
-- before per-agent _agent_entered was rehydrated from competition_trades.
-- The rehydration is now correct in code, but this index makes the invariant
-- structural so no future code path (or manual backfill) can violate it.
--
-- Applied live 2026-06-27 after deleting 4 stale duplicate rows
-- (ct 5070, 5072, 5074, 5161). Idempotent.

CREATE UNIQUE INDEX IF NOT EXISTS uq_comp_trades_open_agent_tp
ON public.competition_trades (agent_id, tracked_position_id)
WHERE exit_price IS NULL AND tracked_position_id IS NOT NULL;
