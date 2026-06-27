-- ROLLBACK for 2026_06_27_drop_demo_orders_signal_account_unique.sql
-- Recreates the old partial unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_demo_orders_signal_account
ON public.demo_orders (tracked_position_id, exchange, account_name)
WHERE tracked_position_id IS NOT NULL;
