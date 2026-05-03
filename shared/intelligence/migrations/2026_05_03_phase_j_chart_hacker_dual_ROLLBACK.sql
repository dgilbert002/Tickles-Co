-- ============================================================================
-- Phase J — ChartHacker Dual Analysis ROLLBACK
-- ============================================================================
-- Reverses 2026_05_03_phase_j_chart_hacker_dual.sql.
--
-- Order matters: drop CHECKs and indexes first, then columns, then the seed row.
-- ============================================================================

BEGIN;

ALTER TABLE public.signal_interpretations
  DROP CONSTRAINT IF EXISTS si_trader_trades_array_check,
  DROP CONSTRAINT IF EXISTS si_chart_hacker_trades_array_check;

ALTER TABLE public.signal_interpretations
  DROP COLUMN IF EXISTS ai_comment,
  DROP COLUMN IF EXISTS ai_agreement_score,
  DROP COLUMN IF EXISTS chart_hacker_trades,
  DROP COLUMN IF EXISTS trader_trades,
  DROP COLUMN IF EXISTS chart_analysis,
  DROP COLUMN IF EXISTS timeframe;

DROP INDEX IF EXISTS idx_tracked_positions_actor_company;
DROP INDEX IF EXISTS idx_tracked_positions_signal_source;

ALTER TABLE public.tracked_positions
  DROP CONSTRAINT IF EXISTS tracked_positions_signal_source_check;

ALTER TABLE public.tracked_positions
  DROP COLUMN IF EXISTS take_profit_6,
  DROP COLUMN IF EXISTS take_profit_5,
  DROP COLUMN IF EXISTS take_profit_4,
  DROP COLUMN IF EXISTS timeframe,
  DROP COLUMN IF EXISTS trade_type,
  DROP COLUMN IF EXISTS signal_source;

DELETE FROM public.trader_profiles
 WHERE platform = 'api' AND handle_normalized = 'chart_hacker';

COMMIT;
