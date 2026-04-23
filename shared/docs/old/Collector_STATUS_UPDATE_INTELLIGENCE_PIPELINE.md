# Status Update: Intelligence Collection Pipeline

> **Date**: 2026-04-18
> **Context**: Extends ROADMAP_V2.md with a new phase for collector infrastructure,
> media processing, and dual-store (Postgres + Qdrant) signal intelligence.
> **Triggered by**: Dean's observation that collectors should feed a queryable
> intelligence layer — "could you tell me the latest news from channels?" — and
> that TradingView chart screenshots from professional traders should be
> LLM-analyzed and searchable.
> **References**: `collector_sources_design.sql` (workspace), `CODE_REVIEW_COLLECTORS.md` (workspace)

---

## 1. The Problem

The collectors work. Discord has 7,783 items, Telegram has 3,286, RSS has 618.
But the data is nearly useless for intelligence queries because:

- **No source tracking** — every row says `source='discord'` but not which server,
  channel, or user posted it
- **No media tracking** — images are downloaded to disk but not tracked in the DB,
  never analyzed, never searchable
- **No structured metadata** — the collectors extract rich context (author, channel,
  server, reply chains, TradingView URLs) then `write_to_db()` throws it all away
  and writes only 8 flat columns
- **No vector search** — Qdrant is running (localhost:6333) with 2 Mem0 points in it.
  Zero news content has ever been embedded. No semantic search exists.
- **No processing pipeline** — a TradingView chart screenshot sits on disk forever.
  No LLM looks at it. No extraction of instrument, bias, levels, or setup type.

**Bottom line**: An agent cannot answer "what bullish BTC setups did professional
traders post in the last 24 hours?" — even though the raw data is sitting right there.

---

## 2. Code Review — 10 Consistency Issues Found

Full details in `~/workspace/schema/CODE_REVIEW_COLLECTORS.md`. Summary:

### Critical (blocks intelligence pipeline)

| # | Issue | Files |
|---|-------|-------|
| 001 | `write_to_db()` discards author, media_type, media_path, media_urls, and full metadata dict | `base.py` |
| 005 | Only first media item per message group is recorded — rest silently dropped | `discord_collector.py`, `telegram_collector.py` |
| 009 | Zero Qdrant/vector integration anywhere in the collector pipeline | All files |

### Moderate (architectural debt)

| # | Issue | Files |
|---|-------|-------|
| 002 | `media_extractor.py` exists as unified service but neither collector uses it — both have duplicate download code | `media_extractor.py`, `discord_collector.py`, `telegram_collector.py` |
| 003 | Config lives in JSON files, not DB — no dynamic management by agents | `discord_config.json`, `telegram_config.json` |
| 004 | Discord JSON config has `category`, `trigger_mode`, `discord_category` fields that the collector never reads | `discord_config.json` |
| 006 | No URL extraction from message text (partial TradingView regex in Discord only) | Both collectors |
| 008 | `CollectorConfig` dataclass duplicates what DB table should provide | `base.py` |

### Low

| # | Issue | Files |
|---|-------|-------|
| 007 | RSS collector ignores media enclosures entirely | `rss_collector.py` |
| 010 | `NewsSource` enum doesn't match CONTEXT_V3 values (`rss` vs `rss_yahoo`, missing `coinglass`) | `base.py` vs CONTEXT_V3.md |

### Data loss per insert (what collectors extract vs what reaches the DB)

```
COLLECTED BUT LOST:                    WHAT ACTUALLY GETS STORED:
─────────────────                      ────────────────────────
author / sender_username               hash_key
channel_id / channel_name              source (just "discord"/"telegram")
server_id / server_name                headline
sender_id / sender_name                content (with media URLs hacked in as text)
msg_count                              sentiment (always NULL)
reply_to info                          instruments (always "[]")
media_type                             published_at
media_path (only first)                collected_at
media_urls (appended as text)
discord_url
TradingView chart URLs
discord_category
trigger_mode
category
```

---

## 3. The Design — Three New Tables + Vector Store

### 3.1 `collector_sources` — DB-driven subscription management

Replaces `discord_config.json` and `telegram_config.json`.

