-- 2026-05-29 — Forensic Paper vs Demo vs Live
-- Add fee + funding columns to demo_orders so the forensic audit can account
-- for "every $0.01": execution fees on entry/exit and overnight/funding fees.
-- These are populated by demo_bridge._reconcile_fills() from the exchange's
-- order/trade history. NULL = not yet fetched (not "zero fees").

ALTER TABLE public.demo_orders
    ADD COLUMN IF NOT EXISTS entry_fee   NUMERIC,   -- execution fee on the fill (USDT)
    ADD COLUMN IF NOT EXISTS exit_fee    NUMERIC,   -- execution fee on the close (USDT)
    ADD COLUMN IF NOT EXISTS funding_fee NUMERIC,   -- accrued funding/overnight fee (USDT)
    ADD COLUMN IF NOT EXISTS fees_synced_at TIMESTAMPTZ;  -- when fees were last fetched

COMMENT ON COLUMN public.demo_orders.entry_fee IS
    'Execution fee charged by the exchange on the entry fill (USDT). '
    'NULL until demo_bridge reconciles the fill from trade history.';
COMMENT ON COLUMN public.demo_orders.exit_fee IS
    'Execution fee charged by the exchange on the closing fill (USDT).';
COMMENT ON COLUMN public.demo_orders.funding_fee IS
    'Accrued funding / overnight holding fee while the position was open (USDT).';
