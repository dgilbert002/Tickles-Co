-- ROLLBACK for 2026_05_29_demo_orders_fees.sql
ALTER TABLE public.demo_orders
    DROP COLUMN IF EXISTS entry_fee,
    DROP COLUMN IF EXISTS exit_fee,
    DROP COLUMN IF EXISTS funding_fee,
    DROP COLUMN IF EXISTS fees_synced_at;
