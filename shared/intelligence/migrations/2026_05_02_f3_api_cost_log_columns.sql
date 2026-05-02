-- Migration: F3 — api_cost_log column drift repair
-- Date: 2026-05-02
-- Context:
--   The api_cost_log writer at shared/utils/api_cost_log.py expects 8 columns
--   (operation, agent_id, temperature, correlation_id, request_path,
--    response_path, success, http_status, extra) plus a unique index on
--   (correlation_id, operation) for ON CONFLICT DO NOTHING idempotency.
--   These columns were missing in production, causing every gateway call
--   to log a warning and silently drop the cost row. F3 (LLM post-mortem
--   service) was the first user-visible failure: chat_completion's
--   fire-and-forget cost log raised UndefinedColumnError on every call.
-- Effect:
--   Adds the missing columns (NOT NULL with safe empty-string / 200 / true
--   defaults so existing rows stay valid) and creates the full unique index
--   so ON CONFLICT (correlation_id, operation) inference works.
-- Idempotent: safe to re-run.

BEGIN;

ALTER TABLE public.api_cost_log
    ADD COLUMN IF NOT EXISTS operation      varchar(100) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS agent_id       varchar(100) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS temperature    numeric(4,2),
    ADD COLUMN IF NOT EXISTS correlation_id varchar(36)  NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS request_path   varchar(200) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS response_path  varchar(200) NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS success        boolean      NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS http_status    integer      NOT NULL DEFAULT 200,
    ADD COLUMN IF NOT EXISTS extra          jsonb;

-- Full (non-partial) unique index — required by asyncpg for ON CONFLICT
-- inference to match. A partial index does NOT satisfy the constraint
-- specification used by shared/utils/api_cost_log.py:_INSERT_SQL.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_api_cost_log_corr_op
    ON public.api_cost_log (correlation_id, operation);

COMMIT;