```
collector_sources
├── id (PK)
├── parent_id (FK self) ──── server → channel → user hierarchy
├── source_type ──────────── discord, telegram, rss, tradingview, api
├── source_level ─────────── server, channel, user, feed
├── platform_id ──────────── "1301127166611689483" (Discord server ID)
├── name ─────────────────── "Chart Hackers"
├── description ──────────── "💥 | SKILLED TRADERS"
├── is_active ────────────── true/false
├── media_policy ─────────── ignore | reference_only | download_keep |
│                             download_analyze_discard | download_analyze_keep
├── collection_interval_s ── 120
├── max_messages_per_cycle ─ 200
├── group_window_s ────────── 60
├── priority ─────────────── 1-10
├── tags ─────────────────── ["alpha_signals", "charts"]
├── allowed_users ─────────── ["user1", "user2"] or empty = all
├── blocked_users ─────────── ["bot1"]
├── platform_config ───────── {"trigger_mode": "confidence", ...}
├── last_collected_at
├── last_hwm_value
├── items_collected_total
├── error_count
├── created_at / updated_at
```

**What this enables**: An agent says "add this Telegram channel" → INSERT a row.
Collector picks it up next cycle. No JSON files, no restarts.

### 3.2 `media_items` — every piece of media, tracked individually

```
media_items
├── id (PK)
├── news_item_id (FK) ────── which message this belongs to
├── source_id (FK) ────────── which collector_source produced it
├── media_type ────────────── photo, video, voice, document, link
├── extraction_method ─────── attached, embedded, linked_in_text,
│                              linked_external, cdn_hosted, forwarded
├── source_url ────────────── original CDN/platform URL
├── resolved_url ──────────── after redirects
├── local_path ────────────── /opt/tickles/data/media/discord/...
├── file_hash ─────────────── SHA-256 of file bytes (cross-channel dedup)
├── file_size_bytes
├── mime_type
├── processing_status ─────── pending → downloading → downloaded →
│                              analyzing → analyzed → discarded
├── processing_result ─────── JSON: the LLM analysis output
│   {
│     "instrument": "BTC/USDT",
│     "timeframe": "4h",
│     "bias": "bullish",
│     "setup_type": "breakout_retest",
│     "key_levels": {"support": 64200, "resistance": 67800},
│     "indicators_visible": ["RSI", "EMA_200"],
│     "confidence": "high",
│     "summary": "Clean breakout above 66k...",
│     "tags": ["breakout", "retest", "BTC"]
│   }
├── processing_model ──────── which LLM analyzed it
├── processing_cost_tokens ── token usage tracking
├── vector_id ─────────────── Qdrant point ID (if embedded)
├── created_at / updated_at
```

**Key design**: A message with 5 images = 5 rows. `file_hash` catches the same
chart shared across 3 channels. `processing_result` JSON is where the real
intelligence value lives.

### 3.3 `news_items` ALTER — add source traceability

```sql
ALTER TABLE news_items ADD COLUMN source_id    BIGINT REFERENCES collector_sources(id);
ALTER TABLE news_items ADD COLUMN author       VARCHAR(200);
ALTER TABLE news_items ADD COLUMN author_id    VARCHAR(100);
ALTER TABLE news_items ADD COLUMN channel_name VARCHAR(200);
ALTER TABLE news_items ADD COLUMN message_id   VARCHAR(100);  -- platform msg ID
ALTER TABLE news_items ADD COLUMN metadata     JSONB;
ALTER TABLE news_items ADD COLUMN has_media    BOOLEAN DEFAULT FALSE;
ALTER TABLE news_items ADD COLUMN media_count  SMALLINT DEFAULT 0;
ALTER TABLE news_items ADD COLUMN vector_id    UUID;           -- Qdrant point ID
```

### 3.4 Qdrant `tickles_signals` collection

```
Collection: tickles_signals
Vectors: 384 dimensions (all-MiniLM-L6-v2, already installed)
Distance: Cosine

Payload per point:
  news_item_id: int
  media_item_id: int (nullable)
  source_type: keyword
  author: keyword
  channel_name: keyword
  instrument: keyword (nullable)
  bias: keyword (nullable)
  media_type: keyword (nullable)
  published_at: datetime
  content_type: keyword  -- "message", "chart_analysis", "voice_transcript"
```

**Query pattern** (agent asks "bullish BTC setups from pro traders, last 24h"):
1. **Postgres** — `WHERE source_type='discord' AND has_media=TRUE AND published_at > now()-'24h'`
2. **Qdrant** — semantic search: `"bullish BTC setup breakout support level"`
3. **Merge** — intersect by `news_item_id`, rank by relevance × recency

---

## 4. The Processing Pipeline

