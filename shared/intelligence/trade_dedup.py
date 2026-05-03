"""
Module: trade_dedup
Purpose: Detect duplicate/continuation trade signals across traders and time.
         If entry/SL/TP are within 2% of an existing open trade, it's a continuation.
Location: /opt/tickles/shared/intelligence/trade_dedup.py

Design (inspired by Jarvais V1 trade_dedup.py):
  * Check existing open tracked_positions within lookback window (default 8h).
  * Compare symbol + direction + entry/SL/TP within tolerance_pct (default 2%).
  * Cross-trader: same symbol+direction from different traders also checked.
  * Returns existing position_id if duplicate, None if unique.
  * Used by InterpretationService before creating new tracked_positions.

Usage:
    from shared.intelligence.trade_dedup import find_duplicate_position
    dup_id = await find_duplicate_position(pool, symbol="BTCUSDT", direction="long",
                                            entry=65000, stop_loss=64000, take_profit=70000)
    if dup_id:
        logger.info("Continuation of existing position %s", dup_id)
"""

import logging
from typing import Any, Dict, List, Optional

from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.intelligence.trade_dedup")


async def find_duplicate_position(
    pool: DatabasePool,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    hours: int = 8,
    tolerance_pct: float = 0.02,
) -> Optional[int]:
    """Check if a near-identical position already exists.

    A position is a duplicate if within the last `hours`, the same symbol +
    direction exists with entry, SL, and TP all within `tolerance_pct` of each
    other. This catches:
      * Same trader reposting their setup
      * Different traders posting the same setup
      * Screenshot updates that don't change levels materially

    Args:
        pool: Shared Postgres pool.
        symbol: Trading pair symbol (e.g. 'BTCUSDT').
        direction: 'long' or 'short'.
        entry: Entry price.
        stop_loss: Stop loss price (optional).
        take_profit: Take profit price (optional).
        hours: Lookback window in hours.
        tolerance_pct: Proximity threshold (0.02 = 2%).

    Returns:
        Existing position_id if duplicate found, None if unique.
    """
    if not symbol or not direction or not entry or entry <= 0:
        return None

    try:
        rows = await pool.fetch_all(
            """
            SELECT
                tp.id,
                tp.entry_price,
                tp.stop_loss,
                tp.take_profit_1
            FROM public.tracked_positions tp
            WHERE tp.instrument_symbol = $1
              AND tp.direction = $2
              AND tp.status IN ('open', 'pending', 'partial_hit')
              AND tp.created_at >= NOW() - INTERVAL '1 hour' * $3
              AND tp.entry_price IS NOT NULL
              AND tp.entry_price > 0
            ORDER BY tp.created_at DESC
            LIMIT 20
            """,
            (symbol, direction, hours),
        )
    except Exception as e:
        logger.warning("Dedup DB query failed for %s %s: %s", symbol, direction, e)
        return None

    if not rows:
        return None

    for row in rows:
        existing_entry = float(row.get("entry_price") or 0)
        if existing_entry <= 0:
            continue

        entry_match = abs(entry - existing_entry) / existing_entry <= tolerance_pct
        sl_match = True
        tp_match = True

        existing_sl = float(row.get("stop_loss") or 0)
        if stop_loss and stop_loss > 0 and existing_sl and existing_sl > 0:
            sl_match = abs(stop_loss - existing_sl) / existing_sl <= tolerance_pct

        existing_tp = float(row.get("take_profit_1") or 0)
        if take_profit and take_profit > 0 and existing_tp and existing_tp > 0:
            tp_match = abs(take_profit - existing_tp) / existing_tp <= tolerance_pct

        if entry_match and sl_match and tp_match:
            logger.info(
                "[Dedup] Duplicate position detected: %s %s "
                "E:%.4f~%.4f SL:%s~%s TP:%s~%s (existing #%d)",
                symbol,
                direction,
                entry,
                existing_entry,
                stop_loss,
                existing_sl,
                take_profit,
                existing_tp,
                row["id"],
            )
            return row["id"]

    return None


