"""
Single-Indicator Strategies — Tickles & Co V2.0
================================================

A strategy is just a callable (df, params) -> Series[int in {-1,0,+1}].

This module defines a handful of canonical single-indicator strategies
that the backtest engine can run. Anything more complex (combinations,
conflict resolution) belongs in `strategies/combo.py` later.

Every strategy here is deterministic and does NOT peek at future bars.
"""
from __future__ import annotations

from typing import Any, Callable, Dict

import numpy as np
import pandas as pd

from backtest.indicators import get as get_indicator
from backtest.indicators.core import _close


def _state_from_ma(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """Return a state series in {-1,0,+1} aligned with input, NaN while warming."""
    s = pd.Series(np.nan, index=fast.index, dtype=float)
    both_valid = fast.notna() & slow.notna()
    s = s.mask(both_valid & (fast > slow), 1.0)
    s = s.mask(both_valid & (fast < slow), -1.0)
    s = s.mask(both_valid & (fast == slow), 0.0)
    return s


def sma_cross(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Long when fast SMA > slow SMA, short when fast < slow, else 0.

    Emits +1/-1 ONLY on the bar where the cross happens (state transition).
    Warmup bars (NaN state) never emit a signal (audit P1-C6).
    """
    fast = int(params.get("fast", 10))
    slow = int(params.get("slow", 30))
    sma_fn = get_indicator("sma").fn
    sma_fast = sma_fn(df, {"period": fast})
    sma_slow = sma_fn(df, {"period": slow})
    state = _state_from_ma(sma_fast, sma_slow)
    prev = state.shift(1)
    # only fire on genuine transition between two valid states
    cross = state.notna() & prev.notna() & (state != prev)
    out = pd.Series(0.0, index=df.index)
    out[cross] = state[cross].astype(float)
    return out


def ema_cross(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    fast = int(params.get("fast", 9))
    slow = int(params.get("slow", 21))
    ema_fn = get_indicator("ema").fn
    e_fast = ema_fn(df, {"period": fast})
    e_slow = ema_fn(df, {"period": slow})
    state = _state_from_ma(e_fast, e_slow)
    prev = state.shift(1)
    cross = state.notna() & prev.notna() & (state != prev)
    out = pd.Series(0.0, index=df.index)
    out[cross] = state[cross].astype(float)
    return out


def rsi_reversal(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Long when RSI crosses up from < oversold; short when down from > overbought."""
    period = int(params.get("period", 14))
    over   = float(params.get("overbought", 70))
    under  = float(params.get("oversold", 30))
    rsi_fn = get_indicator("rsi").fn
    r = rsi_fn(df, {"period": period})
    long_sig  = (r.shift(1) <= under) & (r > under)
    short_sig = (r.shift(1) >= over)  & (r < over)
    sig = pd.Series(0.0, index=df.index)
    sig[long_sig]  = 1.0
    sig[short_sig] = -1.0
    return sig


def bollinger_pullback(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Long when close touches lower band then turns up; symmetric short."""
    period = int(params.get("period", 20))
    stddev = float(params.get("stddev", 2.0))
    pb_fn = get_indicator("bbands_pb").fn
    pb = pb_fn(df, {"period": period, "stddev": stddev})
    long_sig  = (pb.shift(1) < 0.1) & (pb >= 0.1)
    short_sig = (pb.shift(1) > 0.9) & (pb <= 0.9)
    s = pd.Series(0.0, index=df.index)
    s[long_sig] = 1.0
    s[short_sig] = -1.0
    return s


def anchored_vwap_pullback(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Long when price pulls back to session VWAP with bullish reclaim."""
    vwap_fn = get_indicator("vwap_session").fn
    vw = vwap_fn(df, {})
    # Pass 2 fix: use the _close() helper so the strategy works on DFs
    # that use "close" instead of "closePrice" (e.g., unit-test fixtures).
    c = _close(df)
    touched_below = (c.shift(1) < vw.shift(1)) & (c >= vw)  # reclaimed
    touched_above = (c.shift(1) > vw.shift(1)) & (c <= vw)  # rejection
    s = pd.Series(0.0, index=df.index)
    s[touched_below] = 1.0
    s[touched_above] = -1.0
    return s


STRATEGIES: Dict[str, Callable] = {
    "sma_cross":            sma_cross,
    "ema_cross":            ema_cross,
    "rsi_reversal":         rsi_reversal,
    "bollinger_pullback":   bollinger_pullback,
    "anchored_vwap_pullback": anchored_vwap_pullback,
}


def get(name: str) -> Callable:
    try:
        return STRATEGIES[name]
    except KeyError:
        raise KeyError(
            f"unknown strategy {name!r}. Known strategies: {sorted(STRATEGIES)}"
        ) from None


def list_all():
    return sorted(STRATEGIES.keys())
