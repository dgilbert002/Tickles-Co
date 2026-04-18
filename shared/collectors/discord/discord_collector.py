"""
Module: discord_collector
Purpose: Discord message collector — inherits from BaseCollector
Location: /opt/tickles/shared/collectors/discord/discord_collector.py

Connects to Discord using discord.py-self (user self-bot token).
Reads messages from configured channels, groups consecutive messages from the same sender,
downloads media if enabled, and writes everything into tickles_shared.news_items.

Uses high-water marks stored in tickles_shared.system_config (namespace='discord_hwm')
so on restart it picks up exactly where it left off.

Requirements:
    pip install discord.py-self aiohttp

Config: Injected via CollectorConfig or loaded from discord_config.json
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
from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.discord")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_DIR = os.environ.get(
    "TICKLES_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "data"),
)
CONFIG_PATH = os.path.join(CONFIG_DIR, "discord_config.json")

MEDIA_BASE_DIR = os.environ.get(
    "DISCORD_MEDIA_DIR",
    os.path.join(os.path.dirname(__file__), "data", "discord_media"),
)


# ---------------------------------------------------------------------------
# Config Loader
# ---------------------------------------------------------------------------

def _load_config_from_file() -> Dict[str, Any]:
    """Load discord_config.json.

    Security (Phase 3A.1, 2026-04-18):
        The bot token MUST come from the DISCORD_BOT_TOKEN environment variable,
        never from the JSON file. If a plaintext `token` key is still present
        in discord_config.json the function logs a SECURITY warning and ignores
        it. The JSON file is treated as a (soon-to-be-DB-backed) channel
        subscription list only.

    Returns:
        Config dict with 'token' (from env) and 'channels'/'enabled_channels' keys.
    """
    cfg: Dict[str, Any] = {"token": "", "channels": {}}

    # 1. Channels come from JSON (until Phase 3A.2 wires collector_sources)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("token"):
                logger.warning(
                    "SECURITY: plaintext 'token' key found in %s — ignoring. "
                    "Move the bot token to DISCORD_BOT_TOKEN env var and delete "
                    "the key from the JSON file.",
                    CONFIG_PATH,
                )
                raw["token"] = ""  # strip from in-memory copy
            cfg.update(raw)
            logger.info("Loaded Discord config from %s", CONFIG_PATH)
    except Exception as e:
        logger.error("Failed to load Discord config: %s", e)

    # 2. Token comes from env ONLY
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if token:
        cfg["token"] = token
        logger.info("Using DISCORD_BOT_TOKEN from environment")
    else:
        logger.error("DISCORD_BOT_TOKEN env var not set — collector will not start.")
        cfg["token"] = ""

    return cfg


# ---------------------------------------------------------------------------
# High-Water Mark Management
# ---------------------------------------------------------------------------

async def _load_high_water_marks(db_pool: DatabasePool) -> Dict[str, str]:
    """Load high-water marks (last message ID per channel) from system_config.

    Args:
        db_pool: Database connection pool.

    Returns:
        Dict mapping channel_id -> last_message_id (snowflake string).
    """
    hwm: Dict[str, str] = {}
    try:
        rows = await db_pool.fetch_all(
            "SELECT config_key, config_value FROM system_config "
            "WHERE namespace = 'discord_hwm'"
        )
        for row in rows:
            hwm[row["config_key"]] = row["config_value"]
    except Exception as e:
        logger.warning("Failed to load high-water marks: %s", e)
    return hwm


async def _save_high_water_mark(
    db_pool: DatabasePool, channel_id: str, last_msg_id: str
) -> None:
    """Save a high-water mark for a channel to system_config.

    Args:
        db_pool: Database connection pool.
        channel_id: Discord channel ID.
        last_msg_id: Last collected message ID (snowflake).
    """
    try:
        # Postgres upsert: relies on uq_config_ns_key unique constraint
        # on (namespace, config_key). Table lives in public schema of
        # tickles_shared; callers MUST pass the shared pool.
        await db_pool.execute(
            "INSERT INTO system_config (namespace, config_key, config_value) "
            "VALUES ('discord_hwm', %s, %s) "
            "ON CONFLICT (namespace, config_key) "
            "DO UPDATE SET config_value = EXCLUDED.config_value, "
            "              updated_at = CURRENT_TIMESTAMP",
            (channel_id, last_msg_id),
        )
    except Exception as e:
        # Raised from DEBUG to WARNING (2026-04-18) — silent failure
        # previously hid the fact that HWMs weren't persisting at all.
        logger.warning(
            "HWM save failed for channel %s: %s", channel_id, e, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Message Grouping
# ---------------------------------------------------------------------------

def _group_messages(
    messages: List[Dict[str, Any]],
    channel_info: Dict[str, Any],
    group_window_seconds: int = 60,
) -> List[Dict[str, Any]]:
    """Group consecutive messages from the same sender within time window.

    Args:
        messages: List of message dicts sorted by date ascending.
        channel_info: Channel metadata dict.
        group_window_seconds: Max seconds between messages to group.

    Returns:
        List of grouped message dicts ready for DB insertion.
    """
    if not messages:
        return []

    messages.sort(key=lambda m: m.get("date", datetime.min.replace(tzinfo=timezone.utc)))

    groups: List[Dict[str, Any]] = []
    current_group: Optional[Dict[str, Any]] = None

    for msg in messages:
        sender_id = msg.get("sender_id", "")
        msg_date = msg.get("date", datetime.now(timezone.utc))
        if msg_date and msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)

        belongs_to_current = False
        if current_group:
            same_sender = current_group["sender_id"] == sender_id
            time_diff = (msg_date - current_group["last_date"]).total_seconds()
            within_window = time_diff <= group_window_seconds
            belongs_to_current = same_sender and within_window

        if belongs_to_current:
            current_group["messages"].append(msg)
            current_group["last_date"] = msg_date
            if msg.get("text"):
                current_group["texts"].append(msg["text"])
            if msg.get("attachments"):
                current_group["attachments"].extend(msg["attachments"])
            if msg.get("embeds"):
                current_group["embeds"].extend(msg["embeds"])
            if msg.get("media_type") and not current_group.get("media_type"):
                current_group["media_type"] = msg["media_type"]
            if msg.get("media_path") and not current_group.get("media_path"):
                current_group["media_path"] = msg["media_path"]
        else:
            if current_group:
                groups.append(_finalize_group(current_group, channel_info))

            current_group = {
                "sender_id": sender_id,
                "sender_name": msg.get("sender_name", ""),
                "sender_username": msg.get("sender_username", ""),
                "first_date": msg_date,
                "last_date": msg_date,
                "messages": [msg],
                "texts": [msg["text"]] if msg.get("text") else [],
                "attachments": list(msg.get("attachments", [])),
                "embeds": list(msg.get("embeds", [])),
                "reply_to_msg_id": msg.get("reply_to_msg_id"),
                "reply_to_content": msg.get("reply_to_content"),
                "reply_to_author": msg.get("reply_to_author"),
                "channel_id": msg.get("channel_id", ""),
                "media_type": msg.get("media_type"),
                "media_path": msg.get("media_path"),
            }

    if current_group:
        groups.append(_finalize_group(current_group, channel_info))

    return groups


def _finalize_group(group: Dict[str, Any], channel_info: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a raw message group into a NewsItem-ready dict.

    Args:
        group: Raw message group dict.
        channel_info: Channel metadata.

    Returns:
        Finalized group dict.
    """
    combined_text = "\n".join(t for t in group["texts"] if t)

    author = (
        group.get("sender_username")
        or group.get("sender_name")
        or str(group.get("sender_id", ""))
    )

    # Build content with reply context
    content_parts: List[str] = []

    if group.get("reply_to_content"):
        reply_author = group.get("reply_to_author", "?")
        reply_preview = group["reply_to_content"][:500]
        content_parts.append(f"[Reply to @{reply_author}]: {reply_preview}")

    # Collect media URLs from attachments and embeds
    media_urls: List[str] = []
    for att in group.get("attachments", []):
        url = att.get("url", "")
        if url:
            media_urls.append(url)

    for emb in group.get("embeds", []):
        if emb.get("image_url"):
            media_urls.append(emb["image_url"])

    # TradingView chart URL extraction
    tv_page_urls = re.findall(
        r"https?://(?:www\.)?tradingview\.com/x/([A-Za-z0-9]+)/?", combined_text
    )
    for tv_id in tv_page_urls:
        direct_url = f"https://s3.tradingview.com/snapshots/{tv_id[0].lower()}/{tv_id}.png"
        media_urls.append(direct_url)

    if combined_text:
        content_parts.append(combined_text)

    content = "\n".join(content_parts)

    # Headline
    first_text = group["texts"][0] if group["texts"] else ""
    headline = first_text[:200] if first_text else ""

    # Message IDs
    first_msg_id = group["messages"][0]["id"] if group["messages"] else ""
    last_msg_id = group["messages"][-1]["id"] if group["messages"] else ""

    # Server/channel info
    server_name = channel_info.get("server_name", "")
    channel_name = channel_info.get("name", "")
    server_id = channel_info.get("server_id", "")

    # Discord URL
    discord_url = ""
    if server_id and group.get("channel_id") and first_msg_id:
        discord_url = f"https://discord.com/channels/{server_id}/{group['channel_id']}/{first_msg_id}"

    # Metadata
    metadata = {
        "channel_id": group.get("channel_id", ""),
        "server_id": server_id,
        "server_name": server_name,
        "channel_name": channel_name,
        "sender_id": group.get("sender_id", ""),
        "sender_username": group.get("sender_username", ""),
        "sender_name": group.get("sender_name", ""),
        "first_msg_id": first_msg_id,
        "last_msg_id": last_msg_id,
        "msg_count": len(group["messages"]),
        "reply_to_msg_id": group.get("reply_to_msg_id"),
        "reply_to_content": group.get("reply_to_content", ""),
        "reply_to_author": group.get("reply_to_author", ""),
        "discord_url": discord_url,
    }

    return {
        "hash_key": hashlib.sha256(
            f"{group['channel_id']}_{first_msg_id}_{last_msg_id}".encode("utf-8")
        ).hexdigest(),
        "headline": headline,
        "content": content,
        "author": author,
        "url": discord_url,
        "published_at": group["first_date"],
        "media_type": group.get("media_type"),
        "media_path": group.get("media_path"),
        "media_urls": media_urls,
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Media Download
# ---------------------------------------------------------------------------

async def _download_attachment(
    url: str, save_path: str, timeout: int = 30
) -> bool:
    """Download a media attachment from Discord CDN.

    Args:
        url: CDN URL to download.
        save_path: Local file path to save to.
        timeout: Download timeout in seconds.

    Returns:
        True if download succeeded, False otherwise.
    """
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    with open(save_path, "wb") as f:
                        f.write(await resp.read())
                    logger.debug("Downloaded media: %s (%d bytes)", save_path, os.path.getsize(save_path))
                    return True
                else:
                    logger.warning("Media download HTTP %d for %s", resp.status, url)
    except Exception as e:
        logger.debug("Media download failed for %s: %s", url, e)
    return False


async def _download_media_for_messages(
    messages: List[Dict[str, Any]],
    channel_info: Dict[str, Any],
    media_base_dir: str = MEDIA_BASE_DIR,
) -> None:
    """Download media for messages where download_media is enabled.

    Modifies messages in-place by adding 'media_path' key.

    Args:
        messages: List of message dicts.
        channel_info: Channel metadata with 'download_media' flag.
        media_base_dir: Base directory for media storage.
    """
    if not channel_info.get("download_media", False):
        return

    server_id = channel_info.get("server_id", "")
    channel_id = channel_info.get("id", "")

    for msg in messages:
        if not msg.get("attachments"):
            continue

        for att in msg["attachments"]:
            url = att.get("url", "")
            content_type = (att.get("content_type", "") or "").lower()
            filename = (att.get("filename", "") or "").lower()

            is_image = content_type.startswith("image/") or filename.endswith(
                (".png", ".jpg", ".jpeg", ".gif", ".webp")
            )
            is_video = content_type.startswith("video/") or filename.endswith(
                (".mp4", ".webm", ".mov")
            )
            is_audio = content_type.startswith("audio/") or filename.endswith(
                (".mp3", ".ogg", ".wav")
            )

            if not (is_image or is_video or is_audio):
                continue

            if is_image:
                media_type = "photo"
            elif is_video:
                media_type = "video"
            else:
                media_type = "voice"

            ext = os.path.splitext(filename)[1] or ".bin"
            timestamp = msg.get("date", datetime.now(timezone.utc))
            if isinstance(timestamp, datetime):
                ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
            else:
                ts_str = "unknown"

            base_name = f"{media_type}_{msg['id']}_{ts_str}{ext}"
            save_path = os.path.join(media_base_dir, server_id, channel_id, base_name)

            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                msg["media_path"] = save_path
                msg["media_type"] = media_type
                continue

            success = await _download_attachment(url, save_path)
            if success:
                msg["media_path"] = save_path
                msg["media_type"] = media_type


# ---------------------------------------------------------------------------
# Discord Collector (inherits BaseCollector)
# ---------------------------------------------------------------------------

class DiscordCollector(BaseCollector):
    """Discord message collector.

    Connects to Discord using discord.py-self (user self-bot token),
    reads messages from configured channels, groups them, downloads media,
    and writes to tickles_shared.news_items.

    Uses high-water marks in system_config for restart resilience.
    """

    def __init__(
        self,
        config: Optional[CollectorConfig] = None,
        db_pool: Optional[DatabasePool] = None,
    ) -> None:
        """Initialize Discord collector.

        Args:
            config: Optional CollectorConfig. If None, loads from discord_config.json.
            db_pool: Optional DatabasePool. If None, creates one on first use.
        """
        super().__init__("discord", config)
        self._client: Optional[Any] = None  # discord.Client
        self._token: str = ""
        self._channels: Dict[str, Dict[str, Any]] = {}
        self._hwm: Dict[str, str] = {}
        self._db_pool = db_pool
        self._ready = False
        self._media_base_dir = MEDIA_BASE_DIR

        # Load config
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from CollectorConfig or discord_config.json."""
        # If config has channels, use them
        if self.config and self.config.channels:
            self._channels = {}
            for ch in self.config.channels:
                ch_id = str(ch.get("id", ""))
                if ch_id:
                    self._channels[ch_id] = ch
            self._token = self.config.extra.get("token", "")
            self._media_base_dir = self.config.media_base_dir or MEDIA_BASE_DIR
        else:
            # Fallback to JSON file
            cfg = _load_config_from_file()
            self._token = cfg.get("token", "")
            self._channels = cfg.get("channels", {}) or cfg.get("enabled_channels", {})

        # Normalize channel config
        for ch_id, ch_info in self._channels.items():
            ch_info["id"] = ch_id
            ch_info.setdefault("name", f"channel-{ch_id}")
            ch_info.setdefault("server_name", "Unknown")
            ch_info.setdefault("server_id", "")
            ch_info.setdefault("enabled", True)
            ch_info.setdefault("download_media", False)
            ch_info.setdefault("category", "signals")

        enabled = sum(1 for c in self._channels.values() if c.get("enabled", True))
        logger.info(
            "Discord config: %d channels configured, %d enabled",
            len(self._channels),
            enabled,
        )

    async def _ensure_db_pool(self) -> DatabasePool:
        """Ensure DB pool exists.

        Returns:
            DatabasePool instance.
        """
        if self._db_pool is None:
            self._db_pool = DatabasePool()
            await self._db_pool.initialize()
            logger.info("Discord DB pool initialized")
        return self._db_pool

    async def _load_hwm(self) -> None:
        """Load high-water marks from database."""
        try:
            pool = await self._ensure_db_pool()
            self._hwm = await _load_high_water_marks(pool)
            if self._hwm:
                logger.info("Loaded %d high-water marks from DB", len(self._hwm))
        except Exception as e:
            logger.warning("Failed to load HWM: %s", e)
            self._hwm = {}

    async def _save_hwm(self, channel_id: str, last_msg_id: str) -> None:
        """Save high-water mark for a channel.

        Args:
            channel_id: Discord channel ID.
            last_msg_id: Last collected message ID.
        """
        self._hwm[channel_id] = last_msg_id
        try:
            pool = await self._ensure_db_pool()
            await _save_high_water_mark(pool, channel_id, last_msg_id)
        except Exception as e:
            # Bumped from DEBUG to WARNING — see _save_high_water_mark note.
            logger.warning(
                "HWM save wrapper failed for %s: %s", channel_id, e, exc_info=True,
            )

    async def _load_source_id_map(self) -> None:
        """Populate channel_info['source_id'] by looking up each Discord
        channel's platform_id in public.collector_sources.

        A row is added for each channel during Phase 3A.1 seeding
        (see scripts/seed_collector_sources.py). If a channel has no row
        yet (e.g. a brand-new channel added to discord_config.json but not
        yet seeded) source_id stays None and the news_item gets NULL — the
        insert still succeeds, it just can't be traced back to its source.
        """
        if not self._channels:
            return
        try:
            pool = await self._ensure_db_pool()
            rows = await pool.fetch_all(
                "SELECT id, platform_id FROM collector_sources "
                "WHERE source_type = 'discord' AND entity_type = 'channel'"
            )
            id_map = {row["platform_id"]: row["id"] for row in rows}
            for ch_id, ch_info in self._channels.items():
                ch_info["source_id"] = id_map.get(str(ch_id))
            hits = sum(1 for c in self._channels.values() if c.get("source_id"))
            logger.info(
                "Resolved %d/%d Discord channel source_ids from collector_sources",
                hits, len(self._channels),
            )
        except Exception as e:
            logger.warning("Failed to resolve source_id map: %s", e)

    async def _start_client(self) -> None:
        """Start the Discord client and wait for ready."""
        if not self._token:
            logger.error("No Discord token configured")
            return

        try:
            import discord
        except ImportError:
            logger.error(
                "discord.py-self not installed. Install with: pip install discord.py-self"
            )
            return

        try:
            intents = discord.Intents.default()
            intents.message_content = True
            self._client = discord.Client(intents=intents)
        except AttributeError:
            self._client = discord.Client()

        @self._client.event
        async def on_ready() -> None:
            logger.info("Discord: logged in as %s (ID: %s)", self._client.user, self._client.user.id)
            logger.info("Discord: connected to %d servers", len(self._client.guilds))
            self._ready = True

        try:
            await self._client.start(self._token)
        except Exception as e:
            logger.error("Discord client error: %s", e, exc_info=True)
            self._ready = False

    async def _fetch_channel_messages(
        self, channel: Any, after_id: Optional[str] = None, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Fetch messages from a Discord channel.

        Args:
            channel: Discord channel object.
            after_id: Only fetch messages after this snowflake ID.
            limit: Max messages to fetch.

        Returns:
            List of message dicts in chronological order.
        """
        import discord

        messages: List[Dict[str, Any]] = []
        seen_ids: set = set()

        try:
            kwargs: Dict[str, Any] = {"limit": limit}
            if after_id:
                kwargs["after"] = discord.Object(id=int(after_id))

            async for msg in channel.history(**kwargs):
                if msg.id in seen_ids:
                    continue
                seen_ids.add(msg.id)

                msg_data = self._build_msg_data(msg, str(channel.id))
                messages.append(msg_data)

        except Exception as e:
            err_str = str(e)
            if "403" in err_str or "Forbidden" in err_str or "Missing Access" in err_str:
                logger.warning("Discord: channel %s returned 403, skipping", channel.id)
            else:
                logger.error("Discord: error fetching from channel %s: %s", channel.id, e)

        messages.sort(key=lambda m: m.get("date", datetime.min.replace(tzinfo=timezone.utc)))
        return messages

    def _build_msg_data(self, msg: Any, channel_id: str) -> Dict[str, Any]:
        """Build a message data dict from a discord.py message object.

        Args:
            msg: Discord message object.
            channel_id: Channel ID string.

        Returns:
            Message data dict.
        """
        author_name = msg.author.display_name or msg.author.name
        author_username = msg.author.name

        text = msg.content or ""
        text = self._resolve_mentions(text, msg)

        msg_data: Dict[str, Any] = {
            "id": str(msg.id),
            "channel_id": channel_id,
            "date": msg.created_at.replace(tzinfo=timezone.utc) if msg.created_at else datetime.now(timezone.utc),
            "text": text,
            "sender_id": str(msg.author.id),
            "sender_name": author_name,
            "sender_username": author_username,
            "reply_to_msg_id": None,
            "reply_to_content": None,
            "reply_to_author": None,
            "attachments": [],
            "embeds": [],
            "media_type": None,
            "media_path": None,
        }

        # Reply-to info
        if msg.reference and msg.reference.message_id:
            msg_data["reply_to_msg_id"] = str(msg.reference.message_id)
            if msg.reference.resolved:
                ref_msg = msg.reference.resolved
                if hasattr(ref_msg, "content"):
                    msg_data["reply_to_content"] = ref_msg.content[:500] if ref_msg.content else ""
                    msg_data["reply_to_author"] = ref_msg.author.name if ref_msg.author else ""

        # Attachments
        if msg.attachments:
            for att in msg.attachments:
                att_info = {
                    "url": att.url,
                    "filename": att.filename,
                    "size": att.size,
                    "content_type": att.content_type or "",
                }
                msg_data["attachments"].append(att_info)

                if not msg_data["media_type"]:
                    ct = (att.content_type or "").lower()
                    fn = att.filename.lower()
                    if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                        msg_data["media_type"] = "photo"
                    elif ct.startswith("video/") or fn.endswith((".mp4", ".webm", ".mov")):
                        msg_data["media_type"] = "video"
                    elif ct.startswith("audio/") or fn.endswith((".mp3", ".ogg", ".wav")):
                        msg_data["media_type"] = "voice"
                    else:
                        msg_data["media_type"] = "document"

        # Embeds
        if msg.embeds:
            for embed in msg.embeds:
                embed_info: Dict[str, Any] = {}
                if embed.title:
                    embed_info["title"] = embed.title
                if embed.description:
                    embed_info["description"] = embed.description
                if embed.url:
                    embed_info["url"] = embed.url
                if embed.image:
                    embed_info["image_url"] = embed.image.url
                    if not msg_data["media_type"]:
                        msg_data["media_type"] = "photo"
                msg_data["embeds"].append(embed_info)

        return msg_data

    def _resolve_mentions(self, text: str, msg: Any) -> str:
        """Resolve Discord mentions to readable names.

        Args:
            text: Raw message text with <@ID> mentions.
            msg: Discord message object.

        Returns:
            Text with resolved mention names.
        """
        if not text:
            return text

        def _resolve_user(match: re.Match) -> str:
            uid = match.group(1)
            for u in getattr(msg, "mentions", []):
                if str(u.id) == uid:
                    return f"@{u.display_name or u.name}"
            guild = getattr(msg, "guild", None)
            if guild:
                member = guild.get_member(int(uid))
                if member:
                    return f"@{member.display_name or member.name}"
            return f"@user_{uid[:6]}"

        text = re.sub(r"<@!?(\d+)>", _resolve_user, text)

        def _resolve_role(match: re.Match) -> str:
            rid = match.group(1)
            guild = getattr(msg, "guild", None)
            if guild:
                for role in guild.roles:
                    if str(role.id) == rid:
                        return f"@{role.name}"
            return f"@role_{rid[:6]}"

        text = re.sub(r"<@&(\d+)>", _resolve_role, text)

        def _resolve_channel(match: re.Match) -> str:
            cid = match.group(1)
            guild = getattr(msg, "guild", None)
            if guild:
                ch = guild.get_channel(int(cid))
                if ch:
                    return f"#{ch.name}"
            return f"#channel_{cid[:6]}"

        text = re.sub(r"<#(\d+)>", _resolve_channel, text)

        return text

    def _should_include_message(self, msg_data: Dict[str, Any]) -> bool:
        """Check if a message should be included based on user filters.

        Args:
            msg_data: Message data dict with sender info.

        Returns:
            True if message should be included.
        """
        if not self.config:
            return True

        sender_id = str(msg_data.get("sender_id", ""))
        sender_username = (msg_data.get("sender_username") or "").lower()

        # Check blocked users first
        for blocked in self.config.blocked_users:
            blocked_lower = blocked.lower()
            if blocked_lower == sender_id or blocked_lower == sender_username:
                return False

        # If allowed_users is set, only include those users
        if self.config.allowed_users:
            for allowed in self.config.allowed_users:
                allowed_lower = allowed.lower()
                if allowed_lower == sender_id or allowed_lower == sender_username:
                    return True
            return False

        return True

    async def _collect_channel(self, channel_id: str, channel_info: Dict[str, Any]) -> List[NewsItem]:
        """Collect messages from a single channel and return NewsItems.

        Args:
            channel_id: Discord channel ID.
            channel_info: Channel metadata.

        Returns:
            List of NewsItem objects.
        """
        if not self._client or not self._ready:
            return []

        try:
            channel = self._client.get_channel(int(channel_id))
            if not channel:
                try:
                    channel = await self._client.fetch_channel(int(channel_id))
                except Exception as fetch_err:
                    logger.warning(
                        "Discord: channel #%s (%s) not accessible: %s",
                        channel_info.get("name", channel_id),
                        channel_id,
                        fetch_err,
                    )
                    return []

            if not channel:
                return []

            # Get high-water mark
            after_id = self._hwm.get(channel_id)

            # Fetch messages
            limit = self.config.max_messages_per_channel if self.config else 200
            raw_messages = await self._fetch_channel_messages(channel, after_id=after_id, limit=limit)

            logger.info(
                "Discord: #%s — fetched %d messages (HWM: %s)",
                channel_info.get("name", channel_id),
                len(raw_messages),
                after_id or "none/first-run",
            )

            if not raw_messages:
                return []

            # Download media if enabled
            download_media = channel_info.get("download_media", False)
            if self.config:
                download_media = download_media or self.config.download_media
            if download_media:
                await _download_media_for_messages(raw_messages, channel_info, self._media_base_dir)

            # Update high-water mark
            max_id = str(max(int(m["id"]) for m in raw_messages))
            await self._save_hwm(channel_id, max_id)

            # Filter messages by user if configured
            if self.config and (self.config.allowed_users or self.config.blocked_users):
                filtered_messages = [m for m in raw_messages if self._should_include_message(m)]
                if len(filtered_messages) != len(raw_messages):
                    logger.debug(
                        "Discord: #%s — filtered %d -> %d messages by user filter",
                        channel_info.get("name", channel_id),
                        len(raw_messages),
                        len(filtered_messages),
                    )
                raw_messages = filtered_messages

            if not raw_messages:
                return []

            # Group messages
            group_window = self.config.group_window_seconds if self.config else 60
            grouped = _group_messages(raw_messages, channel_info, group_window)

            # Convert to NewsItems
            items: List[NewsItem] = []
            source_id = channel_info.get("source_id")
            channel_name = channel_info.get("name")
            for group in grouped:
                meta = dict(group.get("metadata") or {})
                # Stamp every news_item with its provenance so we can trace
                # back to collector_sources (and ultimately the server/channel).
                if source_id is not None:
                    meta["source_id"] = source_id
                if channel_name:
                    meta.setdefault("channel_name", channel_name)
                meta.setdefault("channel_id", str(channel_id))
                meta.setdefault("server_id", channel_info.get("server_id"))
                meta.setdefault("server_name", channel_info.get("server_name"))

                item = NewsItem(
                    source=NewsSource.DISCORD,
                    headline=group["headline"],
                    content=group["content"],
                    author=group["author"],
                    url=group["url"],
                    published_at=group["published_at"],
                    media_type=group.get("media_type"),
                    media_path=group.get("media_path"),
                    media_urls=group.get("media_urls", []),
                    metadata=meta,
                )
                item.hash_key = group["hash_key"]
                items.append(item)

            logger.info(
                "Discord: #%s — %d msgs -> %d groups -> %d items",
                channel_info.get("name", channel_id),
                len(raw_messages),
                len(grouped),
                len(items),
            )

            return items

        except Exception as e:
            logger.error(
                "Discord: error collecting from #%s: %s",
                channel_info.get("name", channel_id),
                e,
            )
            self.errors += 1
            return []

    async def collect(self) -> List[NewsItem]:
        """Collect messages from all enabled channels.

        Returns:
            List of NewsItem objects ready for DB insertion.
        """
        if not self._ready:
            logger.warning("Discord client not ready, skipping collection")
            return []

        # Reload config each cycle (allows hot-reloading channels)
        self._load_config()

        all_items: List[NewsItem] = []

        for ch_id, ch_info in self._channels.items():
            if not ch_info.get("enabled", True):
                continue

            items = await self._collect_channel(ch_id, ch_info)
            all_items.extend(items)

        return all_items

    async def run_forever(self) -> None:
        """Main loop: connect to Discord and collect every interval."""
        logger.info("Discord collector service starting...")

        await self._load_hwm()
        await self._load_source_id_map()

        if not self._token:
            logger.error(
                "No Discord token — cannot start. "
                "Set DISCORD_BOT_TOKEN env var (see /etc/tickles/discord.env).",
            )
            return

        # Start Discord client in background
        client_task = asyncio.create_task(self._start_client())

        # Wait for client to be ready
        for _ in range(30):
            if self._ready:
                break
            await asyncio.sleep(1)

        if not self._ready:
            logger.error("Discord client failed to become ready within 30s")
            return

        interval = self.config.collection_interval_seconds if self.config else 120
        logger.info("Discord collector ready. Collecting every %d seconds.", interval)

        try:
            while True:
                try:
                    items = await self.collect()
                    if items:
                        pool = await self._ensure_db_pool()
                        inserted = await self.write_to_db(items, pool)
                        if inserted > 0:
                            logger.info("Discord: wrote %d new items", inserted)

                except Exception as e:
                    logger.error("Discord collection cycle error: %s", e, exc_info=True)
                    self.errors += 1

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.info("Discord collector cancelled")
        finally:
            if self._client:
                await self._client.close()
            logger.info("Discord collector stopped. Status: %s", self.get_status())

    def get_status(self) -> Dict[str, Any]:
        """Get current service status.

        Returns:
            Status dict with state, stats, channel info.
        """
        base_status = super().get_status()
        enabled_channels = [
            f"{c.get('server_name', '')} > #{c.get('name', '')}"
            for c in self._channels.values()
            if c.get("enabled", True)
        ]
        base_status.update({
            "ready": self._ready,
            "token_configured": bool(self._token),
            "channels_configured": len(self._channels),
            "channels_enabled": len(enabled_channels),
            "channel_names": enabled_channels,
            "hwm_count": len(self._hwm),
        })
        return base_status


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Main entry point for the Discord collector service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    collector = DiscordCollector()
    await collector.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