```
                    ┌─────────────┐
                    │  Collectors  │  Discord / Telegram / RSS
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  news_items  │  + source_id, author, channel_name
                    │  media_items │  processing_status = 'pending'
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │   MediaProcessorService  │  Async worker
              │                          │
              │  photo → LLM vision      │  "Analyze this TradingView chart"
              │  voice → Whisper STT     │  transcribe audio
              │  video → frame extract   │  key frames → LLM vision
              │  link  → fetch + parse   │  URL → readable content
              └────────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  media_items │  processing_status = 'analyzed'
                    │              │  processing_result = {JSON}
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │   EmbeddingService       │
              │                          │
              │  news content → embed    │
              │  processing_result → embed│
              │  Store in Qdrant         │
              └────────────┬────────────┘
                           │
              ┌────────────▼────────────┐
              │  Postgres    │   Qdrant  │
              │  (structured)│  (semantic)│
              │  WHO/WHERE/  │  WHAT IT   │
              │  WHEN/TYPE   │  MEANS     │
              └──────────────┴───────────┘
                           │
              ┌────────────▼────────────┐
              │   SignalSearchService     │
              │   Dual-store query API   │
              │   for trading agents     │
              └─────────────────────────┘
```

---

## 5. Where This Fits in the Roadmap

ROADMAP_V2.md currently has Phases 0-9. This inserts as **Phase 3A** — after
CandleRollupService (Phase 3) and before Gap Detection (Phase 4). It can also
run in parallel with Phases 2-4 since it touches collectors, not candle infra.

### Updated phase sequence:

| Phase | What | Status |
|-------|------|--------|
| 0 | Infrastructure migration (PG + CH + Redis + MemU) | ✅ Complete |
| 1 | Live cutover to Postgres | ⏳ With Dean |
| 2 | 1-minute candle daemon | ✅ Complete (in Phase 1 overnight build) |
| 3 | CandleRollupService + CandleLoader | ⏰ |
| **3A** | **Intelligence Collection Infrastructure** | **⏰ NEW — this document** |
| 4 | Gap detection + completeness | ⏰ |
| 5 | AgentDataService + CollectorControlService | ⏰ (partially absorbed by 3A) |
| 6 | ClickHouse backtest pipeline | ✅ Complete (in Phase 1 overnight build) |
| 7 | Wire MemU into agent stack | ⏰ |
| 8 | Guardrails | ⏰ |
| 9 | Decommission MySQL | ⏰ |

### Phase 3A breakdown:

**Step 3A.1 — Schema** (1 day)
- CREATE `collector_sources` table in Postgres
- CREATE `media_items` table in Postgres
- ALTER `news_items` with new columns
- CREATE Qdrant `tickles_signals` collection
- Migrate `discord_config.json` → `collector_sources` rows (32 channels)
- Migrate `telegram_config.json` → `collector_sources` rows

**Step 3A.2 — Collector refactor** (2 days)
- Rewrite `base.py` `write_to_db()` → writes news_items + media_items with full metadata
- Refactor `discord_collector.py` → reads config from `collector_sources`, uses `MediaExtractor`, emits ALL media
- Refactor `telegram_collector.py` → same treatment
- Consolidate duplicate download code into `media_extractor.py`
- Add URL extraction utility (TradingView, YouTube, Twitter, general links)
- Update `NewsItem` dataclass with `source_id` field
- Backfill existing 11,687 news_items with author/channel from content parsing where possible

**Step 3A.3 — Media processing pipeline** (2 days)
- Build `MediaProcessorService` — async worker watching for `processing_status='downloaded'`
- Chart analysis prompt: LLM vision call → structured JSON (instrument, bias, levels, setup_type)
- Voice transcription: Whisper/Groq STT → text stored in `processing_result`
- URL fetch + parse for linked content
- Processing status lifecycle: pending → downloading → downloaded → analyzing → analyzed
- Cost tracking per analysis (tokens used, model used)

**Step 3A.4 — Embedding + search** (1 day)
- Build `EmbeddingService` — embeds news content + processing_results into Qdrant
- Uses existing sentence-transformers (all-MiniLM-L6-v2, 384 dims)
- Build `SignalSearchService` — dual-store query: Postgres filter + Qdrant semantic search
- Expose via catalog API: `/signals/search?q=bullish+BTC&hours=24&source=discord`

**Step 3A.5 — Agent wiring** (1 day)
- Add signal search to `AgentDataService` (Phase 5)
- Add collector management to `CollectorControlService` (Phase 5):
  `add_source()`, `remove_source()`, `list_sources()`, `update_media_policy()`
