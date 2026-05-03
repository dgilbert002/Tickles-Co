-- ============================================================================
-- Phase 10 — Surgeon2 Migration Script (one-shot per company)
-- ============================================================================
-- Purpose: Migrate legacy surgeon2_* tables into canonical tracked_positions
--          and position_updates. Idempotent via ON CONFLICT.
--
-- ROLLBACK (run within 30 days):
--   ALTER TABLE _legacy_surgeon2_state     RENAME TO surgeon2_state;
--   ALTER TABLE _legacy_surgeon2_positions RENAME TO surgeon2_positions;
--   ALTER TABLE _legacy_surgeon2_trade_log RENAME TO surgeon2_trade_log;
--
-- Pre-flight checks:
--   1. Verify surgeon2_positions.id does NOT collide with tracked_positions.id
--      (source_position_id is separate; tracked_positions.id is auto-generated)
--   2. Verify no tracked_positions rows already exist with actor_type='agent',
--      actor_id='surgeon2', actor_instance=''
--   3. Verify position_updates table exists
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Migrate surgeon2_positions → tracked_positions
-- ---------------------------------------------------------------------------
INSERT INTO public.tracked_positions
    (actor_type, actor_id, actor_instance, source_position_id,
     instrument_symbol, instrument_exchange,
     direction, entry_price, position_size,
     entry_reason_agent, entry_reason_trader, entry_reason_llm,
     status, opened_at, closed_at, exit_price, realized_pnl_usd_final,
     company_id, created_at)
SELECT
    'agent', 'surgeon2', '', sp.id,
    sp.symbol, COALESCE(sp.exchange, 'bybit'),
    CASE WHEN sp.side = 'buy' THEN 'long' ELSE 'short' END,
    sp.entry_price, sp.margin * sp.leverage,
    COALESCE(sp.reason, 'no_reason_recorded'), NULL, NULL,
    CASE WHEN sp.closed_at IS NOT NULL THEN 'closed' ELSE 'open' END,
    sp.entry_ts, sp.closed_at, NULL, NULL,
    (SELECT company_id FROM company LIMIT 1),  -- rubicon has one row
    COALESCE(sp.closed_at, sp.entry_ts)
FROM surgeon2_positions sp
ON CONFLICT (actor_type, actor_id, actor_instance, source_position_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Migrate surgeon2_state → agent_state
-- ---------------------------------------------------------------------------
INSERT INTO agent_state (agent_name, actor_instance, state_data, updated_at)
SELECT
    'surgeon2', '',
    jsonb_build_object(
        'legacy_state', jsonb_build_object(
            'starting_balance', starting_balance,
            'balance', balance,
            'realized_pnl', realized_pnl,
            'total_fees', total_fees,
            'cumulative_turnover', cumulative_turnover,
            'trade_counter', trade_counter
        )
    ),
    updated_at
FROM surgeon2_state
ON CONFLICT (agent_name, actor_instance) DO UPDATE
   SET state_data = EXCLUDED.state_data,
       updated_at = EXCLUDED.updated_at;

-- ---------------------------------------------------------------------------
-- 3. Migrate surgeon2_trade_log → position_updates
-- ---------------------------------------------------------------------------
INSERT INTO position_updates
    (position_id, snapshot_at, snapshot_price, snapshot_pnl, note)
SELECT
    tp.id, tl.ts, tl.exit_price, tl.net_pnl,
    tl.action || ': ' || COALESCE(tl.reason, 'no_reason')
FROM surgeon2_trade_log tl
JOIN public.tracked_positions tp
  ON tp.actor_type = 'agent'
 AND tp.actor_id   = 'surgeon2'
 AND tp.actor_instance = ''
 AND tp.source_position_id = tl.trade_id;

COMMIT;

-- ---------------------------------------------------------------------------
-- 4. Post-migration: rename legacy tables (run AFTER parity check passes)
-- ---------------------------------------------------------------------------
-- ALTER TABLE surgeon2_state       RENAME TO _legacy_surgeon2_state;
-- ALTER TABLE surgeon2_positions   RENAME TO _legacy_surgeon2_positions;
-- ALTER TABLE surgeon2_trade_log   RENAME TO _legacy_surgeon2_trade_log;
