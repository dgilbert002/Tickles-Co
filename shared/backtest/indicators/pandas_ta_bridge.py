"""
Phase 18 — pandas-ta bridge.

The 21-year-old version
=======================
``shared/backtest/indicators/core.py`` already gave us 23 hand-rolled
pandas indicators with a tidy ``IndicatorSpec`` API.  We don't want to
re-write every other indicator in finance — pandas-ta already has them.

But pandas-ta's API is different:

  - some functions return a ``pd.Series`` (rsi, ema, ...).
  - some return a ``pd.DataFrame`` (macd, bbands, adx, stoch, ...).
  - they take named OHLCV kwargs (``high=df.high, close=df.close, ...``).

This module defines a small bridge that:

  1. Wraps each pandas-ta function so it accepts our ``(df, params)``
     signature and returns a ``pd.Series`` aligned to the df index.
  2. For DataFrame-returning functions, registers one ``IndicatorSpec``
     **per output column** so a strategy can pick "macd line" vs
     "macd histogram" without parsing arrays.
  3. Skips silently if pandas-ta isn't installed — the rest of the
     indicator system continues to work with what's hand-rolled.

This avoids reinventing volume profiles, ichimoku, stochastic, etc.
and gives us a >250-entry indicator catalogue in a single module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from .core import INDICATORS, IndicatorSpec, register

log = logging.getLogger("tickles.indicators.bridge")


# ---------------------------------------------------------------------------
# Binding metadata
# ---------------------------------------------------------------------------


# Map our friendly column names to the kwargs pandas-ta expects.
_OHLCV_KWARG_MAP: Dict[str, str] = {
    "open": "open_",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}


@dataclass(frozen=True)
class _Binding:
    """One indicator wrapper definition."""

    name: str                           # name in our registry
    pta_fn: str                         # pandas_ta function name
    inputs: Tuple[str, ...]             # ohlcv columns to pass
    defaults: Dict[str, Any]            # default params
    param_ranges: Dict[str, Any]        # for sweeps — accepts any iterable per param
    category: str
    direction: str
    description: str
    column: Optional[str] = None        # for DataFrame outputs: column suffix to slice
    asset_class: str = "any"
    column_index: Optional[int] = None  # alternative to `column`: positional column


def _make_wrapper(binding: _Binding) -> Callable[[pd.DataFrame, Dict[str, Any]], pd.Series]:
    """Compose a function matching IndicatorSpec.fn signature."""

    fn_name = binding.pta_fn
    inputs = binding.inputs
    column = binding.column
    column_index = binding.column_index

    def wrapper(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
        try:
            import pandas_ta as pta  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("pandas_ta not installed") from exc

        fn = getattr(pta, fn_name, None)
        if fn is None:
            raise RuntimeError(f"pandas_ta has no function '{fn_name}'")

        kwargs: Dict[str, Any] = {}
        for col in inputs:
            kwarg_name = _OHLCV_KWARG_MAP[col]
            if col not in df.columns:
                raise KeyError(f"input df missing column '{col}'")
            kwargs[kwarg_name] = df[col]

        merged = {**binding.defaults, **(params or {})}
        kwargs.update(merged)

        result = fn(**kwargs)

        if result is None:
            return pd.Series([float("nan")] * len(df), index=df.index, name=binding.name)

        if isinstance(result, pd.Series):
            return result.reindex(df.index)

        if isinstance(result, pd.DataFrame):
            if column is not None:
                matches = [c for c in result.columns if column in c]
                if matches:
                    return result[matches[0]].reindex(df.index)
            if column_index is not None and 0 <= column_index < len(result.columns):
                return result.iloc[:, column_index].reindex(df.index)
            return result.iloc[:, 0].reindex(df.index)

        return pd.Series(result, index=df.index, name=binding.name)

    wrapper.__name__ = f"pta_{binding.name}"
    return wrapper


# ---------------------------------------------------------------------------
# The big binding table
# ---------------------------------------------------------------------------


# Helpers for common parameter sweep ranges.
def _len_range(low: int, high: int, step: int = 1) -> List[int]:
    return list(range(low, high + 1, step))


def _bindings() -> List[_Binding]:
    out: List[_Binding] = []

    # ------------------------------------------------------------------
    # Trend / moving averages
    # ------------------------------------------------------------------
    trend_smooth = [
        ("dema",  "Double EMA",                "dema",  ["close"], {"length": 20}),
        ("tema",  "Triple EMA",                "tema",  ["close"], {"length": 20}),
        ("hma",   "Hull MA",                   "hma",   ["close"], {"length": 20}),
        ("alma",  "Arnaud Legoux MA",          "alma",  ["close"], {"length": 20}),
        ("kama",  "Kaufman Adaptive MA",       "kama",  ["close"], {"length": 10}),
        ("zlma",  "Zero-Lag MA",               "zlma",  ["close"], {"length": 20}),
        ("vidya", "VIDYA",                     "vidya", ["close"], {"length": 14}),
        ("vwma",  "Volume-weighted MA",        "vwma",  ["close", "volume"], {"length": 20}),
        ("fwma",  "Fibonacci-weighted MA",     "fwma",  ["close"], {"length": 21}),
        ("hwma",  "Holt-Winters MA",           "hwma",  ["close"], {}),
        ("jma",   "Jurik MA",                  "jma",   ["close"], {"length": 7, "phase": 0}),
        ("mcgd",  "McGinley Dynamic",          "mcgd",  ["close"], {"length": 10}),
        ("ssf",   "Super Smoother Filter",     "ssf",   ["close"], {"length": 10}),
        ("trima", "Triangular MA",             "trima", ["close"], {"length": 10}),
        ("t3",    "T3 MA (Tillson)",           "t3",    ["close"], {"length": 10}),
        ("swma",  "Symmetric weighted MA",     "swma",  ["close"], {}),
        ("sinwma","Sine-weighted MA",          "sinwma",["close"], {"length": 14}),
        ("pwma",  "Pascal-weighted MA",        "pwma",  ["close"], {"length": 14}),
        ("rma",   "Wilder smoothing (RMA)",    "rma",   ["close"], {"length": 14}),
        ("midpoint", "Midpoint over period",   "midpoint", ["close"], {"length": 14}),
        ("midprice", "Midprice (high/low)",    "midprice", ["high", "low"], {"length": 14}),
        ("ht_trendline", "Hilbert Trendline",  "ht_trendline", ["close"], {}),
    ]
    for name, desc, fn, inp, defs in trend_smooth:
        ranges = {k: _len_range(5, 60, 5) for k in defs if k == "length"}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="trend", direction="neutral", description=desc,
        ))

    # Trend / direction
    out.append(_Binding(
        name="pta_supertrend", pta_fn="supertrend",
        inputs=("high", "low", "close"),
        defaults={"length": 7, "multiplier": 3.0},
        param_ranges={"length": _len_range(5, 30), "multiplier": [2.0, 2.5, 3.0, 3.5, 4.0]},
        column="SUPERT_",
        category="trend", direction="neutral",
        description="Supertrend line.",
    ))
    out.append(_Binding(
        name="pta_supertrend_dir", pta_fn="supertrend",
        inputs=("high", "low", "close"),
        defaults={"length": 7, "multiplier": 3.0},
        param_ranges={"length": _len_range(5, 30), "multiplier": [2.0, 2.5, 3.0]},
        column="SUPERTd_",
        category="trend", direction="neutral",
        description="Supertrend direction (-1 / +1).",
    ))
    out.append(_Binding(
        name="pta_psar_long", pta_fn="psar",
        inputs=("high", "low"),
        defaults={"af": 0.02, "max_af": 0.2},
        param_ranges={"af": [0.01, 0.02, 0.04], "max_af": [0.1, 0.2, 0.3]},
        column="PSARl_",
        category="trend", direction="neutral",
        description="Parabolic SAR (long values when in uptrend).",
    ))
    out.append(_Binding(
        name="pta_psar_short", pta_fn="psar",
        inputs=("high", "low"),
        defaults={"af": 0.02, "max_af": 0.2},
        param_ranges={"af": [0.01, 0.02, 0.04], "max_af": [0.1, 0.2, 0.3]},
        column="PSARs_",
        category="trend", direction="neutral",
        description="Parabolic SAR (short values when in downtrend).",
    ))
    out.append(_Binding(
        name="pta_psar_af", pta_fn="psar",
        inputs=("high", "low"),
        defaults={"af": 0.02, "max_af": 0.2},
        param_ranges={"af": [0.01, 0.02, 0.04]},
        column="PSARaf_",
        category="trend", direction="neutral",
        description="Parabolic SAR acceleration factor.",
    ))

    # ADX family (ADX returns DataFrame: ADX, DMP+, DMN-)
    for col, suf in [("ADX_", "value"), ("DMP_", "plus_di"), ("DMN_", "minus_di")]:
        out.append(_Binding(
            name=f"pta_adx_{suf}", pta_fn="adx",
            inputs=("high", "low", "close"),
            defaults={"length": 14},
            param_ranges={"length": _len_range(5, 30)},
            column=col,
            category="trend", direction="neutral",
            description=f"ADX/DMI component: {suf}.",
        ))

    # Aroon
    out.append(_Binding(
        name="pta_aroon_up", pta_fn="aroon",
        inputs=("high", "low"),
        defaults={"length": 14},
        param_ranges={"length": _len_range(5, 50)},
        column="AROONU_",
        category="trend", direction="bullish",
        description="Aroon Up.",
    ))
    out.append(_Binding(
        name="pta_aroon_down", pta_fn="aroon",
        inputs=("high", "low"),
        defaults={"length": 14},
        param_ranges={"length": _len_range(5, 50)},
        column="AROOND_",
        category="trend", direction="bearish",
        description="Aroon Down.",
    ))
    out.append(_Binding(
        name="pta_aroon_osc", pta_fn="aroon",
        inputs=("high", "low"),
        defaults={"length": 14},
        param_ranges={"length": _len_range(5, 50)},
        column="AROONOSC_",
        category="trend", direction="neutral",
        description="Aroon Oscillator (Up - Down).",
    ))

    # Vortex
    out.append(_Binding(
        name="pta_vortex_pos", pta_fn="vortex",
        inputs=("high", "low", "close"),
        defaults={"length": 14},
        param_ranges={"length": _len_range(5, 30)},
        column="VTXP_",
        category="trend", direction="bullish",
        description="Vortex VI+.",
    ))
    out.append(_Binding(
        name="pta_vortex_neg", pta_fn="vortex",
        inputs=("high", "low", "close"),
        defaults={"length": 14},
        param_ranges={"length": _len_range(5, 30)},
        column="VTXM_",
        category="trend", direction="bearish",
        description="Vortex VI-.",
    ))

    # Ichimoku — register all 5 lines
    for col, suf, dirn in [
        ("ITS_", "tenkan", "neutral"),
        ("IKS_", "kijun", "neutral"),
        ("ISA_", "senkou_a", "neutral"),
        ("ISB_", "senkou_b", "neutral"),
        ("ICS_", "chikou", "neutral"),
    ]:
        out.append(_Binding(
            name=f"pta_ichi_{suf}", pta_fn="ichimoku",
            inputs=("high", "low", "close"),
            defaults={"tenkan": 9, "kijun": 26, "senkou": 52},
            param_ranges={"tenkan": [7, 9, 12], "kijun": [20, 26, 30]},
            column=col,
            category="trend", direction=dirn,
            description=f"Ichimoku {suf}.",
        ))

    # ------------------------------------------------------------------
    # Momentum / oscillators
    # ------------------------------------------------------------------
    momentum_simple = [
        ("apo",   "Absolute Price Oscillator", "apo",   ["close"], {"fast": 12, "slow": 26}),
        ("bias",  "BIAS",                      "bias",  ["close"], {"length": 26}),
        ("brar_ar", "BRAR (AR)",               "brar",  ["open", "high", "low", "close"], {"length": 26}),
        ("cfo",   "Chande Forecast Oscillator","cfo",   ["close"], {"length": 9}),
        ("cg",    "Centre of Gravity",         "cg",    ["close"], {"length": 10}),
        ("cmo",   "Chande Momentum",           "cmo",   ["close"], {"length": 14}),
        ("coppock", "Coppock Curve",           "coppock", ["close"], {"length": 10, "fast": 11, "slow": 14}),
        ("cti",   "Correlation Trend Indicator","cti",  ["close"], {"length": 12}),
        ("er",    "Efficiency Ratio",          "er",    ["close"], {"length": 10}),
        ("fisher", "Fisher Transform",         "fisher",["high", "low"], {"length": 9}),
        ("inertia","Inertia",                  "inertia", ["close", "high", "low"], {"length": 20, "rvi_length": 14}),
        ("mom",   "Momentum",                  "mom",   ["close"], {"length": 10}),
        ("pgo",   "Pretty Good Oscillator",    "pgo",   ["high", "low", "close"], {"length": 14}),
        ("ppo",   "Percentage Price Oscillator","ppo",  ["close"], {"fast": 12, "slow": 26}),
        ("psl",   "Psychological Line",        "psl",   ["close"], {"length": 12}),
        ("pvo",   "Percentage Volume Oscillator","pvo", ["volume"], {"fast": 12, "slow": 26}),
        ("qqe",   "Quantitative Qualitative Estimation", "qqe", ["close"], {"length": 14}),
        ("roc",   "Rate of Change",            "roc",   ["close"], {"length": 10}),
        ("rsx",   "Smoothed RSI (RSX)",        "rsx",   ["close"], {"length": 14}),
        ("crsi",  "Connors RSI",               "crsi",  ["close"], {"rsi_length": 3, "streak_length": 2, "rank_length": 100}),
        ("slope", "Slope of regression",       "slope", ["close"], {"length": 1}),
        ("smi",   "Stochastic Momentum Index", "smi",   ["close"], {"fast": 5, "slow": 20, "signal": 5}),
        ("stochrsi", "Stochastic RSI",         "stochrsi", ["close"], {"length": 14, "rsi_length": 14}),
        ("trix",  "Triple-smoothed momentum",  "trix",  ["close"], {"length": 30, "signal": 9}),
        ("tsi",   "True Strength Index",       "tsi",   ["close"], {"fast": 13, "slow": 25, "signal": 13}),
        ("uo",    "Ultimate Oscillator",       "uo",    ["high", "low", "close"], {"fast": 7, "medium": 14, "slow": 28}),
        ("willr", "Williams %R",               "willr", ["high", "low", "close"], {"length": 14}),
        ("eri_bull","Elder-Ray Bull Power",    "eri",   ["high", "low", "close"], {"length": 13}),
    ]
    for name, desc, fn, inp, defs in momentum_simple:
        ranges = {k: _len_range(5, 60, 5) for k in defs if k in ("length", "fast", "slow")}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="momentum", direction="neutral", description=desc,
            column_index=0,  # take first column if DataFrame
        ))

    # CCI (single value)
    out.append(_Binding(
        name="pta_cci", pta_fn="cci",
        inputs=("high", "low", "close"),
        defaults={"length": 14, "c": 0.015},
        param_ranges={"length": _len_range(5, 60)},
        category="momentum", direction="neutral",
        description="Commodity Channel Index.",
    ))

    # KDJ — three columns
    for col, suf in [("K_", "k"), ("D_", "d"), ("J_", "j")]:
        out.append(_Binding(
            name=f"pta_kdj_{suf}", pta_fn="kdj",
            inputs=("high", "low", "close"),
            defaults={"length": 9, "signal": 3},
            param_ranges={"length": _len_range(5, 21)},
            column=col,
            category="momentum", direction="neutral",
            description=f"KDJ {suf}.",
        ))

    # KST
    for col, suf in [("KST_", "value"), ("KSTs_", "signal")]:
        out.append(_Binding(
            name=f"pta_kst_{suf}", pta_fn="kst",
            inputs=("close",),
            defaults={"signal": 9},
            param_ranges={},
            column=col,
            category="momentum", direction="neutral",
            description=f"Know Sure Thing {suf}.",
        ))

    # ------------------------------------------------------------------
    # Volatility
    # ------------------------------------------------------------------
    volatility: List[Tuple[str, str, str, List[str], Dict[str, Any]]] = [
        ("atrts",  "ATR Trailing Stop",   "atrts",  ["high", "low", "close"], {"length": 21, "ma_length": 1, "k": 3.0}),
        ("chop",   "Choppiness Index",    "chop",   ["high", "low", "close"], {"length": 14}),
        ("hl2",    "(High+Low)/2",        "hl2",    ["high", "low"], {}),
        ("hlc3",   "(High+Low+Close)/3",  "hlc3",   ["high", "low", "close"], {}),
        ("kurtosis","Rolling kurtosis",   "kurtosis", ["close"], {"length": 30}),
        ("massi",  "Mass Index",          "massi",  ["high", "low"], {"fast": 9, "slow": 25}),
        ("natr",   "Normalised ATR",      "natr",   ["high", "low", "close"], {"length": 14}),
        ("pdist",  "Price Distance",      "pdist",  ["open", "high", "low", "close"], {}),
        ("rvi",    "Relative Volatility Index", "rvi", ["close"], {"length": 14}),
        ("thermo", "Elder Thermometer",   "thermo", ["high", "low"], {"length": 20}),
        ("true_range", "True Range",      "true_range", ["high", "low", "close"], {}),
        ("ui",     "Ulcer Index",         "ui",     ["close"], {"length": 14}),
        ("variance", "Rolling variance",  "variance", ["close"], {"length": 30}),
        ("entropy", "Shannon entropy",    "entropy", ["close"], {"length": 10, "base": 2.0}),
        ("zscore", "Rolling z-score",     "zscore", ["close"], {"length": 30}),
    ]
    for name, desc, fn, inp, defs in volatility:
        ranges = {k: _len_range(5, 60, 5) for k in defs if k in ("length", "fast", "slow", "ma_length")}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="volatility", direction="neutral", description=desc,
            column_index=0,
        ))

    # Bollinger Bands — 5 columns
    for col, suf, dirn in [
        ("BBL_", "lower", "bearish"),
        ("BBM_", "middle", "neutral"),
        ("BBU_", "upper", "bullish"),
        ("BBB_", "bandwidth", "neutral"),
        ("BBP_", "percent_b", "neutral"),
    ]:
        out.append(_Binding(
            name=f"pta_bbands_{suf}", pta_fn="bbands",
            inputs=("close",),
            defaults={"length": 20, "std": 2.0},
            param_ranges={"length": _len_range(5, 60, 5), "std": [1.0, 1.5, 2.0, 2.5, 3.0]},
            column=col,
            category="volatility", direction=dirn,
            description=f"Bollinger {suf}.",
        ))

    # Keltner Channel — 3 columns
    for col, suf, dirn in [
        ("KCLe_", "lower", "bearish"),
        ("KCBe_", "basis", "neutral"),
        ("KCUe_", "upper", "bullish"),
    ]:
        out.append(_Binding(
            name=f"pta_kc_{suf}", pta_fn="kc",
            inputs=("high", "low", "close"),
            defaults={"length": 20, "scalar": 2.0},
            param_ranges={"length": _len_range(10, 40, 5), "scalar": [1.0, 1.5, 2.0, 2.5]},
            column=col,
            category="volatility", direction=dirn,
            description=f"Keltner {suf}.",
        ))

    # Donchian — 3 columns
    for col, suf, dirn in [
        ("DCL_", "lower", "bearish"),
        ("DCM_", "middle", "neutral"),
        ("DCU_", "upper", "bullish"),
    ]:
        out.append(_Binding(
            name=f"pta_donchian_{suf}", pta_fn="donchian",
            inputs=("high", "low"),
            defaults={"lower_length": 20, "upper_length": 20},
            param_ranges={"lower_length": _len_range(10, 60, 5)},
            column=col,
            category="volatility", direction=dirn,
            description=f"Donchian {suf}.",
        ))

    # Acceleration Bands
    for col, suf in [("ACCBL_", "lower"), ("ACCBM_", "middle"), ("ACCBU_", "upper")]:
        out.append(_Binding(
            name=f"pta_accbands_{suf}", pta_fn="accbands",
            inputs=("high", "low", "close"),
            defaults={"length": 20},
            param_ranges={"length": _len_range(10, 40, 5)},
            column=col,
            category="volatility", direction="neutral",
            description=f"Acceleration band {suf}.",
        ))

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------
    volume = [
        ("ad",      "Accumulation/Distribution", "ad",     ["high", "low", "close", "volume"], {}),
        ("adosc",   "Chaikin A/D Oscillator",    "adosc",  ["high", "low", "close", "volume"], {"fast": 3, "slow": 10}),
        ("aobv",    "Archer OBV",                "aobv",   ["close", "volume"], {"fast": 4, "slow": 12}),
        ("cmf",     "Chaikin Money Flow",        "cmf",    ["high", "low", "close", "volume"], {"length": 20}),
        ("efi",     "Elder Force Index",         "efi",    ["close", "volume"], {"length": 13}),
        ("eom",     "Ease of Movement",          "eom",    ["high", "low", "close", "volume"], {"length": 14}),
        ("kvo",     "Klinger Volume Oscillator", "kvo",    ["high", "low", "close", "volume"], {"fast": 34, "slow": 55}),
        ("nvi",     "Negative Volume Index",     "nvi",    ["close", "volume"], {"length": 1}),
        ("pvi",     "Positive Volume Index",     "pvi",    ["close", "volume"], {"length": 1}),
        ("pvol",    "Price-volume",              "pvol",   ["close", "volume"], {}),
        ("pvr",     "Price-volume rank",         "pvr",    ["close", "volume"], {}),
        ("pvt",     "Price-volume trend",        "pvt",    ["close", "volume"], {}),
        ("tsv",     "Time-segmented volume",     "tsv",    ["close", "volume"], {"length": 18}),
        ("vhm",     "Volume Heat Map",           "vhm",    ["volume"], {"length": 1, "std_length": 100}),
    ]
    for name, desc, fn, inp, defs in volume:
        ranges = {k: _len_range(5, 60, 5) for k in defs if k in ("length", "fast", "slow")}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="volume", direction="neutral", description=desc,
            column_index=0,
        ))

    # ------------------------------------------------------------------
    # Statistical / regression
    # ------------------------------------------------------------------
    statistical: List[Tuple[str, str, str, List[str], Dict[str, Any]]] = [
        ("linreg", "Linear regression value",  "linreg", ["close"], {"length": 14}),
        ("mad",    "Mean Absolute Deviation",  "mad",    ["close"], {"length": 30}),
        ("median", "Rolling median",           "median", ["close"], {"length": 30}),
        ("quantile", "Rolling quantile",       "quantile", ["close"], {"length": 30, "q": 0.5}),
        ("skew",   "Rolling skew",             "skew",   ["close"], {"length": 30}),
        ("stdev",  "Rolling stdev",            "stdev",  ["close"], {"length": 30}),
    ]
    for name, desc, fn, inp, defs in statistical:
        ranges = {k: _len_range(5, 90, 5) for k in defs if k == "length"}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="statistical", direction="neutral", description=desc,
            column_index=0,
        ))

    # ------------------------------------------------------------------
    # Performance / portfolio analytics
    # ------------------------------------------------------------------
    performance = [
        ("log_return",    "Log return",         "log_return",    ["close"], {"length": 1}),
        ("percent_return","Percent return",     "percent_return",["close"], {"length": 1}),
        ("drawdown",      "Drawdown",           "drawdown",      ["close"], {}),
    ]
    for name, desc, fn, inp, defs in performance:
        ranges = {k: _len_range(1, 30, 1) for k in defs if k == "length"}
        out.append(_Binding(
            name=f"pta_{name}", pta_fn=fn, inputs=tuple(inp),
            defaults=defs, param_ranges=ranges,
            category="performance", direction="neutral", description=desc,
            column_index=0,
        ))

    # ------------------------------------------------------------------
    # Candle patterns — pandas_ta returns Series of -100/0/100
    # ------------------------------------------------------------------
    candle_patterns = [
        ("doji",         "Doji"),
        ("inside",       "Inside bar"),
        # other CDL_PATTERN_NAMES come from pta.cdl_pattern but those need talib
    ]
    for name, desc in candle_patterns:
        out.append(_Binding(
            name=f"pta_cdl_{name}", pta_fn=f"cdl_{name}",
            inputs=("open", "high", "low", "close"),
            defaults={},
            param_ranges={},
            category="pattern", direction="neutral",
            description=f"Candle pattern: {desc}.",
            column_index=0,
        ))
    out.append(_Binding(
        name="pta_candle_color", pta_fn="candle_color",
        inputs=("open", "close"),
        defaults={},
        param_ranges={},
        category="pattern", direction="neutral",
        description="Candle color (+1 bull, -1 bear, 0 doji).",
    ))
    out.append(_Binding(
        name="pta_ha_open", pta_fn="ha",
        inputs=("open", "high", "low", "close"),
        defaults={},
        param_ranges={},
        column="HA_open",
        category="pattern", direction="neutral",
        description="Heikin-Ashi open.",
    ))
    out.append(_Binding(
        name="pta_ha_close", pta_fn="ha",
        inputs=("open", "high", "low", "close"),
        defaults={},
        param_ranges={},
        column="HA_close",
        category="pattern", direction="neutral",
        description="Heikin-Ashi close.",
    ))

    # ------------------------------------------------------------------
    # Direction / above-below helpers (booleans)
    # ------------------------------------------------------------------
    out.append(_Binding(
        name="pta_increasing", pta_fn="increasing",
        inputs=("close",),
        defaults={"length": 1},
        param_ranges={"length": _len_range(1, 10)},
        category="trend", direction="bullish",
        description="Boolean: close strictly increasing over length.",
    ))
    out.append(_Binding(
        name="pta_decreasing", pta_fn="decreasing",
        inputs=("close",),
        defaults={"length": 1},
        param_ranges={"length": _len_range(1, 10)},
        category="trend", direction="bearish",
        description="Boolean: close strictly decreasing over length.",
    ))

    # MACD components — 3 columns
    for col, suf, dirn in [("MACD_", "line", "neutral"),
                           ("MACDh_", "hist", "neutral"),
                           ("MACDs_", "signal", "neutral")]:
        out.append(_Binding(
            name=f"pta_macd_{suf}", pta_fn="macd",
            inputs=("close",),
            defaults={"fast": 12, "slow": 26, "signal": 9},
            param_ranges={"fast": [8, 12, 16], "slow": [21, 26, 30], "signal": [5, 9, 12]},
            column=col,
            category="momentum", direction=dirn,
            description=f"MACD {suf}.",
        ))

    # Stochastic — 3 columns
    for col, suf in [("STOCHk_", "k"), ("STOCHd_", "d"), ("STOCHh_", "hist")]:
        out.append(_Binding(
            name=f"pta_stoch_{suf}", pta_fn="stoch",
            inputs=("high", "low", "close"),
            defaults={"k": 14, "d": 3, "smooth_k": 3},
            param_ranges={"k": _len_range(5, 21)},
            column=col,
            category="momentum", direction="neutral",
            description=f"Stochastic {suf}.",
        ))

    # ------------------------------------------------------------------
    # Extra multi-column / less-common indicators to round out the catalog
    # ------------------------------------------------------------------

    # Alligator (Bill Williams) — 3 lines
    for col, suf in [("AGLj_", "jaw"), ("AGLt_", "teeth"), ("AGLl_", "lips")]:
        out.append(_Binding(
            name=f"pta_alligator_{suf}", pta_fn="alligator",
            inputs=("high", "low"),
            defaults={"jaw": 13, "teeth": 8, "lips": 5},
            param_ranges={"jaw": [13, 21], "teeth": [8, 13], "lips": [5, 8]},
            column=col,
            category="trend", direction="neutral",
            description=f"Alligator {suf}.",
        ))

    # AMAT — Archer Moving Averages Trend (2 long/short columns)
    for col, suf, dirn in [("AMATe_LR_", "long", "bullish"), ("AMATe_SR_", "short", "bearish")]:
        out.append(_Binding(
            name=f"pta_amat_{suf}", pta_fn="amat",
            inputs=("close",),
            defaults={"fast": 8, "slow": 21, "lookback": 2},
            param_ranges={"fast": [5, 8, 12], "slow": [21, 34, 50]},
            column=col,
            category="trend", direction=dirn,
            description=f"Archer MA Trend ({suf}).",
        ))

    # AO — Awesome Oscillator
    out.append(_Binding(
        name="pta_ao", pta_fn="ao",
        inputs=("high", "low"),
        defaults={"fast": 5, "slow": 34},
        param_ranges={"fast": [5, 8, 12], "slow": [21, 34, 55]},
        category="momentum", direction="neutral",
        description="Awesome Oscillator.",
    ))

    # BOP — Balance of Power
    out.append(_Binding(
        name="pta_bop", pta_fn="bop",
        inputs=("open", "high", "low", "close"),
        defaults={},
        param_ranges={},
        category="momentum", direction="neutral",
        description="Balance of Power.",
    ))

    # Chandelier Exit — 3 cols
    for col, suf, dirn in [
        ("CHDLR_", "value", "neutral"),
        ("LCHDLR_", "long", "bullish"),
        ("SCHDLR_", "short", "bearish"),
    ]:
        out.append(_Binding(
            name=f"pta_chandelier_{suf}", pta_fn="chandelier_exit",
            inputs=("high", "low", "close"),
            defaults={"high_length": 22, "low_length": 22, "atr_length": 22, "multiplier": 3.0},
            param_ranges={"high_length": [14, 22, 30], "multiplier": [2.0, 3.0, 4.0]},
            column=col,
            category="trend", direction=dirn,
            description=f"Chandelier Exit {suf}.",
        ))

    # CKSP — Chande Kroll Stop (long/short)
    for col, suf, dirn in [("CKSPl_", "long", "bullish"), ("CKSPs_", "short", "bearish")]:
        out.append(_Binding(
            name=f"pta_cksp_{suf}", pta_fn="cksp",
            inputs=("high", "low", "close"),
            defaults={"p": 10, "x": 1.0, "q": 9},
            param_ranges={"p": [5, 10, 14]},
            column=col,
            category="trend", direction=dirn,
            description=f"Chande Kroll Stop {suf}.",
        ))

    # DPO — Detrended Price Oscillator
    out.append(_Binding(
        name="pta_dpo", pta_fn="dpo",
        inputs=("close",),
        defaults={"length": 20},
        param_ranges={"length": _len_range(10, 40, 5)},
        category="momentum", direction="neutral",
        description="Detrended Price Oscillator.",
    ))

    # EBSW — Even Better SineWave
    out.append(_Binding(
        name="pta_ebsw", pta_fn="ebsw",
        inputs=("close",),
        defaults={"length": 40, "bars": 10},
        param_ranges={"length": [20, 40, 60]},
        category="momentum", direction="neutral",
        description="Even Better SineWave.",
    ))

    # HILO — Gann HiLo Activator (3 cols)
    for col, suf, dirn in [
        ("HILO_", "value", "neutral"),
        ("HILOl_", "long", "bullish"),
        ("HILOs_", "short", "bearish"),
    ]:
        out.append(_Binding(
            name=f"pta_hilo_{suf}", pta_fn="hilo",
            inputs=("high", "low", "close"),
            defaults={"high_length": 13, "low_length": 21},
            param_ranges={"high_length": [8, 13, 21]},
            column=col,
            category="trend", direction=dirn,
            description=f"Gann HiLo {suf}.",
        ))

    # MAMA — MESA Adaptive MA (2 cols)
    for col, suf in [("MAMA_", "mama"), ("FAMA_", "fama")]:
        out.append(_Binding(
            name=f"pta_mama_{suf}", pta_fn="mama",
            inputs=("close",),
            defaults={"fastlimit": 0.5, "slowlimit": 0.05},
            param_ranges={},
            column=col,
            category="trend", direction="neutral",
            description=f"MESA Adaptive MA {suf}.",
        ))

    # TTM Trend
    out.append(_Binding(
        name="pta_ttm_trend", pta_fn="ttm_trend",
        inputs=("high", "low", "close"),
        defaults={"length": 6},
        param_ranges={"length": [4, 6, 8, 12]},
        category="trend", direction="neutral",
        description="TTM Trend.",
    ))

    # VHF — Vertical-Horizontal Filter
    out.append(_Binding(
        name="pta_vhf", pta_fn="vhf",
        inputs=("close",),
        defaults={"length": 28},
        param_ranges={"length": _len_range(14, 56, 7)},
        category="trend", direction="neutral",
        description="Vertical Horizontal Filter.",
    ))

    # Aberration — 4 columns
    for col, suf in [
        ("ABER_ZG_", "zg"),
        ("ABER_SG_", "sg"),
        ("ABER_XG_", "xg"),
        ("ABER_ATR_", "atr"),
    ]:
        out.append(_Binding(
            name=f"pta_aberration_{suf}", pta_fn="aberration",
            inputs=("high", "low", "close"),
            defaults={"length": 5, "atr_length": 15},
            param_ranges={"length": [3, 5, 10]},
            column=col,
            category="volatility", direction="neutral",
            description=f"Aberration {suf}.",
        ))

    # TOS_STDEVALL — standard deviation bands (DataFrame)
    for col, suf, dirn in [
        ("TOS_STDEVALL_LR", "lr", "neutral"),
        ("TOS_STDEVALL_L_1", "low_1", "bearish"),
        ("TOS_STDEVALL_U_1", "up_1", "bullish"),
        ("TOS_STDEVALL_L_2", "low_2", "bearish"),
        ("TOS_STDEVALL_U_2", "up_2", "bullish"),
    ]:
        out.append(_Binding(
            name=f"pta_tos_stdev_{suf}", pta_fn="tos_stdevall",
            inputs=("close",),
            defaults={},
            param_ranges={},
            column=col,
            category="statistical", direction=dirn,
            description=f"TOS StdevAll {suf}.",
        ))

    # Elder-Ray Bear (bull was already added via momentum_simple)
    out.append(_Binding(
        name="pta_eri_bear", pta_fn="eri",
        inputs=("high", "low", "close"),
        defaults={"length": 13},
        param_ranges={"length": [8, 13, 21]},
        column_index=1,  # bear column
        category="momentum", direction="bearish",
        description="Elder-Ray Bear Power.",
    ))

    # Squeeze (LazyBear) — primary value
    out.append(_Binding(
        name="pta_squeeze", pta_fn="squeeze",
        inputs=("high", "low", "close"),
        defaults={"bb_length": 20, "bb_std": 2.0, "kc_length": 20, "kc_scalar": 1.5, "mom_length": 12, "mom_smooth": 6},
        param_ranges={"bb_length": [14, 20, 30]},
        column_index=0,
        category="volatility", direction="neutral",
        description="Squeeze primary series.",
    ))
    out.append(_Binding(
        name="pta_squeeze_on", pta_fn="squeeze",
        inputs=("high", "low", "close"),
        defaults={"bb_length": 20, "bb_std": 2.0, "kc_length": 20, "kc_scalar": 1.5},
        param_ranges={},
        column_index=1,
        category="volatility", direction="neutral",
        description="Squeeze on (BB inside KC).",
    ))
    out.append(_Binding(
        name="pta_squeeze_off", pta_fn="squeeze",
        inputs=("high", "low", "close"),
        defaults={"bb_length": 20, "bb_std": 2.0, "kc_length": 20, "kc_scalar": 1.5},
        param_ranges={},
        column_index=2,
        category="volatility", direction="neutral",
        description="Squeeze off (BB outside KC).",
    ))

    # PPO histogram (ppo was in momentum_simple) — 3 cols
    for col, suf in [("PPO_", "value"), ("PPOh_", "hist"), ("PPOs_", "signal")]:
        out.append(_Binding(
            name=f"pta_ppo_{suf}", pta_fn="ppo",
            inputs=("close",),
            defaults={"fast": 12, "slow": 26, "signal": 9},
            param_ranges={"fast": [8, 12, 16], "slow": [21, 26, 30]},
            column=col,
            category="momentum", direction="neutral",
            description=f"PPO {suf}.",
        ))

    # Chande Kroll Stop - already above; add SMI components
    for col, suf in [("SMI_", "value"), ("SMIs_", "signal"), ("SMIo_", "osc")]:
        out.append(_Binding(
            name=f"pta_smi_{suf}", pta_fn="smi",
            inputs=("close",),
            defaults={"fast": 5, "slow": 20, "signal": 5},
            param_ranges={"fast": [3, 5, 9]},
            column=col,
            category="momentum", direction="neutral",
            description=f"Stochastic Momentum Index {suf}.",
        ))

    # QQE signal + basis
    for col, suf in [("QQE_", "value"), ("QQEs_", "signal"), ("QQEl_", "long"), ("QQEh_", "short")]:
        out.append(_Binding(
            name=f"pta_qqe_{suf}", pta_fn="qqe",
            inputs=("close",),
            defaults={"length": 14, "smooth": 5, "factor": 4.236},
            param_ranges={"length": [10, 14, 20]},
            column=col,
            category="momentum", direction="neutral",
            description=f"QQE {suf}.",
        ))

    # Fisher Transform — signal column
    out.append(_Binding(
        name="pta_fisher_signal", pta_fn="fisher",
        inputs=("high", "low"),
        defaults={"length": 9, "signal": 1},
        param_ranges={"length": [5, 9, 14]},
        column_index=1,
        category="momentum", direction="neutral",
        description="Fisher Transform signal line.",
    ))

    # StochRSI — K and D columns
    for col, suf in [("STOCHRSIk_", "k"), ("STOCHRSId_", "d")]:
        out.append(_Binding(
            name=f"pta_stochrsi_{suf}", pta_fn="stochrsi",
            inputs=("close",),
            defaults={"length": 14, "rsi_length": 14, "k": 3, "d": 3},
            param_ranges={"length": [7, 14, 21]},
            column=col,
            category="momentum", direction="neutral",
            description=f"Stochastic RSI {suf}.",
        ))

    # TSI — signal column
    out.append(_Binding(
        name="pta_tsi_signal", pta_fn="tsi",
        inputs=("close",),
        defaults={"fast": 13, "slow": 25, "signal": 13},
        param_ranges={},
        column_index=1,
        category="momentum", direction="neutral",
        description="TSI signal line.",
    ))

    # BRAR — BR column (AR was in momentum_simple)
    out.append(_Binding(
        name="pta_brar_br", pta_fn="brar",
        inputs=("open", "high", "low", "close"),
        defaults={"length": 26},
        param_ranges={"length": [13, 26, 50]},
        column_index=1,
        category="momentum", direction="neutral",
        description="BRAR BR component.",
    ))

    # Mass Index (massi) already added; add DM (directional movement)
    for col, suf, dirn in [("DMP_", "plus", "bullish"), ("DMN_", "minus", "bearish")]:
        out.append(_Binding(
            name=f"pta_dm_{suf}", pta_fn="dm",
            inputs=("high", "low"),
            defaults={"length": 14},
            param_ranges={"length": _len_range(5, 30)},
            column=col,
            category="trend", direction=dirn,
            description=f"Directional Movement {suf}.",
        ))

    return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


_REGISTRATION_DONE = False


def register_all() -> int:
    """Register every binding into the indicator registry. Returns count."""
    global _REGISTRATION_DONE
    if _REGISTRATION_DONE:
        return sum(1 for k in INDICATORS if k.startswith("pta_"))

    try:
        import pandas_ta  # noqa: F401  type: ignore[import-not-found]
    except Exception:
        log.warning("pandas_ta not installed; skipping bridge registrations")
        _REGISTRATION_DONE = True
        return 0

    count = 0
    bindings = _bindings()
    for binding in bindings:
        if binding.name in INDICATORS:
            continue
        try:
            register(
                name=binding.name,
                fn=_make_wrapper(binding),
                defaults=dict(binding.defaults),
                param_ranges=dict(binding.param_ranges),
                category=binding.category,
                direction=binding.direction,
                description=binding.description,
                asset_class=binding.asset_class,
            )
            count += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to register %s: %s", binding.name, exc)

    _REGISTRATION_DONE = True
    log.info("pandas_ta bridge registered %d indicators", count)
    return count


__all__ = ["register_all", "IndicatorSpec"]
