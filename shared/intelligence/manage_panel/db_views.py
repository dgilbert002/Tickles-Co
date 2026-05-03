"""
Module: db_views
Purpose: Async DB helpers for the manage panel — sources, signals, positions, leaderboard.
Location: /opt/tickles/shared/intelligence/manage_panel/db_views.py
"""

import logging
from typing import Any, Dict, List, Optional

from shared.catalogue import db as catalogue_db
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sources / Channels / Users (re-use catalogue_db where possible)
# ---------------------------------------------------------------------------


async def list_sources(enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Return all collector_catalog rows."""
    return await catalogue_db.list_sources(enabled_only=enabled_only)


async def toggle_source(source_id: int, enabled: bool) -> int:
    """Enable or disable a source."""
    return await catalogue_db.toggle_source(source_id, enabled)


async def list_channels(catalog_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return watched_channels rows."""
    return await catalogue_db.list_channels(catalog_id=catalog_id)


async def toggle_channel(channel_id: int, enabled: bool) -> int:
    """Enable or disable a channel."""
    return await catalogue_db.toggle_channel(channel_id, enabled)


async def toggle_user(user_id: int, enabled: bool) -> int:
    """Enable or disable a user."""
    return await catalogue_db.toggle_user(user_id, enabled)


async def list_users(channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return watched_users rows."""
    return await catalogue_db.list_users(channel_id=channel_id)


async def get_hierarchy() -> List[Dict[str, Any]]:
    """Return nested source -> channel -> user tree."""
    return await catalogue_db.get_hierarchy()


async def add_source(
    source_type: str,
    source_name: str,
    source_slug: str,
    connection_config: Dict[str, Any],
    priority: int = 100,
    primary_asset_class: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Insert a new source into collector_catalog.

    Args:
        source_type: One of discord, telegram, rss, tradingview, twitter, api, webhook.
        source_name: Human-readable name.
        source_slug: Machine identifier (must be unique).
        connection_config: JSON connection parameters.
        priority: Lower = higher priority (default 100).
        primary_asset_class: Optional asset class.
        notes: Optional description.

    Returns:
        The newly inserted row ID.

    Raises:
        Exception: On database error (e.g. duplicate slug).
    """
    pool = await get_shared_pool()
    import json
    row = await pool.fetch_one(
        """
        INSERT INTO public.collector_catalog
            (source_type, source_name, source_slug, connection_config,
             is_enabled, priority, primary_asset_class, notes)
        VALUES ($1, $2, $3, $4, TRUE, $5, $6, $7)
        RETURNING id
        """,
        (
            source_type,
            source_name,
            source_slug,
            json.dumps(connection_config),
            priority,
            primary_asset_class,
            notes,
        ),
    )
    if row is None:
        raise RuntimeError("INSERT returned no row")
    return row["id"]


# ---------------------------------------------------------------------------
# Signals (signal_interpretations — per-company, but we query shared for demo)
# ---------------------------------------------------------------------------


async def list_recent_signals(hours: int = 24, limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent signal_interpretations rows from the default company DB.

    Args:
        hours: Lookback window in hours.
        limit: Max rows to return.

    Returns:
        List of signal dicts.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            si.id,
            si.created_at,
            si.symbol,
            si.instrument_symbol_normalised,
            si.llm_direction,
            si.llm_confidence,
            si.quant_direction,
            si.quant_confidence,
            si.consensus_direction,
            si.consensus_confidence,
            si.prefilter_result,
            si.vision_provider,
            si.vision_model_resolved,
            si.prompt_version,
            si.total_cost_usd,
            si.correlation_id,
            ni.platform,
            ni.trader_handle,
            ni.discord_url,
            mi.media_local_path
        FROM public.signal_interpretations si
        LEFT JOIN public.news_items ni ON si.news_item_id = ni.id
        LEFT JOIN public.media_items mi ON si.media_item_id = mi.id
        WHERE si.created_at >= NOW() - INTERVAL '1 hour' * $1
        ORDER BY si.created_at DESC
        LIMIT $2
        """,
        (hours, limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Positions (tracked_positions — shared ledger)
# ---------------------------------------------------------------------------


async def list_open_positions() -> List[Dict[str, Any]]:
    """Return currently open tracked_positions."""
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            tp.id,
            tp.created_at,
            tp.status,
            tp.direction,
            tp.instrument_symbol,
            tp.entry_price,
            tp.stop_loss,
            tp.take_profit,
            tp.risk_reward_ratio,
            tp.trader_profile_id,
            tpf.display_name AS trader_name,
            tp.realized_pnl_usd
        FROM public.tracked_positions tp
        LEFT JOIN public.trader_profiles tpf ON tp.trader_profile_id = tpf.id
        WHERE tp.status IN ('open', 'partial_close')
        ORDER BY tp.created_at DESC
        """
    )
    return [dict(r) for r in rows]


async def list_closed_positions(days: int = 7, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recently closed tracked_positions."""
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            tp.id,
            tp.created_at,
            tp.closed_at,
            tp.status,
            tp.direction,
            tp.instrument_symbol,
            tp.entry_price,
            tp.stop_loss,
            tp.take_profit,
            tp.risk_reward_ratio,
            tp.trader_profile_id,
            tpf.display_name AS trader_name,
            tp.realized_pnl_usd,
            tp.outcome
        FROM public.tracked_positions tp
        LEFT JOIN public.trader_profiles tpf ON tp.trader_profile_id = tpf.id
        WHERE tp.status = 'closed'
          AND tp.closed_at >= NOW() - INTERVAL '1 day' * $1
        ORDER BY tp.closed_at DESC
        LIMIT $2
        """,
        (days, limit),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


async def get_leaderboard(days: int = 30) -> List[Dict[str, Any]]:
    """Return trader leaderboard with win rate, R:R, P&L, directional bias."""
    return await catalogue_db.get_leaderboard(days=days)


# ---------------------------------------------------------------------------
# Trader drill-down
# ---------------------------------------------------------------------------


async def get_trader_signals(trader_profile_id: int, hours: int = 168) -> List[Dict[str, Any]]:
    """Return all signals for a specific trader in the lookback window.

    Args:
        trader_profile_id: The trader profile ID.
        hours: Lookback window in hours (default 7 days).

    Returns:
        List of signal dicts.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            si.id,
            si.created_at,
            si.symbol,
            si.llm_direction,
            si.llm_confidence,
            si.quant_direction,
            si.quant_confidence,
            si.consensus_direction,
            si.consensus_confidence,
            si.llm_levels_json,
            si.total_cost_usd,
            si.correlation_id,
            ni.discord_url,
            mi.media_local_path
        FROM public.signal_interpretations si
        LEFT JOIN public.news_items ni ON si.news_item_id = ni.id
        LEFT JOIN public.media_items mi ON si.media_item_id = mi.id
        WHERE ni.trader_profile_id = $1
          AND si.created_at >= NOW() - INTERVAL '1 hour' * $2
        ORDER BY si.created_at DESC
        """,
        (trader_profile_id, hours),
    )
    return [dict(r) for r in rows]


async def get_trader_positions(trader_profile_id: int, days: int = 30) -> List[Dict[str, Any]]:
    """Return all positions for a specific trader in the lookback window.

    Args:
        trader_profile_id: The trader profile ID.
        days: Lookback window in days.

    Returns:
        List of position dicts.
    """
    pool = await get_shared_pool()
    rows = await pool.fetch_all(
        """
        SELECT
            tp.id,
            tp.created_at,
            tp.closed_at,
            tp.status,
            tp.direction,
            tp.instrument_symbol,
            tp.entry_price,
            tp.stop_loss,
            tp.take_profit,
            tp.risk_reward_ratio,
            tp.realized_pnl_usd,
            tp.outcome
        FROM public.tracked_positions tp
        WHERE tp.trader_profile_id = $1
          AND tp.created_at >= NOW() - INTERVAL '1 day' * $2
        ORDER BY tp.created_at DESC
        """,
        (trader_profile_id, days),
    )
    return [dict(r) for r in rows]


async def get_trader_profile(trader_profile_id: int) -> Optional[Dict[str, Any]]:
    """Return a single trader profile by ID.

    Args:
        trader_profile_id: The trader profile ID.

    Returns:
        Trader profile dict or None.
    """
    pool = await get_shared_pool()
    row = await pool.fetch_one(
        """
        SELECT
            id,
            display_name,
            handle_normalized,
            platform,
            created_at,
            updated_at
        FROM public.trader_profiles
        WHERE id = $1
        """,
        (trader_profile_id,),
    )
    return dict(row) if row else None