- Wire into MemU: significant chart analyses get written as `market_signals` insights

**Estimated total: 7 days** (can overlap with Phases 3-4)

---

## 6. Code Changes Required After Schema

| File | Change | Effort |
|------|--------|--------|
| `shared/collectors/base.py` | Rewrite `write_to_db()` for new columns + media_items insert. Add `source_id` to `NewsItem`. | Major |
| `shared/collectors/discord/discord_collector.py` | Read config from DB, use `MediaExtractor`, emit all media per group, stop using inline download functions. | Major |
| `shared/collectors/telegram/telegram_collector.py` | Same as Discord — DB config, `MediaExtractor`, all media. | Major |
| `shared/collectors/media_extractor.py` | Becomes central: writes to `media_items`, adds `file_hash` computation, URL extraction. | Major |
| `shared/collectors/news/rss_collector.py` | Read feed URLs from `collector_sources`. Minor. | Minor |
| `shared/services/run_all_collectors.py` | Read collection targets from DB. | Minor |
| **New**: `shared/services/media_processor.py` | LLM vision for charts, Whisper for audio, URL fetcher. | New |
| **New**: `shared/services/embedding_service.py` | Embed news + processing_results into Qdrant. | New |
| **New**: `shared/services/signal_search.py` | Dual-store query (Postgres + Qdrant). | New |
| **New**: `shared/utils/url_extractor.py` | Extract and classify URLs from text. | New |

---

## 7. Migration Script Needed

```
scripts/migrate_collector_sources.py
  1. Read discord_config.json → INSERT collector_sources (server + channel rows)
  2. Read telegram_config.json → INSERT collector_sources
  3. Read RSS DEFAULT_FEEDS from rss_collector.py → INSERT collector_sources
  4. Migrate discord_hwm / telegram_hwm from system_config → last_hwm_value column
  5. Backfill news_items: parse content for [CDN_URLS] and [LOCAL_MEDIA] text hacks,
     update has_media/media_count columns
  6. Backfill news_items: extract author from first line patterns where possible
```

---

## 8. What This Delivers

When Phase 3A is complete, an agent can:

1. **"What's the latest from Chart Hackers?"**
   → `SELECT * FROM news_items ni JOIN collector_sources cs ON ni.source_id=cs.id
      WHERE cs.name='Chart Hackers' ORDER BY published_at DESC LIMIT 10`

2. **"Any bullish BTC setups from Nagel in the last 24h?"**
   → Postgres: filter by author LIKE '%nagel%', has_media=true, 24h window
   → Qdrant: semantic search "bullish BTC setup breakout"
   → Merge + rank

3. **"Add this Telegram channel to monitoring"**
   → `INSERT INTO collector_sources (source_type, source_level, platform_id, name, ...)`
   → Collector picks it up next cycle. No config files, no restarts.

4. **"What did the chart analysis find on that SOL screenshot?"**
   → `SELECT mi.processing_result FROM media_items mi
      WHERE mi.news_item_id=X AND mi.processing_status='analyzed'`
   → Returns structured JSON with instrument, bias, key levels, setup type

5. **"Show me all voice notes from Telegram channels this week"**
   → `SELECT * FROM media_items WHERE media_type='voice'
      AND processing_status='analyzed' AND created_at > now()-'7d'`
   → Returns transcriptions in processing_result

---

## 9. Dependencies and Prerequisites

| Dependency | Status | Notes |
|------------|--------|-------|
| Postgres 16 | ✅ Running | Tables go here |
| Qdrant | ✅ Running (localhost:6333) | New `tickles_signals` collection needed |
| sentence-transformers | ✅ Installed | all-MiniLM-L6-v2, 384 dims |
| LLM vision API | ⏰ Need to configure | OpenRouter for chart analysis (gpt-4.1 or gemini-2.5-pro) |
| Whisper/Groq STT | ⏰ Need Groq API key | For voice note transcription |
| Phase 1 cutover | ⏳ Waiting on Dean | Collectors must target Postgres before refactor |
| `collector_sources_design.sql` | ✅ Written | In workspace/schema/ — needs Postgres DDL translation |

---

## 10. Decisions Needed From Dean

1. **Priority**: Do this before or after candle rollup (Phase 3)?
   They can run in parallel, but sequencing matters for dev focus.
2. **LLM for chart analysis**: Use gpt-4.1 (vision)? gemini-2.5-pro?
   Cost vs quality tradeoff per image.
