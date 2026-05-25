"""
Module: db
Purpose: Async Postgres client for the Collector Catalogue tables.
Location: /opt/tickles/shared/catalogue/db.py
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)


async def list_sources(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Return all collector_catalog rows ordered by source_type, source_name.

    Args:
        enabled_only: If True, filter to is_enabled = TRUE.

    Returns:
        List of row dicts.
    """
    pool = await get_shared_pool()
    where = "WHERE is_enabled = TRUE" if enabled_only else ""
    sql = f"""
        SELECT id, source_type, source_name, source_slug,
               connection_config, is_enabled, notes AS description,
               created_at, updated_at
        FROM public.collector_catalog
        {where}
        ORDER BY source_type, source_name
    """
    return await pool.fetch_all(sql)


async def get_source(source_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single source by ID."""
    pool = await get_shared_pool()
    return await pool.fetch_one(
        """
        SELECT id, source_type, source_name, source_slug,
               connection_config, is_enabled, notes AS description,
               created_at, updated_at
        FROM public.collector_catalog
        WHERE id = $1
        """,
        (source_id,),
    )


async def toggle_source(source_id: int, enabled: bool) -> int:
    """Enable or disable a source. Returns affected row count."""
    pool = await get_shared_pool()
    return await pool.execute(
        "UPDATE public.collector_catalog SET is_enabled = $1 WHERE id = $2",
        (enabled, source_id),
    )


async def list_channels(
    catalog_id: Optional[int] = None, enabled_only: bool = False
) -> List[Dict[str, Any]]:
    """Return watched_channels rows, optionally filtered by catalog_id."""
    pool = await get_shared_pool()
    conditions: List[str] = []
    params: List[Any] = []
    if catalog_id is not None:
        conditions.append(f"catalog_id = ${len(params) + 1}")
        params.append(catalog_id)
    if enabled_only:
        conditions.append("is_enabled = TRUE")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"""
        SELECT id, collector_id AS catalog_id, channel_slug, channel_name,
               platform_channel_id, is_enabled,
               collect_text, collect_images, collect_charts, collect_videos,
               instrument_patterns,
               max_messages_per_run, group_window_seconds AS poll_interval_seconds,
               created_at, updated_at
        FROM public.watched_channels
        {where}
        ORDER BY channel_name
    """
    return await pool.fetch_all(sql, tuple(params) if params else None)


async def toggle_channel(channel_id: int, enabled: bool) -> int:
    """Enable or disable a channel."""
    pool = await get_shared_pool()
    return await pool.execute(
        "UPDATE public.watched_channels SET is_enabled = $1 WHERE id = $2",
        (enabled, channel_id),
    )


async def update_channel_collection(
    channel_id: int,
    *,
    collect_text: Optional[bool] = None,
    collect_images: Optional[bool] = None,
    collect_charts: Optional[bool] = None,
    collect_videos: Optional[bool] = None,
) -> int:
    """Update what media types to collect for a channel."""
    pool = await get_shared_pool()
    fields: List[str] = []
    params: List[Any] = []
    for name, val in (
        ("collect_text", collect_text),
        ("collect_images", collect_images),
        ("collect_charts", collect_charts),
        ("collect_videos", collect_videos),
    ):
        if val is not None:
            fields.append(f"{name} = ${len(params) + 1}")
            params.append(val)
    if not fields:
        return 0
    params.append(channel_id)
    sql = f"""
        UPDATE public.watched_channels
        SET {', '.join(fields)}
        WHERE id = ${len(params)}
    """
    return await pool.execute(sql, tuple(params))


async def list_users(
    channel_id: Optional[int] = None,
    trader_profile_id: Optional[int] = None,
    enabled_only: bool = False,
) -> List[Dict[str, Any]]:
    """Return watched_users rows with optional filters."""
    pool = await get_shared_pool()
    conditions: List[str] = []
    params: List[Any] = []
    if channel_id is not None:
        conditions.append(f"channel_id = ${len(params) + 1}")
        params.append(channel_id)
    if trader_profile_id is not None:
        conditions.append(f"trader_profile_id = ${len(params) + 1}")
        params.append(trader_profile_id)
    if enabled_only:
        conditions.append("is_enabled = TRUE")
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    sql = f"""
        SELECT id, channel_id, trader_profile_id, platform_user_id,
               username_raw AS platform_handle, display_name, is_enabled,
               track_trades, track_charts, track_commentary,
               track_advice,
               created_at, updated_at
        FROM public.watched_users
        {where}
        ORDER BY display_name, username_raw
    """
    return await pool.fetch_all(sql, tuple(params) if params else None)


async def toggle_user(user_id: int, enabled: bool) -> int:
    """Enable or disable a watched user."""
    pool = await get_shared_pool()
    return await pool.execute(
        "UPDATE public.watched_users SET is_enabled = $1 WHERE id = $2",
        (enabled, user_id),
    )


async def update_user_tracking(
    user_id: int,
    *,
    track_trades: Optional[bool] = None,
    track_charts: Optional[bool] = None,
    track_commentary: Optional[bool] = None,
    track_advice: Optional[bool] = None,
    track_media: Optional[bool] = None,
    auto_detect_entries: Optional[bool] = None,
    auto_detect_sl_tp: Optional[bool] = None,
    min_confidence_threshold: Optional[float] = None,
) -> int:
    """Update what to track for a specific user."""
    pool = await get_shared_pool()
    fields: List[str] = []
    params: List[Any] = []
    for name, val in (
        ("track_trades", track_trades),
        ("track_charts", track_charts),
        ("track_commentary", track_commentary),
        ("track_advice", track_advice),
        ("track_media", track_media),
        ("auto_detect_entries", auto_detect_entries),
        ("auto_detect_sl_tp", auto_detect_sl_tp),
        ("min_confidence_threshold", min_confidence_threshold),
    ):
        if val is not None:
            fields.append(f"{name} = ${len(params) + 1}")
            params.append(val)
    if not fields:
        return 0
    params.append(user_id)
    sql = f"""
        UPDATE public.watched_users
        SET {', '.join(fields)}
        WHERE id = ${len(params)}
    """
    return await pool.execute(sql, tuple(params))


async def get_hierarchy() -> List[Dict[str, Any]]:
    """Return nested source -> channel -> user structure as raw dicts.

    Returns:
        List of source dicts, each with 'channels' list, each with 'users' list.
    """
    pool = await get_shared_pool()
    sources = await pool.fetch_all(
        """
        SELECT id, source_type, source_name, source_slug,
               is_enabled, notes AS description
        FROM public.collector_catalog
        ORDER BY source_type, source_name
        """
    )
    channels = await pool.fetch_all(
        """
        SELECT id, collector_id AS catalog_id, channel_slug, channel_name,
               platform_channel_id, is_enabled
        FROM public.watched_channels
        ORDER BY channel_name
        """
    )
    users = await pool.fetch_all(
        """
        SELECT id, channel_id, platform_handle, display_name,
               is_enabled, track_trades, track_charts, track_commentary,
               track_advice, track_media
        FROM public.watched_users
        ORDER BY display_name, platform_handle
        """
    )

    # Build nested structure
    chan_by_src: Dict[int, List[Dict[str, Any]]] = {}
    for c in channels:
        chan_by_src.setdefault(c["catalog_id"], []).append(c)

    user_by_chan: Dict[int, List[Dict[str, Any]]] = {}
    for u in users:
        user_by_chan.setdefault(u["channel_id"], []).append(u)

    result: List[Dict[str, Any]] = []
    for s in sources:
        s_copy = dict(s)
        s_channels = []
        for c in chan_by_src.get(s["id"], []):
            c_copy = dict(c)
            c_copy["users"] = user_by_chan.get(c["id"], [])
            s_channels.append(c_copy)
        s_copy["channels"] = s_channels
        result.append(s_copy)
    return result


async def get_leaderboard(days: int = 30) -> List[Dict[str, Any]]:
    """Return trader leaderboard with win rate, R:R, P&L, directional bias.

    Args:
        days: Lookback period in days.

    Returns:
        List of trader stat dicts ordered by total_pnl DESC.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            tpf.id AS trader_id,
            tpf.display_name,
            tpf.handle_normalized,
            COUNT(tp.id) AS total_trades,
            SUM(CASE WHEN tp.outcome = 'take_profit' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN tp.outcome = 'stop_loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN tp.outcome = 'breakeven' THEN 1 ELSE 0 END) AS breakevens,
            SUM(tp.realized_pnl_usd) AS total_pnl,
            AVG(tp.risk_reward_ratio) AS avg_rr,
            MODE() WITHIN GROUP (ORDER BY tp.instrument_symbol) AS most_traded_symbol,
            COUNT(DISTINCT CASE WHEN tp.direction = 'long' THEN tp.id END) AS long_count,
            COUNT(DISTINCT CASE WHEN tp.direction = 'short' THEN tp.id END) AS short_count
        FROM public.trader_profiles tpf
        LEFT JOIN public.tracked_positions tp
            ON tp.trader_profile_id = tpf.id
            AND tp.created_at >= NOW() - INTERVAL '1 day' * $1
        GROUP BY tpf.id, tpf.display_name, tpf.handle_normalized
        HAVING COUNT(tp.id) > 0
        ORDER BY total_pnl DESC NULLS LAST
        """,
        (days,),
    )
    return [dict(r) for r in rows]


async def get_stats() -> Dict[str, int]:
    """Return counts for sources, channels, users, and active positions."""
    pool = await get_shared_pool()
    src = await pool.fetch_val(
        "SELECT COUNT(*) FROM public.collector_catalog WHERE is_enabled = TRUE"
    )
    ch = await pool.fetch_val(
        "SELECT COUNT(*) FROM public.watched_channels WHERE is_enabled = TRUE"
    )
    usr = await pool.fetch_val(
        "SELECT COUNT(*) FROM public.watched_users WHERE is_enabled = TRUE"
    )
    pos = await pool.fetch_val(
        "SELECT COUNT(*) FROM public.tracked_positions WHERE status IN ('open','partial_exit')"
    )
    return {
        "active_sources": int(src or 0),
        "active_channels": int(ch or 0),
        "active_users": int(usr or 0),
        "open_positions": int(pos or 0),
    }
