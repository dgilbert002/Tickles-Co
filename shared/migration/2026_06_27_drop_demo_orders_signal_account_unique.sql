-- Drop legacy unique index on (tracked_position_id, exchange, account_name).
-- This index was blocking new demo_orders rows when the same tracked_position
-- produced a fresh competition_trade (e.g. re-entry, or after manual cleanup).
-- The new canonical unique guard is uq_demo_orders_open_ct on
-- competition_trade_id for pending/filled rows.
DROP INDEX IF EXISTS public.idx_demo_orders_signal_account;
