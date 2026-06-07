-- Migration: rename copy-agent id  copy_ch_ai_vision  ->  copy_charthacker
-- Date: 2026-05-29
--
-- Why: the agent_id "copy_ch_ai_vision" (display "CH: AI Vision") implied a
-- second, independent AI brain. It is NOT one — it is the paper wallet that
-- mirrors chart_hacker's signals. Renamed so the dashboard (which shows the
-- raw agent_id) reads honestly as "copy_charthacker".
--
-- Safety: agent_id has no FK references; only data rows are touched.
-- Run with both copy-trade-monitor AND demo-bridge STOPPED so neither writes
-- the old id mid-migration. Wrapped in a transaction.
--
-- Tables touched (rows as of 2026-05-29): copy_agent_state(1),
-- competition_trades(9), contest_participants(1),
-- competition_agent_exchanges(2), demo_orders(4).

BEGIN;

UPDATE public.copy_agent_state
   SET agent_id = 'copy_charthacker'
 WHERE agent_id = 'copy_ch_ai_vision';

UPDATE public.competition_trades
   SET agent_id = 'copy_charthacker'
 WHERE agent_id = 'copy_ch_ai_vision';

UPDATE public.contest_participants
   SET agent_id = 'copy_charthacker'
 WHERE agent_id = 'copy_ch_ai_vision';

UPDATE public.competition_agent_exchanges
   SET agent_id = 'copy_charthacker'
 WHERE agent_id = 'copy_ch_ai_vision';

UPDATE public.demo_orders
   SET agent_id = 'copy_charthacker'
 WHERE agent_id = 'copy_ch_ai_vision';

COMMIT;
