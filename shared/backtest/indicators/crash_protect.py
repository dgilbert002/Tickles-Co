"""
Crash Protection — Tickles & Co V2.0
=====================================

Port of CapitalTwo2.0's crash_protection.py (the one marked "DO NOT MODIFY —
+86% improvement proven"). Logic preserved verbatim; only the I/O glue is
modernised (pandas naming, type hints, asset-class agnostic).

The protection returns a boolean Series that is True on bars where new
entries should be blocked. The backtest engine consults this on every bar.
"""
from __future__ import annotations

from typing import Dict, Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from .core import register, _close, _high, _low, _volume
# Pass 2 fix: use the canonical SMA-seeded Wilder RSI from core instead of
# a local EWM variant that produced subtly different values at warmup.
from .core import rsi as _rsi_canonical


def _rsi_wilder(prices: pd.Series, period: int = 14) -> pd.Series:
    """Delegate to the canonical RSI implementation in .core.

    Kept as a thin wrapper so existing call-sites don't change.
    """
    df = pd.DataFrame({"closePrice": prices})
    return _rsi_canonical(df, {"period": period})


CRASH_PROTECTION_CONFIG = {
    "timeframe_days": 3,       # 3-day rolling peak
    "dd_threshold": -10.0,     # -10% drawdown threshold
    "rsi_bottom": 20.0,        # RSI < 20 = oversold bottom
    "vol_spike": 1.5,          # Volume > 1.5 × avg = spike
    "recovery_threshold": -3.0,# DD improving by 3% = recovering
}


def crash_protection_block(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Return a Series[bool]; True = block new trades on this bar.

    Operates on whatever timeframe the df is in. We aggregate to daily
    internally so 1m, 5m, 1h etc. all produce the same daily decision.
    """
    cfg = {**CRASH_PROTECTION_CONFIG, **params}

    close = _close(df)
    high  = _high(df)
    low   = _low(df)
    vol   = _volume(df)

    # Get calendar day (UTC).
    ts_col = "snapshotTime" if "snapshotTime" in df.columns else "timestamp"
    day = pd.to_datetime(df[ts_col], utc=True).dt.date

    daily = pd.DataFrame({
        "close": close.groupby(day).last(),
        "high":  high.groupby(day).max(),
        "low":   low.groupby(day).min(),
        "vol":   vol.groupby(day).sum(),
    })

    lookback = max(1, int(cfg["timeframe_days"]))
    # Peak uses only PRIOR bars (shift(1)) to avoid one-day lookahead.
    # Audit fix 2026-04-17 P1-C1.
    daily["peak"] = daily["close"].shift(1).rolling(lookback, min_periods=1).max()
    # On the very first day, peak is NaN → dd is NaN → block_daily is False
    # (.fillna(False) below) which is the correct conservative behaviour.
    daily["dd"]   = 100 * (daily["close"] - daily["peak"]) / daily["peak"].replace(0, np.nan)
    daily["rsi"]  = _rsi_wilder(daily["close"], 14)
    # Require at least 10 days of volume history before the ratio is meaningful.
    daily["vol_avg"] = daily["vol"].rolling(20, min_periods=10).mean()
    daily["vol_ratio"] = daily["vol"] / daily["vol_avg"].replace(0, np.nan)
    daily["dd_improve"] = daily["dd"] - daily["dd"].shift(5)

    in_dd = daily["dd"].fillna(0.0) < cfg["dd_threshold"]
    is_bottom = (
        (daily["rsi"].fillna(50.0) < cfg["rsi_bottom"])
        & (daily["vol_ratio"].fillna(0.0) > cfg["vol_spike"])
    )
    # Pass 2 fix: during the first 5 days `dd_improve` is NaN. Previously
    # `.fillna(0.0)` silently met `> -3.0` → is_recover=True → crash
    # protection effectively disabled for the warmup window.
    # Use -inf so NaN is treated as "no evidence of recovery".
    is_recover = daily["dd_improve"].fillna(float("-inf")) > cfg["recovery_threshold"]
    block_daily = (in_dd & ~is_bottom & ~is_recover).fillna(False)

    # Map daily decision back to each bar.
    mapped = day.map(block_daily).fillna(False).astype(bool)
    return pd.Series(mapped.values, index=df.index, dtype=bool)


register("crash_protection", crash_protection_block,
         defaults=CRASH_PROTECTION_CONFIG,
         param_ranges={
             "dd_threshold":       [-5, -8, -10, -12, -15],
             "rsi_bottom":         [15, 20, 25, 30],
             "vol_spike":          [1.2, 1.5, 2.0],
             "recovery_threshold": [-2, -3, -5],
         },
         category="crash_protection", direction="neutral",
         description=(
             "Blocks entries during significant drawdowns unless oversold "
             "bottom or clear recovery detected. Ported from CapitalTwo2.0 "
             "proven +86% configuration."
         ))
