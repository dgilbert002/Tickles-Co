"""
Module: media_extractor
Purpose: Unified media extraction and download service for all Tickles V2 collectors
Location: /opt/tickles/shared/collectors/media_extractor.py

Consolidates media download logic from Discord and Telegram collectors into a single
reusable service. Handles URL-based downloads (Discord CDN) and Telethon message-based
downloads (Telegram) with consistent file naming, type classification, and deduplication.

Media types supported:
    - photo: Images (png, jpg, jpeg, gif, webp)
    - video: Video clips (mp4, webm, mov)
    - voice: Voice messages / audio (mp3, ogg, wav)
    - document: Other files (pdf, zip, etc.)

Storage structure:
    {base_dir}/{source}/{channel_or_chat_id}/{media_type}_{message_id}_{timestamp}.{ext}
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("tickles.collectors.media")

# ---------------------------------------------------------------------------
# Media Type Classification
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff")
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".avi", ".mkv")
AUDIO_EXTENSIONS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".aac")

IMAGE_MIME_PREFIXES = ("image/",)
VIDEO_MIME_PREFIXES = ("video/",)
AUDIO_MIME_PREFIXES = ("audio/",)


def classify_media(
    content_type: str = "",
    filename: str = "",
) -> Tuple[str, str]:
    """Classify a media file by MIME type or filename extension.

    Args:
        content_type: MIME content type (e.g., 'image/png').
        filename: Original filename with extension.

    Returns:
        Tuple of (media_type, default_extension).
        media_type is one of: 'photo', 'video', 'voice', 'document'.
    """
    ct = (content_type or "").lower()
    fn = (filename or "").lower()
    ext = os.path.splitext(fn)[1] if fn else ""

    # Check MIME type first
    if any(ct.startswith(p) for p in IMAGE_MIME_PREFIXES):
        return "photo", ext or ".jpg"
    if any(ct.startswith(p) for p in VIDEO_MIME_PREFIXES):
        return "video", ext or ".mp4"
    if any(ct.startswith(p) for p in AUDIO_MIME_PREFIXES):
        return "voice", ext or ".ogg"

    # Fallback to extension
    if ext in IMAGE_EXTENSIONS:
        return "photo", ext
    if ext in VIDEO_EXTENSIONS:
        return "video", ext
    if ext in AUDIO_EXTENSIONS:
        return "voice", ext

    return "document", ext or ".bin"


def classify_telethon_media(msg: Any) -> Optional[str]:
    """Classify media type from a Telethon message object.

    Args:
        msg: Telethon message object.

    Returns:
        Media type string or None if no media.
    """
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "document", None):
        return "document"
    if getattr(msg, "audio", None):
        return "voice"
    return None


# ---------------------------------------------------------------------------
# Filename Generation
# ---------------------------------------------------------------------------

def generate_filename(
    media_type: str,
    message_id: Any,
    timestamp: Optional[datetime] = None,
    extension: str = "",
) -> str:
    """Generate a standardized filename for downloaded media.

    Args:
        media_type: One of 'photo', 'video', 'voice', 'document'.
        message_id: Message identifier (int, str, or snowflake).
        timestamp: Message timestamp for filename.
        extension: File extension including dot (e.g., '.jpg').

    Returns:
        Filename string like 'photo_12345_20260115_101640.jpg'.
    """
    if timestamp and isinstance(timestamp, datetime):
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
    else:
        ts_str = "unknown"

    ext = extension if extension.startswith(".") else f".{extension}" if extension else ""
    return f"{media_type}_{message_id}_{ts_str}{ext}"


# ---------------------------------------------------------------------------
# URL-Based Download (Discord, generic HTTP)
# ---------------------------------------------------------------------------

async def download_from_url(
    url: str,
    save_path: str,
    timeout: int = 30,
) -> bool:
    """Download media from a URL to a local file.

    Args:
        url: HTTP/HTTPS URL to download.
        save_path: Absolute local path to save the file.
        timeout: Download timeout in seconds.

    Returns:
        True if download succeeded, False otherwise.
    """
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    with open(save_path, "wb") as f:
                        f.write(await resp.read())
                    file_size = os.path.getsize(save_path)
                    logger.debug(
                        "Downloaded media: %s (%d bytes)", save_path, file_size
                    )
                    return True
                logger.warning("Media download HTTP %d for %s", resp.status, url)
    except Exception as e:
        logger.debug("Media download failed for %s: %s", url, e)
    return False


# ---------------------------------------------------------------------------
# Telethon-Based Download (Telegram)
# ---------------------------------------------------------------------------

async def download_telethon_media(
    msg: Any,
    save_path: str,
) -> bool:
    """Download media from a Telethon message to a local file.

    Args:
        msg: Telethon message object with media.
        save_path: Absolute local path to save the file.

    Returns:
        True if download succeeded, False otherwise.
    """
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        await msg.download_media(file=save_path)
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            logger.debug(
                "Downloaded Telegram media: %s (%d bytes)",
                save_path,
                os.path.getsize(save_path),
            )
            return True
    except Exception as e:
        logger.warning("Telegram media download failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# MediaExtractor — Unified Service
# ---------------------------------------------------------------------------

class MediaExtractor:
    """Unified media extraction service for all collectors.

    Provides consistent media download, classification, and storage across
    Discord, Telegram, and any future collectors.

    Usage:
        extractor = MediaExtractor(base_dir="/opt/tickles/data/media")

        # Discord: download from CDN URL
        result = await extractor.download_discord_attachment(
            url="https://cdn.discordapp.com/...",
            content_type="image/png",
            filename="screenshot.png",
            message_id=12345,
            timestamp=datetime.now(timezone.utc),
            source="discord",
            channel_id="67890",
        )

        # Telegram: download from Telethon message
        result = await extractor.download_telegram_message(
            msg=telethon_message,
            source="telegram",
            chat_id="11111",
        )
    """

    def __init__(self, base_dir: str = "") -> None:
        """Initialize the media extractor.

        Args:
            base_dir: Base directory for media storage.
                Defaults to TICKLES_MEDIA_DIR env var or /opt/tickles/data/media.
        """
        self.base_dir = base_dir or os.environ.get(
            "TICKLES_MEDIA_DIR",
            "/opt/tickles/data/media",
        )
        logger.info("MediaExtractor initialized with base_dir: %s", self.base_dir)

    def _build_save_path(
        self,
        source: str,
        channel_id: str,
        filename: str,
    ) -> str:
        """Build the full save path for a media file.

        Args:
            source: Source name ('discord', 'telegram', etc.).
            channel_id: Channel or chat ID.
            filename: Generated filename.

        Returns:
            Absolute path like '{base_dir}/discord/67890/photo_123_20260115_101640.jpg'.
        """
        return os.path.join(self.base_dir, source, str(channel_id), filename)

    async def download_discord_attachment(
        self,
        url: str,
        content_type: str = "",
        filename: str = "",
        message_id: Any = "",
        timestamp: Optional[datetime] = None,
        source: str = "discord",
        channel_id: str = "",
    ) -> Dict[str, Any]:
        """Download a Discord attachment from CDN.

        Args:
            url: CDN URL of the attachment.
            content_type: MIME type from Discord API.
            filename: Original filename from Discord.
            message_id: Discord message snowflake ID.
            timestamp: Message timestamp.
            source: Source identifier for path construction.
            channel_id: Discord channel ID.

        Returns:
            Dict with keys: media_type, media_path, success.
        """
        media_type, ext = classify_media(content_type, filename)
        gen_filename = generate_filename(media_type, message_id, timestamp, ext)
        save_path = self._build_save_path(source, channel_id, gen_filename)

        # Skip if already downloaded
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            logger.debug("Media already exists: %s", save_path)
            return {"media_type": media_type, "media_path": save_path, "success": True}

        success = await download_from_url(url, save_path)
        return {
            "media_type": media_type if success else None,
            "media_path": save_path if success else None,
            "success": success,
        }

    async def download_discord_embed(
        self,
        image_url: str,
        message_id: Any = "",
        timestamp: Optional[datetime] = None,
        source: str = "discord",
        channel_id: str = "",
    ) -> Dict[str, Any]:
        """Download an image from a Discord embed.

        Args:
            image_url: Direct image URL from embed.
            message_id: Discord message snowflake ID.
            timestamp: Message timestamp.
            source: Source identifier.
            channel_id: Discord channel ID.

        Returns:
            Dict with keys: media_type, media_path, success.
        """
        gen_filename = generate_filename("photo", message_id, timestamp, ".png")
        save_path = self._build_save_path(source, channel_id, gen_filename)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return {"media_type": "photo", "media_path": save_path, "success": True}

        success = await download_from_url(image_url, save_path)
        return {
            "media_type": "photo" if success else None,
            "media_path": save_path if success else None,
            "success": success,
        }

    async def download_telegram_message(
        self,
        msg: Any,
        source: str = "telegram",
        chat_id: str = "",
    ) -> Dict[str, Any]:
        """Download media from a Telethon message.

        Args:
            msg: Telethon message object.
            source: Source identifier for path construction.
            chat_id: Telegram chat/channel ID.

        Returns:
            Dict with keys: media_type, media_path, success.
        """
        media_type = classify_telethon_media(msg)
        if not media_type:
            return {"media_type": None, "media_path": None, "success": False}

        # Determine extension based on media type
        ext = ".jpg"
        if media_type == "video":
            mime = getattr(getattr(msg, "video", None), "mime_type", "") or ""
            ext = ".webm" if "webm" in mime else ".mp4"
        elif media_type == "voice":
            ext = ".ogg"
        elif media_type == "document":
            mime = getattr(getattr(msg, "document", None), "mime_type", "") or ""
            if "pdf" in mime:
                ext = ".pdf"
            elif "zip" in mime:
                ext = ".zip"

        gen_filename = generate_filename(
            media_type, msg.id, getattr(msg, "date", None), ext
        )
        save_path = self._build_save_path(source, chat_id, gen_filename)

        # Skip if already downloaded
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            logger.debug("Telegram media already exists: %s", save_path)
            return {"media_type": media_type, "media_path": save_path, "success": True}

        success = await download_telethon_media(msg, save_path)
        return {
            "media_type": media_type if success else None,
            "media_path": save_path if success else None,
            "success": success,
        }

    async def download_messages_batch(
        self,
        messages: List[Dict[str, Any]],
        source: str,
        channel_id: str,
    ) -> None:
        """Download media for a batch of message dicts (Discord-style).

        Modifies messages in-place by adding 'media_path' and 'media_type' keys.

        Args:
            messages: List of message dicts with 'attachments' and 'embeds'.
            source: Source identifier.
            channel_id: Channel ID for path construction.
        """
        for msg in messages:
            # Process attachments
            for att in msg.get("attachments", []):
                url = att.get("url", "")
                if not url:
                    continue

                result = await self.download_discord_attachment(
                    url=url,
                    content_type=att.get("content_type", ""),
                    filename=att.get("filename", ""),
                    message_id=msg.get("id", ""),
                    timestamp=msg.get("date"),
                    source=source,
                    channel_id=channel_id,
                )

                if result["success"] and not msg.get("media_path"):
                    msg["media_path"] = result["media_path"]
                    msg["media_type"] = result["media_type"]

            # Process embed images
            for emb in msg.get("embeds", []):
                image_url = emb.get("image_url", "")
                if not image_url:
                    continue

                result = await self.download_discord_embed(
                    image_url=image_url,
                    message_id=msg.get("id", ""),
                    timestamp=msg.get("date"),
                    source=source,
                    channel_id=channel_id,
                )

                if result["success"] and not msg.get("media_path"):
                    msg["media_path"] = result["media_path"]
                    msg["media_type"] = result["media_type"]
