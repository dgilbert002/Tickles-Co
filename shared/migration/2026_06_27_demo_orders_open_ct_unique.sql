-- Phase C (Mirror Rewrite): one LIVE exchange order per open paper trade.
--
-- The bridge now mirrors competition_trades keyed by competition_trade_id.
-- This partial unique index guarantees at most ONE pending/filled demo_orders
-- row per competition_trade_id, so a duplicate placement is impossible at the
-- DB level (the bridge's ON CONFLICT DO NOTHING relies on it). cancelled/
-- rejected rows are excluded so retries after a transient failure still work.
--
-- Applied live 2026-06-27. Idempotent.

CREATE UNIQUE INDEX IF NOT EXISTS uq_demo_orders_open_ct
ON public.demo_orders (competition_trade_id)
WHERE competition_trade_id IS NOT NULL AND status IN ('pending','filled');
