-- Phase M6: mcp_tool_requests table
-- Stores tool requests from agents for CEO review.
-- Deduplication is by content_hash (SHA-256 of name + rationale).

CREATE TABLE IF NOT EXISTS public.mcp_tool_requests (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    example_input   JSONB,
    example_output  JSONB,
    requested_by    TEXT NOT NULL DEFAULT 'anonymous',
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at     TIMESTAMPTZ,
    review_note     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS mcp_tool_requests_hash_idx
    ON public.mcp_tool_requests (content_hash);

CREATE INDEX IF NOT EXISTS mcp_tool_requests_status_idx
    ON public.mcp_tool_requests (status, created_at DESC);

-- Rollback:
-- BEGIN;
-- DROP TABLE IF EXISTS public.mcp_tool_requests;
-- COMMIT;
