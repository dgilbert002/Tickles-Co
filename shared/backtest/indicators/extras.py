"""
Phase 18 — extra hand-rolled indicators.

The 21-year-old version
=======================
``core.py`` has the well-known classics; ``pandas_ta_bridge.py`` wraps
the pandas-ta library.  This file fills two roles:

  1. It implements indicators that are **not** in pandas-ta but that
     traders ask for constantly (Yang-Zhang volatility, percentile
     rank, Hurst exponent, range-relative position, ATR%, etc.).

  2. It exposes the **pure-pandas** versions of useful pandas-ta
     indicators with our own deterministic implementations, so that
     even if pandas-ta is uninstalled later, the strategy library
     keeps working with these names.

All functions follow the existing IndicatorSpec contract:

    f(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series

aligned to the input index.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Tuple

import numpy as np
import pandas as pd

from .core import register


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _close(df: pd.DataFrame) -> pd.Series:
    return df["close"].astype(float)


def _high(df: pd.DataFrame) -> pd.Series:
    return df["high"].astype(float)


def _low(df: pd.DataFrame) -> pd.Series:
    return df["low"].astype(float)


def _vol(df: pd.DataFrame) -> pd.Series:
    return df["volume"].astype(float)


# ---------------------------------------------------------------------------
# Statistical
# ---------------------------------------------------------------------------


def _zscore(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    s = _close(df)
    mean = s.rolling(n).mean()
    std = s.rolling(n).std(ddof=0)
    return (s - mean) / std.replace(0, np.nan)


def _percentile_rank(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 100))
    s = _close(df)
    return s.rolling(n).apply(lambda w: (w.rank(pct=True).iloc[-1]) * 100.0, raw=False)


def _rolling_skew(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    return _close(df).pct_change().rolling(int(params.get("length", 30))).skew()


def _rolling_kurt(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    return _close(df).pct_change().rolling(int(params.get("length", 30))).kurt()


def _rolling_corr(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    return _close(df).rolling(n).corr(_vol(df))


def _hurst_exponent(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Rolling Hurst exponent estimate (R/S).

    Returns a value per bar in [0, 1]:
      < 0.5: mean-reverting; 0.5: random walk; > 0.5: trending.
    """
    n = int(params.get("length", 100))
    lags_max = max(2, int(math.log2(n)))

    def _h(window: np.ndarray) -> float:
        if len(window) < 8:
            return float("nan")
        try:
            lags = range(2, min(lags_max, len(window) // 2))
            tau = []
            for lag in lags:
                diffs = np.subtract(window[lag:], window[:-lag])
                if diffs.std() <= 0:
                    return float("nan")
                tau.append(np.sqrt(diffs.std()))
            poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
            return float(poly[0] * 2.0)
        except Exception:
            return float("nan")

    return _close(df).rolling(n).apply(_h, raw=True)


# ---------------------------------------------------------------------------
# Volatility (ranges, GK, YZ)
# ---------------------------------------------------------------------------


def _true_range(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    high, low, close = _high(df), _low(df), _close(df)
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr


def _atr_pct(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 14))
    tr = _true_range(df, {})
    atr = tr.rolling(n).mean()
    return (atr / _close(df)) * 100.0


def _garman_klass(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    o = df["open"].astype(float)
    h, l_, c = _high(df), _low(df), _close(df)
    log_hl = np.log(h / l_)
    log_co = np.log(c / o)
    rs = 0.5 * log_hl ** 2 - (2 * math.log(2) - 1) * log_co ** 2
    return np.sqrt(rs.rolling(n).mean())


def _yang_zhang(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    o, h, l_, c = (
        df["open"].astype(float),
        _high(df),
        _low(df),
        _close(df),
    )
    o_to_c = np.log(c / o)
    o_to_o = np.log(o / c.shift(1))
    rs = (
        np.log(h / o) * np.log(h / c)
        + np.log(l_ / o) * np.log(l_ / c)
    )
    sigma_oc = o_to_c.rolling(n).var()
    sigma_oo = o_to_o.rolling(n).var()
    sigma_rs = rs.rolling(n).mean()
    k = 0.34 / (1.34 + (n + 1) / max(n - 1, 1))
    return np.sqrt(sigma_oo + k * sigma_oc + (1 - k) * sigma_rs)


def _high_low_range(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 14))
    return _high(df).rolling(n).max() - _low(df).rolling(n).min()


def _high_low_range_pct(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    rng = _high_low_range(df, params)
    return (rng / _close(df)) * 100.0


def _close_position_in_range(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Where is close inside the rolling [low, high] window? 0..100."""
    n = int(params.get("length", 14))
    hi = _high(df).rolling(n).max()
    lo = _low(df).rolling(n).min()
    rng = (hi - lo).replace(0, np.nan)
    return ((_close(df) - lo) / rng) * 100.0


# ---------------------------------------------------------------------------
# Returns / performance
# ---------------------------------------------------------------------------


def _log_return(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 1))
    c = _close(df)
    return np.log(c / c.shift(n))


def _pct_return(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 1))
    return _close(df).pct_change(n)


def _cumulative_return(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    return _close(df) / _close(df).iloc[0] - 1.0


def _drawdown(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    c = _close(df)
    peak = c.cummax()
    return (c - peak) / peak


def _max_drawdown(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    return _drawdown(df, {}).rolling(n).min()


def _rolling_sharpe(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    rets = _close(df).pct_change()
    mean = rets.rolling(n).mean()
    std = rets.rolling(n).std()
    annualisation = math.sqrt(int(params.get("annualisation", 252)))
    return (mean / std.replace(0, np.nan)) * annualisation


def _rolling_sortino(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    rets = _close(df).pct_change()
    downside = rets.where(rets < 0, 0.0)
    downside_std = downside.rolling(n).std()
    annualisation = math.sqrt(int(params.get("annualisation", 252)))
    return (rets.rolling(n).mean() / downside_std.replace(0, np.nan)) * annualisation


def _rolling_calmar(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    ann_ret = _close(df).pct_change().rolling(n).mean() * int(params.get("annualisation", 252))
    mdd = _max_drawdown(df, {"length": n}).abs()
    return ann_ret / mdd.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Trend / momentum
# ---------------------------------------------------------------------------


def _smma(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Smoothed MA (a.k.a. RMA, Wilder)."""
    n = int(params.get("length", 14))
    return _close(df).ewm(alpha=1 / n, adjust=False).mean()


def _ma_envelope_upper(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    pct = float(params.get("pct", 2.5))
    sma = _close(df).rolling(n).mean()
    return sma * (1.0 + pct / 100.0)


def _ma_envelope_lower(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    pct = float(params.get("pct", 2.5))
    sma = _close(df).rolling(n).mean()
    return sma * (1.0 - pct / 100.0)


def _price_above_sma(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 200))
    return (_close(df) > _close(df).rolling(n).mean()).astype(int)


def _ema_slope(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    ema = _close(df).ewm(span=n, adjust=False).mean()
    return ema.diff()


def _slope_n(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 5))
    return _close(df).diff(n) / n


def _gain_ratio(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    """Up-bars / total bars over the rolling window."""
    n = int(params.get("length", 14))
    diff = _close(df).diff()
    ups = (diff > 0).rolling(n).sum()
    return ups / n * 100.0


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def _volume_zscore(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 30))
    v = _vol(df)
    return (v - v.rolling(n).mean()) / v.rolling(n).std(ddof=0).replace(0, np.nan)


def _volume_ratio(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    v = _vol(df)
    return v / v.rolling(n).mean()


def _dollar_volume(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    return _close(df) * _vol(df)


def _vwap_anchored_session(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    """Daily anchored VWAP using df['timestamp'] (or index if it's datetime)."""
    if isinstance(df.index, pd.DatetimeIndex):
        ts = df.index
    elif "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
    else:
        return pd.Series([np.nan] * len(df), index=df.index)
    day = ts.tz_convert("UTC").date if hasattr(ts, "tz_convert") else pd.Series(ts).dt.date
    pv = ((_high(df) + _low(df) + _close(df)) / 3.0) * _vol(df)
    grouped_pv = pd.Series(pv.values, index=df.index).groupby(day).cumsum()
    grouped_v = pd.Series(_vol(df).values, index=df.index).groupby(day).cumsum()
    return grouped_pv / grouped_v.replace(0, np.nan)


def _accumulation_pct(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 14))
    direction = np.sign(_close(df).diff())
    signed_v = _vol(df) * direction
    return signed_v.rolling(n).sum() / _vol(df).rolling(n).sum() * 100.0


# ---------------------------------------------------------------------------
# Pattern / regime helpers
# ---------------------------------------------------------------------------


def _bullish_streak(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    diff = _close(df).diff()
    return (diff > 0).astype(int).groupby((diff <= 0).cumsum()).cumsum()


def _bearish_streak(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    diff = _close(df).diff()
    return (diff < 0).astype(int).groupby((diff >= 0).cumsum()).cumsum()


def _gap_pct(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    o = df["open"].astype(float)
    prev_c = _close(df).shift(1)
    return (o - prev_c) / prev_c * 100.0


def _body_pct(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    body = (_close(df) - df["open"].astype(float)).abs()
    rng = (_high(df) - _low(df)).replace(0, np.nan)
    return body / rng * 100.0


def _upper_wick_pct(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    upper = _high(df) - df[["open", "close"]].max(axis=1)
    rng = (_high(df) - _low(df)).replace(0, np.nan)
    return upper / rng * 100.0


def _lower_wick_pct(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    lower = df[["open", "close"]].min(axis=1) - _low(df)
    rng = (_high(df) - _low(df)).replace(0, np.nan)
    return lower / rng * 100.0


def _is_inside_bar(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    return ((_high(df) < _high(df).shift(1)) & (_low(df) > _low(df).shift(1))).astype(int)


def _is_outside_bar(df: pd.DataFrame, _params: Dict[str, Any]) -> pd.Series:
    return ((_high(df) > _high(df).shift(1)) & (_low(df) < _low(df).shift(1))).astype(int)


# ---------------------------------------------------------------------------
# Range / channel helpers
# ---------------------------------------------------------------------------


def _rolling_high(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    return _high(df).rolling(int(params.get("length", 20))).max()


def _rolling_low(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    return _low(df).rolling(int(params.get("length", 20))).min()


def _rolling_high_pos(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    return _high(df).rolling(n).apply(lambda w: float(np.argmax(w[::-1])), raw=True)


def _rolling_low_pos(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    return _low(df).rolling(n).apply(lambda w: float(np.argmin(w[::-1])), raw=True)


def _close_to_high(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    return (_high(df).rolling(n).max() - _close(df)) / _close(df) * 100.0


def _close_to_low(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    n = int(params.get("length", 20))
    return (_close(df) - _low(df).rolling(n).min()) / _close(df) * 100.0


# ---------------------------------------------------------------------------
# Registration table
# ---------------------------------------------------------------------------


_ExtraFn = Callable[[pd.DataFrame, Dict[str, Any]], pd.Series]
_ExtraRow = Tuple[str, _ExtraFn, Dict[str, Any], Dict[str, Any], str, str, str]

_EXTRAS: List[_ExtraRow] = [
    # statistical
    ("ext_zscore",          _zscore,         {"length": 30},                {"length": [10, 20, 30, 60, 90]},  "statistical", "neutral", "Rolling z-score of close."),
    ("ext_percentile_rank", _percentile_rank, {"length": 100},               {"length": [50, 100, 250]},          "statistical", "neutral", "Percentile rank of close in window."),
    ("ext_skew",            _rolling_skew,   {"length": 30},                {"length": [10, 30, 60]},            "statistical", "neutral", "Rolling skew of returns."),
    ("ext_kurt",            _rolling_kurt,   {"length": 30},                {"length": [10, 30, 60]},            "statistical", "neutral", "Rolling kurtosis of returns."),
    ("ext_corr_close_volume", _rolling_corr, {"length": 30},                {"length": [10, 30, 60]},            "statistical", "neutral", "Rolling correlation between close and volume."),
    ("ext_hurst",           _hurst_exponent, {"length": 100},               {"length": [60, 100, 200]},          "statistical", "neutral", "Rolling Hurst exponent estimate."),

    # volatility
    ("ext_true_range",       _true_range,    {},                            {},                                 "volatility", "neutral", "True range per bar."),
    ("ext_atr_pct",          _atr_pct,       {"length": 14},                {"length": [7, 14, 21, 30]},        "volatility", "neutral", "ATR as percent of close."),
    ("ext_garman_klass",     _garman_klass,  {"length": 30},                {"length": [10, 30, 60]},           "volatility", "neutral", "Garman-Klass volatility."),
    ("ext_yang_zhang",       _yang_zhang,    {"length": 30},                {"length": [10, 30, 60]},           "volatility", "neutral", "Yang-Zhang volatility."),
    ("ext_high_low_range",   _high_low_range, {"length": 14},               {"length": [7, 14, 30]},            "volatility", "neutral", "High - low over rolling window."),
    ("ext_high_low_range_pct", _high_low_range_pct, {"length": 14},         {"length": [7, 14, 30]},            "volatility", "neutral", "High-low range as percent of close."),
    ("ext_close_pos_in_range", _close_position_in_range, {"length": 14},    {"length": [7, 14, 30]},            "volatility", "neutral", "Close position inside [low, high] window (0..100)."),

    # returns / performance
    ("ext_log_return",       _log_return,    {"length": 1},                 {"length": [1, 5, 10]},             "performance", "neutral", "Log return over N bars."),
    ("ext_pct_return",       _pct_return,    {"length": 1},                 {"length": [1, 5, 10]},             "performance", "neutral", "Percent return over N bars."),
    ("ext_cum_return",       _cumulative_return, {},                        {},                                 "performance", "neutral", "Cumulative return from first bar."),
    ("ext_drawdown",         _drawdown,      {},                            {},                                 "performance", "neutral", "Live drawdown from peak."),
    ("ext_max_drawdown",     _max_drawdown,  {"length": 30},                {"length": [10, 30, 90]},           "performance", "neutral", "Rolling max drawdown."),
    ("ext_rolling_sharpe",   _rolling_sharpe, {"length": 30, "annualisation": 252}, {"length": [30, 60, 90]},   "performance", "neutral", "Rolling annualised Sharpe ratio."),
    ("ext_rolling_sortino",  _rolling_sortino, {"length": 30, "annualisation": 252}, {"length": [30, 60, 90]},  "performance", "neutral", "Rolling annualised Sortino ratio."),
    ("ext_rolling_calmar",   _rolling_calmar, {"length": 30, "annualisation": 252}, {"length": [30, 60, 90]},   "performance", "neutral", "Rolling Calmar ratio."),

    # trend / momentum
    ("ext_smma",             _smma,          {"length": 14},                {"length": [7, 14, 21, 30]},        "trend",      "neutral",  "Smoothed MA (Wilder/RMA)."),
    ("ext_ma_env_upper",     _ma_envelope_upper, {"length": 20, "pct": 2.5},{"length": [10, 20, 30], "pct": [1.0, 2.0, 5.0]}, "trend", "bullish", "Moving average envelope upper."),
    ("ext_ma_env_lower",     _ma_envelope_lower, {"length": 20, "pct": 2.5},{"length": [10, 20, 30], "pct": [1.0, 2.0, 5.0]}, "trend", "bearish", "Moving average envelope lower."),
    ("ext_price_above_sma",  _price_above_sma, {"length": 200},             {"length": [50, 100, 200]},         "trend",      "bullish",  "1 if close > SMA(N) else 0."),
    ("ext_ema_slope",        _ema_slope,     {"length": 20},                {"length": [10, 20, 50]},           "trend",      "neutral",  "First difference of EMA(N)."),
    ("ext_close_slope",      _slope_n,       {"length": 5},                 {"length": [3, 5, 10]},             "trend",      "neutral",  "Slope: (close - close.shift(N))/N."),
    ("ext_gain_ratio",       _gain_ratio,    {"length": 14},                {"length": [7, 14, 30]},            "momentum",   "neutral",  "Up-bars / total bars (% over window)."),

    # volume
    ("ext_volume_zscore",    _volume_zscore, {"length": 30},                {"length": [10, 30, 60]},           "volume",     "neutral",  "Volume z-score over rolling window."),
    ("ext_volume_ratio",     _volume_ratio,  {"length": 20},                {"length": [10, 20, 50]},           "volume",     "neutral",  "Volume / SMA(volume)."),
    ("ext_dollar_volume",    _dollar_volume, {},                            {},                                 "volume",     "neutral",  "Close * volume per bar."),
    ("ext_vwap_session",     _vwap_anchored_session, {},                    {},                                 "volume",     "neutral",  "Daily-anchored VWAP."),
    ("ext_accumulation_pct", _accumulation_pct, {"length": 14},             {"length": [7, 14, 30]},            "volume",     "neutral",  "Signed-volume accumulation as % of total."),

    # pattern / regime
    ("ext_bullish_streak",   _bullish_streak, {},                           {},                                 "pattern",    "bullish",  "Consecutive up-bars count."),
    ("ext_bearish_streak",   _bearish_streak, {},                           {},                                 "pattern",    "bearish",  "Consecutive down-bars count."),
    ("ext_gap_pct",          _gap_pct,       {},                            {},                                 "pattern",    "neutral",  "Gap from prior close as percent."),
    ("ext_body_pct",         _body_pct,      {},                            {},                                 "pattern",    "neutral",  "Candle body as percent of range."),
    ("ext_upper_wick_pct",   _upper_wick_pct, {},                           {},                                 "pattern",    "neutral",  "Upper wick as percent of range."),
    ("ext_lower_wick_pct",   _lower_wick_pct, {},                           {},                                 "pattern",    "neutral",  "Lower wick as percent of range."),
    ("ext_inside_bar",       _is_inside_bar, {},                            {},                                 "pattern",    "neutral",  "1 if inside bar else 0."),
    ("ext_outside_bar",      _is_outside_bar, {},                           {},                                 "pattern",    "neutral",  "1 if outside bar else 0."),

    # range / channel helpers
    ("ext_rolling_high",     _rolling_high,  {"length": 20},                {"length": [10, 20, 50, 100]},      "trend",      "neutral",  "Rolling max(high)."),
    ("ext_rolling_low",      _rolling_low,   {"length": 20},                {"length": [10, 20, 50, 100]},      "trend",      "neutral",  "Rolling min(low)."),
    ("ext_rolling_high_age", _rolling_high_pos, {"length": 20},             {"length": [10, 20, 50]},           "trend",      "neutral",  "Bars since rolling high."),
    ("ext_rolling_low_age",  _rolling_low_pos, {"length": 20},              {"length": [10, 20, 50]},           "trend",      "neutral",  "Bars since rolling low."),
    ("ext_close_to_high",    _close_to_high, {"length": 20},                {"length": [10, 20, 50]},           "trend",      "neutral",  "% distance from close to rolling high."),
    ("ext_close_to_low",     _close_to_low,  {"length": 20},                {"length": [10, 20, 50]},           "trend",      "neutral",  "% distance from close to rolling low."),
]


_REG_DONE = False


def register_all() -> int:
    global _REG_DONE
    if _REG_DONE:
        return len(_EXTRAS)
    count = 0
    for name, fn, defs, ranges, cat, dirn, desc in _EXTRAS:
        try:
            register(
                name=name,
                fn=fn,
                defaults=dict(defs),
                param_ranges=dict(ranges),
                category=cat,
                direction=dirn,
                description=desc,
                asset_class="any",
            )
            count += 1
        except Exception:  # noqa: BLE001
            pass
    _REG_DONE = True
    return count


__all__ = ["register_all"]
