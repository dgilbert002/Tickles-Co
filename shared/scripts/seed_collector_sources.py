"""
Script: seed_collector_sources
Purpose: One-shot seeder that takes the legacy discord_config.json and
         materialises it as rows in public.collector_sources.
Location: /opt/tickles/shared/scripts/seed_collector_sources.py

Phase 3A.1 (2026-04-18).

USAGE
-----
    # On the VPS, after applying 2026_04_18_phase3a1_collector_sources.sql:
    python3 -m shared.scripts.seed_collector_sources

    # Dry-run (shows what would be inserted without touching the DB):
    python3 -m shared.scripts.seed_collector_sources --dry-run

BEHAVIOUR
---------
  * For every distinct (server_id, server_name) pair in discord_config.json
    a row with entity_type='server', platform_id=server_id is inserted.
  * For every channel a row with entity_type='channel' is inserted with
    parent_id pointing at the server row.
  * Uses ON CONFLICT (source_type, platform_id) DO NOTHING — rerunnable.
  * Channel `download_media` true => media_policy='download_keep'.
    Channel `download_media` false => media_policy='reference_only'.
  * Channel `enabled` flag is copied into `enabled` column.
  * Channel `discord_category`, `category`, `trigger_mode` JSON are bundled
    into `platform_config` JSONB for later reference.
  * The discord bot token is NEVER read by this script.

ROLLBACK
--------
    DELETE FROM public.collector_sources WHERE source_type = 'discord';
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Tuple

from shared.utils import db

logger = logging.getLogger("tickles.seed_collector_sources")

DISCORD_CONFIG_PATH = os.environ.get(
    "DISCORD_CONFIG_PATH",
    "/opt/tickles/shared/collectors/discord/data/discord_config.json",
)


def _load_discord_channels(path: str) -> List[Dict[str, Any]]:
    """Read discord_config.json and return a list of channel dicts.

    Silently strips the token if it's still present (defence in depth).
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"discord_config.json not found at {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.pop("token", None)
    channels = data.get("enabled_channels") or data.get("channels") or {}
    if isinstance(channels, dict):
        return list(channels.values())
    if isinstance(channels, list):
        return channels
    logger.warning("Unknown channels structure in %s: %r", path, type(channels))
    return []


def _derive_servers(channels: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """Return a sorted list of unique (server_id, server_name) tuples."""
    seen: Dict[str, str] = {}
    for ch in channels:
        sid = str(ch.get("server_id") or "").strip()
        if not sid:
            continue
        sname = ch.get("server_name") or "Unknown"
        if sid not in seen:
            seen[sid] = sname
    return sorted(seen.items())


async def _seed(dry_run: bool = False) -> None:
    channels = _load_discord_channels(DISCORD_CONFIG_PATH)
    logger.info("Loaded %d Discord channels from %s", len(channels), DISCORD_CONFIG_PATH)

    servers = _derive_servers(channels)
    logger.info("Derived %d unique Discord servers", len(servers))

    pool = await db.get_shared_pool()

    server_id_map: Dict[str, int] = {}

    insert_source_sql = """
        INSERT INTO collector_sources (
            parent_id, source_type, entity_type, platform_id, name,
            description, enabled, media_policy, platform_config
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        ON CONFLICT (source_type, platform_id) DO UPDATE
            SET name = EXCLUDED.name,
                enabled = EXCLUDED.enabled,
                platform_config = EXCLUDED.platform_config,
                updated_at = CURRENT_TIMESTAMP
        RETURNING id
    """

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ---- Servers ----
            for sid, sname in servers:
                platform_config = json.dumps({"source": "discord_config.json"})
                if dry_run:
                    logger.info("[dry-run] SERVER %s / %s", sid, sname)
                    server_id_map[sid] = -1
                    continue

                new_id = await conn.fetchval(
                    insert_source_sql,
                    None,
                    "discord",
                    "server",
                    sid,
                    sname,
                    f"Discord server: {sname}",
                    True,
                    "reference_only",
                    platform_config,
                )
                server_id_map[sid] = int(new_id)
                logger.info("SERVER  id=%s platform=%s name=%s", new_id, sid, sname)

            # ---- Channels ----
            for ch in channels:
                cid = str(ch.get("id") or "").strip()
                if not cid:
                    logger.warning("Skipping channel without id: %r", ch)
                    continue
                sid = str(ch.get("server_id") or "").strip()
                parent_id = server_id_map.get(sid)

                download = bool(ch.get("download_media", False))
                media_policy = "download_keep" if download else "reference_only"

                enabled = bool(ch.get("enabled", True))
                name = ch.get("name") or f"channel-{cid}"

                platform_config = json.dumps({
                    "discord_category": ch.get("discord_category"),
                    "category": ch.get("category"),
                    "trigger_mode": ch.get("trigger_mode"),
                    "channel_type": ch.get("type"),
                    "download_media": download,
                })

                if dry_run:
                    logger.info(
                        "[dry-run] CHANNEL %s / %s  parent_server=%s  policy=%s",
                        cid, name, sid, media_policy,
                    )
                    continue

                new_id = await conn.fetchval(
                    insert_source_sql,
                    parent_id,
                    "discord",
                    "channel",
                    cid,
                    name,
                    ch.get("discord_category") or "",
                    enabled,
                    media_policy,
                    platform_config,
                )
                logger.debug(
                    "CHANNEL id=%s parent=%s platform=%s name=%s policy=%s",
                    new_id, parent_id, cid, name, media_policy,
                )

    if dry_run:
        logger.info("Dry-run complete — no rows written.")
        return

    n_sources = await pool.fetch_val("SELECT COUNT(*) FROM collector_sources")
    n_discord = await pool.fetch_val(
        "SELECT COUNT(*) FROM collector_sources WHERE source_type = 'discord'"
    )
    n_servers = await pool.fetch_val(
        "SELECT COUNT(*) FROM collector_sources WHERE source_type = 'discord' AND entity_type = 'server'"
    )
    n_channels = await pool.fetch_val(
        "SELECT COUNT(*) FROM collector_sources WHERE source_type = 'discord' AND entity_type = 'channel'"
    )
    logger.info(
        "Seed complete. collector_sources rows: total=%s discord=%s (servers=%s, channels=%s)",
        n_sources, n_discord, n_servers, n_channels,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed collector_sources from discord_config.json")
    p.add_argument("--dry-run", action="store_true", help="Show what would change without writing.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )
    try:
        asyncio.run(_seed(dry_run=args.dry_run))
    except Exception:
        logger.exception("Seed failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
