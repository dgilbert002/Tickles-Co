-- Phase 35: Local-to-VPS Backtest Submission.
--
-- Durable audit + status table for every backtest job submitted to
-- the VPS. The existing Redis queue (shared.backtest.queue) remains
-- the execution transport, but this table is what operators (and the
-- local CLI) query for history, idempotency, and result retrieval.
--
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.backtest_submissions (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    client_id       TEXT,                        -- who submitted (hostname/user)
    spec            JSONB NOT NULL,              -- full canonical job payload
    spec_hash       TEXT NOT NULL,               -- sha256 of canonical spec
    status          TEXT NOT NULL DEFAULT 'submitted',
        -- submitted | queued | running | completed | failed | cancelled
    queue_job_id    TEXT,                        -- envelope id in Redis queue
    result_summary  JSONB,                       -- pnl, trades, sharpe, …
    artefacts       JSONB NOT NULL DEFAULT '{}'::JSONB,
        -- {ch_table: …, equity_curve_path: …, …}
    error           TEXT,                        -- last error on failure
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    queued_at       TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS backtest_submissions_status_idx
    ON public.backtest_submissions (status, submitted_at DESC);

CREATE INDEX IF NOT EXISTS backtest_submissions_client_idx
    ON public.backtest_submissions (client_id, submitted_at DESC);

-- Dedupe: the same canonical spec cannot be resubmitted while still
-- active or completed. Cancelled / failed jobs release the lock so
-- operators can retry after fixing the underlying issue.
CREATE UNIQUE INDEX IF NOT EXISTS backtest_submissions_hash_active_idx
    ON public.backtest_submissions (spec_hash)
    WHERE status IN ('submitted', 'queued', 'running', 'completed');

CREATE OR REPLACE VIEW public.backtest_submissions_active AS
SELECT *
FROM public.backtest_submissions
WHERE status IN ('submitted', 'queued', 'running');

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW IF EXISTS public.backtest_submissions_active;
-- DROP TABLE IF EXISTS public.backtest_submissions;
-- COMMIT;
