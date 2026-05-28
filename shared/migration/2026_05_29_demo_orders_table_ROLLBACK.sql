-- ============================================================================
-- ROLLBACK: demo_orders_table
-- Date: 2026-05-29
-- Purpose: Reverse 2026_05_29_demo_orders_table.sql.
--
-- WARNING: DESTRUCTIVE. This DROPS the demo_orders table and ALL paper-vs-demo
--          mirror history. Only run this in a fresh/dev environment, NEVER on
--          production unless you have a verified backup and intend to discard
--          all demo-order tracking.
-- ============================================================================

BEGIN;

DROP TABLE IF EXISTS public.demo_orders;

COMMIT;
