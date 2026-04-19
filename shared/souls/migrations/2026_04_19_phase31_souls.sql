-- Phase 31: Apex / Quant / Ledger modernised souls
-- Each "soul" (Paperclip agent) has a stable identity, a versioned
-- prompt, and leaves behind an auditable trail of decisions. LLM
-- inference is optional: deterministic Python souls can run in the
-- same tables so Rule 1 (backtests == live) stays tractable even when
-- LLMs are mocked.
-- Ownership: tickles_shared.

BEGIN;

CREATE TABLE IF NOT EXISTS public.agent_personas (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,     -- apex / quant / ledger / ...
    role            TEXT NOT NULL,            -- decision / research / bookkeeper / ...
    description     TEXT,
    default_llm     TEXT,                     -- adapter identifier in openclaw
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.agent_prompts (
    id              BIGSERIAL PRIMARY KEY,
    persona_id      BIGINT NOT NULL REFERENCES public.agent_personas(id),
    version         INTEGER NOT NULL,
    template        TEXT NOT NULL,
    variables       JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (persona_id, version)
);

CREATE TABLE IF NOT EXISTS public.agent_decisions (
    id              BIGSERIAL PRIMARY KEY,
    persona_id      BIGINT NOT NULL REFERENCES public.agent_personas(id),
    company_id      TEXT,
    correlation_id  TEXT NOT NULL,            -- links together a flow (e.g. strategy intent id)
    mode            TEXT NOT NULL,            -- deterministic / llm / hybrid
    verdict         TEXT NOT NULL,            -- approve / reject / propose / journal / observe
    confidence      NUMERIC(6,4) NOT NULL DEFAULT 0,
    rationale       TEXT,
    inputs          JSONB NOT NULL DEFAULT '{}'::JSONB,
    outputs         JSONB NOT NULL DEFAULT '{}'::JSONB,
    metadata        JSONB NOT NULL DEFAULT '{}'::JSONB,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS agent_decisions_persona_idx
    ON public.agent_decisions (persona_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS agent_decisions_corr_idx
    ON public.agent_decisions (correlation_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS agent_decisions_company_idx
    ON public.agent_decisions (company_id, decided_at DESC)
    WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS agent_decisions_verdict_idx
    ON public.agent_decisions (verdict, decided_at DESC);

CREATE OR REPLACE VIEW public.agent_decisions_latest AS
SELECT DISTINCT ON (persona_id, correlation_id)
    d.id, d.persona_id, p.name AS persona_name, d.company_id,
    d.correlation_id, d.mode, d.verdict, d.confidence, d.rationale,
    d.inputs, d.outputs, d.metadata, d.decided_at
FROM public.agent_decisions d
JOIN public.agent_personas p ON p.id = d.persona_id
ORDER BY d.persona_id, d.correlation_id, d.decided_at DESC;

COMMIT;

-- Rollback:
-- BEGIN;
-- DROP VIEW  IF EXISTS public.agent_decisions_latest;
-- DROP TABLE IF EXISTS public.agent_decisions;
-- DROP TABLE IF EXISTS public.agent_prompts;
-- DROP TABLE IF EXISTS public.agent_personas;
-- COMMIT;
