"""
Smart-Money / Market-Structure Indicators — Tickles & Co V2.0
==============================================================

These are simpler, deterministic versions of the "smart money" family that
most pro-prop traders swear by. They are not a substitute for a full SMC
engine, but they give strategies enough signal to test the thesis.

Each function returns a Series aligned with the input DataFrame.

Inspired by CapitalTwo2.0's `indicators_comprehensive` but rewritten to
stay dependency-free (no scipy, no ta-lib) — pure pandas + numpy.
"""
from __future__ import annotations

from typing import Dict, Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from .core import register, _close, _high, _low, _volume


def swing_high(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Returns 1 where a swing high is detected, else 0.

    A swing high is a bar whose high is STRICTLY greater than the N bars
    before AND the N bars after it. We emit the signal with a `.shift(lookback)`
    so the bar at which the signal appears is the first bar at which the
    swing could realistically be confirmed (no look-ahead).
    """
    lookback = max(1, int(params.get("lookback", 3)))
    h = _high(df)
    hn = h.to_numpy()
    out = np.zeros(len(h), dtype=np.int8)
    for i in range(lookback, len(h) - lookback):
        cur = hn[i]
        if not np.isfinite(cur):
            continue
        left_max  = hn[i - lookback:i].max() if lookback else -np.inf
        right_max = hn[i + 1:i + 1 + lookback].max() if lookback else -np.inf
        if cur > left_max and cur > right_max:
            out[i] = 1
    s = pd.Series(out, index=h.index)
    return s.shift(lookback).fillna(0).astype(int)


def swing_low(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    lookback = max(1, int(params.get("lookback", 3)))
    lo = _low(df)
    ln = lo.to_numpy()
    out = np.zeros(len(lo), dtype=np.int8)
    for i in range(lookback, len(lo) - lookback):
        cur = ln[i]
        if not np.isfinite(cur):
            continue
        left_min  = ln[i - lookback:i].min() if lookback else np.inf
        right_min = ln[i + 1:i + 1 + lookback].min() if lookback else np.inf
        if cur < left_min and cur < right_min:
            out[i] = 1
    s = pd.Series(out, index=lo.index)
    return s.shift(lookback).fillna(0).astype(int)


def bos_bullish(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Break-of-Structure bullish.

    Fires 1 on the bar that closes above the most recent swing high.
    """
    lookback = int(params.get("lookback", 5))
    c = _close(df)
    recent_high = _high(df).rolling(lookback).max().shift(1)
    return (c > recent_high).astype(int)


def bos_bearish(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    lookback = int(params.get("lookback", 5))
    c = _close(df)
    recent_low = _low(df).rolling(lookback).min().shift(1)
    return (c < recent_low).astype(int)


def fair_value_gap_up(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Fair-value gap (bullish imbalance).

    Detected when bar[t].low > bar[t-2].high → there's an untouched gap
    between the wicks two bars ago and now. Returns the gap size in price
    units (useful for position-sizing); 0 otherwise.
    """
    h = _high(df)
    lo = _low(df)
    gap = lo - h.shift(2)
    return gap.where(gap > 0, 0.0)


def fair_value_gap_down(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    h = _high(df)
    lo = _low(df)
    gap = lo.shift(2) - h
    return gap.where(gap > 0, 0.0)


def liquidity_sweep_high(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Sweep of recent high followed by close back inside.

    Fires 1 on a bar whose high exceeded the prior N-bar high but closed
    below it — a classic stop-hunt / liquidity grab.
    """
    lookback = int(params.get("lookback", 20))
    h = _high(df)
    c = _close(df)
    prior_high = h.rolling(lookback).max().shift(1)
    swept = (h > prior_high) & (c < prior_high)
    return swept.astype(int)


def liquidity_sweep_low(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    lookback = int(params.get("lookback", 20))
    lo = _low(df)
    c = _close(df)
    prior_low = lo.rolling(lookback).min().shift(1)
    swept = (lo < prior_low) & (c > prior_low)
    return swept.astype(int)


def volume_spike(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Binary: 1 if current bar volume > mult × rolling(period) mean.

    The rolling baseline EXCLUDES the current bar (`.shift(1)`) so a genuine
    10x spike doesn't inflate its own reference. (Audit fix 2026-04-17 P0.)
    """
    period = int(params.get("period", 20))
    mult = float(params.get("mult", 2.0))
    v = _volume(df)
    avg = v.rolling(period, min_periods=period).mean().shift(1)
    return (v > mult * avg).fillna(False).astype(int)


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
register("swing_high", swing_high,
         defaults={"lookback": 3},
         param_ranges={"lookback": [2, 3, 5, 7, 10]},
         category="smart_money", direction="neutral",
         description="Fractal swing high (N-bar peak).")
register("swing_low", swing_low,
         defaults={"lookback": 3},
         param_ranges={"lookback": [2, 3, 5, 7, 10]},
         category="smart_money", direction="neutral",
         description="Fractal swing low (N-bar trough).")
register("bos_bullish", bos_bullish,
         defaults={"lookback": 5},
         param_ranges={"lookback": [3, 5, 8, 13, 21]},
         category="smart_money", direction="bullish",
         description="Break-of-Structure: close above prior N-bar high.")
register("bos_bearish", bos_bearish,
         defaults={"lookback": 5},
         param_ranges={"lookback": [3, 5, 8, 13, 21]},
         category="smart_money", direction="bearish",
         description="Break-of-Structure: close below prior N-bar low.")
register("fvg_up", fair_value_gap_up,
         defaults={}, param_ranges={},
         category="smart_money", direction="bullish",
         description="Bullish fair-value gap size (0 if none).")
register("fvg_down", fair_value_gap_down,
         defaults={}, param_ranges={},
         category="smart_money", direction="bearish",
         description="Bearish fair-value gap size (0 if none).")
register("liq_sweep_high", liquidity_sweep_high,
         defaults={"lookback": 20},
         param_ranges={"lookback": [10, 20, 50, 100]},
         category="smart_money", direction="bearish",
         description="Liquidity sweep above prior high with bearish close.")
register("liq_sweep_low", liquidity_sweep_low,
         defaults={"lookback": 20},
         param_ranges={"lookback": [10, 20, 50, 100]},
         category="smart_money", direction="bullish",
         description="Liquidity sweep below prior low with bullish close.")
register("volume_spike", volume_spike,
         defaults={"period": 20, "mult": 2.0},
         param_ranges={"period": [10, 20, 50], "mult": [1.5, 2.0, 3.0]},
         category="volume", direction="neutral",
         description="Binary volume spike detector.")
