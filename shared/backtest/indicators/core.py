"""
Core Technical Indicators — Tickles & Co V2.0
===============================================

Deterministic, pandas-based, vectorised implementations of the indicators
that almost every strategy needs. Each function takes a DataFrame of candles
(the schema used by the backtest engine) plus a ``params`` dict, and returns
a Series aligned to the input index with the indicator value at each bar.

Design rules (Rule #1 — backtests must equal live):
  * Pure functions, no global state.
  * No look-ahead — we use only data up to and including the current bar.
  * Pandas rolling / EMA with the same formulas everywhere (Wilder for RSI).
  * Return Series indexed identically to the input DataFrame.

A few of these are intentionally simple. The strategy layer composes them
(e.g. "EMA cross" = two EMA calls + a cross detector). Keep this file the
single source of truth for math.

Every indicator registers itself in the module-level INDICATORS dict at the
bottom via ``register(name, fn, defaults, category)``. The registry module
walks this dict and mirrors it into the Postgres ``indicator_catalog`` table
on boot so agents can discover what exists via the Data Catalog.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Any

import logging

import numpy as np
import pandas as pd  # type: ignore[import-untyped]


log = logging.getLogger("tickles.indicators")


# ---------------------------------------------------------------------------
# Indicator registry (in-process). Catalog writes to Postgres.
# ---------------------------------------------------------------------------
@dataclass
class IndicatorSpec:
    name: str
    fn: Callable[[pd.DataFrame, Dict[str, Any]], pd.Series]
    defaults: Dict[str, Any]
    param_ranges: Dict[str, Any]   # for optimisation sweeps
    category: str                   # trend / momentum / volatility / volume / smart_money / crash
    direction: str                  # bullish / bearish / neutral
    description: str
    asset_class: str                # 'any' | 'crypto' | 'cfd' | 'equity'


INDICATORS: Dict[str, IndicatorSpec] = {}


def register(
    name: str,
    fn: Callable[[pd.DataFrame, Dict[str, Any]], pd.Series],
    defaults: Dict[str, Any],
    param_ranges: Dict[str, Any],
    category: str,
    direction: str = "neutral",
    description: str = "",
    asset_class: str = "any",
) -> None:
    if name in INDICATORS:
        # Duplicate registration is almost always a bug — warn loudly.
        log.warning("indicator register: duplicate name %r, overwriting", name)
    INDICATORS[name] = IndicatorSpec(
        name, fn, defaults, param_ranges,
        category, direction, description, asset_class,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _close(df: pd.DataFrame) -> pd.Series:
    """Return the close series, handling both engine schema and raw OHLC."""
    if "closePrice" in df.columns:
        return df["closePrice"].astype(float)
    return df["close"].astype(float)


def _high(df: pd.DataFrame) -> pd.Series:
    return (df["highPrice"] if "highPrice" in df.columns else df["high"]).astype(float)


def _low(df: pd.DataFrame) -> pd.Series:
    return (df["lowPrice"] if "lowPrice" in df.columns else df["low"]).astype(float)


def _volume(df: pd.DataFrame) -> pd.Series:
    if "lastTradedVolume" in df.columns:
        return df["lastTradedVolume"].astype(float)
    return df["volume"].astype(float)


# ---------------------------------------------------------------------------
# Trend: SMA, EMA, WMA
# ---------------------------------------------------------------------------
def sma(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 20))
    return _close(df).rolling(window=period, min_periods=period).mean()


def ema(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 20))
    return _close(df).ewm(span=period, adjust=False, min_periods=period).mean()


def wma(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 20))
    weights = np.arange(1, period + 1)
    return _close(df).rolling(period).apply(
        lambda s: np.dot(s, weights) / weights.sum(), raw=True
    )


# ---------------------------------------------------------------------------
# Momentum: RSI (Wilder), Stoch, MACD, ROC, Momentum
# ---------------------------------------------------------------------------
def rsi(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Wilder's RSI seeded with SMA of the first `period` values (matches
    TradingView for series ≥ ~5× period; the first `period` bars are NaN).

    Formula: after initial SMA seed, smoothing uses Wilder's RMA
    (alpha = 1/period).
    """
    period = int(params.get("period", 14))
    c = _close(df)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Seed with SMA over the first `period` values (Wilder's canonical approach).
    # Then apply Wilder RMA: current = prev*(p-1)/p + new/p.
    n = len(c)
    if n == 0:
        return pd.Series([], index=c.index, dtype=float)
    out = np.full(n, np.nan)
    avg_gain = np.nan
    avg_loss = np.nan
    g = gain.fillna(0).to_numpy()
    lo = loss.fillna(0).to_numpy()
    for i in range(n):
        if i < period:
            continue
        if np.isnan(avg_gain):
            avg_gain = g[1:period + 1].mean() if i == period else g[i - period + 1:i + 1].mean()
            avg_loss = lo[1:period + 1].mean() if i == period else lo[i - period + 1:i + 1].mean()
        else:
            avg_gain = (avg_gain * (period - 1) + g[i]) / period
            avg_loss = (avg_loss * (period - 1) + lo[i]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return pd.Series(out, index=c.index, dtype=float)


def stochastic_k(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 14))
    h = _high(df).rolling(period).max()
    lo = _low(df).rolling(period).min()
    c = _close(df)
    return 100 * (c - lo) / (h - lo).replace(0, np.nan)


def macd_line(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    c = _close(df)
    ema_fast = c.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = c.ewm(span=slow, adjust=False, min_periods=slow).mean()
    return ema_fast - ema_slow


def macd_hist(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    signal = int(params.get("signal", 9))
    line = macd_line(df, {"fast": fast, "slow": slow})
    # Require `signal` non-NaN line values before emitting histogram.
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line - sig


def roc(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 10))
    c = _close(df)
    return 100 * (c - c.shift(period)) / c.shift(period).replace(0, np.nan)


# ---------------------------------------------------------------------------
# Volatility: ATR (Wilder), Bollinger %B, Keltner width
# ---------------------------------------------------------------------------
def atr(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 14))
    h, lo, c = _high(df), _low(df), _close(df)
    prev_close = c.shift(1)
    tr = pd.concat([
        h - lo,
        (h - prev_close).abs(),
        (lo - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger_percent_b(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    period = int(params.get("period", 20))
    mult = float(params.get("stddev", 2.0))
    c = _close(df)
    # Population stddev (ddof=0) — matches TradingView / most charting platforms.
    mid = c.rolling(period, min_periods=period).mean()
    std = c.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    return (c - lower) / (upper - lower).replace(0, np.nan)


# ---------------------------------------------------------------------------
# Volume: OBV, VWAP (session), AVWAP (anchored), MFI
# ---------------------------------------------------------------------------
def obv(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    c, v = _close(df), _volume(df)
    direction = np.sign(c.diff().fillna(0))
    return (direction * v).cumsum()


def vwap_session(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Session VWAP — resets every calendar day (UTC)."""
    c, v = _close(df), _volume(df)
    h, lo = _high(df), _low(df)
    tp = (h + lo + c) / 3.0  # typical price
    pv = tp * v
    # group by UTC date (if dates column missing, compute it)
    if "date" in df.columns:
        day = df["date"]
    else:
        day = pd.to_datetime(
            df["snapshotTime"] if "snapshotTime" in df.columns else df["timestamp"],
            utc=True,
        ).dt.date
    grouped_pv = pv.groupby(day).cumsum()
    grouped_v = v.groupby(day).cumsum().replace(0, np.nan)
    return grouped_pv / grouped_v


def mfi(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Money Flow Index — volume-weighted RSI."""
    period = int(params.get("period", 14))
    h, lo, c, v = _high(df), _low(df), _close(df), _volume(df)
    tp = (h + lo + c) / 3.0
    mf = tp * v
    positive = mf.where(tp > tp.shift(1), 0.0)
    negative = mf.where(tp < tp.shift(1), 0.0)
    pos_sum = positive.rolling(period).sum()
    neg_sum = negative.rolling(period).sum().replace(0, np.nan)
    mfr = pos_sum / neg_sum
    return 100 - (100 / (1 + mfr))


# ---------------------------------------------------------------------------
# Register all of the above. Default param_ranges tuned for sensible
# optimisation sweeps; agents can override.
# ---------------------------------------------------------------------------
register("sma", sma,
         defaults={"period": 20},
         param_ranges={"period": list(range(5, 201, 5))},
         category="trend", direction="neutral",
         description="Simple moving average of close price.")
register("ema", ema,
         defaults={"period": 20},
         param_ranges={"period": list(range(5, 201, 5))},
         category="trend", direction="neutral",
         description="Exponential moving average of close price.")
register("wma", wma,
         defaults={"period": 20},
         param_ranges={"period": list(range(5, 101, 5))},
         category="trend", direction="neutral",
         description="Weighted moving average of close price.")

register("rsi", rsi,
         defaults={"period": 14},
         param_ranges={"period": list(range(5, 31))},
         category="momentum", direction="neutral",
         description="Wilder's Relative Strength Index.")
register("stoch_k", stochastic_k,
         defaults={"period": 14},
         param_ranges={"period": list(range(5, 31))},
         category="momentum", direction="neutral",
         description="Stochastic %K oscillator.")
register("macd_line", macd_line,
         defaults={"fast": 12, "slow": 26},
         param_ranges={"fast": [8, 10, 12, 14], "slow": [21, 26, 30, 34]},
         category="momentum", direction="neutral",
         description="MACD line (EMA(fast) - EMA(slow)).")
register("macd_hist", macd_hist,
         defaults={"fast": 12, "slow": 26, "signal": 9},
         param_ranges={"fast": [8, 12, 14], "slow": [21, 26, 30], "signal": [7, 9, 11]},
         category="momentum", direction="neutral",
         description="MACD histogram.")
register("roc", roc,
         defaults={"period": 10},
         param_ranges={"period": list(range(5, 51, 5))},
         category="momentum", direction="neutral",
         description="Rate of Change (%).")

register("atr", atr,
         defaults={"period": 14},
         param_ranges={"period": list(range(5, 31))},
         category="volatility", direction="neutral",
         description="Average True Range (Wilder).")
register("bbands_pb", bollinger_percent_b,
         defaults={"period": 20, "stddev": 2.0},
         param_ranges={"period": list(range(10, 51, 5)), "stddev": [1.5, 2.0, 2.5, 3.0]},
         category="volatility", direction="neutral",
         description="Bollinger Bands %B.")

register("obv", obv,
         defaults={},
         param_ranges={},
         category="volume", direction="neutral",
         description="On-Balance Volume.")
register("vwap_session", vwap_session,
         defaults={},
         param_ranges={},
         category="volume", direction="neutral",
         description="Session VWAP, resets daily at UTC midnight.")
register("mfi", mfi,
         defaults={"period": 14},
         param_ranges={"period": list(range(5, 31))},
         category="volume", direction="neutral",
         description="Money Flow Index.")
