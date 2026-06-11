# PHASE 2 — SCHEMA (storage that can hold everything Discord sends)

> Prereq: Phase 1 DONE and GREEN. Read `00_MASTER_INDEX.md` rules.
> Goal: add the columns we need for reply-chains and clickable local media, widen any
> too-tight columns, add indexes the feed will query on, and mirror the high-water mark
> into `collector_sources` for observability. Pure additive migration — nothing dropped.

This phase touches the DATABASE only (no Python yet). All changes are additive and
idempotent (safe to re-run).

---

## STEP 0 — Snapshot current schema (so we can prove what changed)
```bash
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -c "\d news_items" > /tmp/news_items_before.txt
echo "saved /tmp/news_items_before.txt"
```

## STEP 1 — Write the migration file
Create `/opt/tickles/shared/migrations/bible_p2_discord_schema.sql` with EXACTLY:
```sql
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
```

## STEP 2 — Apply it
```bash
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared \
  -v ON_ERROR_STOP=1 -f /opt/tickles/shared/migrations/bible_p2_discord_schema.sql
echo "exit=$?  (MUST be 0)"
```

## STEP 3 — Heal the correlation_id columns so future ids are never truncated
Phase 1 clamped ids to <=36, so this is optional insurance, NOT required. Only widen if
you want headroom; widening VARCHAR is a safe metadata-only change in Postgres:
```sql
-- OPTIONAL insurance (run only if you want to never think about cid length again):
ALTER TABLE public.api_cost_log          ALTER COLUMN correlation_id TYPE varchar(64);
ALTER TABLE public.signal_interpretations ALTER COLUMN correlation_id TYPE varchar(64);
ALTER TABLE public.tracked_positions      ALTER COLUMN correlation_id TYPE varchar(64);
ALTER TABLE public.position_postmortems   ALTER COLUMN correlation_id TYPE varchar(64);
```
> If you run these, note it in PROGRESS. If you skip them, that's fine — Phase 1's clamp
> already prevents the overflow. Do NOT narrow any column, ever.

## STEP 4 — Mirror HWM into collector_sources (observability only; system_config stays canonical)
The collector keeps its live high-water mark in `system_config` (namespace
`discord_hwm`). We ADDITIONALLY surface "last seen message id" so the control room can
show freshness. `collector_sources.platform_config` is jsonb — store it there:
```sql
-- One-time backfill from system_config into collector_sources.platform_config.last_hwm
UPDATE public.collector_sources cs
SET platform_config = COALESCE(cs.platform_config, '{}'::jsonb)
    || jsonb_build_object('last_hwm', sc.config_value)
FROM public.system_config sc
WHERE sc.namespace = 'discord_hwm'
  AND cs.source_type = 'discord'
  AND cs.platform_id = sc.config_key;
```
(Phase 3 will keep this fresh on each write; this step just seeds it.)

---

## VERIFY (all GREEN before Phase 3)
```bash
echo "=== new columns exist ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc "
SELECT string_agg(column_name, ', ' ORDER BY column_name)
FROM information_schema.columns
WHERE table_name='news_items'
  AND column_name IN ('reply_to_msg_id','reply_to_author','reply_to_content','local_media_paths','author_role_color');"
echo "   ^ MUST list all 5"

echo "=== indexes exist ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc "
SELECT count(*) FROM pg_indexes WHERE tablename='news_items'
AND indexname IN ('idx_news_items_discord_channel_time','idx_news_items_tg_channel_time',
'idx_news_items_message_id','idx_news_items_source_id_time');"
echo "   ^ MUST be 4"

echo "=== default works: a fresh select returns [] not null for local_media_paths ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc \
"SELECT local_media_paths FROM news_items ORDER BY id DESC LIMIT 1;"
echo "   ^ existing rows show NULL (fine); new rows will default to []"
```

## DOWNSTREAM SAFETY
- Confirm the collector STILL writes after the migration (no column-mismatch on insert):
  `... "SELECT count(*) FROM news_items WHERE source='discord' AND collected_at > now() - interval '5 minutes';"` > 0 while channels active.
- Confirm no service crashed on the schema change:
  `systemctl is-active tickles-discord-collector tickles-interpretation tickles-dashboard`
  — all `active`.
- Diff the schema snapshot to SEE exactly what changed:
  `PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -c "\d news_items" > /tmp/news_items_after.txt; diff /tmp/news_items_before.txt /tmp/news_items_after.txt`
  — should show ONLY additions.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 2 — DONE <iso> — added reply_to_*, local_media_paths, author_role_color + 4 indexes to news_items; seeded collector_sources.platform_config.last_hwm. Additive only.`
Then open `03_PHASE_REPLY_AND_MEDIA_CAPTURE.md`.
