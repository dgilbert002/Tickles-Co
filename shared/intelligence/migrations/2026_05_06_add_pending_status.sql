-- Migration: Add 'pending' to tracked_positions.status CHECK constraint
-- Phase: Position Lifecycle — Pending→Active Gating
-- Date: 2026-05-06
--
-- Design: Signals detected create tracked_positions with status='pending'.
-- PositionMonitor watches live price; when entry price is reached, flips
-- to status='open'. Auto-expire after 7 days if entry never hit.
--
-- This migration drops and recreates the CHECK constraint to include
-- the new 'pending' value.

BEGIN;

-- Drop the existing CHECK constraint and recreate with 'pending'
ALTER TABLE public.tracked_positions
    DROP CONSTRAINT IF EXISTS tracked_positions_status_check;

ALTER TABLE public.tracked_positions
    ADD CONSTRAINT tracked_positions_status_check
        CHECK (status IN (
            'pending',
            'open',
            'partial_exit',
            'closed',
            'expired',
            'invalidated',
            'cancelled'
        ));

-- Update the default status from 'open' to 'pending'
ALTER TABLE public.tracked_positions
    ALTER COLUMN status SET DEFAULT 'pending';

COMMIT;
