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
# Bug H5 fix (Bug Hunter 1 §H1): canonicalise symbol form before any dedup
# query. Without this, ``BTCUSDT`` (raw text-extractor output) and
# ``BTC/USDT`` (canonical form stored on `tracked_positions`) compare unequal
# even though they refer to the same instrument, so duplicate positions slip
# through.
try:
    from shared.utils.instrument_normaliser import to_canonical_symbol  # type: ignore
except Exception:  # pragma: no cover — defensive fallback if module shape changes
    def to_canonical_symbol(s: str) -> str:  # type: ignore[misc]
        return s


def _canonicalise_symbol(symbol: Optional[str]) -> Optional[str]:
    """Normalise a free-text symbol into the canonical slash form (D6).

    Returns the original string unchanged on any failure so we never lose a
    legitimate dedup query because of a bad input format.
    """
    if not symbol:
        return symbol
    try:
        canon = to_canonical_symbol(symbol)
        return canon or symbol
    except Exception:
        return symbol


def _compact_symbol(symbol: Optional[str]) -> Optional[str]:
    """Round-3 review fix (BH1 #9, BH2 #4): the canonical form is
    ``BTC/USDT`` but legacy rows in ``tracked_positions`` /
    ``signal_interpretations`` were written with the compact ``BTCUSDT`` form
    AND have ``instrument_symbol_normalised IS NULL``. The Bug C OR-match
    against canonical alone misses those rows. We bind a second parameter
    in compact form so the SQL can match either side of the migration.

    Round-6 sweep (BH1 #10, BH2 #5): also strip ``_`` (legacy
    ``BTC_USDT``) and ``:`` (CCXT settle suffix ``BTC/USDT:USDT``) and
    upper-case the result. The returned compact form is exactly the set of
    alphanumerics in the original symbol, so any legacy storage scheme that
    used a separator + same letters still matches.
    """
    if not symbol:
        return symbol
    # Strip every common separator we have ever observed in legacy storage.
    # Order does not matter — each replace is independent.
    cleaned = (
        symbol.replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(":", "")
        .strip()
    )
    return cleaned.upper() or symbol


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
    # Bug H5: normalise symbol BEFORE the SQL query so 'BTCUSDT' matches
    # rows stored as 'BTC/USDT'.
    symbol = _canonicalise_symbol(symbol) or symbol
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
        # Bug C fix (2026-05-24 second-round audit), refined in round-3 review
        # (BH1 #9, BH2 #4): bind BOTH the canonical form ($1, e.g. "BTC/USDT")
        # AND the compact form ($2, e.g. "BTCUSDT") so the dedup query catches
        # all four storage states:
        #   - raw=canonical, normalised=canonical    → matches on $1
        #   - raw=compact,   normalised=canonical    → matches on $1 (norm)
        #   - raw=compact,   normalised=NULL         → matches on $2 (raw)
        #   - raw=canonical, normalised=NULL         → matches on $1 (raw)
        # This closes the gap that the previous OR-match missed: legacy rows
        # with raw="BTCUSDT" + normalised=NULL no longer slip through dedup
        # when the caller passes "BTCUSDT" (canonicalised by H5 to "BTC/USDT").
        compact = _compact_symbol(symbol) or symbol
        rows = await pool.fetch_all(
            """
            SELECT
                tp.id,
                tp.entry_price,
                tp.stop_loss,
                tp.take_profit_1
            FROM public.tracked_positions tp
            WHERE (tp.instrument_symbol = $1
                   OR tp.instrument_symbol = $2
                   OR tp.instrument_symbol_normalised = $1
                   OR UPPER(tp.instrument_symbol) = UPPER($2)
                   OR UPPER(tp.instrument_symbol_normalised) = UPPER($1))
              AND tp.direction = $3
              AND tp.status IN ('open', 'pending', 'partial_exit')
              AND tp.created_at >= NOW() - INTERVAL '1 hour' * $4
              AND tp.entry_price IS NOT NULL
              AND tp.entry_price > 0
            ORDER BY tp.created_at DESC
            LIMIT 20
            """,
            (symbol, compact, direction, hours),
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
    # Bug H5: normalise symbol BEFORE the SQL query so 'BTCUSDT' matches
    # rows stored as 'BTC/USDT'.
    symbol = _canonicalise_symbol(symbol) or symbol
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
        # Bug C fix (2026-05-24), refined in round-3 review (BH1 #9, BH2 #4):
        # bind canonical AND compact form (see find_duplicate_position above
        # for the full rationale).
        compact = _compact_symbol(symbol) or symbol
        rows = await pool.fetch_all(
            """
            SELECT
                si.id,
                (si.llm_levels->>'entry')::numeric AS entry_price,
                (si.llm_levels->>'stop_loss')::numeric AS stop_loss,
                COALESCE(
                    (si.llm_levels->>'take_profit_1')::numeric,
                    (si.llm_levels->>'take_profit')::numeric
                ) AS take_profit_1
            FROM signal_interpretations si
            WHERE (si.instrument_symbol = $1
                   OR si.instrument_symbol = $2
                   OR si.instrument_symbol_normalised = $1
                   OR UPPER(si.instrument_symbol) = UPPER($2)
                   OR UPPER(si.instrument_symbol_normalised) = UPPER($1))
              AND si.consensus_direction = $3
              AND si.created_at >= NOW() - INTERVAL '1 hour' * $4
              AND si.llm_levels IS NOT NULL
              AND (si.llm_levels->>'entry')::numeric > 0
            ORDER BY si.created_at DESC
            LIMIT 20
            """,
            (symbol, compact, direction, hours),
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
