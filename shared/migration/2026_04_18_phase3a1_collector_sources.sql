-- ============================================================================
-- Phase 3A.1 — Intelligence Collection Schema (Postgres)
-- Date: 2026-04-18
-- Target DB: tickles_shared
-- Target schema: public
-- Author: ported from collector_sources_design.sql (MySQL, 2026-04-16)
-- ============================================================================
-- WHAT THIS DOES
--   1. Creates `public.collector_sources` — DB-driven subscription table that
--      replaces discord_config.json / telegram_config.json.
--   2. Creates `public.media_items` — one row per downloaded/linked media
--      attachment so every photo/video/voice note is independently trackable.
--   3. Extends `public.news_items` with the metadata columns the collectors
--      already assemble in memory (author, channel, message_id, etc.) — they
--      were being thrown away when writing to the narrow original schema.
--
-- SAFETY
--   * Idempotent (uses IF NOT EXISTS everywhere).
--   * Wrapped in a single transaction so partial application rolls back.
--   * Does NOT delete or rewrite existing rows.
--
-- ROLLBACK
--   See companion file `2026_04_18_phase3a1_collector_sources_ROLLBACK.sql`.
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. collector_sources — DB-driven subscription management
-- ---------------------------------------------------------------------------
-- Self-referencing hierarchy:
--   discord:  server(parent=NULL) -> channel(parent=server) -> user(parent=channel)
--   telegram: group/channel(parent=NULL) -> user(parent=channel)
--   rss:      feed(parent=NULL)
-- Enums are implemented as CHECK constraints so we can add values later
-- without the DDL gymnastics that pg enum types require.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.collector_sources (
    id                          BIGSERIAL       PRIMARY KEY,
    parent_id                   BIGINT          REFERENCES public.collector_sources(id) ON DELETE SET NULL,

    source_type                 VARCHAR(32)     NOT NULL
        CHECK (source_type IN ('telegram','discord','rss','tradingview','api')),
    entity_type                 VARCHAR(32)     NOT NULL
        CHECK (entity_type IN ('server','group','channel','user','feed','topic')),
    platform_id                 VARCHAR(128)    NOT NULL,
    name                        VARCHAR(255)    NOT NULL DEFAULT '',
    description                 TEXT,

    enabled                     BOOLEAN         NOT NULL DEFAULT TRUE,
    priority                    SMALLINT        NOT NULL DEFAULT 5
        CHECK (priority BETWEEN 1 AND 10),
    collection_interval_seconds INT             NOT NULL DEFAULT 120,
    max_messages_per_cycle      INT             NOT NULL DEFAULT 200,
    group_window_seconds        INT             NOT NULL DEFAULT 60,

    media_policy                VARCHAR(64)     NOT NULL DEFAULT 'reference_only'
        CHECK (media_policy IN (
            'ignore',
            'reference_only',
            'download_keep',
            'download_analyze_discard',
            'download_analyze_keep'
        )),

    allowed_users               JSONB,
    blocked_users               JSONB,

    last_collected_at           TIMESTAMPTZ,
    last_error                  TEXT,
    error_count                 INT             NOT NULL DEFAULT 0,
    items_collected             BIGINT          NOT NULL DEFAULT 0,

    platform_config             JSONB,

    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_platform
    ON public.collector_sources (source_type, platform_id);
CREATE INDEX IF NOT EXISTS idx_sources_type
    ON public.collector_sources (source_type);
CREATE INDEX IF NOT EXISTS idx_sources_entity
    ON public.collector_sources (entity_type);
CREATE INDEX IF NOT EXISTS idx_sources_parent
    ON public.collector_sources (parent_id);
CREATE INDEX IF NOT EXISTS idx_sources_enabled
    ON public.collector_sources (enabled, source_type);
CREATE INDEX IF NOT EXISTS idx_sources_last_col
    ON public.collector_sources (last_collected_at);

-- Generic updated_at trigger (reused by other tables that add it later)
CREATE OR REPLACE FUNCTION public.trg_set_updated_at() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_collector_sources_updated_at ON public.collector_sources;
CREATE TRIGGER trg_collector_sources_updated_at
    BEFORE UPDATE ON public.collector_sources
    FOR EACH ROW EXECUTE FUNCTION public.trg_set_updated_at();


-- ---------------------------------------------------------------------------
-- 2. media_items — every piece of media, tracked individually
-- ---------------------------------------------------------------------------
-- Many rows per news_item. Enables per-file dedup (file_hash), independent
-- processing lifecycle (pending -> analyzed), and per-file analysis results.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.media_items (
    id                          BIGSERIAL       PRIMARY KEY,
    news_item_id                BIGINT          NOT NULL
        REFERENCES public.news_items(id) ON DELETE CASCADE,
    source_id                   BIGINT
        REFERENCES public.collector_sources(id) ON DELETE SET NULL,

    media_type                  VARCHAR(32)     NOT NULL
        CHECK (media_type IN (
            'image','video','audio','document','voice',
            'link','embed','webpage','stream'
        )),

    extraction_method           VARCHAR(32)     NOT NULL
        CHECK (extraction_method IN (
            'attached','embedded','linked_in_text',
            'linked_external','cdn_hosted','forwarded'
        )),

    source_url                  TEXT,
    resolved_url                TEXT,
    local_path                  VARCHAR(512),
    thumbnail_path              VARCHAR(512),

    mime_type                   VARCHAR(128),
    file_size_bytes             BIGINT,
    file_hash                   CHAR(64),
    duration_seconds            INT,
    dimensions                  VARCHAR(32),

    processing_status           VARCHAR(32)     NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN (
            'pending','downloading','downloaded','analyzing',
            'analyzed','discarded','failed','skipped'
        )),

    processing_result           JSONB,
    processing_error            TEXT,
    processed_at                TIMESTAMPTZ,

    platform_file_id            VARCHAR(255),
    metadata                    JSONB,

    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_media_news_item  ON public.media_items (news_item_id);
CREATE INDEX IF NOT EXISTS idx_media_source     ON public.media_items (source_id);
CREATE INDEX IF NOT EXISTS idx_media_type       ON public.media_items (media_type);
CREATE INDEX IF NOT EXISTS idx_media_processing ON public.media_items (processing_status);
CREATE INDEX IF NOT EXISTS idx_media_file_hash  ON public.media_items (file_hash);
CREATE INDEX IF NOT EXISTS idx_media_extraction ON public.media_items (extraction_method);
CREATE INDEX IF NOT EXISTS idx_media_created    ON public.media_items (created_at);


-- ---------------------------------------------------------------------------
-- 3. news_items — ALTER to carry traceability + metadata
-- ---------------------------------------------------------------------------
-- All new columns are NULLable (or have defaults) so existing 0-row table
-- and future INSERTs without these columns keep working.
-- ---------------------------------------------------------------------------

ALTER TABLE public.news_items
    ADD COLUMN IF NOT EXISTS source_id    BIGINT       REFERENCES public.collector_sources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS channel_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS author       VARCHAR(255),
    ADD COLUMN IF NOT EXISTS author_id    VARCHAR(128),
    ADD COLUMN IF NOT EXISTS message_id   VARCHAR(128),
    ADD COLUMN IF NOT EXISTS metadata     JSONB,
    ADD COLUMN IF NOT EXISTS has_media    BOOLEAN      NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS media_count  SMALLINT     NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_news_source_id ON public.news_items (source_id);
CREATE INDEX IF NOT EXISTS idx_news_author    ON public.news_items (author);
CREATE INDEX IF NOT EXISTS idx_news_channel   ON public.news_items (channel_name);
CREATE INDEX IF NOT EXISTS idx_news_has_media ON public.news_items (has_media);

COMMIT;

-- ============================================================================
-- SANITY QUERIES (run manually after COMMIT)
-- ============================================================================
-- \d+ public.collector_sources
-- \d+ public.media_items
-- \d+ public.news_items
-- SELECT COUNT(*) FROM public.collector_sources;  -- expect 0 before seeding
-- ============================================================================
