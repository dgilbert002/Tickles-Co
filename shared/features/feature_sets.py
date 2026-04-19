"""Built-in feature views for Phase 20.

Three starter views — more can be added in later phases without
schema changes. All three consume a canonical OHLCV dataframe and
emit per-bar feature vectors.

  * ``returns_basic``      — log returns + rolling momentum.
  * ``volatility_basic``   — rolling stdev + ATR + rolling range.
  * ``microstructure_basic`` — volume z-score + body/wick ratios.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from shared.features.registry import register_feature_view
from shared.features.schema import Entity, Feature, FeatureDtype, FeatureView

_ASSET = Entity(
    name="asset",
    description="Trading symbol at a specific venue (e.g. binance:BTC/USDT).",
    join_keys=["asset"],
)


# ----------------------------- returns --------------------------------


def _returns_basic(
    df: pd.DataFrame, entity_key: str, params: Dict[str, Any]  # noqa: ARG001
) -> pd.DataFrame:
    close = df["close"].astype(float)
    log_ret_1 = np.log(close / close.shift(1))
    log_ret_5 = np.log(close / close.shift(5))
    log_ret_15 = np.log(close / close.shift(15))
    mom_1h = close.pct_change(60)
    mom_4h = close.pct_change(240)
    out = pd.DataFrame(
        {
            "log_ret_1m": log_ret_1,
            "log_ret_5m": log_ret_5,
            "log_ret_15m": log_ret_15,
            "mom_1h_pct": mom_1h,
            "mom_4h_pct": mom_4h,
        },
        index=df.index,
    )
    return out


FV_RETURNS = FeatureView(
    name="returns_basic",
    entities=[_ASSET],
    features=[
        Feature("log_ret_1m", FeatureDtype.FLOAT, "Log return, 1 bar (1m)."),
        Feature("log_ret_5m", FeatureDtype.FLOAT, "Log return over 5 bars."),
        Feature("log_ret_15m", FeatureDtype.FLOAT, "Log return over 15 bars."),
        Feature("mom_1h_pct", FeatureDtype.FLOAT, "% change over 60 bars."),
        Feature("mom_4h_pct", FeatureDtype.FLOAT, "% change over 240 bars."),
    ],
    compute=_returns_basic,
    description="Basic return + short-horizon momentum features.",
    tags={"category": "returns"},
)


# --------------------------- volatility -------------------------------


def _volatility_basic(
    df: pd.DataFrame, entity_key: str, params: Dict[str, Any]  # noqa: ARG001
) -> pd.DataFrame:
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    prev_close = close.shift(1)

    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr_14 = tr.rolling(14).mean()
    ret = close.pct_change()
    std_30 = ret.rolling(30).std(ddof=0)
    std_240 = ret.rolling(240).std(ddof=0)
    range_30 = (high.rolling(30).max() - low.rolling(30).min()) / close
    out = pd.DataFrame(
        {
            "atr_14": atr_14,
            "ret_std_30": std_30,
            "ret_std_240": std_240,
            "range_30_pct": range_30,
        },
        index=df.index,
    )
    return out


FV_VOLATILITY = FeatureView(
    name="volatility_basic",
    entities=[_ASSET],
    features=[
        Feature("atr_14", FeatureDtype.FLOAT, "Average True Range, 14 bars."),
        Feature("ret_std_30", FeatureDtype.FLOAT, "Return stdev, 30 bars."),
        Feature("ret_std_240", FeatureDtype.FLOAT, "Return stdev, 240 bars."),
        Feature("range_30_pct", FeatureDtype.FLOAT, "30-bar HL range, fraction of close."),
    ],
    compute=_volatility_basic,
    description="Short + medium horizon volatility features.",
    tags={"category": "volatility"},
)


# ------------------------- microstructure ----------------------------


def _microstructure_basic(
    df: pd.DataFrame, entity_key: str, params: Dict[str, Any]  # noqa: ARG001
) -> pd.DataFrame:
    open_ = df["open"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)

    body = (close - open_).abs()
    hl_range = (high - low).replace(0, np.nan)
    body_frac = (body / hl_range).clip(0, 1)
    upper_wick = (high - close.where(close >= open_, open_)) / hl_range
    lower_wick = (close.where(close <= open_, open_) - low) / hl_range

    vol_mean = vol.rolling(30).mean()
    vol_std = vol.rolling(30).std(ddof=0).replace(0, np.nan)
    vol_z = (vol - vol_mean) / vol_std

    out = pd.DataFrame(
        {
            "body_frac": body_frac,
            "upper_wick_frac": upper_wick,
            "lower_wick_frac": lower_wick,
            "vol_z_30": vol_z,
        },
        index=df.index,
    )
    return out


FV_MICROSTRUCTURE = FeatureView(
    name="microstructure_basic",
    entities=[_ASSET],
    features=[
        Feature("body_frac", FeatureDtype.FLOAT, "|close-open|/(high-low)."),
        Feature("upper_wick_frac", FeatureDtype.FLOAT, "Upper wick as fraction of HL range."),
        Feature("lower_wick_frac", FeatureDtype.FLOAT, "Lower wick as fraction of HL range."),
        Feature("vol_z_30", FeatureDtype.FLOAT, "Volume z-score over 30 bars."),
    ],
    compute=_microstructure_basic,
    description="Candle body/wick geometry + volume anomaly features.",
    tags={"category": "microstructure"},
)


# --------------------------- registration ----------------------------


def register_all() -> None:
    register_feature_view(FV_RETURNS)
    register_feature_view(FV_VOLATILITY)
    register_feature_view(FV_MICROSTRUCTURE)
