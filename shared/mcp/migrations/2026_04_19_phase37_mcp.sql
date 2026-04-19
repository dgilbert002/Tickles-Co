-- Phase 37: MCP stack.
--
-- Two tables:
--   mcp_tools         : declarative registry of exposed tools so
--                       operators can see (and potentially toggle)
--                       which tools are reachable by agents.
--   mcp_invocations   : append-only audit log of every tool call so
--                       we can answer "which agent ran which tool
--                       when, with what parameters, and what did it
--                       return?".
--
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.mcp_tools (
    id             BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    version        TEXT NOT NULL DEFAULT '1',
    description    TEXT NOT NULL DEFAULT '',
    input_schema   JSONB NOT NULL DEFAULT '{}'::JSONB,
    output_schema  JSONB NOT NULL DEFAULT '{}'::JSONB,
    read_only      BOOLEAN NOT NULL DEFAULT TRUE,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    tags           JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS mcp_tools_enabled_idx
    ON public.mcp_tools (enabled);

CREATE TABLE IF NOT EXISTS public.mcp_invocations (
    id             BIGSERIAL PRIMARY KEY,
    tool_name      TEXT NOT NULL,
    tool_version   TEXT,
    caller         TEXT,                 -- chat_id or session id or "stdio"
    transport      TEXT,                 -- stdio | http
    params         JSONB NOT NULL DEFAULT '{}'::JSONB,
    status         TEXT NOT NULL DEFAULT 'ok',   -- ok | error
    result         JSONB,
    error          TEXT,
    latency_ms     INT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS mcp_invocations_tool_idx
    ON public.mcp_invocations (tool_name, started_at DESC);

CREATE INDEX IF NOT EXISTS mcp_invocations_status_idx
    ON public.mcp_invocations (status, started_at DESC);

CREATE OR REPLACE VIEW public.mcp_invocations_recent AS
SELECT id, tool_name, caller, transport, status,
       latency_ms, started_at, completed_at
FROM public.mcp_invocations
ORDER BY started_at DESC
LIMIT 500;

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW IF EXISTS public.mcp_invocations_recent;
-- DROP TABLE IF EXISTS public.mcp_invocations;
-- DROP TABLE IF EXISTS public.mcp_tools;
-- COMMIT;
