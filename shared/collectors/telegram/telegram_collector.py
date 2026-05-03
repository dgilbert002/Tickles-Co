"""
Module: telegram_collector
Purpose: Telegram message collector with media download — inherits from BaseCollector
Location: /opt/tickles/shared/collectors/telegram/telegram_collector.py

Connects to Telegram using Telethon (MTProto API).
Reads messages from configured channels/groups, groups consecutive messages,
downloads media if enabled, and writes to tickles_shared.news_items.

Uses high-water marks stored in tickles_shared.system_config (namespace='telegram_hwm')
so on restart it picks up exactly where it left off.

Requirements:
    pip install telethon aiohttp
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from shared.collectors.base import BaseCollector, CollectorConfig, NewsItem, NewsSource
from shared.collectors.rate_limit import for_source as rate_limit_for_source
from shared.intelligence.zone_filter import classify, passes
from shared.intelligence.image_phash import compute_phash, is_near_duplicate
from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.telegram")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_DIR = os.environ.get(
    "TICKLES_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "data"),
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "telegram_config.json")

MEDIA_BASE_DIR = os.environ.get(
    "TELEGRAM_MEDIA_DIR",
    os.path.join(os.path.dirname(__file__), "data", "telegram_media"),
)

SESSION_DIR = os.environ.get(
    "TELEGRAM_SESSION_DIR",
    os.path.join(os.path.dirname(__file__), "data"),
)
SESSION_PATH = os.path.join(SESSION_DIR, "tickles_telegram")


# ---------------------------------------------------------------------------
# Config Loader
# ---------------------------------------------------------------------------

def _load_config_from_file() -> Dict[str, Any]:
    """Load telegram_config.json if it exists.

    Returns:
        Config dict with 'api_id', 'api_hash', 'channels', etc.
    """
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            logger.info("Loaded Telegram config from %s", CONFIG_PATH)
            return cfg
    except Exception as e:
        logger.error("Failed to load Telegram config: %s", e)

    return {}


def _load_config_from_env() -> Dict[str, Any]:
    """Load Telegram config from environment variables.

    Returns:
        Config dict.
    """
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "")
    channels_raw = os.environ.get("TELEGRAM_CHANNELS", "")

    channels = []
    if channels_raw:
        for ch in channels_raw.split(","):
            ch = ch.strip()
            if ch:
                channels.append({"id": ch, "name": ch, "enabled": True, "download_media": False})

    return {
        "api_id": api_id,
        "api_hash": api_hash,
        "session_string": session_string,
        "channels": channels,
    }


# ---------------------------------------------------------------------------
# High-Water Mark Management
# ---------------------------------------------------------------------------

async def _load_high_water_marks(db_pool: DatabasePool) -> Dict[str, str]:
    """Load high-water marks from system_config.

    Args:
        db_pool: Database connection pool.

    Returns:
        Dict mapping channel_id -> last_message_id.
    """
    hwm: Dict[str, str] = {}
    try:
        rows = await db_pool.fetch_all(
            "SELECT config_key, config_value FROM tickles_shared.system_config "
            "WHERE namespace = 'telegram_hwm'"
        )
        for row in rows:
            hwm[row["config_key"]] = row["config_value"]
    except Exception as e:
        logger.warning("Failed to load Telegram high-water marks: %s", e)
    return hwm


async def _save_high_water_mark(
    db_pool: DatabasePool, channel_id: str, last_msg_id: str
) -> None:
    """Save a high-water mark for a channel.

    Args:
        db_pool: Database connection pool.
        channel_id: Telegram channel/chat ID.
        last_msg_id: Last collected message ID.
    """
    try:
        await db_pool.execute(
            "INSERT INTO system_config (namespace, config_key, config_value) "
            "VALUES ('telegram_hwm', %s, %s) "
            "ON CONFLICT (namespace, config_key) "
            "DO UPDATE SET config_value = EXCLUDED.config_value",
            (str(channel_id), str(last_msg_id)),
        )
    except Exception as e:
        logger.debug("Failed to save Telegram HWM for %s: %s", channel_id, e)


# ---------------------------------------------------------------------------
# Message Grouping
# ---------------------------------------------------------------------------

def _group_messages(
    messages: List[Any],
    channel_info: Dict[str, Any],
    group_window_seconds: int = 60,
) -> List[Dict[str, Any]]:
    """Group consecutive messages from same sender within time window.

    Args:
        messages: List of Telethon message objects.
        channel_info: Channel metadata.
        group_window_seconds: Max seconds between messages to group.

    Returns:
        List of grouped message dicts.
    """
    if not messages:
        return []

    messages.sort(key=lambda m: m.date)

    groups: List[Dict[str, Any]] = []
    current_group: Optional[Dict[str, Any]] = None

    for msg in messages:
        sender_id = msg.sender_id or 0
        msg_date = msg.date
        if msg_date and msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)

        belongs_to_current = False
        if current_group:
            same_sender = current_group["sender_id"] == sender_id
            time_diff = (msg_date - current_group["last_date"]).total_seconds()
            within_window = time_diff <= group_window_seconds
            same_album = (
                getattr(msg, "grouped_id", None)
                and msg.grouped_id == current_group.get("grouped_id")
            )
            belongs_to_current = (same_sender and within_window) or same_album

        if belongs_to_current:
            current_group["messages"].append(msg)
            current_group["last_date"] = msg_date
            if msg.text:
                current_group["texts"].append(msg.text)
            if getattr(msg, "grouped_id", None):
                current_group["grouped_id"] = msg.grouped_id
            # Track media
            if msg.photo and not current_group.get("media_type"):
                current_group["media_type"] = "photo"
            if msg.video and not current_group.get("media_type"):
                current_group["media_type"] = "video"
            if msg.document and not current_group.get("media_type"):
                current_group["media_type"] = "document"
            if msg.voice and not current_group.get("media_type"):
                current_group["media_type"] = "voice"
        else:
            if current_group:
                groups.append(current_group)

            sender_name = ""
            sender_username = ""
            try:
                sender = msg.sender
                if sender:
                    sender_name = (
                        f"{getattr(sender, 'first_name', '') or ''} "
                        f"{getattr(sender, 'last_name', '') or ''}"
                    ).strip()
                    sender_username = getattr(sender, "username", "") or ""
            except Exception:
                pass

            media_type = None
            if msg.photo:
                media_type = "photo"
            elif msg.video:
                media_type = "video"
            elif msg.voice:
                media_type = "voice"
            elif msg.document:
                media_type = "document"

            current_group = {
                "sender_id": sender_id,
                "sender_name": sender_name,
                "sender_username": sender_username,
                "first_date": msg_date or datetime.now(timezone.utc),
                "last_date": msg_date or datetime.now(timezone.utc),
                "messages": [msg],
                "texts": [msg.text] if msg.text else [],
                "channel_id": channel_info["id"],
                "channel_info": channel_info,
                "grouped_id": getattr(msg, "grouped_id", None),
                "media_type": media_type,
                "media_path": None,
                "media_urls": [],
            }

    if current_group:
        groups.append(current_group)

    return groups


def _finalize_group(group: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw message group into a NewsItem-ready dict.

    Args:
        group: Raw message group dict.

    Returns:
        Finalized group dict.
    """
    combined_text = "\n".join(t for t in group["texts"] if t)

    channel_info = group.get("channel_info", {})
    channel_name = channel_info.get("name", "")
    first_msg_id = group["messages"][0].id if group["messages"] else ""
    last_msg_id = group["messages"][-1].id if group["messages"] else ""

    headline = combined_text[:200] if combined_text else ""
    pub_date = group["first_date"]

    author = (
        group["sender_username"]
        or group["sender_name"]
        or str(group["sender_id"])
    )
    if group["sender_username"]:
        author = f"@{group['sender_username']}"

    # Build content
    content_parts: List[str] = []
    if combined_text:
        content_parts.append(combined_text)

    content = "\n".join(content_parts)

    # Hash
    hash_key = hashlib.sha256(
        f"telegram_{group['channel_id']}_{first_msg_id}_{last_msg_id}".encode("utf-8")
    ).hexdigest()

    # Metadata
    metadata = {
        "channel_id": group["channel_id"],
        "channel_name": channel_name,
        "sender_id": str(group["sender_id"]),
        "sender_username": group["sender_username"],
        "sender_name": group["sender_name"],
        "first_msg_id": str(first_msg_id),
        "last_msg_id": str(last_msg_id),
        "msg_count": len(group["messages"]),
    }

    return {
        "hash_key": hash_key,
        "headline": headline,
        "content": content,
        "author": author,
        "url": "",
        "published_at": pub_date,
        "media_type": group.get("media_type"),
        "media_path": group.get("media_path"),
        "media_urls": group.get("media_urls", []),
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Media Download
# ---------------------------------------------------------------------------

async def _download_media_for_group(
    group: Dict[str, Any],
    media_base_dir: str,
    channel_info: Dict[str, Any],
) -> None:
    """Download media for a message group.

    Modifies group in-place by adding 'media_path' and 'media_urls'.

    Args:
        group: Message group dict.
        media_base_dir: Base directory for media storage.
        channel_info: Channel metadata.
    """
    if not channel_info.get("download_media", False):
        return

    chat_id = str(group.get("channel_id", ""))

    for msg in group.get("messages", []):
        # Photos
        if msg.photo:
            try:
                os.makedirs(os.path.join(media_base_dir, chat_id), exist_ok=True)
                timestamp = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "unknown"
                filename = f"photo_{msg.id}_{timestamp}.jpg"
                save_path = os.path.join(media_base_dir, chat_id, filename)

                if not os.path.exists(save_path):
                    await msg.download_media(file=save_path)
                    logger.debug("Downloaded Telegram photo: %s", save_path)

                if not group.get("media_path"):
                    group["media_path"] = save_path
                    group["media_type"] = "photo"
            except Exception as e:
                logger.warning("Failed to download Telegram photo: %s", e)

        # Videos
        elif msg.video:
            try:
                os.makedirs(os.path.join(media_base_dir, chat_id), exist_ok=True)
                timestamp = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "unknown"
                ext = ".mp4"
                if msg.video.mime_type and "webm" in msg.video.mime_type:
                    ext = ".webm"
                filename = f"video_{msg.id}_{timestamp}{ext}"
                save_path = os.path.join(media_base_dir, chat_id, filename)

                if not os.path.exists(save_path):
                    await msg.download_media(file=save_path)
                    logger.debug("Downloaded Telegram video: %s", save_path)

                if not group.get("media_path"):
                    group["media_path"] = save_path
                    group["media_type"] = "video"
            except Exception as e:
                logger.warning("Failed to download Telegram video: %s", e)

        # Voice messages
        elif msg.voice:
            try:
                os.makedirs(os.path.join(media_base_dir, chat_id), exist_ok=True)
                timestamp = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "unknown"
                filename = f"voice_{msg.id}_{timestamp}.ogg"
                save_path = os.path.join(media_base_dir, chat_id, filename)

                if not os.path.exists(save_path):
                    await msg.download_media(file=save_path)
                    logger.debug("Downloaded Telegram voice: %s", save_path)

                if not group.get("media_path"):
                    group["media_path"] = save_path
                    group["media_type"] = "voice"
            except Exception as e:
                logger.warning("Failed to download Telegram voice: %s", e)

        # Documents
        elif msg.document:
            try:
                os.makedirs(os.path.join(media_base_dir, chat_id), exist_ok=True)
                timestamp = msg.date.strftime("%Y%m%d_%H%M%S") if msg.date else "unknown"
                ext = ""
                if msg.document.mime_type:
                    if "pdf" in msg.document.mime_type:
                        ext = ".pdf"
                    elif "zip" in msg.document.mime_type:
                        ext = ".zip"
                filename = f"doc_{msg.id}_{timestamp}{ext}"
                save_path = os.path.join(media_base_dir, chat_id, filename)

                if not os.path.exists(save_path):
                    await msg.download_media(file=save_path)
                    logger.debug("Downloaded Telegram document: %s", save_path)

                if not group.get("media_path"):
                    group["media_path"] = save_path
                    group["media_type"] = "document"
            except Exception as e:
                logger.warning("Failed to download Telegram document: %s", e)


# ---------------------------------------------------------------------------
# Telegram Collector
# ---------------------------------------------------------------------------

class TelegramCollector(BaseCollector):
    """Telegram message collector with media download.

    Uses Telethon to connect via Telegram MTProto API.
    Requires TELEGRAM_API_ID, TELEGRAM_API_HASH env vars or config file.
    Supports high-water marks, media download, and dynamic channel config.
    """

    def __init__(
        self,
        config: Optional[CollectorConfig] = None,
        db_pool: Optional[DatabasePool] = None,
    ) -> None:
        """Initialize Telegram collector.

        Args:
            config: Optional CollectorConfig. If None, loads from env/JSON.
            db_pool: Optional DatabasePool. If None, creates one on first use.
        """
        super().__init__("telegram", config)
        self._db_pool = db_pool
        self._hwm: Dict[str, str] = {}
        self._media_base_dir = MEDIA_BASE_DIR

        # Load config
        self._api_id = ""
        self._api_hash = ""
        self._session_string = ""
        self._channels: List[Dict[str, Any]] = []
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from CollectorConfig, JSON file, or env vars."""
        # If config has channels, use them
        if self.config and self.config.channels:
            self._channels = self.config.channels
            self._api_id = self.config.extra.get("api_id", "")
            self._api_hash = self.config.extra.get("api_hash", "")
            self._session_string = self.config.extra.get("session_string", "")
            self._media_base_dir = self.config.media_base_dir or MEDIA_BASE_DIR
        else:
            # Try JSON file first, then env vars
            file_cfg = _load_config_from_file()
            if file_cfg.get("api_id"):
                self._api_id = file_cfg.get("api_id", "")
                self._api_hash = file_cfg.get("api_hash", "")
                self._session_string = file_cfg.get("session_string", "")
                self._channels = file_cfg.get("channels", [])
            else:
                env_cfg = _load_config_from_env()
                self._api_id = env_cfg.get("api_id", "")
                self._api_hash = env_cfg.get("api_hash", "")
                self._session_string = env_cfg.get("session_string", "")
                self._channels = env_cfg.get("channels", [])

        # Normalize channel config
        for ch in self._channels:
            ch.setdefault("enabled", True)
            ch.setdefault("download_media", False)
            ch.setdefault("name", str(ch.get("id", "unknown")))

        enabled = sum(1 for c in self._channels if c.get("enabled", True))
        logger.info(
            "Telegram config: %d channels configured, %d enabled",
            len(self._channels),
            enabled,
        )

    async def _ensure_db_pool(self) -> DatabasePool:
        """Ensure DB pool exists."""
        if self._db_pool is None:
            self._db_pool = DatabasePool()
            await self._db_pool.initialize()
            logger.info("Telegram DB pool initialized")
        return self._db_pool

    async def _load_hwm(self) -> None:
        """Load high-water marks from database."""
        try:
            pool = await self._ensure_db_pool()
            self._hwm = await _load_high_water_marks(pool)
            if self._hwm:
                logger.info("Loaded %d Telegram high-water marks", len(self._hwm))
        except Exception as e:
            logger.warning("Failed to load Telegram HWM: %s", e)
            self._hwm = {}

    async def _save_hwm(self, channel_id: str, last_msg_id: str) -> None:
        """Save high-water mark for a channel."""
        self._hwm[str(channel_id)] = str(last_msg_id)
        try:
            pool = await self._ensure_db_pool()
            await _save_high_water_mark(pool, channel_id, last_msg_id)
        except Exception as e:
            logger.debug("Telegram HWM save failed for %s: %s", channel_id, e)

    def _should_include_message(self, msg: Any) -> bool:
        """Check if a message should be included based on user filters.

        Args:
            msg: Telethon message object.

        Returns:
            True if message should be included.
        """
        if not self.config:
            return True

        sender_id = str(getattr(msg, "sender_id", "") or "")
        sender_username = ""
        try:
            sender = msg.sender
            if sender:
                sender_username = (getattr(sender, "username", "") or "").lower()
        except Exception:
            pass

        # Check blocked users first
        for blocked in self.config.blocked_users:
            blocked_lower = blocked.lower()
            if blocked_lower == sender_id or (sender_username and blocked_lower == sender_username):
                return False

        # If allowed_users is set, only include those users
        if self.config.allowed_users:
            for allowed in self.config.allowed_users:
                allowed_lower = allowed.lower()
                if allowed_lower == sender_id or (sender_username and allowed_lower == sender_username):
                    return True
            return False

        return True

    # ------------------------------------------------------------------
    # Phase 9 — zone filter, context window, rate limit, image dedup
    # ------------------------------------------------------------------

    async def _build_context_window_telegram(
        self,
        client: Any,
        entity: Any,
        message: Any,
    ) -> Optional[Dict[str, Any]]:
        """Build ±10 message context window around a Telegram message.

        Args:
            client: Telethon TelegramClient.
            entity: Channel/group entity.
            message: Telethon message object.

        Returns:
            Dict with 'before' and 'after' message snapshots, or None on error.
        """
        try:
            before = []
            async for m in client.iter_messages(entity, limit=10, max_id=message.id):
                before.append({
                    "id": str(m.id),
                    "author": str(getattr(m.sender, "username", "") or ""),
                    "text": (m.message or "")[:2000],
                    "timestamp": m.date.isoformat() if m.date else None,
                    "has_attachment": bool(m.media),
                })
            after = []
            async for m in client.iter_messages(entity, limit=10, min_id=message.id):
                after.append({
                    "id": str(m.id),
                    "author": str(getattr(m.sender, "username", "") or ""),
                    "text": (m.message or "")[:2000],
                    "timestamp": m.date.isoformat() if m.date else None,
                    "has_attachment": bool(m.media),
                })
            return {"before": list(reversed(before)), "after": after}
        except Exception as exc:
            logger.warning("Telegram context-window failed: %s", exc)
            return None

    async def _apply_zone_filter_and_dedup_telegram(
        self,
        item: NewsItem,
        source_id: Optional[int],
        channel_id: str,
        pool: DatabasePool,
    ) -> bool:
        """Apply zone filter, image dedup, and enrichment_status to a NewsItem.

        Mutates item in-place with enrichment_status, zone_filter_* fields,
        image_phash, duplicate_of_id.

        Args:
            item: NewsItem to process.
            source_id: collector_sources.id for per-source overrides.
            channel_id: Telegram channel ID string.
            pool: Database pool for dedup lookups.

        Returns:
            True if item should proceed to DB write (not dropped by rate limit).
            False if item was dropped (should not be written).
        """
        cid = f"telegram_{channel_id}_{item.hash_key[:16]}"

        # --- Phase 9 [BB] rate limiter ---
        default_rate = float(os.environ.get("TELEGRAM_RATE_LIMIT_MSGS_PER_SEC", "30"))
        bucket = rate_limit_for_source(source_id or 0, default_rate)
        if not await bucket.acquire(timeout=30.0):
            logger.warning("Telegram rate-limit timeout source_id=%s; dropping message", source_id)
            return False

        # --- Phase 9 §A zone filter ---
        catalog: Optional[Dict[str, Any]] = None
        if source_id is not None:
            try:
                row = await pool.fetchrow(
                    "SELECT zone_filter_enabled, zone_filter_threshold, rate_limit_msgs_per_sec "
                    "FROM collector_sources WHERE id = $1", source_id,
                )
                if row:
                    catalog = dict(row)
            except Exception as exc:
                logger.warning("Failed to load collector_catalog for source_id=%s: %s", source_id, exc)

        zone_filter_enabled = catalog.get("zone_filter_enabled", True) if catalog else True
        per_source_threshold = catalog.get("zone_filter_threshold") if catalog else None

        if zone_filter_enabled and item.content:
            zone = await classify(item.content, source_id=source_id or 0, correlation_id=cid)
            item.zone_filter_confidence = zone.get("confidence")
            item.zone_filter_reason = zone.get("reason")
            if not passes(zone, per_source_threshold=per_source_threshold):
                item.enrichment_status = "non_signal"
                logger.debug("Telegram zone-filter rejected: confidence=%s reason=%s",
                             item.zone_filter_confidence, item.zone_filter_reason)
            else:
                item.enrichment_status = "pending"
        else:
            item.enrichment_status = "pending"

        # --- Phase 9 [BA] image perceptual-hash dedup ---
        if item.media_path:
            try:
                phash = compute_phash(Path(item.media_path))
                if phash:
                    item.image_phash = phash
                    async with pool.acquire() as conn:
                        canonical = await conn.fetchrow(
                            "SELECT id FROM news_items "
                            "WHERE image_phash IS NOT NULL "
                            "  AND duplicate_of_id IS NULL "
                            "  AND collected_at > now() - interval '60 minutes' "
                            "  AND image_phash = $1 "
                            "ORDER BY collected_at ASC LIMIT 1",
                            phash,
                        )
                        if canonical:
                            item.duplicate_of_id = canonical["id"]
                            item.enrichment_status = "duplicate_zone"
                            logger.info("Telegram image dedup: %s is duplicate of %s",
                                        item.hash_key[:16], canonical["id"])
            except Exception as exc:
                logger.warning("Telegram image dedup failed for %s: %s", item.media_path, exc)

        return True

    async def collect(self) -> List[NewsItem]:
        """Collect recent messages from Telegram channels.

        Returns:
            List of NewsItem objects.
        """
        if not self._api_id or not self._api_hash:
            logger.warning("Telegram collector: no API credentials, skipping")
            return []

        await self._load_hwm()

        items: List[NewsItem] = []

        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            if self._session_string:
                session = StringSession(self._session_string)
            elif os.path.exists(f"{SESSION_PATH}.session"):
                session = SESSION_PATH
            else:
                session = "tickles_telegram"

            client = TelegramClient(
                session,
                int(self._api_id),
                self._api_hash,
            )

            await client.connect()

            if not await client.is_user_authorized():
                logger.error("Telegram session not authorized")
                await client.disconnect()
                return []

            # Reload config each cycle
            self._load_config()

            for channel_cfg in self._channels:
                if not channel_cfg.get("enabled", True):
                    continue

                channel_id = channel_cfg.get("id", "")
                channel_name = channel_cfg.get("name", str(channel_id))

                try:
                    entity = await client.get_entity(channel_id)
                    channel_info = {
                        "id": str(entity.id),
                        "name": getattr(entity, "title", channel_name),
                        "type": "channel",
                        "download_media": channel_cfg.get("download_media", False),
                    }

                    # Get high-water mark
                    after_id = self._hwm.get(str(entity.id))

                    raw_messages = []
                    limit = self.config.max_messages_per_channel if self.config else 200

                    if after_id:
                        async for msg in client.iter_messages(
                            entity, limit=limit, min_id=int(after_id)
                        ):
                            if msg is not None:
                                raw_messages.append(msg)
                    else:
                        async for msg in client.iter_messages(
                            entity, limit=limit, reverse=True
                        ):
                            if msg is not None:
                                raw_messages.append(msg)

                    if not raw_messages:
                        logger.debug("Telegram: no new messages from %s", channel_name)
                        continue

                    logger.info(
                        "Telegram: %s — fetched %d messages",
                        channel_name,
                        len(raw_messages),
                    )

                    # Filter messages by user if configured
                    if self.config and (self.config.allowed_users or self.config.blocked_users):
                        filtered_messages = [m for m in raw_messages if self._should_include_message(m)]
                        if len(filtered_messages) != len(raw_messages):
                            logger.debug(
                                "Telegram: %s — filtered %d -> %d messages by user filter",
                                channel_name,
                                len(raw_messages),
                                len(filtered_messages),
                            )
                        raw_messages = filtered_messages

                    if not raw_messages:
                        continue

                    # Update high-water mark
                    max_id = max(m.id for m in raw_messages)
                    await self._save_hwm(str(entity.id), str(max_id))

                    # Group messages
                    group_window = self.config.group_window_seconds if self.config else 60
                    grouped = _group_messages(raw_messages, channel_info, group_window)

                    # Download media if enabled
                    download_media = channel_info.get("download_media", False)
                    if self.config:
                        download_media = download_media or self.config.download_media

                    if download_media:
                        for group in grouped:
                            await _download_media_for_group(
                                group, self._media_base_dir, channel_info
                            )

                    # Convert to NewsItems + Phase 9 zone filter / dedup / context window
                    pool = await self._ensure_db_pool()
                    source_id = channel_info.get("source_id")

                    for group in grouped:
                        finalized = _finalize_group(group)
                        item = NewsItem(
                            source=NewsSource.TELEGRAM,
                            headline=finalized["headline"],
                            content=finalized["content"],
                            author=finalized["author"],
                            url=finalized["url"],
                            published_at=finalized["published_at"],
                            media_type=finalized.get("media_type"),
                            media_path=finalized.get("media_path"),
                            media_urls=finalized.get("media_urls", []),
                            metadata=finalized.get("metadata", {}),
                        )
                        item.hash_key = finalized["hash_key"]

                        # Phase 9 — apply zone filter, rate limit, image dedup
                        keep = await self._apply_zone_filter_and_dedup_telegram(
                            item, source_id, str(entity.id), pool,
                        )
                        if not keep:
                            continue

                        # Phase 9 — context window for signal-classified items
                        if item.enrichment_status == "pending" and group.get("messages"):
                            try:
                                first_msg = group["messages"][0]
                                ctx = await self._build_context_window_telegram(
                                    client, entity, first_msg,
                                )
                                if ctx:
                                    item.context_window = ctx
                            except Exception as exc:
                                logger.debug("Telegram context-window skipped: %s", exc)

                        items.append(item)

                    logger.info(
                        "Telegram: %s — %d msgs -> %d groups -> %d items (%d pending, %d non_signal, %d duplicate)",
                        channel_name,
                        len(raw_messages),
                        len(grouped),
                        len([i for i in items if i.metadata.get("channel_id") == str(entity.id)]),
                        sum(1 for i in items if i.enrichment_status == "pending"),
                        sum(1 for i in items if i.enrichment_status == "non_signal"),
                        sum(1 for i in items if i.enrichment_status == "duplicate_zone"),
                    )

                except (ValueError, TypeError) as e:
                    logger.warning("Invalid Telegram channel %s: %s", channel_name, e)
                except Exception as e:
                    logger.warning(
                        "Failed to fetch from Telegram channel %s: %s",
                        channel_name,
                        e,
                    )

            await client.disconnect()

            logger.info(
                "Telegram: collected %d total items from %d channels",
                len(items),
                len(self._channels),
            )

        except ImportError:
            logger.error(
                "Telethon not installed. Install with: pip install telethon"
            )
            self.errors += 1
        except Exception as e:
            logger.error("Telegram collector failed: %s", e, exc_info=True)
            self.errors += 1

        return items
