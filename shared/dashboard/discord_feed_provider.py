"""BIBLE-P4: Discord/Telegram feed + tree + config providers (read/write helpers)."""
from __future__ import annotations
import json, logging
from typing import Any, Optional
logger = logging.getLogger(__name__)

from shared.utils.db import get_shared_pool

async def get_source_tree(source_type: str = "discord") -> list[dict]:
    """Return the server->group->channel tree from collector_sources, with freshness +
    follow + media policy, so the sidebar and control room share one source of truth."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, parent_id, entity_type, platform_id, name, enabled, "
            "       media_policy, allowed_users, blocked_users, last_collected_at, "
            "       last_error, error_count, "
            "       (platform_config->>'last_hwm') AS last_hwm, "
            "       (platform_config->>'discord_category') AS discord_category "
            "FROM collector_sources WHERE source_type = $1 ORDER BY priority NULLS LAST, name",
            source_type,
        )
    read_map = {
        "ignore": "none",
        "reference_only": "text",
        "download_keep": "text_images",
        "download_analyze_discard": "everything",
        "download_analyze_keep": "everything",
    }
    out = []
    for r in rows:
        d = dict(r)
        db_policy = d.get("media_policy") or "reference_only"
        d["media_policy"] = read_map.get(db_policy, "text_images")
        out.append(d)
    return out

async def get_feed(channel_name: Optional[str], source: str = "discord",
                   limit: int = 100, before_id: Optional[int] = None,
                   after_id: Optional[int] = None, following_only: bool = False,
                   since_days: int = 7) -> list[dict]:
    """Most-recent messages for a channel (or all channels of a source), newest-first,
    keyset-paginated by id for the slow auto-refresh + infinite scroll.
    Supports following_only to filter by public.trader_profiles.is_tracked=true."""
    pool = await get_shared_pool()
    clauses = ["n.source = $1"]
    args: list[Any] = [source]
    if channel_name:
        args.append(channel_name); clauses.append(f"n.channel_name = ${len(args)}")
    # Default to last 7 days for dashboard; override via ?since_days= parameter
    sd = since_days if since_days and since_days > 0 else 7
    clauses.append(f"COALESCE(n.published_at, n.collected_at) > now() - interval '{sd} days'")
    # Round 15: Keyset pagination MUST use the same sort key as the main query.
    # Since we sort by published_at DESC, before_id/after_id logic based on ID
    # is mathematically incorrect and causes "Ghost" old messages to appear as new.
    # We now filter by the time instead of the ID.
    if before_id:
        # Load messages older than the oldest one on screen
        clauses.append("COALESCE(n.published_at, n.collected_at) < (SELECT COALESCE(published_at, collected_at) FROM news_items WHERE id = $"+str(len(args)+1)+")")
        args.append(before_id)
    if after_id:
        # Load messages newer than the newest one on screen
        clauses.append("COALESCE(n.published_at, n.collected_at) > (SELECT COALESCE(published_at, collected_at) FROM news_items WHERE id = $"+str(len(args)+1)+")")
        args.append(after_id)
        
    if following_only:
        # Filter by normalized handles of tracked traders
        clauses.append("n.author IN (SELECT handle_normalized FROM trader_profiles WHERE is_tracked=true)")
    args.append(max(1, min(limit, 200)))
    where = " AND ".join(clauses)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT n.id, n.message_id, n.channel_name, n.author, n.author_id, n.author_role_color, "
            f"       n.content, n.headline, n.published_at, n.collected_at, n.has_media, n.media_count, "
            f"       n.local_media_paths, n.reply_to_msg_id, n.reply_to_author, n.reply_to_content, "
            f"       n.enrichment, n.enrichment_status, n.instruments, n.metadata, "
            f"       p.display_name AS author_display_name, "
            f"       p2.display_name AS reply_to_author_display_name, "
            f"       (SELECT si.id FROM public.signal_interpretations si WHERE si.news_item_id = n.id LIMIT 1) AS signal_interpretation_id "
            f"FROM news_items n "
            f"LEFT JOIN trader_profiles p ON p.handle_normalized = n.author "
            f"LEFT JOIN trader_profiles p2 ON p2.handle_normalized = n.reply_to_author "
            f"WHERE {where} ORDER BY COALESCE(n.published_at, n.collected_at) DESC, n.id DESC LIMIT ${len(args)}",
            *args,
        )
    return [dict(r) for r in rows]

async def get_tracked_traders() -> list[str]:
    """Return a flat list of normalized handles of followed traders."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT handle_normalized FROM trader_profiles WHERE is_tracked=true")
    return [r["handle_normalized"] for r in rows if r["handle_normalized"]]

async def get_user_config() -> list[dict]:
    """Return all traders from trader_profiles with is_tracked + tracked_media_types."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, handle_normalized AS handle, display_name, is_tracked, "
            "       COALESCE(tracked_media_types,'all') AS tracked_media_types "
            "FROM trader_profiles ORDER BY is_tracked DESC, handle_normalized"
        )
    return [dict(r) for r in rows]

async def set_user_config(trader_id: int, *, is_tracked: Optional[bool] = None,
                          tracked_media_types: Optional[str] = None) -> dict:
    """Update trader_profiles.is_tracked and/or tracked_media_types."""
    sets, args = [], []
    if is_tracked is not None:
        args.append(is_tracked); sets.append(f"is_tracked = ${len(args)}")
    if tracked_media_types is not None:
        args.append(tracked_media_types); sets.append(f"tracked_media_types = ${len(args)}")
    if not sets:
        return {"updated": False, "reason": "no fields"}
    args.append(trader_id)
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE trader_profiles SET {', '.join(sets)} WHERE id = ${len(args)}", *args
        )
    return {"updated": True, "trader_id": trader_id}

async def set_source_config(source_id: int, *, enabled: Optional[bool] = None,
                            media_policy: Optional[str] = None,
                            allowed_users: Optional[list] = None,
                            blocked_users: Optional[list] = None) -> dict:
    """Control-room write: update follow + media policy + user filters for one source."""
    pool = await get_shared_pool()
    sets, args = [], []
    if enabled is not None:      args.append(enabled);            sets.append(f"enabled = ${len(args)}")
    if media_policy is not None:
        write_map = {
            "none": "ignore",
            "text": "reference_only",
            "text_images": "download_keep",
            "everything": "download_analyze_keep",
        }
        db_policy = write_map.get(media_policy, "reference_only")
        args.append(db_policy); sets.append(f"media_policy = ${len(args)}")
    if allowed_users is not None: args.append(json.dumps(allowed_users)); sets.append(f"allowed_users = ${len(args)}::jsonb")
    if blocked_users is not None: args.append(json.dumps(blocked_users)); sets.append(f"blocked_users = ${len(args)}::jsonb")
    if not sets:
        return {"updated": False, "reason": "no fields"}
    args.append(source_id)
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE collector_sources SET {', '.join(sets)}, updated_at = now() WHERE id = ${len(args)}",
            *args,
        )
    return {"updated": True, "source_id": source_id}
