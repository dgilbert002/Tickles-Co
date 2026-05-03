-- ============================================================================
-- Phase 6 — pgvector reason embedding + prompt_versions registry
-- ============================================================================
-- Run during low-traffic window: ivfflat index build on populated table
-- may take a few minutes per 10k rows.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;   -- already present from Phase 0; idempotent

-- ============================================================================
-- 1. Add embedding column + index to tracked_positions (shared schema)
-- ============================================================================
ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS entry_reason_trader_embedding vector(384);

-- ivfflat is a good default for ~100k–1M rows; lists tuned for our scale
CREATE INDEX IF NOT EXISTS idx_tp_entry_reason_embed_cosine
  ON public.tracked_positions
  USING ivfflat (entry_reason_trader_embedding vector_cosine_ops)
  WITH (lists = 50);

-- ============================================================================
-- 2. prompt_versions registry (shared schema)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.prompt_versions (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT        NOT NULL,                       -- 'chart_analysis' | 'text_signal_extraction' | 'postmortem' | 'chart_hacker_opinion'
  version       VARCHAR(32) NOT NULL,                       -- '2026.04.29-anti-hallucination-v2'
  prompt_hash   CHAR(16)    NOT NULL,                       -- first 16 hex chars of SHA-256(system + body + taxonomy_rule)
  system        TEXT,                                       -- system message verbatim (may be NULL)
  body          TEXT        NOT NULL,                       -- user/template body verbatim
  taxonomy_rule TEXT,                                       -- G1 clause snapshotted at registration
  model_hint    TEXT,                                       -- e.g. 'requesty/tickles-vision' — informational only
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_by    TEXT,                                       -- commit author / service name
  notes         TEXT,
  UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_name ON public.prompt_versions (name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_hash ON public.prompt_versions (prompt_hash);

-- ============================================================================
-- 3. Ensure ANALYZE so ivfflat index is useful immediately
-- ============================================================================
ANALYZE public.tracked_positions;