3. **Groq API key**: Needed for voice transcription. Free tier available
   at console.groq.com/keys.
4. **Media retention**: How long to keep downloaded images on disk?
   Suggestion: 30 days for analyzed, 7 days for unprocessed.
5. **Embedding scope**: Embed ALL news_items, or only items from
   `alpha_signals` / `signals` category sources?

---

*This document extends ROADMAP_V2.md. The full SQL design is in
`~/workspace/schema/collector_sources_design.sql` and the code review
is in `~/workspace/schema/CODE_REVIEW_COLLECTORS.md`.*

---

## 11. Proposed Code Edits (Illustrative)

This section provides concrete before/after examples for the most critical code changes required. The goal is to show the *intent* of the changes, not production-ready code.

### 11.1 `shared/collectors/base.py` — `write_to_db()`

**Objective**: Stop throwing away metadata. Write to the new `news_items` columns and create corresponding `media_items` rows.

```diff
--- a/shared/collectors/base.py
+++ b/shared/collectors/base.py
@@ -107,44 +107,81 @@
         if not items:
             return 0
 
-        values = []
+        news_values = []
+        media_to_insert = []
+
         for item in items:
             if not item.hash_key:
                 item.compute_hash()
 
-            # Build content with media references if present
-            content = item.content
-            if item.media_urls:
-                media_section = f"\n[CDN_URLS]: {json.dumps(item.media_urls)}"
-                content = content + media_section if content else media_section
-            if item.media_path:
-                media_section = f"\n[LOCAL_MEDIA]: {item.media_path}"
-                content = content + media_section if content else media_section
+            # Ensure timezone-aware datetimes
+            published_at = item.published_at.astimezone(timezone.utc) if item.published_at and item.published_at.tzinfo else item.published_at
 
-            values.append((
+            # Extract author info from1 metadata if available
+            author_name = item.metadata.get("sender_username") or item.metadata.get("sender_name") or item.author
+            author_id = item.metadata.get("sender_id")
+            channel_name = item.metadata.get("channel_name")
+            message_id = item.metadata.get("first_msg_id") or item.metadata.get("message_id")
+            source_id = item.metadata.get("source_id") # FK to collector_sources
+
+            news_values.append((
                 item.hash_key,
                 item.source.value,
                 item.headline,
-                content,
-                None,  # sentiment — no detection
-                "[]",  # instruments — no detection
+                item.content,
+                source_id,                      # NEW: Foreign Key
+                author_name,                    # NEW: Author Name
+                author_id,                      # NEW: Author ID
+                channel_name,                   # NEW: Channel Name
+                message_id,                     # NEW: Message ID
                 published_at,
                 item.collected_at,
+                json.dumps(item.metadata),      # NEW: Full metadata as JSONB
+                bool(item.media_urls or item.media_path), # NEW: has_media
+                len(item.media_urls or []),       # NEW: media_count
             ))
 
-        query = """INSERT IGNORE INTO tickles_shared.news_items
-                   (hash_key, source, headline, content, sentiment, instruments,
-                    published_at, collected_at)
-                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"""
+            # Prepare media_items for batch insert
+            if item.media_urls:
+                for url in item.media_urls:
+                    media_to_insert.append({
+                        "news_item_hash": item.hash_key, "source_id": source_id,
+                        "media_type": item.media_type or "photo", "extraction_method": "cdn_hosted",
+                        "source_url": url, "processing_status": "pending"
+                    })
+
+        news_query = """INSERT INTO tickles_shared.news_items
+                   (hash_key, source, headline, content, source_id, author, author_id,
+                    channel_name, message_id, published_at, collected_at, metadata,
+                    has_media, media_count)
+                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
+                   ON CONFLICT (hash_key) DO NOTHING
+                   RETURNING id, hash_key"""
+
+        media_query = """INSERT INTO tickles_shared.media_items
+                    (news_item_id, source_id, media_type, extraction_method, source_url, processing_status)
+                    VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING"""
 
         try:
-            inserted_count = await db_pool.execute_many(query, values)
-            if inserted_count > 0:
-                self.items_collected += inserted_count
+            # Insert news items and get their new IDs to link media
+            inserted_news = await db_pool.fetch_all(news_query, news_values)
+            inserted_count = len(inserted_news)
+
+            if inserted_count:
                 self.last_collected = datetime.now(timezone.utc)
                 logger.info(
                      "Collector '%s': %d/%d items inserted",
                      self.name, inserted_count, len(items),
                 )
+
+            # Map hash_key to news_item.id for media FK
+            hash_to_id = {row['hash_key']: row['id'] for row in inserted_news}
+            media_values = [
+                (hash_to_id[m['news_item_hash']], m['source_id'], m['media_type'], m['extraction_method'], m['source_url'], m['processing_status'])
+                for m in media_to_insert if m['news_item_hash'] in hash_to_id
+            ]
+            if media_values:
+                await db_pool.execute_many(media_query, media_values)
+
             return inserted_count
         except Exception as e:
             self.errors += 1
```

