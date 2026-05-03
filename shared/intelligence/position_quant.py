"""
Module: position_quant
Purpose: Pure quant functions for position tracking — P&L, distance, time metrics.
Location: /opt/tickles/shared/intelligence/position_quant.py
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def compute_pnl(
    side: str,
    entry_price: float,
    current_price: float,
    quantity: float,
    leverage: float = 1.0,
) -> float:
    """Return unrealized P&L in base currency terms.

    Args:
        side: "long" or "short".
        entry_price: Entry price.
        current_price: Current market price.
        quantity: Position size (units of base asset).
        leverage: Leverage multiplier (default 1.0 = spot).

    Returns:
        Unrealized P&L (positive = profit, negative = loss).
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be 'long' or 'short', got {side}")
    if entry_price <= 0 or current_price <= 0:
        raise ValueError("Prices must be positive")

    direction = 1.0 if side == "long" else -1.0
    price_change = current_price - entry_price
    raw_pnl = direction * price_change * quantity
    return raw_pnl * leverage


def compute_pnl_pct(
    side: str,
    entry_price: float,
    current_price: float,
) -> float:
    """Return P&L as a percentage of entry price.

    Args:
        side: "long" or "short".
        entry_price: Entry price.
        current_price: Current market price.

    Returns:
        P&L percentage (e.g., 0.05 = +5%).
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    direction = 1.0 if side == "long" else -1.0
    return direction * (current_price - entry_price) / entry_price


def compute_distance_to_sl_tp(
    side: str,
    current_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> Dict[str, Optional[float]]:
    """Return distance (price difference and percentage) to SL and TP.

    Args:
        side: "long" or "short".
        current_price: Current market price.
        stop_loss: Stop-loss price (None if not set).
        take_profit: Take-profit price (None if not set).

    Returns:
        Dict with keys: distance_to_sl, distance_to_sl_pct,
        distance_to_tp, distance_to_tp_pct.
    """
    result: Dict[str, Optional[float]] = {
        "distance_to_sl": None,
        "distance_to_sl_pct": None,
        "distance_to_tp": None,
        "distance_to_tp_pct": None,
    }
    if current_price <= 0:
        return result

    if stop_loss is not None and stop_loss > 0:
        result["distance_to_sl"] = current_price - stop_loss
        result["distance_to_sl_pct"] = (current_price - stop_loss) / current_price

    if take_profit is not None and take_profit > 0:
        result["distance_to_tp"] = take_profit - current_price
        result["distance_to_tp_pct"] = (take_profit - current_price) / current_price

    return result


def compute_time_metrics(
    entry_time: datetime,
    now: Optional[datetime] = None,
) -> Dict[str, float]:
    """Return time-in-trade metrics.

    Args:
        entry_time: When the position was opened.
        now: Reference time (default UTC now).

    Returns:
        Dict with keys: hours_open, minutes_open, seconds_open.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    delta = now - entry_time
    total_seconds = max(0.0, delta.total_seconds())
    return {
        "hours_open": total_seconds / 3600.0,
        "minutes_open": total_seconds / 60.0,
        "seconds_open": total_seconds,
    }


def check_sl_tp_hit(
    side: str,
    current_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> Tuple[bool, bool]:
    """Check if price has hit SL or TP.

    Args:
        side: "long" or "short".
        current_price: Current market price.
        stop_loss: Stop-loss price.
        take_profit: Take-profit price.

    Returns:
        Tuple of (sl_hit, tp_hit) booleans.
    """
    sl_hit = False
    tp_hit = False

    if side == "long":
        if stop_loss is not None and current_price <= stop_loss:
            sl_hit = True
        if take_profit is not None and current_price >= take_profit:
            tp_hit = True
    else:  # short
        if stop_loss is not None and current_price >= stop_loss:
            sl_hit = True
        if take_profit is not None and current_price <= take_profit:
            tp_hit = True

    return sl_hit, tp_hit


def compute_risk_reward_ratio(
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
) -> Optional[float]:
    """Compute risk:reward ratio from entry, SL, and TP.

    Args:
        entry_price: Entry price.
        stop_loss: Stop-loss price.
        take_profit: Take-profit price.

    Returns:
        R:R ratio (e.g., 2.0 means 1:2 risk:reward), or None if SL/TP missing.
    """
    if entry_price <= 0 or stop_loss is None or take_profit is None:
        return None
    risk = abs(entry_price - stop_loss)
    reward = abs(take_profit - entry_price)
    if risk == 0:
        return None
    return reward / risk


def compute_max_adverse_excursion(
    side: str,
    entry_price: float,
    worst_price_seen: float,
) -> float:
    """Compute maximum adverse excursion (MAE) as a percentage.

    Args:
        side: "long" or "short".
        entry_price: Entry price.
        worst_price_seen: Most unfavorable price since entry.

    Returns:
        MAE as a positive percentage (e.g., 0.03 = 3% against position).
    """
    if entry_price <= 0:
        return 0.0
    if side == "long":
        return max(0.0, (entry_price - worst_price_seen) / entry_price)
    else:
        return max(0.0, (worst_price_seen - entry_price) / entry_price)


def compute_max_favorable_excursion(
    side: str,
    entry_price: float,
    best_price_seen: float,
) -> float:
    """Compute maximum favorable excursion (MFE) as a percentage.

    Args:
        side: "long" or "short".
        entry_price: Entry price.
        best_price_seen: Most favorable price since entry.

    Returns:
        MFE as a positive percentage (e.g., 0.05 = 5% in favor).
    """
    if entry_price <= 0:
        return 0.0
    if side == "long":
        return max(0.0, (best_price_seen - entry_price) / entry_price)
    else:
        return max(0.0, (entry_price - best_price_seen) / entry_price)
