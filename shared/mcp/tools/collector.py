"""
MCP tools: Discord/Telegram collector management.
Location: /opt/tickles/shared/mcp/tools/collector.py

Tools:
  collector.channels   — List all collector channels with HWM, status, item counts
  collector.hwm.reset  — Reset high-water mark for a channel
  collector.messages   — Fetch recent Discord messages via REST API
  collector.health     — Quick health: count channels with stale/poisoned HWMs
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext

logger = logging.getLogger(__name__)

STALE_HWM_HOURS = 2  # HWMs older than this without new items are "stale"


async def _get_pool():
    from shared.utils.db import DatabasePool
    return await DatabasePool.get_instance()


def _fmt_ts(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _handle_channels(p: Dict[str, Any]) -> Dict[str, Any]:
    """List collector channels with HWM status and item counts."""
    pool = await _get_pool()

    platform = str(p.get("platform", "")).strip().lower() or None
    stale_only = bool(p.get("staleOnly", False))

    rows = await pool.fetch_all("""
        SELECT
            cs.id, cs.name, cs.platform_id, cs.source_type, cs.enabled,
            cs.last_collected_at, cs.items_collected,
            sc.config_value as hwm, sc.updated_at as hwm_updated_at,
            (SELECT count(*) FROM news_items n
             WHERE n.source_id = cs.id AND n.collected_at > now() - interval '24 hours'
            ) as items_24h,
            (SELECT max(n.collected_at) FROM news_items n WHERE n.source_id = cs.id
            ) as last_item_at
        FROM collector_sources cs
        LEFT JOIN system_config sc ON sc.namespace = 'discord_hwm'
            AND sc.config_key = cs.platform_id
        WHERE cs.source_type IN ('discord', 'telegram')
          AND cs.entity_type = 'channel'
          AND ($1::text IS NULL OR cs.source_type = $1)
        ORDER BY cs.source_type, items_24h DESC, cs.name
    """, (platform,))

    channels = []
    now = datetime.now(timezone.utc)
    for r in rows:
        hwm_age_hours = None
        if r["hwm_updated_at"]:
            hwm_age_hours = round((now - r["hwm_updated_at"]).total_seconds() / 3600, 1)

        # Determine status
        items = int(r["items_24h"] or 0)
        has_hwm = bool(r["hwm"])
        status = "healthy"
        if not r["enabled"]:
            status = "disabled"
        elif not has_hwm:
            status = "first_run"
        elif items == 0 and hwm_age_hours and hwm_age_hours > STALE_HWM_HOURS:
            status = "stale"
        elif items == 0:
            status = "empty"

        channels.append({
            "id": int(r["id"]),
            "name": r["name"],
            "platform": r["source_type"],
            "platform_id": r["platform_id"],
            "enabled": bool(r["enabled"]),
            "status": status,
            "hwm": r["hwm"],
            "hwm_age_hours": hwm_age_hours,
            "hwm_updated_at": _fmt_ts(r["hwm_updated_at"]),
            "items_24h": items,
            "items_total": int(r["items_collected"] or 0),
            "last_item_at": _fmt_ts(r["last_item_at"]),
            "last_collected_at": _fmt_ts(r["last_collected_at"]),
        })

    if stale_only:
        channels = [c for c in channels if c["status"] in ("stale", "empty")]

    return {
        "ok": True,
        "count": len(channels),
        "channels": channels,
    }


async def _handle_hwm_reset(p: Dict[str, Any]) -> Dict[str, Any]:
    """Reset the high-water mark for a collector channel."""
    pool = await _get_pool()

    channel_id = p.get("channelId")
    channel_name = p.get("channelName")
    reset_all = bool(p.get("resetAll", False))

    if not channel_id and not channel_name and not reset_all:
        return {"ok": False, "error": "Provide channelId, channelName, or resetAll=true"}

    async with pool.acquire() as conn:
        if reset_all:
            result = await conn.execute(
                "DELETE FROM system_config WHERE namespace = 'discord_hwm'"
            )
            count = 0
            if hasattr(result, "split"):
                count = int(result.split()[-1])
            return {"ok": True, "reset": count, "message": f"Cleared {count} HWMs"}

        # Find channel
        if channel_id:
            row = await conn.fetchrow(
                "SELECT id, name, platform_id FROM collector_sources WHERE id = $1",
                int(channel_id),
            )
        else:
            row = await conn.fetchrow(
                "SELECT id, name, platform_id FROM collector_sources "
                "WHERE name ILIKE $1 AND source_type IN ('discord','telegram') "
                "LIMIT 1",
                f"%{channel_name}%",
            )

        if not row:
            return {"ok": False, "error": "Channel not found"}

        result = await conn.execute(
            "DELETE FROM system_config "
            "WHERE namespace = 'discord_hwm' AND config_key = $1",
            row["platform_id"],
        )

        return {
            "ok": True,
            "channel_id": int(row["id"]),
            "channel_name": row["name"],
            "message": f"HWM reset for {row['name']}. Restart collector to pick up changes.",
        }


async def _handle_messages(p: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch recent messages from a Discord channel via REST API."""
    import os
    
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        # MCP daemon may not have .env loaded — try loading it
        try:
            from dotenv import load_dotenv
            load_dotenv("/opt/tickles/.env")
            token = os.environ.get("DISCORD_BOT_TOKEN", "")
        except Exception:
            pass
    if not token:
        return {"ok": False, "error": "DISCORD_BOT_TOKEN not set"}

    channel_id = str(p.get("channelId") or "")
    channel_name = str(p.get("channelName") or "")
    limit = min(int(p.get("limit", 50)), 200)

    if not channel_id and not channel_name:
        return {"ok": False, "error": "Provide channelId or channelName"}

    pool = await _get_pool()

    # Resolve channel name to ID if needed
    if not channel_id:
        row = await pool.fetch_one(
            "SELECT platform_id, name FROM collector_sources "
            "WHERE name ILIKE $1 AND source_type = 'discord' "
            "ORDER BY enabled DESC LIMIT 1",
            (f"%{channel_name}%",),
        )
        if not row:
            return {"ok": False, "error": f"Channel not found: {channel_name}"}
        channel_id = row["platform_id"]

    # Fetch from Discord REST API
    import aiohttp
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
    headers = {"Authorization": f"Bot {token}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 403:
                    return {"ok": False, "error": "403 Forbidden — bot lacks access to this channel"}
                if resp.status != 200:
                    return {"ok": False, "error": f"Discord API returned {resp.status}"}

                raw_msgs = await resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    messages = []
    for msg in raw_msgs:
        attachments = []
        for att in msg.get("attachments", []):
            attachments.append({
                "id": att.get("id"),
                "filename": att.get("filename"),
                "url": att.get("url"),
                "content_type": att.get("content_type"),
                "size": att.get("size"),
            })

        # Handle reply context
        ref = msg.get("message_reference")
        replied_to = None
        if ref:
            replied_to = {
                "message_id": ref.get("message_id"),
                "channel_id": ref.get("channel_id"),
            }

        messages.append({
            "id": msg.get("id"),
            "author": msg.get("author", {}).get("username", "unknown"),
            "author_global": msg.get("author", {}).get("global_name"),
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp"),
            "attachments": attachments,
            "reply_to": replied_to,
            "has_embeds": len(msg.get("embeds", [])) > 0,
        })

    messages.sort(key=lambda m: m["id"])

    return {
        "ok": True,
        "channel_id": channel_id,
        "count": len(messages),
        "messages": messages,
    }

async def _handle_health(p: Dict[str, Any]) -> Dict[str, Any]:
    """Quick health check for collector channels."""
    pool = await _get_pool()

    # Count channels by status
    rows = await pool.fetch_all("""
        SELECT
            cs.source_type,
            cs.enabled,
            CASE
                WHEN NOT cs.enabled THEN 'disabled'
                WHEN sc.config_key IS NULL THEN 'no_hwm'
                WHEN (SELECT count(*) FROM news_items n
                      WHERE n.source_id = cs.id AND n.collected_at > now() - interval '24 hours') > 0
                THEN 'collecting'
                WHEN sc.updated_at < now() - interval '2 hours' THEN 'stale'
                ELSE 'empty'
            END as status,
            count(*) as cnt
        FROM collector_sources cs
        LEFT JOIN system_config sc ON sc.namespace = 'discord_hwm'
            AND sc.config_key = cs.platform_id
        WHERE cs.source_type IN ('discord', 'telegram')
          AND cs.entity_type = 'channel'
        GROUP BY cs.source_type, cs.enabled,
            CASE
                WHEN NOT cs.enabled THEN 'disabled'
                WHEN sc.config_key IS NULL THEN 'no_hwm'
                WHEN (SELECT count(*) FROM news_items n
                      WHERE n.source_id = cs.id AND n.collected_at > now() - interval '24 hours') > 0
                THEN 'collecting'
                WHEN sc.updated_at < now() - interval '2 hours' THEN 'stale'
                ELSE 'empty'
            END
        ORDER BY cs.source_type, status
    """)

    summary = {}
    for r in rows:
        key = f"{r['source_type']}_{r['status']}"
        summary[key] = int(r["cnt"])

    total_enabled = sum(v for k, v in summary.items() if "disabled" not in k)
    stale_count = summary.get("discord_stale", 0) + summary.get("telegram_stale", 0)
    collecting = sum(v for k, v in summary.items() if "collecting" in k)

    return {
        "ok": True,
        "total_channels": sum(summary.values()),
        "enabled": total_enabled,
        "collecting_24h": collecting,
        "stale": stale_count,
        "breakdown": summary,
        "healthy": stale_count == 0 and collecting > 0,
    }


# BIBLE-P8 — new handler functions
async def _handle_source_tree(ctx: "ToolContext", args: dict) -> dict:
    """Return the server->group->channel tree with follow/media/health."""
    src = (args.get("platform") or "discord").lower()
    if src not in ("discord", "telegram"):
        src = "discord"
    pool = await _get_pool()
    rows = await pool.fetch(
        "SELECT id, parent_id, entity_type, name, enabled, media_policy, "
        "       last_collected_at, last_error, error_count, "
        "       (platform_config->>'last_hwm') AS last_hwm "
        "FROM collector_sources WHERE source_type=$1 "
        "ORDER BY priority NULLS LAST, name", src,
    )
    return {"source": src, "count": len(rows), "tree": [dict(r) for r in rows]}

async def _handle_set_media_policy(ctx: "ToolContext", args: dict) -> dict:
    """Set media_policy for one source (none|text|text_images|everything)."""
    sid = args.get("sourceId")
    policy = (args.get("policy") or "").lower()
    if not isinstance(sid, int):
        return {"error": "sourceId (int) required"}
    if policy not in ("none", "text", "text_images", "everything"):
        return {"error": "policy must be none|text|text_images|everything"}
    pool = await _get_pool()
    await pool.execute(
        "UPDATE collector_sources SET media_policy=$2, updated_at=now() WHERE id=$1",
        sid, policy,
    )
    return {"updated": True, "sourceId": sid, "policy": policy}

async def _handle_backfill_channel(ctx: "ToolContext", args: dict) -> dict:
    """Force a channel to re-scan by clearing its HWM. Returns prior HWM."""
    sid = args.get("sourceId")
    if not isinstance(sid, int):
        return {"error": "sourceId (int) required"}
    pool = await _get_pool()
    row = await pool.fetchrow(
        "SELECT platform_id, platform_config->>'last_hwm' AS prior_hwm "
        "FROM collector_sources WHERE id=$1", sid,
    )
    if not row:
        return {"error": f"source {sid} not found"}
    await pool.execute(
        "DELETE FROM system_config WHERE namespace='discord_hwm' AND config_key=$1",
        str(row["platform_id"]),
    )
    return {"backfillQueued": True, "sourceId": sid,
            "channelId": row["platform_id"], "priorHwm": row["prior_hwm"]}


# ---------------------------------------------------------------------------
# Registration

def _build_tools(ctx: ToolContext):
    return [
        (
            McpTool(
                name="collector.channels",
                description=(
                    "List collector channels with HWM status, item counts, and health. "
                    "Filter by platform (discord/telegram) or show only stale channels."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "platform": {
                            "type": "string",
                            "description": "Filter by platform: 'discord' or 'telegram'.",
                        },
                        "staleOnly": {
                            "type": "boolean",
                            "description": "Show only channels with stale or empty HWMs.",
                            "default": False,
                        },
                    },
                },
                tags={"group": "collector", "status": "live"},
            ),
            _handle_channels,
        ),
        (
            McpTool(
                name="collector.hwm.reset",
                description=(
                    "Reset the high-water mark for a collector channel. "
                    "Use resetAll=true to clear all HWMs. The collector will "
                    "re-scan from the latest messages on next poll."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "channelId": {
                            "type": "integer",
                            "description": "Collector source ID to reset.",
                        },
                        "channelName": {
                            "type": "string",
                            "description": "Channel name (partial match) to reset.",
                        },
                        "resetAll": {
                            "type": "boolean",
                            "description": "Reset ALL Discord HWM entries. USE CAREFULLY.",
                            "default": False,
                        },
                    },
                },
                tags={"group": "collector", "status": "live"},
            ),
            _handle_hwm_reset,
        ),
        (
            McpTool(
                name="collector.health",
                description=(
                    "Quick health check for collector channels. "
                    "Returns counts by status (collecting/stale/empty/disabled)."
                ),
                input_schema={"type": "object", "properties": {}},
                tags={"group": "collector", "status": "live"},
            ),
            _handle_health,
        ),
        (
            McpTool(
                name="collector.messages",
                description=(
                    "Fetch recent messages from a Discord channel via REST API. "
                    "Returns message content, author, attachments, and reply chains. "
                    "Use this to preview what the collector would see in a channel."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "channelId": {
                            "type": "string",
                            "description": "Discord channel ID to fetch from.",
                        },
                        "channelName": {
                            "type": "string",
                            "description": "Channel name (partial match) — resolved to ID automatically.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max messages to return (default 50, max 200).",
                            "default": 50,
                        },
                    },
                },
                tags={"group": "collector", "status": "live"},
            ),
            _handle_messages,
        ),
        # BIBLE-P8: new tools
        (
            McpTool(
                name="collector.source_tree",
                description="Return the collector source hierarchy (server->group->channel) with follow state, media policy, last-seen freshness, and health for discord or telegram.",
                input_schema={"type":"object","properties":{"platform":{"type":"string","description":"discord|telegram"}}},
                tags={"group":"collector","status":"live"},
            ),
            _handle_source_tree,
        ),
        (
            McpTool(
                name="collector.set_media_policy",
                description="Set what media a source ingests: none | text | text_images | everything.",
                input_schema={"type":"object","required":["sourceId","policy"],"properties":{"sourceId":{"type":"integer"},"policy":{"type":"string","enum":["none","text","text_images","everything"]}}},
                tags={"group":"collector","status":"live"},
            ),
            _handle_set_media_policy,
        ),
        (
            McpTool(
                name="collector.backfill_channel",
                description="Force a single channel to re-scan its backlog by clearing its high-water mark; next poll drains it oldest-first. Returns the prior HWM for auditability.",
                input_schema={"type":"object","required":["sourceId"],"properties":{"sourceId":{"type":"integer"}}},
                tags={"group":"collector","status":"live"},
            ),
            _handle_backfill_channel,
        ),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
    logger.info("[collector] registered %d tools", len(_build_tools(ctx)))