async def find_duplicate_signal(
    pool: DatabasePool,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    hours: int = 8,
    tolerance_pct: float = 0.02,
) -> Optional[int]:
    """Check signal_interpretations for duplicates (before position creation).

    This is a lighter check on signal_interpretations table, used when we
    haven't yet created a tracked_position.

    Args:
        pool: Company Postgres pool.
        symbol: Trading pair symbol.
        direction: 'long' or 'short'.
        entry: Entry price.
        stop_loss: Stop loss price.
        take_profit: Take profit price.
        hours: Lookback window.
        tolerance_pct: Proximity threshold.

    Returns:
        Existing signal_interpretation_id if duplicate, None if unique.
    """
    if not symbol or not direction or not entry or entry <= 0:
        return None

    try:
        rows = await pool.fetch_all(
            """
            SELECT
                si.id,
                (si.llm_levels->>'entry')::numeric AS entry_price,
                (si.llm_levels->>'stop_loss')::numeric AS stop_loss,
                (si.llm_levels->>'take_profit_1')::numeric AS take_profit_1
            FROM signal_interpretations si
            WHERE si.instrument_symbol = $1
              AND si.consensus_direction = $2
              AND si.created_at >= NOW() - INTERVAL '1 hour' * $3
              AND si.llm_levels IS NOT NULL
              AND (si.llm_levels->>'entry')::numeric > 0
            ORDER BY si.created_at DESC
            LIMIT 20
            """,
            (symbol, direction, hours),
        )
    except Exception as e:
        logger.warning("Dedup signal query failed for %s %s: %s", symbol, direction, e)
        return None

    if not rows:
        return None

    for row in rows:
        existing_entry = float(row.get("entry_price") or 0)
        if existing_entry <= 0:
            continue

        entry_match = abs(entry - existing_entry) / existing_entry <= tolerance_pct
        sl_match = True
        tp_match = True

        existing_sl = float(row.get("stop_loss") or 0)
        if stop_loss and stop_loss > 0 and existing_sl and existing_sl > 0:
            sl_match = abs(stop_loss - existing_sl) / existing_sl <= tolerance_pct

        existing_tp = float(row.get("take_profit_1") or 0)
        if take_profit and take_profit > 0 and existing_tp and existing_tp > 0:
            tp_match = abs(take_profit - existing_tp) / existing_tp <= tolerance_pct

        if entry_match and sl_match and tp_match:
            logger.info(
                "[Dedup] Duplicate signal detected: %s %s "
                "E:%.4f~%.4f (existing signal #%d)",
                symbol,
                direction,
                entry,
                existing_entry,
                row["id"],
            )
            return row["id"]

    return None


def is_continuation_text(text: str) -> bool:
    """Heuristic: detect if message text indicates a continuation/update.

    Looks for keywords like 'update', 'still in', 'at the trade', etc.

    Args:
        text: Message content text.

    Returns:
        True if text suggests this is a continuation, not a new signal.
    """
    if not text:
        return False

    text_lower = text.lower()
    continuation_keywords = [
        "update",
        "still in",
        "at the trade",
        "in the trade",
        "running",
        "moved",
        "adjusted",
        "updated",
        "same setup",
        "same trade",
        "continuation",
        "follow up",
        "follow-up",
        "reminder",
        "repost",
        "same entry",
        "same sl",
        "same tp",
    ]

    for kw in continuation_keywords:
        if kw in text_lower:
            return True

    # Emoji-only messages with trade-related emojis often indicate updates
    trade_emojis = ["🟢", "🔴", "🟩", "🟥", "📈", "📉", "🚀", "💎", "⚡", "🎯", "✅", "❌"]
    emoji_count = sum(1 for e in trade_emojis if e in text)
    if emoji_count >= 2 and len(text.strip()) < 50:
        return True

    return False
