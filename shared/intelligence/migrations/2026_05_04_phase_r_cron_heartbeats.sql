-- shared/intelligence/migrations/2026_05_04_phase_r_cron_heartbeats.sql
-- Phase R [BM] — Cron health canary
-- Rollback: DROP TABLE public.cron_heartbeats;

CREATE TABLE IF NOT EXISTS public.cron_heartbeats (
    agent_id        TEXT PRIMARY KEY,
    last_run_at     TIMESTAMPTZ NOT NULL,
    last_status     TEXT NOT NULL CHECK (last_status IN ('ok', 'error', 'partial')),
    last_message    TEXT,
    expected_interval_seconds INT NOT NULL,
    consecutive_failures INT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cron_heartbeats_stale
    ON public.cron_heartbeats (last_run_at)
    WHERE last_status != 'ok';

COMMENT ON TABLE public.cron_heartbeats IS
    'Phase R [BM] — every cron-driven agent UPSERTs on each fire. cron_canary daemon polls and alerts when stale.';
