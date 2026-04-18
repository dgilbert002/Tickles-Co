"""
Module: base
Purpose: Base collector ABC, NewsItem dataclass, and CollectorConfig for all
         data collectors. Writes to `public.news_items` and `public.media_items`
         in the `tickles_shared` Postgres database.
Location: /opt/tickles/shared/collectors/base.py

Phase 3A.1 update (2026-04-18):
  * Replaced MySQL `INSERT IGNORE INTO tickles_shared.news_items ...` with the
    Postgres `INSERT ... ON CONFLICT (hash_key) DO NOTHING RETURNING id` form.
    The previous query was a hard syntax error in Postgres and silently
    dropped 5 days of Discord writes.
  * Writes the extended metadata columns added in migration
    `2026_04_18_phase3a1_collector_sources.sql`: source_id, channel_name,
    author, author_id, message_id, metadata (jsonb), has_media, media_count.
  * For every NewsItem with media, inserts one row per attachment into
    `public.media_items` (extraction_method=cdn_hosted for CDN URLs,
    extraction_method=attached for local_path). Processing status defaults
    to 'pending' — the MediaProcessor (Phase 3A.3) picks these up later.
  * `content` is no longer polluted with [CDN_URLS] / [LOCAL_MEDIA] text hacks;
    media lives in its own table.

Rollback: revert this file + run the ROLLBACK SQL. The collector falls back
to the old narrow column set.
"""

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict

from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.collectors")


class NewsSource(Enum):
    """News source types."""
    RSS = "rss"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    TRADINGVIEW = "tradingview"
    API = "api"


@dataclass
class CollectorConfig:
    """Dynamic configuration for a collector instance.

    Allows per-company, per-source configuration of channels, media, and filters.
    """
    source: NewsSource
    channels: List[Dict[str, Any]] = field(default_factory=list)
    download_media: bool = False
    media_base_dir: str = ""
    max_messages_per_channel: int = 200
    group_window_seconds: int = 60
    collection_interval_seconds: int = 120
    allowed_users: List[str] = field(default_factory=list)
    blocked_users: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NewsItem:
    """Normalized news item — maps to public.news_items.

    Collectors write raw content only. Detection (symbols, signals, relevance)
    is handled downstream by analysis services.

    Media references travel with the item via media_urls / media_path and are
    persisted as individual rows in public.media_items during write_to_db.
    """
    source: NewsSource
    headline: str
    content: str

    url: str = ""
    author: str = ""

    hash_key: str = ""
    published_at: Optional[datetime] = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    media_type: Optional[str] = None
    media_path: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)

    def compute_hash(self) -> None:
        """Compute SHA-256 content hash for deduplication.

        Hash is based on headline + content[:500] to catch duplicates
        even if the URL differs.
        """
        hash_content = f"{self.headline}|||{self.content[:500]}"
        self.hash_key = hashlib.sha256(hash_content.encode("utf-8")).hexdigest()


class CollectorStatus(TypedDict):
    """Status dict for collector monitoring."""
    name: str
    enabled: bool
    last_collected: Optional[str]
    items_collected: int
    errors: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MEDIA_TYPE_BY_EXT = {
    # images
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".svg": "image",
    # video
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".avi": "video", ".wmv": "video", ".m4v": "video",
    # audio / voice
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".ogg": "voice",
    ".opus": "voice", ".flac": "audio",
    # docs
    ".pdf": "document", ".doc": "document", ".docx": "document",
    ".txt": "document", ".csv": "document", ".xlsx": "document",
}


def _guess_media_type(url_or_path: str, fallback: Optional[str] = None) -> str:
    """Classify a media reference by extension. Falls back to 'link'."""
    if not url_or_path:
        return fallback or "link"
    lower = url_or_path.lower().split("?")[0]
    for ext, kind in _MEDIA_TYPE_BY_EXT.items():
        if lower.endswith(ext):
            return kind
    return fallback or "link"


# ---------------------------------------------------------------------------
# BaseCollector
# ---------------------------------------------------------------------------

