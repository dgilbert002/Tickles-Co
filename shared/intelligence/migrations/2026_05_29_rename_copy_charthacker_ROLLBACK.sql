-- ROLLBACK: revert  copy_charthacker  ->  copy_ch_ai_vision
-- Date: 2026-05-29
-- Run with copy-trade-monitor AND demo-bridge STOPPED, then revert the code
-- changes (copy_trade_monitor.py, demo_bridge.py, mirror.js, etc.).

BEGIN;

UPDATE public.copy_agent_state
   SET agent_id = 'copy_ch_ai_vision'
 WHERE agent_id = 'copy_charthacker';

UPDATE public.competition_trades
   SET agent_id = 'copy_ch_ai_vision'
 WHERE agent_id = 'copy_charthacker';

UPDATE public.contest_participants
   SET agent_id = 'copy_ch_ai_vision'
 WHERE agent_id = 'copy_charthacker';

UPDATE public.competition_agent_exchanges
   SET agent_id = 'copy_ch_ai_vision'
 WHERE agent_id = 'copy_charthacker';

UPDATE public.demo_orders
   SET agent_id = 'copy_ch_ai_vision'
 WHERE agent_id = 'copy_charthacker';

COMMIT;
