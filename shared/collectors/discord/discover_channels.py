"""
Module: discover_discord_channels
Purpose: Auto-discover all Discord channels the bot can see and output config JSON.
Location: /opt/tickles/shared/collectors/discord/discover_channels.py

Usage:
    python -m shared.collectors.discord.discover_channels

Outputs a JSON snippet that can be merged into discord_config.json.
Also prints a TUI-friendly channel list.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _load_existing_config() -> Dict[str, Any]:
    """Load existing discord_config.json to preserve enabled flags."""
    config_dir = os.environ.get(
        "TICKLES_DATA_DIR",
        os.path.join(os.path.dirname(__file__), "data"),
    )
    config_path = os.path.join(config_dir, "discord_config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load existing config: %s", e)
    return {"enabled_channels": {}}


async def discover_channels() -> Dict[str, Any]:
    """Connect to Discord and enumerate all visible channels.

    Returns:
        Dict with 'enabled_channels' keyed by channel_id.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        logger.error("DISCORD_BOT_TOKEN env var not set")
        sys.exit(1)

    try:
        import discord
    except ImportError:
        logger.error("discord.py-self not installed. Run: pip install discord.py-self")
        sys.exit(1)

    try:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
    except AttributeError:
        client = discord.Client()

    discovered: Dict[str, Any] = {}
    existing = _load_existing_config().get("enabled_channels", {})

    @client.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (ID: %s)", client.user, client.user.id)
        logger.info("Connected to %d server(s)", len(client.guilds))

        for guild in client.guilds:
            logger.info("\n📁 Server: %s (ID: %s)", guild.name, guild.id)
            for channel in guild.channels:
                if isinstance(channel, discord.TextChannel):
                    ch_id = str(channel.id)
                    # Preserve existing config if present
                    if ch_id in existing:
                        discovered[ch_id] = existing[ch_id]
                        status = "[KEEP]"
                    else:
                        discovered[ch_id] = {
                            "id": ch_id,
                            "name": channel.name,
                            "discord_category": channel.category.name if channel.category else "Uncategorized",
                            "server_id": str(guild.id),
                            "server_name": guild.name,
                            "type": "text",
                            "enabled": True,
                            "download_media": True,
                            "category": "alpha_signals",
                            "trigger_mode": "confidence",
                        }
                        status = "[NEW]"
                    logger.info(
                        "  %s #%s (%s) — category: %s",
                        status,
                        channel.name,
                        ch_id,
                        channel.category.name if channel.category else "Uncategorized",
                    )

        await client.close()

    try:
        await client.start(token)
    except Exception as e:
        logger.error("Discord connection failed: %s", e)
        sys.exit(1)

    return {"enabled_channels": discovered}


def main() -> None:
    """Entry point for channel discovery."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = asyncio.run(discover_channels())

    # Write to stdout as JSON
    print("\n" + "=" * 60)
    print("DISCOVERED CHANNELS JSON (merge into discord_config.json)")
    print("=" * 60)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # Summary
    total = len(result["enabled_channels"])
    existing_count = sum(
        1 for ch_id in result["enabled_channels"]
        if ch_id in _load_existing_config().get("enabled_channels", {})
    )
    print(f"\nSummary: {total} total channels, {existing_count} existing, {total - existing_count} new")


if __name__ == "__main__":
    main()
