"""
Trade Deduplication — Single source of truth for duplicate detection.
Used by: signal_ai, alpha_analysis, trading_floor, trade_dossier (shadow trades).

A trade is a duplicate if within the last 8 hours, the same symbol + direction exists
with entry, SL, and TP all within 1% of each other.
"""
import logging
from typing import Optional

logger = logging.getLogger("jarvais.trade_dedup")


def is_duplicate_signal(db, symbol: str, direction: str,
                        entry: float, stop_loss: float, take_profit: float,
                        hours: int = 8, tolerance_pct: float = 0.01,
                        table: str = "parsed_signals") -> Optional[int]:
    """Check if a near-identical signal already exists.

    Returns the existing signal ID if duplicate found, None if unique.
    Checks parsed_signals by default. Can also check apex_shadow_trades.
    """
    if not symbol or not direction or not entry or entry <= 0:
        return None

    try:
        if table == "parsed_signals":
            rows = db.fetch_all("""
                SELECT id, entry_price, stop_loss, take_profit_1
                FROM parsed_signals
                WHERE symbol = %s AND direction = %s
                  AND parsed_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
                  AND entry_price IS NOT NULL AND entry_price > 0
                ORDER BY parsed_at DESC
                LIMIT 20
            """, (symbol, direction, hours))
        elif table == "apex_shadow_trades":
            rows = db.fetch_all("""
                SELECT id, entry_price, stop_loss, take_profit_1
                FROM apex_shadow_trades
                WHERE symbol = %s AND direction = %s
                  AND created_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
                  AND entry_price IS NOT NULL AND entry_price > 0
                ORDER BY created_at DESC
                LIMIT 20
            """, (symbol, direction, hours))
        elif table == "shadow_queue":
            rows = db.fetch_all("""
                SELECT id, entry_price, stop_loss, take_profit_1
                FROM shadow_queue
                WHERE symbol = %s AND direction = %s
                  AND queue_status IN ('queued', 'placed', 'filled')
                  AND entry_price IS NOT NULL AND entry_price > 0
                ORDER BY added_at DESC
                LIMIT 20
            """, (symbol, direction))
        else:
            return None

        if not rows:
            return None

        for r in rows:
            existing_entry = float(r.get("entry_price") or 0)
            existing_sl = float(r.get("stop_loss") or 0)
            existing_tp = float(r.get("take_profit_1") or 0)

            if existing_entry <= 0:
                continue

            entry_match = abs(entry - existing_entry) / existing_entry <= tolerance_pct
            sl_match = True
            tp_match = True

            if stop_loss and stop_loss > 0 and existing_sl and existing_sl > 0:
                sl_match = abs(stop_loss - existing_sl) / existing_sl <= tolerance_pct
            if take_profit and take_profit > 0 and existing_tp and existing_tp > 0:
                tp_match = abs(take_profit - existing_tp) / existing_tp <= tolerance_pct

            if entry_match and sl_match and tp_match:
                logger.info(
                    f"[Dedup] Duplicate {table}: {symbol} {direction} "
                    f"E:{entry:.6f}~{existing_entry:.6f} "
                    f"(existing #{r['id']})")
                return r["id"]

        return None

    except Exception as e:
        logger.debug(f"[Dedup] Check failed for {symbol}: {e}")
        return None


def is_duplicate_trade(db, symbol: str, direction: str,
                       entry: float, stop_loss: float, take_profit: float,
                       hours: int = 8, tolerance_pct: float = 0.01) -> bool:
    """Check ALL tables for duplicates. Returns True if found anywhere."""
    for table in ["parsed_signals", "apex_shadow_trades", "shadow_queue"]:
        dup_id = is_duplicate_signal(
            db, symbol, direction, entry, stop_loss, take_profit,
            hours, tolerance_pct, table)
        if dup_id is not None:
            return True
    return False
