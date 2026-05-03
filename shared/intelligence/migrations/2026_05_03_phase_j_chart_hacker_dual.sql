-- ============================================================================
-- Phase J — ChartHacker Dual Analysis
-- ============================================================================
-- Date:    2026-05-03
-- Plan:    shared/docs/PHASE_J_CHART_HACKER_DUAL_ANALYSIS.md
-- Target:  tickles_shared (positions + interpretations live here)
--
-- Purpose: Treat the Interpreter (chart_hacker) as a first-class actor that
--          produces its own independent trade ideas alongside the trader's.
--          Both sides land in tracked_positions and feed the same downstream
--          pipeline (monitor → postmortem → mem0 → Phase Y skill score).
--
-- Adds:
--   tracked_positions:
--     signal_source     — 'trader' or 'chart_hacker'
--     trade_type        — 'swing' / 'scalp' / 'degen' / 'position'
--     timeframe         — '1m','5m','15m','1h','4h','1d',...
--     take_profit_4..6  — additional TP levels (Lens prompt extracts up to 6)
--
--   signal_interpretations:
--     timeframe           — chart timeframe identified by the LLM
--     chart_analysis      — JSONB: market_structure, indicators, patterns, key_levels
--     trader_trades       — JSONB array: setups the LLM thinks the trader marked
--     chart_hacker_trades — JSONB array: chart_hacker's own independent setups
--     ai_agreement_score  — 0..1 — alignment between trader and chart_hacker views
--     ai_comment          — chart_hacker's assessment of the trader's thesis
--
-- Seeds:
--   trader_profiles row for chart_hacker so it appears on the leaderboard
--   alongside human traders.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- tracked_positions: dual-source columns
-- ---------------------------------------------------------------------------
ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS signal_source VARCHAR(20) NOT NULL DEFAULT 'trader',
  ADD COLUMN IF NOT EXISTS trade_type    VARCHAR(20),
  ADD COLUMN IF NOT EXISTS timeframe     VARCHAR(8),
  ADD COLUMN IF NOT EXISTS take_profit_4 DECIMAL(20,8),
  ADD COLUMN IF NOT EXISTS take_profit_5 DECIMAL(20,8),
  ADD COLUMN IF NOT EXISTS take_profit_6 DECIMAL(20,8);

-- Drop and re-add the CHECK so it's idempotent on re-run
ALTER TABLE public.tracked_positions
  DROP CONSTRAINT IF EXISTS tracked_positions_signal_source_check;
ALTER TABLE public.tracked_positions
  ADD CONSTRAINT tracked_positions_signal_source_check
    CHECK (signal_source IN ('trader','chart_hacker'));

CREATE INDEX IF NOT EXISTS idx_tracked_positions_signal_source
  ON public.tracked_positions(signal_source);

CREATE INDEX IF NOT EXISTS idx_tracked_positions_actor_company
  ON public.tracked_positions(actor_id, company_id)
  WHERE actor_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- signal_interpretations: Lens-prompt output columns
-- ---------------------------------------------------------------------------
ALTER TABLE public.signal_interpretations
  ADD COLUMN IF NOT EXISTS timeframe           VARCHAR(8),
  ADD COLUMN IF NOT EXISTS chart_analysis      JSONB,
  ADD COLUMN IF NOT EXISTS trader_trades       JSONB,
  ADD COLUMN IF NOT EXISTS chart_hacker_trades JSONB,
  ADD COLUMN IF NOT EXISTS ai_agreement_score  DECIMAL(3,2),
  ADD COLUMN IF NOT EXISTS ai_comment          TEXT;

-- Sanity guards on JSONB shape
ALTER TABLE public.signal_interpretations
  DROP CONSTRAINT IF EXISTS si_trader_trades_array_check;
ALTER TABLE public.signal_interpretations
  ADD CONSTRAINT si_trader_trades_array_check
    CHECK (
      trader_trades IS NULL
      OR jsonb_typeof(trader_trades) = 'array'
    );

ALTER TABLE public.signal_interpretations
  DROP CONSTRAINT IF EXISTS si_chart_hacker_trades_array_check;
ALTER TABLE public.signal_interpretations
  ADD CONSTRAINT si_chart_hacker_trades_array_check
    CHECK (
      chart_hacker_trades IS NULL
      OR jsonb_typeof(chart_hacker_trades) = 'array'
    );

-- ---------------------------------------------------------------------------
-- Seed chart_hacker as an actor on trader_profiles (idempotent)
-- ---------------------------------------------------------------------------
-- Platform check constraint allows: discord/telegram/twitter/tradingview/rss/api/unknown
-- trader_type check constraint allows: pro/amateur/bot/news/unknown
-- chart_hacker is system-generated → platform='api', trader_type='bot'
INSERT INTO public.trader_profiles
  (platform, handle_raw, handle_normalized, display_name, trader_type,
   first_seen_at, last_seen_at, metadata)
VALUES
  ('api', 'chart_hacker', 'chart_hacker',
   'ChartHacker (AI vision agent)', 'bot',
   NOW(), NOW(),
   '{"role": "interpreter", "kind": "dual_analysis", "is_bot": true}'::jsonb)
ON CONFLICT (platform, handle_normalized) DO UPDATE
   SET last_seen_at = EXCLUDED.last_seen_at,
       display_name = COALESCE(EXCLUDED.display_name,
                               public.trader_profiles.display_name),
       metadata     = COALESCE(public.trader_profiles.metadata, '{}'::jsonb)
                      || EXCLUDED.metadata;

COMMIT;
