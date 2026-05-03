-- ============================================================================
-- Phase 7 — MemU outbox table (shared schema)
-- ============================================================================
-- Durable outbox for broadcast insights. Listener back-fills unprocessed rows.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.memu_outbox (
    id            BIGSERIAL PRIMARY KEY,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ NULL,
    last_error    TEXT NULL,
    attempt_count INT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memu_outbox_unprocessed
    ON public.memu_outbox (created_at)
    WHERE processed_at IS NULL;
