"""Validate and apply Chart Hacker critic SL/TP suggestions to tracked positions."""
from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


def parse_level(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def is_valid_critic_sl(direction: str, entry: float, sl: float) -> bool:
    """Accept protective stops and breakeven / trailing locks."""
    if entry <= 0 or sl <= 0:
        return False
    d = str(direction or "").lower()
    if d == "long":
        # Below entry (protective) or at/near breakeven.
        return sl <= entry * 1.002
    if d == "short":
        # Above entry (protective) or below entry (trailing profit lock).
        return True
    return False


def is_valid_critic_tp(direction: str, entry: float, tp: float) -> bool:
    if entry <= 0 or tp <= 0:
        return False
    d = str(direction or "").lower()
    if d == "long":
        return tp > entry
    if d == "short":
        return tp < entry
    return False


def pick_critic_levels(
    *,
    direction: str,
    entry: float,
    missing_sl: bool,
    missing_tp: bool,
    critic_sl: Any,
    critic_tp: Any,
) -> Tuple[Optional[float], Optional[float]]:
    """Return (sl, tp) to backfill — only fields that were missing and validate."""
    out_sl: Optional[float] = None
    out_tp: Optional[float] = None
    if entry <= 0:
        return out_sl, out_tp

    if missing_sl:
        sl = parse_level(critic_sl)
        if sl is not None and is_valid_critic_sl(direction, entry, sl):
            out_sl = sl
        elif sl is not None:
            logger.info(
                "critic_levels: reject SL %.6g for %s entry %.6g",
                sl, direction, entry,
            )

    if missing_tp:
        tp = parse_level(critic_tp)
        if tp is not None and is_valid_critic_tp(direction, entry, tp):
            out_tp = tp
        elif tp is not None:
            logger.info(
                "critic_levels: reject TP %.6g for %s entry %.6g",
                tp, direction, entry,
            )

    return out_sl, out_tp