class BaseCollector(ABC):
    """Abstract base class for all data collectors.

    Every collector (RSS, Telegram, Discord, TradingView) inherits from this.
    Collectors are responsible for:
      1. Fetching raw content from their source
      2. Normalizing into NewsItem objects (with metadata + media refs)
      3. Writing to Postgres via `write_to_db()`

    Detection and analysis are NOT the collector's responsibility.
    """

    def __init__(self, name: str, config: Optional[CollectorConfig] = None):
        """Initialize collector with a name and optional config.

        Args:
            name: Human-readable collector name (e.g. 'rss', 'telegram', 'discord').
            config: Optional CollectorConfig for dynamic channel/media settings.
        """
        self.name = name
        self.config = config or CollectorConfig(source=NewsSource.API)
        self.enabled = True
        self.last_collected: Optional[datetime] = None
        self.items_collected = 0
        self.errors = 0
        logger.info("Collector '%s' initialized", name)

    @abstractmethod
    async def collect(self) -> List[NewsItem]:
        """Collect data from the source and return normalized items."""

    # ------------------------------------------------------------------
    # DB write path (Phase 3A.1)
    # ------------------------------------------------------------------

    async def write_to_db(self, items: List[NewsItem], db_pool: DatabasePool) -> int:
        """Persist NewsItems + media attachments to Postgres.

        One transaction per batch. Each item is inserted with ON CONFLICT
        DO NOTHING on the hash_key unique constraint. If a row is inserted
        (RETURNING id returns a value) any associated media references are
        written into public.media_items as 'pending' rows.

        Args:
            items: List of NewsItem objects to persist.
            db_pool: Database connection pool (must target tickles_shared DB).

        Returns:
            Number of new news_items rows actually inserted.
        """
        if not items:
            return 0

        news_insert_sql = """
            INSERT INTO news_items (
                hash_key, source, headline, content, sentiment, instruments,
                published_at, collected_at,
                source_id, channel_name, author, author_id, message_id,
                metadata, has_media, media_count
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb,
                $7, $8,
                $9, $10, $11, $12, $13,
                $14::jsonb, $15, $16
            )
            ON CONFLICT (hash_key) DO NOTHING
            RETURNING id
        """

        media_insert_sql = """
            INSERT INTO media_items (
                news_item_id, source_id, media_type, extraction_method,
                source_url, local_path, platform_file_id, metadata,
                processing_status
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8::jsonb,
                'pending'
            )
        """

        inserted_count = 0
        try:
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    for item in items:
                        if not item.hash_key:
                            item.compute_hash()

                        published_at = item.published_at
                        if published_at and published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)

                        meta = item.metadata or {}
                        source_id = meta.get("source_id")
                        channel_name = meta.get("channel_name")
                        author_name = (
                            meta.get("sender_username")
                            or meta.get("sender_name")
                            or meta.get("author")
                            or item.author
                            or None
                        )
                        author_id = meta.get("sender_id") or meta.get("author_id")
                        message_id = (
                            meta.get("first_msg_id")
                            or meta.get("message_id")
                        )

                        media_urls = item.media_urls or []
                        has_local = bool(item.media_path)
                        media_count = len(media_urls) + (1 if has_local else 0)
                        has_media = media_count > 0

                        # published_at may be int / float / date — asyncpg needs tz-aware datetime
                        # (we rely on collectors producing proper datetimes — if not, keep None).

                        new_id = await conn.fetchval(
                            news_insert_sql,
                            item.hash_key,
                            item.source.value,
                            item.headline,
                            item.content,
                            None,                           # sentiment — no detection
                            json.dumps([]),                 # instruments
                            published_at,
                            item.collected_at,
                            source_id,
                            channel_name,
                            author_name,
                            author_id,
                            message_id,
                            json.dumps(meta, default=str),  # metadata jsonb
                            has_media,
                            media_count,
                        )

                        if new_id is None:
                            # Conflict — hash already existed, skip media writes.
                            continue

                        inserted_count += 1

                        # ---- Media rows (one per attachment) ----
                        for url in media_urls:
                            media_type = _guess_media_type(
                                url, fallback=item.media_type or "link"
                            )
                            await conn.execute(
                                media_insert_sql,
                                new_id,
                                source_id,
                                media_type,
                                "cdn_hosted",
                                url,
                                None,
                                None,
                                json.dumps({"origin": "collector_batch"}),
                            )

                        if has_local:
                            media_type = _guess_media_type(
                                item.media_path, fallback=item.media_type or "document"
                            )
                            await conn.execute(
                                media_insert_sql,
                                new_id,
                                source_id,
                                media_type,
                                "attached",
                                None,
                                item.media_path,
                                None,
                                json.dumps({"origin": "collector_batch"}),
                            )

            if inserted_count > 0:
                self.items_collected += inserted_count
                self.last_collected = datetime.now(timezone.utc)
                logger.info(
                    "Collector '%s': %d/%d items inserted",
                    self.name, inserted_count, len(items),
                )
            return inserted_count

        except Exception as exc:
            self.errors += 1
            logger.error(
                "Collector '%s' DB write failed: %s",
                self.name, exc, exc_info=True,
            )
            return 0

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def get_status(self) -> CollectorStatus:
        """Return status dict for monitoring."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "last_collected": self.last_collected.isoformat() if self.last_collected else None,
            "items_collected": self.items_collected,
            "errors": self.errors,
        }