### 11.2 `shared/collectors/discord_collector.py` — Config & Media Handling

**Objective**: Replace hardcoded JSON config with a DB query; hand off media to a central service instead of using inline download logic.

```diff
--- a/shared/collectors/discord/discord_collector.py
+++ b/shared/collectors/discord/discord_collector.py
@@ -1,13 +1,13 @@
 import asyncio
 import json
 import logging
 import os
-import re
 from1 datetime import datetime, timezone
 from1 typing import Any, Dict, List, Optional
 
 import aiohttp
 
 from1 shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
+from1 shared.services.media_extractor_service import MediaExtractorService # Assumed refactor
 from1 shared.utils.db import DatabasePool
 
 logger = logging.getLogger("tickles.discord")
@@ -20,29 +20,21 @@
 
 HWM_NAMESPACE = "discord_hwm"
 
-def _load_json_config() -> Dict[str, Any]:
-    if os.path.exists(CONFIG_PATH):
-        with open(CONFIG_PATH, "r") as f:
-            return json.load(f)
-    return {}
-
 class DiscordCollector(BaseCollector):
 
     def __init__(
         self,
         config: Optional[CollectorConfig] = None,
         db_pool: Optional[DatabasePool] = None,
-        token: Optional[str] = None,
     ) -> None:
         super().__init__("discord", config)
         self._db_pool = db_pool
-        cfg = _load_json_config()
-        self.token = token or cfg.get("token")
+        self.token = os.environ.get("DISCORD_TOKEN")
         self.http_session: Optional[aiohttp.ClientSession] = None
-        self.channels: List[Dict] = list(cfg.get("enabled_channels", {}).values())
+        self.sources: List[Dict] = [] # NEW: This will hold sources from1 DB
         self.hwm: Dict[str, str] = {}
 
+    async def initialize(self) -> None:
+        """Load sources and high-water marks from1 the database."""
+        pool = await self._ensure_db_pool()
+        self.sources = await pool.fetch_all("SELECT * FROM tickles_shared.collector_sources WHERE source_type = 'discord' AND is_active = TRUE")
+        hwm_rows = await pool.fetch_all(f"SELECT config_key, config_value FROM tickles_shared.system_config WHERE namespace = '{HWM_NAMESPACE}'")
+        self.hwm = {row['config_key']: row['config_value'] for row in hwm_rows}
+
     async def collect(self) -> List[NewsItem]:
-        await self._load_hwm()
+        await self.initialize()
         items: List[NewsItem] = []
         self.http_session = aiohttp.ClientSession(headers={"Authorization": self.token})
 
         try:
-            for channel_cfg in self.channels:
+            for source_cfg in self.sources:
-                channel_id = channel_cfg["id"]
+                channel_id = source_cfg["platform_id"]
-                raw_messages = await self._fetch_messages(channel_id)
+                raw_messages = await self._fetch_messages(channel_id, source_cfg.get("max_messages_per_cycle", 200))
                 if not raw_messages:
                     continue
 
                 # ... (grouping logic remains similar)
 
-                # Attach channel config for media download decisions
-                for group in grouped_messages:
-                    group["channel_config"] = channel_cfg
+                # Attach source_id for media processing
+                for group in grouped_messages:
+                    group["metadata"]["source_id"] = source_cfg["id"]
 
                 # Hand off to a central media service
-                await self._download_media_for_messages(grouped_messages)
+                await MediaExtractorService.get_instance().process_discord_media_groups(grouped_messages)
 
                 for group in grouped_messages:
                     # ... (item creation remains similar)
                     items.append(item)
         finally:
             if self.http_session:
                 await self.http_session.close()
 
         return items
 
-    async def _download_media_for_messages(self, messages: List[Dict[str, Any]]):
-        # ... (This logic is removed and moved to a central service)
-        pass
```

This completes the document by showing concrete examples of the required refactoring, as you requested.
