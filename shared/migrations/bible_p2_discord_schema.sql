-- BIBLE-P2 — additive, idempotent schema for reply chains + clickable local media.
-- Safe to run multiple times. No drops, no type narrowing.
BEGIN;

-- 1) Reply-chain capture (Discord + Telegram). The collector already parses these in
--    memory; Phase 3 wires them to these columns.
ALTER TABLE public.news_items ADD COLUMN IF NOT EXISTS reply_to_msg_id  varchar(128);
ALTER TABLE public.news_items ADD COLUMN IF NOT EXISTS reply_to_author  varchar(255);
ALTER TABLE public.news_items ADD COLUMN IF NOT EXISTS reply_to_content text;

-- 2) Local media paths for click-to-load fullscreen (jsonb array of relative paths).
--    has_media / media_count already exist; this stores WHERE the files actually are.
ALTER TABLE public.news_items ADD COLUMN IF NOT EXISTS local_media_paths jsonb DEFAULT '[]'::jsonb;

-- 3) Author role color (for Discord-identical role-colored usernames). Nullable hex.
ALTER TABLE public.news_items ADD COLUMN IF NOT EXISTS author_role_color varchar(9);

-- 4) Indexes the feed/tree queries will hit hard.
--    Feed paginates by channel + recency:
CREATE INDEX IF NOT EXISTS idx_news_items_discord_channel_time
    ON public.news_items (channel_name, collected_at DESC)
    WHERE source = 'discord';
--    Telegram feed:
CREATE INDEX IF NOT EXISTS idx_news_items_tg_channel_time
    ON public.news_items (channel_name, collected_at DESC)
    WHERE source = 'telegram';
--    Reply lookups (show parent on top):
CREATE INDEX IF NOT EXISTS idx_news_items_message_id
    ON public.news_items (message_id)
    WHERE message_id IS NOT NULL;
--    Unread/recent counting per source_id:
CREATE INDEX IF NOT EXISTS idx_news_items_source_id_time
    ON public.news_items (source_id, collected_at DESC)
    WHERE source_id IS NOT NULL;

COMMIT;

-- NOTE: correlation_id VARCHAR widen skipped — position_postmortems is used by a view
-- (v_memory_feed_7d). Phase 1's clamp already prevents the overflow; the column widening
-- is optional insurance and not needed.
