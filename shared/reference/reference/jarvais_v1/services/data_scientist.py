"""
JarvAIs Data Scientist Agent
Computes technical indicators from raw candle data on demand using TA-Lib.
Exposes an "indicator manifest" so Stage 2 knows what analytical tools are available.

Standard: RSI, EMA, SMA, MACD, Bollinger, ATR, CCI, Volume Profile
Smart Money: Fibonacci OTE (Goldilocks/Discount), Order Blocks, FVG, Anchored VWAP
Session: ORB, Session Levels, Session Sweeps
Exhaustion: TD Sequential, Divergence, Volume Climax
Detection: Sell-off, Momentum Burst, Volume Trend
Order Flow: Relative Volume, Delta (approximated), Absorption Detection
"""

import logging
import time as _time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dt_time, timezone

import talib
from typing import Optional, Dict, Any, List, Tuple
from functools import lru_cache

logger = logging.getLogger("jarvais.data_scientist")


def _utcnow() -> datetime:
    """Naive-UTC now — no DeprecationWarning, compatible with MySQL datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


import threading as _threading
_ta_cache: Dict[str, Dict] = {}
_ta_cache_lock = _threading.Lock()
_TA_CACHE_TTL = 900  # 15 minutes


# ═══════════════════════════════════════════════════════════════════════
# INDICATOR MANIFEST (exposed to Stage 2 prompt)
# ═══════════════════════════════════════════════════════════════════════

INDICATOR_MANIFEST = {
    "library": "TA-Lib (C) + NumPy/Pandas",
    "capabilities": [
        {"name": "RSI", "params": "period (default 14)", "output": "RSI value 0-100, overbought/oversold status"},
        {"name": "EMA", "params": "periods: 9, 20, 50, 200", "output": "EMA values, golden/death cross, price position"},
        {"name": "SMA", "params": "periods: 20, 50, 200", "output": "SMA values"},
        {"name": "MACD", "params": "fast=12, slow=26, signal=9", "output": "MACD line, signal, histogram, cross status"},
        {"name": "Bollinger Bands", "params": "period=20, std=2", "output": "upper/middle/lower bands, squeeze detection"},
        {"name": "ATR", "params": "period=14", "output": "ATR value, volatility classification"},
        {"name": "CCI", "params": "period=20", "output": "CCI value, overbought/oversold status"},
        {"name": "Fibonacci OTE", "params": "auto-detected swing", "output": "Levels + Goldilocks(0.618-0.65) + Discount(0.786-0.83) zones"},
        {"name": "Anchored VWAP", "params": "from swing or sell-off", "output": "VWAP value, premium/discount zone"},
        {"name": "Volume Profile", "params": "num_bins=20", "output": "POC, Value Area High/Low, price vs POC"},
        {"name": "Volume Trend", "params": "per timeframe", "output": "increasing/decreasing/spike across M5/M15/H1"},
        {"name": "Volume Climax", "params": "auto", "output": "Exhaustion detection (volume dropping on continuation)"},
        {"name": "ORB", "params": "Asia/London/NY session", "output": "ORB high/low, breakout/failure status per session"},
        {"name": "Session Levels", "params": "Asia/London/NY", "output": "Session high/low/open + sweep detection"},
        {"name": "TD Sequential", "params": "auto", "output": "Setup count (1-13), buy/sell exhaustion (blue dot)"},
        {"name": "Divergence", "params": "RSI or MACD vs price", "output": "Bullish/bearish divergence detection"},
        {"name": "Order Blocks", "params": "from structure breaks", "output": "Bullish/bearish OB zones with timing"},
        {"name": "Fair Value Gaps", "params": "3-candle patterns", "output": "FVG zones with fill status"},
        {"name": "Sell-off Detection", "params": "multi-timeframe", "output": "Tier (pullback/correction/crash), % drop, recovery status"},
        {"name": "Momentum Burst", "params": "lookback candles", "output": "Rapid move detection, direction, magnitude"},
        {"name": "Change of Character", "params": "swing detection", "output": "CHoCH events — break of recent swing H/L suggesting trend reversal"},
        {"name": "Break of Structure", "params": "swing detection", "output": "BOS events — higher high (bullish) or lower low (bearish) confirming trend"},
        {"name": "Monday Levels", "params": "weekly anchor", "output": "Monday open, Monday high, Monday low — key weekly anchor levels"},
        {"name": "Previous Day Levels", "params": "D1", "output": "PDH (Previous Day High), PDL (Previous Day Low), PDO (Previous Day Open)"},
        {"name": "Previous Week Levels", "params": "W1", "output": "PWH (Previous Week High), PWL (Previous Week Low)"},
        {"name": "Liquidity Grab", "params": "swing + wick analysis", "output": "Wick beyond key level that immediately rejects — potential entry signal"},
        {"name": "AMD Cycle", "params": "session-based", "output": "Accumulation / Manipulation / Distribution phase detection per session"},
        {"name": "POC", "params": "volume_profile, bins=20", "output": "Point of Control — highest volume price node"},
        {"name": "VAH VAL", "params": "volume_profile, value_area_pct=0.7", "output": "Value Area High and Low (70%% of traded volume range)"},
        {"name": "Relative Volume", "params": "lookback=20", "output": "R-Vol ratio (current vs average), spike detection — mirrors Aggr R-Vol panel"},
        {"name": "Delta", "params": "approximated from OHLCV", "output": "Buy/sell pressure per candle, cumulative delta, delta bars — mirrors Aggr Delta Spot"},
        {"name": "Absorption", "params": "delta + price action at extremes", "output": "Detects high volume/delta failing to move price at key levels — core Aggr signal"},
    ]
}


def get_indicator_manifest_text() -> str:
    """Return a text version of the manifest for LLM prompts."""
    lines = ["## AVAILABLE TECHNICAL INDICATORS (Data Scientist Agent)", ""]
    lines.append("The following indicators can be computed on demand from raw candle data:")
    lines.append("You may request any of these in your follow-up tracker responses.\n")
    for cap in INDICATOR_MANIFEST["capabilities"]:
        lines.append(f"- **{cap['name']}** ({cap['params']}): {cap['output']}")
    return "\n".join(lines)


def get_indicator_manifest_compact() -> str:
    """Compact manifest listing request keys for the Tracker system prompt."""
    lines = []
    for cap in INDICATOR_MANIFEST["capabilities"]:
        key = cap["name"].lower().replace(" ", "_").replace("-", "")
        mapped = INDICATOR_NAME_MAP.get(key, [key])
        lines.append(f"  {mapped[0]:20s} — {cap['name']} ({cap['params']})")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# INDICATOR NAME NORMALIZATION (Tracker ↔ BillNye protocol)
# ═══════════════════════════════════════════════════════════════════════

ALL_VALID_INDICATORS = {
    "rsi", "ema", "sma", "macd", "bollinger", "atr", "cci", "fibonacci",
    "vwap", "td_sequential", "divergence", "order_blocks", "fvg",
    "volume_profile", "volume_trend", "volume_climax", "orb",
    "session_levels", "selloff", "momentum_burst",
    "relative_volume", "delta", "absorption",
}

INDICATOR_NAME_MAP = {
    "rsi": ["rsi"], "ema": ["ema"], "sma": ["sma"], "macd": ["macd"],
    "bollinger": ["bollinger"], "bollinger bands": ["bollinger"],
    "bollinger_bands": ["bollinger"], "bb": ["bollinger"], "boll": ["bollinger"],
    "atr": ["atr"], "cci": ["cci"],
    "fibonacci": ["fibonacci"], "fibonacci ote": ["fibonacci"],
    "fibonacci_ote": ["fibonacci"], "fib": ["fibonacci"], "fib_ote": ["fibonacci"],
    "vwap": ["vwap"], "anchored vwap": ["vwap"], "anchored_vwap": ["vwap"],
    "td_sequential": ["td_sequential"], "td sequential": ["td_sequential"],
    "td": ["td_sequential"], "tds": ["td_sequential"],
    "divergence": ["divergence"], "div": ["divergence"],
    "order_blocks": ["order_blocks"], "order blocks": ["order_blocks"],
    "ob": ["order_blocks"],
    "fvg": ["fvg"], "fair value gaps": ["fvg"], "fair_value_gaps": ["fvg"],
    "volume_profile": ["volume_profile"], "volume profile": ["volume_profile"],
    "vol_profile": ["volume_profile"],
    "volume_trend": ["volume_trend"], "volume trend": ["volume_trend"],
    "vol_trend": ["volume_trend"],
    "volume_climax": ["volume_climax"], "volume climax": ["volume_climax"],
    "volume": ["volume_profile", "volume_trend", "volume_climax"],
    "orb": ["orb"],
    "session_levels": ["session_levels"], "session levels": ["session_levels"],
    "session": ["session_levels", "orb"],
    "selloff": ["selloff"], "sell-off": ["selloff"],
    "sell-off detection": ["selloff"], "selloff_detection": ["selloff"],
    "momentum_burst": ["momentum_burst"], "momentum burst": ["momentum_burst"],
    "momentum": ["momentum_burst"],
    "support_resistance": ["fibonacci", "session_levels", "order_blocks"],
    "support resistance": ["fibonacci", "session_levels", "order_blocks"],
    "s/r": ["fibonacci", "session_levels", "order_blocks"],
    "sr_levels": ["fibonacci", "session_levels", "order_blocks"],
    "levels": ["fibonacci", "session_levels"],
    "choch": ["choch"], "change_of_character": ["choch"],
    "change of character": ["choch"],
    "bos": ["bos"], "break_of_structure": ["bos"],
    "break of structure": ["bos"],
    "monday_levels": ["monday_levels"], "monday levels": ["monday_levels"],
    "monday": ["monday_levels"],
    "pdh": ["prev_day_levels"], "pdl": ["prev_day_levels"],
    "previous_day_levels": ["prev_day_levels"], "previous day levels": ["prev_day_levels"],
    "prev_day": ["prev_day_levels"],
    "pwh": ["prev_week_levels"], "pwl": ["prev_week_levels"],
    "previous_week_levels": ["prev_week_levels"], "previous week levels": ["prev_week_levels"],
    "prev_week": ["prev_week_levels"],
    "liquidity_grab": ["liquidity_grab"], "liquidity grab": ["liquidity_grab"],
    "liquidity": ["liquidity_grab"],
    "amd": ["amd_cycle"], "amd_cycle": ["amd_cycle"], "amd cycle": ["amd_cycle"],
    "poc": ["poc"], "point_of_control": ["poc"], "point of control": ["poc"],
    "vah": ["vah_val"], "val": ["vah_val"], "vah_val": ["vah_val"],
    "value_area": ["vah_val"], "value area": ["vah_val"],
    "smc": ["choch", "bos", "liquidity_grab", "order_blocks", "fvg", "amd_cycle"],
    "smart_money": ["choch", "bos", "liquidity_grab", "order_blocks", "fvg", "amd_cycle"],
    "smart money": ["choch", "bos", "liquidity_grab", "order_blocks", "fvg", "amd_cycle"],
    "relative_volume": ["relative_volume"], "relative volume": ["relative_volume"],
    "r_vol": ["relative_volume"], "rvol": ["relative_volume"], "r-vol": ["relative_volume"],
    "delta": ["delta"], "delta_spot": ["delta"], "delta spot": ["delta"],
    "absorption": ["absorption"], "absorb": ["absorption"],
    "order_flow": ["relative_volume", "delta", "absorption"],
    "orderflow": ["relative_volume", "delta", "absorption"],
    "order flow": ["relative_volume", "delta", "absorption"],
    "aggr": ["relative_volume", "delta", "absorption"],
}

VALID_TIMEFRAMES = {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"}


def normalize_indicator_names(raw_requests: list) -> Tuple[List[str], List[str]]:
    """Normalize LLM indicator requests to internal compute keys.

    Accepts both string names and structured dicts with 'indicator' key.
    Returns (valid_keys, unknown_names) — both deduplicated.
    """
    valid = []
    unknown = []
    for req in (raw_requests or []):
        if isinstance(req, dict):
            name = str(req.get("indicator", "")).strip().lower()
        else:
            name = str(req).strip().lower()
        if not name:
            continue
        mapped = INDICATOR_NAME_MAP.get(name)
        if mapped:
            valid.extend(mapped)
        elif name.replace(" ", "_") in ALL_VALID_INDICATORS:
            valid.append(name.replace(" ", "_"))
        else:
            unknown.append(name)
    seen = set()
    deduped = [k for k in valid if not (k in seen or seen.add(k))]
    return deduped, unknown


def parse_ta_requests(ta_reqs_raw: list) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
    """Parse structured or simple ta_requests from Tracker LLM response.

    Handles both formats:
      Old: ["bollinger", "volume"]
      New: [{"indicator": "bollinger", "timeframes": ["M15","H1"], "reason": "..."}]

    Returns:
      (indicator_keys, timeframe_overrides, unknown_names)
      timeframe_overrides: {indicator_key: [timeframes]} only for items that specified them
    """
    names_for_normalize = []
    tf_overrides: Dict[str, List[str]] = {}

    for r in (ta_reqs_raw or []):
        if isinstance(r, dict):
            ind = r.get("indicator", "")
            names_for_normalize.append(ind)
            tfs = r.get("timeframes", [])
            if tfs and isinstance(tfs, list):
                clean_tfs = [t.upper() for t in tfs if t.upper() in VALID_TIMEFRAMES]
                if clean_tfs:
                    tf_overrides[ind.strip().lower()] = clean_tfs
        else:
            names_for_normalize.append(str(r))

    valid_keys, unknown = normalize_indicator_names(names_for_normalize)
    return valid_keys, tf_overrides, unknown


# ═══════════════════════════════════════════════════════════════════════
# CORE TA ENGINE
# ═══════════════════════════════════════════════════════════════════════

class DataScientist:
    """Computes technical analysis indicators from candle data."""

    def __init__(self, db=None):
        self.db = db

    def compute_all(self, symbol: str, candles_by_tf: Dict[str, List[Dict]],
                    requested: List[str] = None) -> Dict[str, Any]:
        """
        Run requested indicators (or all) on the provided candle data.
        candles_by_tf: {"M5": [...], "M15": [...], "H1": [...], ...}
        Returns structured results dict.
        """
        global _ta_cache

        # -- cache key: symbol + sorted TFs + candle counts + latest close per TF --
        key_parts = [symbol]
        for tf in sorted(candles_by_tf.keys()):
            clist = candles_by_tf[tf]
            cnt = len(clist) if clist else 0
            last_close = str(clist[-1].get("close", "")) if clist else ""
            key_parts.append(f"{tf}:{cnt}:{last_close}")
        if requested:
            key_parts.append("|".join(sorted(requested)))
        cache_key = "::".join(key_parts)

        now = _time.time()
        with _ta_cache_lock:
            entry = _ta_cache.get(cache_key)
            if entry and now - entry["ts"] < _TA_CACHE_TTL:
                logger.debug(f"[DataScientist] Cache HIT for {symbol} (age {now - entry['ts']:.0f}s)")
                return entry["result"]

            if len(_ta_cache) > 200:
                stale_keys = [k for k, v in _ta_cache.items() if now - v["ts"] >= _TA_CACHE_TTL]
                for k in stale_keys:
                    del _ta_cache[k]

        results = {"symbol": symbol, "computed_at": _utcnow().isoformat()}

        for tf, candles in candles_by_tf.items():
            if not candles or len(candles) < 5:
                continue
            df = self._candles_to_df(candles)
            if df.empty:
                continue

            tf_results = {}
            all_indicators = not requested

            req = set(requested or [])

            if all_indicators or "rsi" in req:
                tf_results["rsi"] = self._compute_rsi(df)
            if all_indicators or "ema" in req:
                tf_results["ema"] = self._compute_ema(df)
            if all_indicators or "sma" in req:
                tf_results["sma"] = self._compute_sma(df)
            if all_indicators or "macd" in req:
                tf_results["macd"] = self._compute_macd(df)
            if all_indicators or "bollinger" in req:
                tf_results["bollinger"] = self._compute_bollinger(df)
            if all_indicators or "atr" in req:
                tf_results["atr"] = self._compute_atr(df)
            if all_indicators or "cci" in req:
                tf_results["cci"] = self._compute_cci(df)
            if all_indicators or "fibonacci" in req:
                tf_results["fibonacci"] = self._compute_fibonacci(df)
            if all_indicators or "vwap" in req:
                tf_results["vwap"] = self._compute_anchored_vwap(df)
            if all_indicators or "td_sequential" in req:
                tf_results["td_sequential"] = self._compute_td_sequential(df)
            if all_indicators or "divergence" in req:
                tf_results["divergence"] = self._detect_divergence(df)
            if all_indicators or "order_blocks" in req:
                tf_results["order_blocks"] = self._detect_order_blocks(df)
            if all_indicators or "fvg" in req:
                tf_results["fvg"] = self._detect_fair_value_gaps(df)
            if all_indicators or "volume_profile" in req:
                tf_results["volume_profile"] = self._compute_volume_profile(df)
            if all_indicators or "volume_trend" in req:
                tf_results["volume_trend"] = self._compute_volume_trend(df)
            if all_indicators or "volume_climax" in req:
                tf_results["volume_climax"] = self._detect_volume_climax(df)
            if all_indicators or "selloff" in req:
                tf_results["selloff"] = self._detect_selloff(df)
            if all_indicators or "momentum_burst" in req:
                tf_results["momentum_burst"] = self._detect_momentum_burst(df)

            # Order flow indicators (Aggr-equivalent)
            if "relative_volume" in req:
                tf_results["relative_volume"] = self._compute_relative_volume(df)
            if "delta" in req:
                tf_results["delta"] = self._compute_delta(df)
            if "absorption" in req:
                tf_results["absorption"] = self._detect_absorption(df)

            if tf in ("M5", "M15", "M30", "H1"):
                if all_indicators or "orb" in req:
                    tf_results["orb"] = self._compute_orb(df)
                if all_indicators or "session_levels" in req:
                    tf_results["session_levels"] = self._compute_session_levels(df)
                if all_indicators or "amd_cycle" in req:
                    tf_results["amd_cycle"] = self._detect_amd_cycle(df)

            # SMC indicators (all timeframes)
            if "choch" in req:
                tf_results["choch"] = self._detect_choch(df)
            if "bos" in req:
                tf_results["bos"] = self._detect_bos(df)
            if "liquidity_grab" in req:
                tf_results["liquidity_grab"] = self._detect_liquidity_grab(df)
            if "poc" in req:
                tf_results["poc"] = self._compute_poc(df)
            if "vah_val" in req:
                tf_results["vah_val"] = self._compute_vah_val(df)

            # Day/week levels (need D1/H4/H1 candles)
            if tf in ("D1", "H4", "H1"):
                if "monday_levels" in req:
                    tf_results["monday_levels"] = self._compute_monday_levels(df)
                if "prev_day_levels" in req:
                    tf_results["prev_day_levels"] = self._compute_prev_day_levels(df)
                if "prev_week_levels" in req:
                    tf_results["prev_week_levels"] = self._compute_prev_week_levels(df)

            results[tf] = tf_results

        with _ta_cache_lock:
            _ta_cache[cache_key] = {"ts": _time.time(), "result": results}
        logger.debug(f"[DataScientist] Cache STORE for {symbol} (cache size={len(_ta_cache)})")
        return results

    def compute_for_tracker(self, symbol: str, candles_by_tf: Dict,
                            ta_requests: List[str] = None) -> Dict:
        """Compute specific indicators requested by the tracker/Stage 2."""
        return self.compute_all(symbol, candles_by_tf, ta_requests)

    def compute_batch(self, requests: List[Dict]) -> List[Dict]:
        """Compute multiple indicator requests efficiently.
        Each request: {"symbol": str, "timeframe": str, "indicator": str, "params": {}}
        """
        results = []
        for req in requests:
            symbol = req.get("symbol", "?")
            tf = req.get("timeframe", "M15")
            indicator = req.get("indicator", "")
            candles = self.get_candles_from_db(symbol, {tf: 168})
            if tf in candles:
                df = self._candles_to_df(candles[tf])
                method = getattr(self, f"_compute_{indicator}", None) or \
                         getattr(self, f"_detect_{indicator}", None)
                if method and not df.empty:
                    results.append({"symbol": symbol, "timeframe": tf,
                                    "indicator": indicator, "result": method(df)})
                else:
                    results.append({"symbol": symbol, "timeframe": tf,
                                    "indicator": indicator, "result": {"error": "unknown_indicator"}})
            else:
                results.append({"symbol": symbol, "timeframe": tf,
                                "indicator": indicator, "result": {"error": "no_candle_data"}})
        return results

    def get_candles_from_db(self, symbol: str,
                            timeframes: Dict[str, int] = None,
                            as_of: 'datetime' = None) -> Dict[str, List[Dict]]:
        """Fetch candles from database for multiple timeframes.
        Resolves aliases (GOOGL→GOOG) and tries canonical if symbol has no data.
        If as_of is provided, fetches candles up to that timestamp (for backtesting)."""
        if not self.db:
            return {}
        if not timeframes:
            timeframes = {"M5": 24, "M15": 72, "H1": 264, "H4": 1080, "D1": 6240}

        from db.market_symbols import resolve_symbol as _resolve_alias
        canonical = _resolve_alias(symbol, self.db)
        symbols_to_try = ([canonical, symbol] if canonical != symbol else [symbol])

        result = {}
        for tf, hours in timeframes.items():
            rows = None
            for try_sym in symbols_to_try:
                if as_of:
                    rows = self.db.fetch_all("""
                        SELECT candle_time, open, high, low, close, volume
                        FROM candles
                        WHERE symbol = %s AND timeframe = %s
                        AND candle_time <= %s
                        AND candle_time >= %s - INTERVAL %s HOUR
                        ORDER BY candle_time ASC
                    """, (try_sym, tf, as_of, as_of, hours))
                else:
                    rows = self.db.fetch_all("""
                        SELECT candle_time, open, high, low, close, volume
                        FROM candles
                        WHERE symbol = %s AND timeframe = %s
                        AND candle_time >= NOW() - INTERVAL %s HOUR
                        ORDER BY candle_time ASC
                    """, (try_sym, tf, hours))
                if rows:
                    break
            if rows:
                result[tf] = [
                    {"time": r["candle_time"], "open": float(r["open"]),
                     "high": float(r["high"]), "low": float(r["low"]),
                     "close": float(r["close"]),
                     "volume": float(r.get("volume") or 0)}
                    for r in rows
                ]
        return result

    # ── Helpers ───────────────────────────────────────────────────────

    def _aggregate_d3_candles(self, df_d1: pd.DataFrame) -> pd.DataFrame:
        """Aggregate D1 candles into 3-day windows for macro regime analysis.
        Requires at least 9 D1 rows to produce meaningful results."""
        if df_d1 is None or len(df_d1) < 9:
            return pd.DataFrame()
        df = df_d1.copy()
        if "time" in df.columns:
            df = df.set_index("time")
        df.index = pd.to_datetime(df.index)
        agg = df.resample("3D").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()
        agg = agg.reset_index().rename(columns={"index": "time"})
        return agg

    def _candles_to_df(self, candles: List[Dict]) -> pd.DataFrame:
        """Convert candle list to pandas DataFrame."""
        df = pd.DataFrame(candles)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            df = df.sort_values("time").reset_index(drop=True)
        return df

    # ── RSI ───────────────────────────────────────────────────────────

    def _compute_rsi(self, df: pd.DataFrame, period: int = 14) -> Dict:
        """Compute RSI using TA-Lib (Wilder smoothing, C library)."""
        if len(df) < period + 1:
            return {"value": None, "status": "insufficient_data"}

        close = df["close"].values.astype(float)
        rsi_arr = talib.RSI(close, timeperiod=period)
        if rsi_arr is None or len(rsi_arr) == 0:
            return {"value": None, "status": "insufficient_data", "period": period}
        current = round(float(rsi_arr[-1]), 2) if not np.isnan(rsi_arr[-1]) else None
        prev = round(float(rsi_arr[-2]), 2) if len(rsi_arr) > 1 and not np.isnan(rsi_arr[-2]) else None

        status = "neutral"
        if current is not None:
            if current >= 70:
                status = "overbought"
            elif current >= 60:
                status = "strong"
            elif current <= 30:
                status = "oversold"
            elif current <= 40:
                status = "weak"

        return {"value": current, "previous": prev, "status": status, "period": period}

    # ── EMA ───────────────────────────────────────────────────────────

    def _compute_ema(self, df: pd.DataFrame) -> Dict:
        """Compute 9/20/50/200 EMAs using TA-Lib with cross detection."""
        result = {}
        close = df["close"].values.astype(float)
        price = float(close[-1])
        for period in [9, 20, 50, 200]:
            if len(df) >= period:
                ema = talib.EMA(close, timeperiod=period)
                val = float(ema[-1])
                if np.isnan(val):
                    continue
                val = round(val, 4)
                result[f"ema_{period}"] = val
                dist_pct = round((price - val) / val * 100, 3) if val and abs(val) > 1e-10 else 0.0
                result[f"price_vs_ema_{period}"] = "above" if price > val else "below"
                result[f"ema_{period}_dist_pct"] = dist_pct

        if "ema_50" in result and "ema_200" in result:
            result["golden_cross"] = result["ema_50"] > result["ema_200"]
        return result

    def _compute_sma(self, df: pd.DataFrame) -> Dict:
        """Compute 20/50/200 SMAs using TA-Lib."""
        result = {}
        close = df["close"].values.astype(float)
        price = float(close[-1])
        for period in [20, 50, 200]:
            if len(df) >= period:
                sma = talib.SMA(close, timeperiod=period)
                val = float(sma[-1])
                if np.isnan(val):
                    continue
                val = round(val, 4)
                result[f"sma_{period}"] = val
                result[f"price_vs_sma_{period}"] = "above" if price > val else "below"
        return result

    # ── MACD ──────────────────────────────────────────────────────────

    def _compute_macd(self, df: pd.DataFrame,
                      fast: int = 12, slow: int = 26, signal: int = 9) -> Dict:
        """Compute MACD using TA-Lib with histogram and cross status."""
        if len(df) < slow + signal:
            return {"status": "insufficient_data"}
        close = df["close"].values.astype(float)
        macd_line, signal_line, histogram = talib.MACD(
            close, fastperiod=fast, slowperiod=slow, signalperiod=signal)

        cur_macd = round(float(macd_line[-1]), 4) if not np.isnan(macd_line[-1]) else 0
        cur_signal = round(float(signal_line[-1]), 4) if not np.isnan(signal_line[-1]) else 0
        cur_hist = round(float(histogram[-1]), 4) if not np.isnan(histogram[-1]) else 0
        prev_hist = round(float(histogram[-2]), 4) if len(histogram) > 1 and not np.isnan(histogram[-2]) else 0

        cross = "none"
        if cur_hist > 0 and prev_hist <= 0:
            cross = "bullish_cross"
        elif cur_hist < 0 and prev_hist >= 0:
            cross = "bearish_cross"

        return {"macd": cur_macd, "signal": cur_signal, "histogram": cur_hist,
                "cross": cross, "momentum": "bullish" if cur_hist > 0 else "bearish"}

    # ── Bollinger Bands ───────────────────────────────────────────────

    def _compute_bollinger(self, df: pd.DataFrame,
                           period: int = 20, std_dev: float = 2.0) -> Dict:
        """Compute Bollinger Bands using TA-Lib with squeeze detection."""
        if len(df) < period:
            return {"status": "insufficient_data"}
        close = df["close"].values.astype(float)
        upper, middle, lower = talib.BBANDS(close, timeperiod=period,
                                            nbdevup=std_dev, nbdevdn=std_dev)
        price = float(close[-1])
        bb_upper = round(float(upper[-1]), 4) if not np.isnan(upper[-1]) else None
        bb_lower = round(float(lower[-1]), 4) if not np.isnan(lower[-1]) else None
        bb_mid = round(float(middle[-1]), 4) if not np.isnan(middle[-1]) else None

        if bb_upper is None:
            return {"status": "computation_error"}

        bb_width = round((bb_upper - bb_lower) / bb_mid * 100, 4) if bb_mid and abs(bb_mid) > 1e-10 else 0

        position = "middle"
        if price > bb_upper:
            position = "above_upper"
        elif price < bb_lower:
            position = "below_lower"
        elif price > bb_mid:
            position = "upper_half"
        else:
            position = "lower_half"

        valid = upper[~np.isnan(upper)]
        valid_lower = lower[~np.isnan(lower)]
        valid_mid = middle[~np.isnan(middle)]
        if len(valid) > 50 and len(valid_mid) > 50:
            mid_slice = valid_mid[-50:]
            safe_mid = np.where(mid_slice != 0, mid_slice, np.nan)
            widths = np.nan_to_num((valid[-50:] - valid_lower[-50:]) / safe_mid * 100, nan=0.0)
            avg_width = float(np.nanmean(widths)) if np.any(~np.isnan(widths)) else bb_width
        else:
            avg_width = bb_width
        squeeze = bb_width < avg_width * 0.6

        return {"upper": bb_upper, "middle": bb_mid, "lower": bb_lower,
                "width_pct": bb_width, "position": position, "squeeze": squeeze}

    # ── ATR ───────────────────────────────────────────────────────────

    def _compute_atr(self, df: pd.DataFrame, period: int = 14) -> Dict:
        """Compute ATR using TA-Lib for volatility assessment."""
        if len(df) < period + 1:
            return {"value": None, "status": "insufficient_data"}
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        atr = talib.ATR(high, low, close, timeperiod=period)

        current = round(float(atr[-1]), 4) if not np.isnan(atr[-1]) else None
        price = float(close[-1])
        pct = round((current / price) * 100, 3) if current and price > 0 else 0

        volatility = "normal"
        if pct > 3.5:
            volatility = "extreme"
        elif pct > 2.0:
            volatility = "high"
        elif pct < 0.5:
            volatility = "low"

        return {"value": current, "pct_of_price": pct, "volatility": volatility}

    def compute_multi_tf_atr(self, symbol: str) -> Dict:
        """Compute ATR across multiple timeframes for SL context.

        Returns a profile Apex can reason with — NOT a mechanical override.
        Each timeframe shows ATR(14) value, % of price, and volatility label.
        """
        tf_configs = {
            "M5":  {"hours": 24,  "label": "5-minute (micro noise)"},
            "M15": {"hours": 48,  "label": "15-minute (intraday baseline)"},
            "H1":  {"hours": 168, "label": "1-hour (swing context)"},
            "H4":  {"hours": 720, "label": "4-hour (structural)"},
        }
        profile = {}
        for tf, cfg in tf_configs.items():
            try:
                candles = self.get_candles_from_db(symbol, {tf: cfg["hours"]})
                rows = candles.get(tf, []) if candles else []
                if not rows or len(rows) < 15:
                    profile[tf] = {"status": "insufficient_data", "label": cfg["label"]}
                    continue
                df = self._candles_to_df(rows)
                atr_data = self._compute_atr(df, period=14)
                atr_data["label"] = cfg["label"]
                atr_data["candle_count"] = len(df)
                if atr_data.get("value") and atr_data["value"] > 0:
                    atr_data["sl_floor_1x"] = round(atr_data["value"], 6)
                    atr_data["sl_floor_1_5x"] = round(atr_data["value"] * 1.5, 6)
                profile[tf] = atr_data
            except Exception as e:
                logger.debug(f"[DataScientist] Multi-TF ATR {tf} for {symbol}: {e}")
                profile[tf] = {"status": "error", "label": cfg["label"]}

        return profile

    # ── CCI ───────────────────────────────────────────────────────────

    def _compute_cci(self, df: pd.DataFrame, period: int = 20) -> Dict:
        """Compute CCI using TA-Lib for overbought/oversold detection."""
        if len(df) < period + 1:
            return {"value": None, "status": "insufficient_data"}
        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        cci = talib.CCI(high, low, close, timeperiod=period)

        current = round(float(cci[-1]), 2) if not np.isnan(cci[-1]) else None
        status = "neutral"
        if current is not None:
            if current > 200:
                status = "extreme_overbought"
            elif current > 100:
                status = "overbought"
            elif current < -200:
                status = "extreme_oversold"
            elif current < -100:
                status = "oversold"

        return {"value": current, "status": status, "period": period}

    # ── Fibonacci Retracement ─────────────────────────────────────────

    def _compute_fibonacci(self, df: pd.DataFrame) -> Dict:
        """Auto-detect swing points and compute Fibonacci retracement levels.
        Includes Goldilocks zone (0.618-0.65) and Discount zone (0.786-0.83)."""
        if len(df) < 20:
            return {"status": "insufficient_data"}

        recent = df.tail(min(100, len(df)))
        swing_high = float(recent["high"].max())
        swing_low = float(recent["low"].min())
        high_idx = recent["high"].idxmax()
        low_idx = recent["low"].idxmin()
        price = float(df["close"].iloc[-1])

        is_uptrend = low_idx < high_idx
        diff = swing_high - swing_low

        fib_ratios = {"0.0": 0.0, "0.236": 0.236, "0.382": 0.382,
                      "0.5": 0.5, "0.618": 0.618, "0.65": 0.65,
                      "0.786": 0.786, "0.83": 0.83, "1.0": 1.0}
        levels = {}
        for name, ratio in fib_ratios.items():
            if is_uptrend:
                levels[name] = round(swing_high - diff * ratio, 2)
            else:
                levels[name] = round(swing_low + diff * ratio, 2)

        # Goldilocks zone: 0.618-0.65 retracement (optimal trade entry)
        goldilocks_top = levels.get("0.618", 0)
        goldilocks_bot = levels.get("0.65", 0)
        in_goldilocks = min(goldilocks_top, goldilocks_bot) <= price <= max(goldilocks_top, goldilocks_bot)

        # Discount zone: 0.786-0.83 retracement (deep discount)
        discount_top = levels.get("0.786", 0)
        discount_bot = levels.get("0.83", 0)
        in_discount = min(discount_top, discount_bot) <= price <= max(discount_top, discount_bot)

        # OTE is the broader zone 0.618 to 0.786
        ote_top = goldilocks_top
        ote_bot = discount_top
        in_ote = min(ote_top, ote_bot) <= price <= max(ote_top, ote_bot)

        return {
            "swing_high": swing_high, "swing_low": swing_low,
            "trend": "uptrend" if is_uptrend else "downtrend",
            "levels": levels,
            "goldilocks_zone": {"top": goldilocks_top, "bottom": goldilocks_bot,
                                "label": "0.618-0.65 Optimal Trade Entry"},
            "discount_zone": {"top": discount_top, "bottom": discount_bot,
                              "label": "0.786-0.83 Deep Discount"},
            "ote_zone": {"top": ote_top, "bottom": ote_bot},
            "price_in_goldilocks": in_goldilocks,
            "price_in_discount": in_discount,
            "price_in_ote": in_ote,
            "current_price": price
        }

    # ── Anchored VWAP ─────────────────────────────────────────────────

    def _compute_anchored_vwap(self, df: pd.DataFrame) -> Dict:
        """Compute VWAP anchored from the most significant swing point."""
        if len(df) < 10:
            return {"status": "insufficient_data"}
        if "volume" not in df.columns or df["volume"].sum() == 0:
            return {"status": "no_volume_data"}

        recent = df.tail(min(100, len(df))).copy()
        high_idx = recent["high"].idxmax()
        low_idx = recent["low"].idxmin()
        anchor_idx = min(high_idx, low_idx)

        subset = df.loc[anchor_idx:].copy()
        typical = (subset["high"] + subset["low"] + subset["close"]) / 3
        cum_vol = subset["volume"].cumsum()
        cum_tp_vol = (typical * subset["volume"]).cumsum()
        vwap = cum_tp_vol / cum_vol.clip(lower=1e-10)

        current_vwap = round(float(vwap.iloc[-1]), 4) if not vwap.empty else None
        price = float(df["close"].iloc[-1])

        return {
            "value": current_vwap,
            "anchor_from": str(df.loc[anchor_idx, "time"]) if "time" in df.columns else "?",
            "price_vs_vwap": "premium" if price > (current_vwap or 0) else "discount"
        }

    # ── TD Sequential ─────────────────────────────────────────────────

    def _compute_td_sequential(self, df: pd.DataFrame) -> Dict:
        """Compute TD Sequential setup count (simplified)."""
        if len(df) < 10:
            return {"status": "insufficient_data"}

        buy_count = 0
        sell_count = 0
        for i in range(4, len(df)):
            if df["close"].iloc[i] < df["close"].iloc[i - 4]:
                buy_count += 1
                sell_count = 0
            elif df["close"].iloc[i] > df["close"].iloc[i - 4]:
                sell_count += 1
                buy_count = 0
            else:
                buy_count = 0
                sell_count = 0

        signal = "none"
        if buy_count >= 9:
            signal = "buy_exhaustion"
        elif sell_count >= 9:
            signal = "sell_exhaustion"
        elif buy_count >= 7:
            signal = "buy_approaching"
        elif sell_count >= 7:
            signal = "sell_approaching"

        return {"buy_count": buy_count, "sell_count": sell_count, "signal": signal}

    # ── Divergence Detection ──────────────────────────────────────────

    def _detect_divergence(self, df: pd.DataFrame) -> Dict:
        """Detect bullish/bearish RSI divergence from price."""
        if len(df) < 30:
            return {"status": "insufficient_data"}

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta).where(delta < 0, 0.0).rolling(14).mean()
        loss_safe = loss.replace(0, np.nan)
        rs = gain / loss_safe
        rsi = 100 - (100 / (1 + rs))
        # All-gain period (loss all zero/NaN): RSI should be 100
        rsi = rsi.fillna(100.0)

        lookback = min(30, len(df) - 1)
        recent = df.tail(lookback)
        recent_rsi = rsi.tail(lookback)

        price_ll = float(recent["low"].iloc[-1]) < float(recent["low"].iloc[0])
        rsi_hl = float(recent_rsi.iloc[-1]) > float(recent_rsi.iloc[0]) if not recent_rsi.isna().all() else False

        price_hh = float(recent["high"].iloc[-1]) > float(recent["high"].iloc[0])
        rsi_lh = float(recent_rsi.iloc[-1]) < float(recent_rsi.iloc[0]) if not recent_rsi.isna().all() else False

        divergence = "none"
        if price_ll and rsi_hl:
            divergence = "bullish"
        elif price_hh and rsi_lh:
            divergence = "bearish"

        return {"type": divergence, "lookback_candles": lookback}

    # ── Order Blocks ──────────────────────────────────────────────────

    def _detect_order_blocks(self, df: pd.DataFrame) -> Dict:
        """Detect potential order block zones from structure breaks."""
        if len(df) < 10:
            return {"blocks": []}

        blocks = []
        for i in range(2, len(df) - 1):
            prev_body = df["close"].iloc[i-1] - df["open"].iloc[i-1]
            curr_body = df["close"].iloc[i] - df["open"].iloc[i]

            # Bearish OB: bullish candle followed by strong bearish engulfing
            if prev_body > 0 and curr_body < 0 and abs(curr_body) > abs(prev_body) * 1.5:
                blocks.append({
                    "type": "bearish_ob",
                    "high": round(float(df["high"].iloc[i-1]), 2),
                    "low": round(float(df["low"].iloc[i-1]), 2),
                    "time": str(df["time"].iloc[i-1]) if "time" in df.columns else i-1
                })

            # Bullish OB: bearish candle followed by strong bullish engulfing
            if prev_body < 0 and curr_body > 0 and abs(curr_body) > abs(prev_body) * 1.5:
                blocks.append({
                    "type": "bullish_ob",
                    "high": round(float(df["high"].iloc[i-1]), 2),
                    "low": round(float(df["low"].iloc[i-1]), 2),
                    "time": str(df["time"].iloc[i-1]) if "time" in df.columns else i-1
                })

        return {"blocks": blocks[-10:]}

    # ── Fair Value Gaps ───────────────────────────────────────────────

    def _detect_fair_value_gaps(self, df: pd.DataFrame) -> Dict:
        """Detect FVG (3-candle gaps where middle candle body doesn't overlap)."""
        if len(df) < 3:
            return {"gaps": []}

        gaps = []
        price = float(df["close"].iloc[-1])

        for i in range(1, len(df) - 1):
            # Bullish FVG: candle[i-1].high < candle[i+1].low
            if df["high"].iloc[i-1] < df["low"].iloc[i+1]:
                gap_low = float(df["high"].iloc[i-1])
                gap_high = float(df["low"].iloc[i+1])
                filled = price < gap_low
                gaps.append({
                    "type": "bullish_fvg", "high": round(gap_high, 2),
                    "low": round(gap_low, 2), "filled": filled,
                    "time": str(df["time"].iloc[i]) if "time" in df.columns else i
                })

            # Bearish FVG: candle[i-1].low > candle[i+1].high
            if df["low"].iloc[i-1] > df["high"].iloc[i+1]:
                gap_high = float(df["low"].iloc[i-1])
                gap_low = float(df["high"].iloc[i+1])
                filled = price > gap_high
                gaps.append({
                    "type": "bearish_fvg", "high": round(gap_high, 2),
                    "low": round(gap_low, 2), "filled": filled,
                    "time": str(df["time"].iloc[i]) if "time" in df.columns else i
                })

        return {"gaps": gaps[-10:]}

    # ── Volume Profile ────────────────────────────────────────────────

    def _compute_volume_profile(self, df: pd.DataFrame, num_bins: int = 20) -> Dict:
        """Compute volume profile: POC, Value Area High/Low."""
        if len(df) < 10 or "volume" not in df.columns or df["volume"].sum() == 0:
            return {"status": "insufficient_data_or_no_volume"}

        price_min = float(df["low"].min())
        price_max = float(df["high"].max())
        if price_max == price_min:
            return {"poc": price_min, "vah": price_max, "val": price_min}

        bins = np.linspace(price_min, price_max, num_bins + 1)
        vol_at_price = np.zeros(num_bins)
        for _, row in df.iterrows():
            mid = (float(row["high"]) + float(row["low"])) / 2
            idx = min(int((mid - price_min) / (price_max - price_min) * num_bins), num_bins - 1)
            vol_at_price[idx] += float(row.get("volume", 0))

        poc_idx = int(np.argmax(vol_at_price))
        poc = round((bins[poc_idx] + bins[poc_idx + 1]) / 2, 2)

        total_vol = vol_at_price.sum()
        target = total_vol * 0.7
        cum = vol_at_price[poc_idx]
        lo_idx, hi_idx = poc_idx, poc_idx
        while cum < target and (lo_idx > 0 or hi_idx < num_bins - 1):
            if lo_idx > 0:
                lo_idx -= 1
                cum += vol_at_price[lo_idx]
            if hi_idx < num_bins - 1:
                hi_idx += 1
                cum += vol_at_price[hi_idx]

        vah = round((bins[hi_idx] + bins[hi_idx + 1]) / 2, 2)
        val_ = round((bins[lo_idx] + bins[lo_idx + 1]) / 2, 2)
        price = float(df["close"].iloc[-1])

        return {"poc": poc, "vah": vah, "val": val_,
                "price_vs_poc": "above" if price > poc else "below"}

    # ── Volume Trend ──────────────────────────────────────────────────

    def _compute_volume_trend(self, df: pd.DataFrame) -> Dict:
        """Analyze volume trend: increasing, decreasing, or spike."""
        if len(df) < 10 or "volume" not in df.columns:
            return {"status": "insufficient_data"}

        vol = df["volume"].values.astype(float)
        recent_avg = float(np.mean(vol[-5:])) if len(vol) >= 5 else float(vol[-1])
        prev_avg = float(np.mean(vol[-15:-5])) if len(vol) >= 15 else float(np.mean(vol[:max(1, len(vol)-5)]))
        overall_avg = float(np.mean(vol[-20:])) if len(vol) >= 20 else float(np.mean(vol))

        if prev_avg == 0:
            prev_avg = 1
        ratio = recent_avg / prev_avg

        trend = "flat"
        if ratio > 1.5:
            trend = "spike"
        elif ratio > 1.15:
            trend = "increasing"
        elif ratio < 0.7:
            trend = "decreasing"

        return {"trend": trend, "recent_avg": round(recent_avg, 0),
                "overall_avg": round(overall_avg, 0), "ratio": round(ratio, 2)}

    # ── Volume Climax ─────────────────────────────────────────────────

    def _detect_volume_climax(self, df: pd.DataFrame) -> Dict:
        """Detect volume exhaustion: price continuing but volume dropping."""
        if len(df) < 10 or "volume" not in df.columns:
            return {"status": "insufficient_data"}

        last5 = df.tail(5)
        price_trend = float(last5["close"].iloc[-1]) - float(last5["close"].iloc[0])
        vol_trend = float(last5["volume"].iloc[-1]) - float(last5["volume"].iloc[0])

        exhaustion = False
        direction = "none"
        if abs(price_trend) > 0:
            if price_trend > 0 and vol_trend < 0:
                exhaustion = True
                direction = "bullish_exhaustion"
            elif price_trend < 0 and vol_trend > 0:
                exhaustion = True
                direction = "bearish_climax"

        return {"exhaustion": exhaustion, "direction": direction,
                "price_change": round(price_trend, 2),
                "vol_change": round(vol_trend, 0)}

    # ── Sell-off Detection ────────────────────────────────────────────

    def _detect_selloff(self, df: pd.DataFrame) -> Dict:
        """Detect sell-off tiers: pullback (<3%), correction (3-10%), crash (>10%)."""
        if len(df) < 5:
            return {"status": "insufficient_data"}

        recent_high = float(df["high"].rolling(20).max().iloc[-1]) if len(df) >= 20 else float(df["high"].max())
        current = float(df["close"].iloc[-1])
        drop_pct = round((recent_high - current) / recent_high * 100, 2) if recent_high > 0 else 0

        tier = "none"
        if drop_pct >= 10:
            tier = "crash"
        elif drop_pct >= 3:
            tier = "correction"
        elif drop_pct >= 1:
            tier = "pullback"

        prev_low = float(df["low"].iloc[-2]) if len(df) > 1 else current
        recovering = current > prev_low

        return {"tier": tier, "drop_pct": drop_pct,
                "recent_high": recent_high, "current": current,
                "recovering": recovering}

    # ── Momentum Burst ────────────────────────────────────────────────

    def _detect_momentum_burst(self, df: pd.DataFrame, lookback: int = 12) -> Dict:
        """Detect rapid price moves (momentum bursts)."""
        if len(df) < lookback:
            return {"status": "insufficient_data"}

        recent = df.tail(lookback)
        start_price = float(recent["close"].iloc[0])
        end_price = float(recent["close"].iloc[-1])
        move_pct = round((end_price - start_price) / start_price * 100, 3) if start_price > 0 else 0

        burst = False
        direction = "flat"
        if abs(move_pct) > 1.0:
            burst = True
            direction = "bullish_burst" if move_pct > 0 else "bearish_burst"

        max_range = float(recent["high"].max()) - float(recent["low"].min())
        range_pct = round(max_range / start_price * 100, 3) if start_price > 0 else 0

        return {"burst": burst, "direction": direction,
                "move_pct": move_pct, "range_pct": range_pct,
                "lookback_candles": lookback}

    # ── Relative Volume (R-Vol) — Aggr equivalent ──────────────────────

    def _compute_relative_volume(self, df: pd.DataFrame, lookback: int = 20) -> Dict:
        """Compute relative volume: current candle volume vs rolling average.
        Mirrors the R-Vol panel in Aggr (https://aggr.trade).
        A spike (>2x average) at a key level is a prerequisite for absorption signals.
        """
        if len(df) < lookback or "volume" not in df.columns or df["volume"].sum() == 0:
            return {"status": "insufficient_data"}

        vol = df["volume"].values.astype(float)
        rolling_avg = float(np.mean(vol[-lookback - 1:-1])) if len(vol) > lookback else float(np.mean(vol[:-1]))
        if rolling_avg == 0:
            rolling_avg = 1.0

        current_vol = float(vol[-1])
        r_vol = round(current_vol / rolling_avg, 2)

        recent_bars = []
        start = max(0, len(vol) - 10)
        for i in range(start, len(vol)):
            bar_avg = float(np.mean(vol[max(0, i - lookback):i])) if i > 0 else 1.0
            if bar_avg == 0:
                bar_avg = 1.0
            recent_bars.append({
                "index": int(i),
                "volume": round(float(vol[i]), 0),
                "r_vol": round(float(vol[i]) / bar_avg, 2),
                "spike": float(vol[i]) / bar_avg > 2.0,
            })

        spike_count = sum(1 for b in recent_bars if b["spike"])

        return {
            "current_r_vol": r_vol,
            "current_volume": round(current_vol, 0),
            "rolling_avg_volume": round(rolling_avg, 0),
            "is_spike": r_vol > 2.0,
            "recent_bars": recent_bars,
            "spike_count_last_10": spike_count,
            "lookback": lookback,
        }

    # ── Delta (approximated) — Aggr Delta Spot equivalent ─────────────

    def _compute_delta(self, df: pd.DataFrame) -> Dict:
        """Approximate buy/sell delta per candle from OHLCV data.
        Mirrors the Delta Spot panel in Aggr (https://aggr.trade).

        Without taker buy/sell split from the exchange, we approximate using
        the standard candle-body method:
          buy_vol  = volume * (close - low) / (high - low)
          sell_vol = volume * (high - close) / (high - low)
          delta    = buy_vol - sell_vol

        This is the same approximation used by most charting tools when
        raw tick-level data is unavailable.
        """
        if len(df) < 5 or "volume" not in df.columns or df["volume"].sum() == 0:
            return {"status": "insufficient_data"}

        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close_arr = df["close"].values.astype(float)
        vol = df["volume"].values.astype(float)

        candle_range = high - low
        candle_range[candle_range == 0] = 1e-10

        buy_pct = (close_arr - low) / candle_range
        sell_pct = (high - close_arr) / candle_range

        buy_vol = vol * buy_pct
        sell_vol = vol * sell_pct
        delta = buy_vol - sell_vol

        cum_delta = np.cumsum(delta)

        recent_bars = []
        start = max(0, len(delta) - 10)
        for i in range(start, len(delta)):
            recent_bars.append({
                "index": int(i),
                "delta": round(float(delta[i]), 0),
                "buy_vol": round(float(buy_vol[i]), 0),
                "sell_vol": round(float(sell_vol[i]), 0),
                "cum_delta": round(float(cum_delta[i]), 0),
                "direction": "positive" if delta[i] > 0 else "negative",
            })

        current_delta = float(delta[-1])
        avg_abs_delta = float(np.mean(np.abs(delta[-20:]))) if len(delta) >= 20 else float(np.mean(np.abs(delta)))
        if avg_abs_delta == 0:
            avg_abs_delta = 1.0
        delta_magnitude = round(abs(current_delta) / avg_abs_delta, 2)

        return {
            "current_delta": round(current_delta, 0),
            "cumulative_delta": round(float(cum_delta[-1]), 0),
            "delta_direction": "positive" if current_delta > 0 else "negative",
            "delta_magnitude": delta_magnitude,
            "is_large_delta": delta_magnitude > 2.0,
            "recent_bars": recent_bars,
            "method": "ohlcv_approximation",
            "note": "Approximated from candle body. For exact data, Aggr (aggr.trade) provides real-time taker delta.",
        }

    # ── Absorption Detection — core Aggr signal ──────────────────────

    def _detect_absorption(self, df: pd.DataFrame) -> Dict:
        """Detect absorption: high volume/delta at price extremes that fails to sustain movement.
        This is the core signal that Aggr visualizes when large +Delta bars
        appear at resistance but price reverses (sellers absorb buyers), or
        large -Delta bars at support but price reverses (buyers absorb sellers).

        Absorption is a necessary confirmation for mean-reversion strategies like Morin.
        """
        if len(df) < 20 or "volume" not in df.columns or df["volume"].sum() == 0:
            return {"status": "insufficient_data"}

        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close_arr = df["close"].values.astype(float)
        open_arr = df["open"].values.astype(float)
        vol = df["volume"].values.astype(float)

        candle_range = high - low
        candle_range[candle_range == 0] = 1e-10
        buy_pct = (close_arr - low) / candle_range
        sell_pct = (high - close_arr) / candle_range
        delta = (vol * buy_pct) - (vol * sell_pct)

        avg_vol = float(np.mean(vol[-20:])) if np.mean(vol[-20:]) > 0 else 1.0
        avg_abs_delta = float(np.mean(np.abs(delta[-20:]))) if np.mean(np.abs(delta[-20:])) > 0 else 1.0

        lookback = min(10, len(df) - 3)
        recent_high = float(np.max(high[-lookback:]))
        recent_low = float(np.min(low[-lookback:]))

        events = []
        for i in range(max(3, len(df) - lookback), len(df) - 1):
            bar_vol = float(vol[i])
            bar_delta = float(delta[i])
            r_vol = bar_vol / avg_vol
            delta_mag = abs(bar_delta) / avg_abs_delta

            if r_vol < 1.5 or delta_mag < 1.5:
                continue

            next_close = float(close_arr[i + 1])
            bar_close = float(close_arr[i])
            bar_high = float(high[i])
            bar_low = float(low[i])

            near_high = bar_high >= recent_high * 0.998
            near_low = bar_low <= recent_low * 1.002

            if near_high and bar_delta > 0 and next_close < bar_close:
                events.append({
                    "type": "bearish_absorption",
                    "index": int(i),
                    "price": round(bar_high, 5),
                    "delta": round(bar_delta, 0),
                    "r_vol": round(r_vol, 2),
                    "description": "Large buying delta at resistance absorbed by sellers; price reversed down",
                })
            elif near_low and bar_delta < 0 and next_close > bar_close:
                events.append({
                    "type": "bullish_absorption",
                    "index": int(i),
                    "price": round(bar_low, 5),
                    "delta": round(bar_delta, 0),
                    "r_vol": round(r_vol, 2),
                    "description": "Large selling delta at support absorbed by buyers; price reversed up",
                })

        return {
            "events": events[-5:],
            "total_detected": len(events),
            "data_source": "Approximated from OHLCV. For real-time taker flow, use Aggr (aggr.trade).",
        }

    # ── Opening Range Breakout ────────────────────────────────────────

    def _compute_orb(self, df: pd.DataFrame) -> Dict:
        """Compute Opening Range Breakout for Asia, London, and NY sessions."""
        if len(df) < 6 or "time" not in df.columns:
            return {"status": "insufficient_data"}

        today = df["time"].iloc[-1].date() if hasattr(df["time"].iloc[-1], "date") else None
        if not today:
            return {"status": "no_date_info"}

        price = float(df["close"].iloc[-1])
        session_opens = {
            "asia": pd.Timestamp(today).replace(hour=0, minute=0),
            "london": pd.Timestamp(today).replace(hour=8, minute=0),
            "ny": pd.Timestamp(today).replace(hour=14, minute=30),
        }
        result = {}
        for name, open_time in session_opens.items():
            orb_candles = df[(df["time"] >= open_time) &
                            (df["time"] < open_time + timedelta(minutes=30))]
            if orb_candles.empty:
                continue
            orb_high = round(float(orb_candles["high"].max()), 2)
            orb_low = round(float(orb_candles["low"].min()), 2)

            breakout = "inside"
            if price > orb_high:
                breakout = "above"
            elif price < orb_low:
                breakout = "below"

            # Check if breakout failed (broke then returned inside)
            post_orb = df[df["time"] >= open_time + timedelta(minutes=30)]
            failed = False
            if not post_orb.empty and breakout != "inside":
                if breakout == "above" and float(post_orb["close"].iloc[-1]) < orb_high:
                    failed = True
                elif breakout == "below" and float(post_orb["close"].iloc[-1]) > orb_low:
                    failed = True

            result[name] = {"orb_high": orb_high, "orb_low": orb_low,
                            "range": round(orb_high - orb_low, 2),
                            "breakout": breakout, "failed": failed}

        return result if result else {"status": "no_session_candles_today"}

    # ── Session Levels ────────────────────────────────────────────────

    def _compute_session_levels(self, df: pd.DataFrame) -> Dict:
        """Compute Asia, London, NY session high/low/open with sweep detection."""
        if "time" not in df.columns or df.empty:
            return {"status": "insufficient_data"}

        sessions = {
            "asia": (0, 8),
            "london": (8, 13),
            "ny": (13, 22),
        }
        result = {}
        today = df["time"].iloc[-1].date() if hasattr(df["time"].iloc[-1], "date") else None
        if not today:
            return {"status": "no_date_info"}

        price = float(df["close"].iloc[-1])
        completed_sessions = []

        for name, (start_h, end_h) in sessions.items():
            start = pd.Timestamp(today).replace(hour=start_h, minute=0)
            end = pd.Timestamp(today).replace(hour=end_h, minute=0)
            sess = df[(df["time"] >= start) & (df["time"] < end)]
            if not sess.empty:
                s_high = round(float(sess["high"].max()), 2)
                s_low = round(float(sess["low"].min()), 2)
                s_open = round(float(sess["open"].iloc[0]), 2)
                completed_sessions.append((name, s_high, s_low))
                result[name] = {
                    "high": s_high, "low": s_low, "open": s_open,
                    "candles": len(sess)
                }

        # Detect sweeps: later session wicks into earlier session levels then reverses
        after_data = df[df["time"] >= pd.Timestamp(today).replace(hour=0)]
        for name, s_high, s_low in completed_sessions:
            later = after_data[after_data["time"] >= pd.Timestamp(today).replace(
                hour=sessions[name][1], minute=0)]
            if not later.empty:
                swept_high = float(later["high"].max()) > s_high and price < s_high
                swept_low = float(later["low"].min()) < s_low and price > s_low
                if name in result:
                    result[name]["high_swept"] = swept_high
                    result[name]["low_swept"] = swept_low

        return result

    # ── Smart Money Concepts (SMC) ──────────────────────────────────

    def _detect_choch(self, df: pd.DataFrame) -> Dict:
        """Detect Change of Character — break of recent swing high/low
        suggesting a potential trend reversal."""
        if len(df) < 20:
            return {"status": "insufficient_data"}
        highs = df["high"].values
        lows = df["low"].values
        close = df["close"].values

        events = []
        lookback = min(50, len(df) - 1)
        for i in range(5, lookback):
            # Bearish CHoCH: price breaks below a recent swing low after making HH
            if i >= 3:
                recent_low = lows[max(0, i - 10):i].min()
                if close[i] < recent_low and close[i - 1] >= recent_low:
                    events.append({
                        "type": "bearish_choch", "index": int(i),
                        "price": round(float(close[i]), 5),
                        "broken_level": round(float(recent_low), 5),
                    })
                recent_high = highs[max(0, i - 10):i].max()
                if close[i] > recent_high and close[i - 1] <= recent_high:
                    events.append({
                        "type": "bullish_choch", "index": int(i),
                        "price": round(float(close[i]), 5),
                        "broken_level": round(float(recent_high), 5),
                    })

        recent = events[-3:] if events else []
        return {"events": recent, "total_detected": len(events)}

    def _detect_bos(self, df: pd.DataFrame) -> Dict:
        """Detect Break of Structure — higher high (bullish) or lower low (bearish)
        confirming a trending move."""
        if len(df) < 20:
            return {"status": "insufficient_data"}
        highs = df["high"].values
        lows = df["low"].values

        events = []
        lookback = min(50, len(df) - 1)
        for i in range(5, lookback):
            prev_high = highs[max(0, i - 10):i - 1].max()
            prev_low = lows[max(0, i - 10):i - 1].min()

            if highs[i] > prev_high:
                events.append({
                    "type": "bullish_bos", "index": int(i),
                    "price": round(float(highs[i]), 5),
                    "broken_level": round(float(prev_high), 5),
                })
            if lows[i] < prev_low:
                events.append({
                    "type": "bearish_bos", "index": int(i),
                    "price": round(float(lows[i]), 5),
                    "broken_level": round(float(prev_low), 5),
                })

        recent = events[-3:] if events else []
        return {"events": recent, "total_detected": len(events)}

    def _compute_monday_levels(self, df: pd.DataFrame) -> Dict:
        """Compute Monday open, high, low as weekly anchor levels."""
        if "time" not in df.columns or df.empty:
            return {"status": "insufficient_data"}
        df_with_dow = df.copy()
        df_with_dow["dow"] = pd.to_datetime(df_with_dow["time"]).dt.dayofweek
        monday = df_with_dow[df_with_dow["dow"] == 0]
        if monday.empty:
            return {"status": "no_monday_data"}
        return {
            "monday_open": round(float(monday["open"].iloc[0]), 5),
            "monday_high": round(float(monday["high"].max()), 5),
            "monday_low": round(float(monday["low"].min()), 5),
            "candles": len(monday),
        }

    def _compute_prev_day_levels(self, df: pd.DataFrame) -> Dict:
        """Compute Previous Day High, Low, Open (PDH/PDL/PDO)."""
        if "time" not in df.columns or len(df) < 2:
            return {"status": "insufficient_data"}
        dates = pd.to_datetime(df["time"]).dt.date
        unique_dates = sorted(dates.unique())
        if len(unique_dates) < 2:
            return {"status": "need_2_days"}
        prev_date = unique_dates[-2]
        prev_day = df[dates == prev_date]
        if prev_day.empty:
            return {"status": "no_previous_day"}
        return {
            "pdh": round(float(prev_day["high"].max()), 5),
            "pdl": round(float(prev_day["low"].min()), 5),
            "pdo": round(float(prev_day["open"].iloc[0]), 5),
            "pdc": round(float(prev_day["close"].iloc[-1]), 5),
        }

    def _compute_prev_week_levels(self, df: pd.DataFrame) -> Dict:
        """Compute Previous Week High and Low (PWH/PWL)."""
        if "time" not in df.columns or len(df) < 2:
            return {"status": "insufficient_data"}
        weeks = pd.to_datetime(df["time"]).dt.isocalendar().week
        unique_weeks = sorted(weeks.unique())
        if len(unique_weeks) < 2:
            return {"status": "need_2_weeks"}
        prev_week_num = unique_weeks[-2]
        prev_week = df[weeks == prev_week_num]
        if prev_week.empty:
            return {"status": "no_previous_week"}
        return {
            "pwh": round(float(prev_week["high"].max()), 5),
            "pwl": round(float(prev_week["low"].min()), 5),
        }

    def _detect_liquidity_grab(self, df: pd.DataFrame) -> Dict:
        """Detect liquidity grabs: wicks beyond key levels that immediately reject.
        A wick > 2x the body that sweeps a recent high/low then closes back inside."""
        if len(df) < 10:
            return {"status": "insufficient_data"}
        events = []
        lookback = min(30, len(df) - 1)
        for i in range(3, lookback):
            o, h, l, c = (float(df["open"].iloc[i]), float(df["high"].iloc[i]),
                          float(df["low"].iloc[i]), float(df["close"].iloc[i]))
            body = abs(c - o)
            upper_wick = h - max(o, c)
            lower_wick = min(o, c) - l
            if body < 0.0001:
                continue

            recent_high = float(df["high"].iloc[max(0, i - 10):i].max())
            recent_low = float(df["low"].iloc[max(0, i - 10):i].min())

            if upper_wick > body * 2 and h > recent_high and c < recent_high:
                events.append({
                    "type": "bearish_liquidity_grab", "index": int(i),
                    "wick_high": round(h, 5), "close": round(c, 5),
                    "swept_level": round(recent_high, 5),
                })
            if lower_wick > body * 2 and l < recent_low and c > recent_low:
                events.append({
                    "type": "bullish_liquidity_grab", "index": int(i),
                    "wick_low": round(l, 5), "close": round(c, 5),
                    "swept_level": round(recent_low, 5),
                })

        recent = events[-3:] if events else []
        return {"events": recent, "total_detected": len(events)}

    def _detect_amd_cycle(self, df: pd.DataFrame) -> Dict:
        """Detect Accumulation/Manipulation/Distribution (AMD) phases
        within the current session data."""
        if "time" not in df.columns or len(df) < 15:
            return {"status": "insufficient_data"}
        total = len(df)
        third = total // 3
        phase1 = df.iloc[:third]
        phase2 = df.iloc[third:2 * third]
        phase3 = df.iloc[2 * third:]

        p1_range = float(phase1["high"].max() - phase1["low"].min())
        p2_range = float(phase2["high"].max() - phase2["low"].min())
        p3_range = float(phase3["high"].max() - phase3["low"].min())
        total_range = float(df["high"].max() - df["low"].min())

        if total_range < 0.0001:
            return {"status": "no_range"}

        return {
            "accumulation": {"range": round(p1_range, 5),
                             "pct_of_total": round(p1_range / total_range * 100, 1)},
            "manipulation": {"range": round(p2_range, 5),
                             "pct_of_total": round(p2_range / total_range * 100, 1)},
            "distribution": {"range": round(p3_range, 5),
                             "pct_of_total": round(p3_range / total_range * 100, 1)},
            "current_phase": "distribution" if p3_range > p1_range
                            else "manipulation" if p2_range > p1_range
                            else "accumulation",
        }

    def _compute_poc(self, df: pd.DataFrame) -> Dict:
        """Compute Point of Control from volume profile."""
        vp = self._compute_volume_profile(df)
        if "poc" in vp:
            return {"poc": vp["poc"], "poc_volume": vp.get("poc_volume")}
        return {"status": "insufficient_data"}

    def _compute_vah_val(self, df: pd.DataFrame) -> Dict:
        """Compute Value Area High and Low from volume profile."""
        vp = self._compute_volume_profile(df)
        if "vah" in vp and "val" in vp:
            return {"vah": vp["vah"], "val": vp["val"],
                    "poc": vp.get("poc")}
        return {"status": "insufficient_data"}

    # ── Chart Tradeability Score (CTS) ──────────────────────────────

    def compute_chart_tradeability_score(self, symbol: str,
                                         candles_by_tf: Dict[str, List[Dict]] = None
                                         ) -> Dict[str, Any]:
        """Compute a 0-100 Chart Tradeability Score (CTS) for *symbol*.

        Measures structural quality — whether the chart *looks tradeable* to a
        professional trader — using pure math, zero LLM.

        Sub-scores (each 0-100, weighted):
          trend_clarity  (20%) — clean EMA alignment across timeframes
          structure      (25%) — Fib respect, BOS/CHoCH events, swing clarity
          volatility_fit (15%) — ATR normalised; not too flat, not too chaotic
          volume_health  (15%) — relative volume + trend confirmation
          confluence     (15%) — how many independent signals agree on direction
          mean_reversion (10%) — Bollinger position + RSI extremity proximity

        Returns dict with 'cts' (composite 0-100), sub-scores, and 'grade'.
        """
        if candles_by_tf is None:
            candles_by_tf = self.get_candles_from_db(
                symbol, {"M15": 72, "H1": 264, "H4": 1080, "D1": 6240})
        if not candles_by_tf:
            return {"cts": 0, "grade": "F", "reason": "no_candle_data"}

        subs = {}

        # --- Trend Clarity (EMA alignment across timeframes) ---
        trend_points = 0
        trend_checks = 0
        for tf in ["H1", "H4", "D1"]:
            df = self._safe_df(candles_by_tf, tf, min_rows=52)
            if df is None:
                continue
            ema_res = self._compute_ema(df)
            e9  = ema_res.get("ema_9")
            e20 = ema_res.get("ema_20")
            e50 = ema_res.get("ema_50")
            if e9 and e20 and e50:
                trend_checks += 1
                if e9 > e20 > e50 or e9 < e20 < e50:
                    trend_points += 100
                elif (e9 > e20 and e20 < e50) or (e9 < e20 and e20 > e50):
                    trend_points += 40
                else:
                    trend_points += 15
        subs["trend_clarity"] = round(trend_points / max(trend_checks, 1), 1)

        # --- Structure (Fib respect + swing quality) ---
        struct_score = 50
        for tf in ["H1", "H4"]:
            df = self._safe_df(candles_by_tf, tf, min_rows=30)
            if df is None:
                continue
            fib = self._compute_fibonacci(df)
            if fib.get("price_in_goldilocks") or fib.get("price_in_discount"):
                struct_score = min(100, struct_score + 25)
            elif fib.get("levels"):
                struct_score = min(100, struct_score + 10)
            if hasattr(self, '_detect_choch'):
                try:
                    choch = self._detect_choch(df)
                    if choch.get("events"):
                        struct_score = min(100, struct_score + 15)
                except Exception:
                    pass
            if hasattr(self, '_detect_bos'):
                try:
                    bos = self._detect_bos(df)
                    if bos.get("events"):
                        struct_score = min(100, struct_score + 10)
                except Exception:
                    pass
        subs["structure"] = min(100, round(struct_score, 1))

        # --- Volatility Fitness (Goldilocks ATR — not flat, not chaos) ---
        vol_score = 50
        df_h1 = self._safe_df(candles_by_tf, "H1", min_rows=20)
        if df_h1 is not None:
            atr = self._compute_atr(df_h1)
            atr_val = atr.get("value")
            close = float(df_h1["close"].iloc[-1]) if len(df_h1) > 0 else 0
            if atr_val and close > 0:
                atr_pct = (atr_val / close) * 100
                if 0.3 <= atr_pct <= 3.0:
                    vol_score = 85
                elif 0.15 <= atr_pct <= 5.0:
                    vol_score = 60
                else:
                    vol_score = 25
        subs["volatility_fit"] = round(vol_score, 1)

        # --- Volume Health (relative volume + trend confirmation) ---
        vol_health = 50
        df_m15 = self._safe_df(candles_by_tf, "M15", min_rows=25)
        if df_m15 is not None:
            rvol = self._compute_relative_volume(df_m15)
            ratio = rvol.get("current_r_vol", 1.0)
            if ratio >= 1.5:
                vol_health = 90
            elif ratio >= 1.0:
                vol_health = 70
            elif ratio >= 0.5:
                vol_health = 45
            else:
                vol_health = 20
            vtrend = self._compute_volume_trend(df_m15)
            if vtrend.get("trend") == "increasing":
                vol_health = min(100, vol_health + 10)
        subs["volume_health"] = round(vol_health, 1)

        # --- Confluence (how many independent signals agree) ---
        signals_bull = 0
        signals_bear = 0
        if df_h1 is not None:
            rsi = self._compute_rsi(df_h1)
            rsi_val = rsi.get("value")
            if rsi_val is not None:
                if rsi_val < 35:
                    signals_bull += 1
                elif rsi_val > 65:
                    signals_bear += 1
            macd = self._compute_macd(df_h1)
            if macd.get("cross") == "bullish_cross":
                signals_bull += 1
            elif macd.get("cross") == "bearish_cross":
                signals_bear += 1
            bb = self._compute_bollinger(df_h1)
            pos = bb.get("position")
            if pos == "below_lower":
                signals_bull += 1
            elif pos == "above_upper":
                signals_bear += 1
        agreement = max(signals_bull, signals_bear)
        total_signals = signals_bull + signals_bear
        if total_signals >= 3 and agreement >= 2:
            conf_score = 90
        elif total_signals >= 2 and agreement >= 2:
            conf_score = 75
        elif agreement >= 1:
            conf_score = 50
        else:
            conf_score = 25
        subs["confluence"] = round(conf_score, 1)

        # --- Mean Reversion Potential ---
        mr_score = 50
        if df_h1 is not None:
            bb = self._compute_bollinger(df_h1)
            bw = bb.get("width_pct")
            if bw is not None and bw < 3.0:
                mr_score = 80
            rsi = self._compute_rsi(df_h1)
            rsi_val = rsi.get("value")
            if rsi_val is not None:
                if rsi_val < 25 or rsi_val > 75:
                    mr_score = min(100, mr_score + 20)
        subs["mean_reversion"] = round(mr_score, 1)

        # --- Weighted composite ---
        weights = {
            "trend_clarity": 0.20,
            "structure": 0.25,
            "volatility_fit": 0.15,
            "volume_health": 0.15,
            "confluence": 0.15,
            "mean_reversion": 0.10,
        }

        has_real_data = (
            trend_checks > 0
            or df_h1 is not None
            or df_m15 is not None
        )
        if not has_real_data:
            return {"cts": 0, "grade": "F", "reason": "insufficient_data",
                    "sub_scores": subs}

        cts = sum(subs.get(k, 50) * w for k, w in weights.items())
        cts = round(max(0, min(100, cts)), 1)

        grade_map = [(90, "A+"), (80, "A"), (70, "B+"), (60, "B"),
                     (50, "C+"), (40, "C"), (30, "D"), (0, "F")]
        grade = "F"
        for threshold, g in grade_map:
            if cts >= threshold:
                grade = g
                break

        logger.debug(f"[CTS] {symbol}: {cts} ({grade}) | {subs}")
        return {"cts": cts, "grade": grade, "sub_scores": subs, "symbol": symbol}

    def _safe_df(self, candles_by_tf: Dict, tf: str,
                 min_rows: int = 14) -> Optional[pd.DataFrame]:
        """Convert candles for *tf* to a DataFrame, or None if insufficient."""
        candles = candles_by_tf.get(tf)
        if not candles or len(candles) < min_rows:
            return None
        return self._candles_to_df(candles)

    # ── Format for LLM ────────────────────────────────────────────────

    def format_for_prompt(self, ta_results: Dict) -> str:
        """Format TA results as structured text for LLM consumption."""
        lines = [f"## DATA SCIENTIST TECHNICAL ANALYSIS: {ta_results.get('symbol', '?')}",
                 f"Computed: {ta_results.get('computed_at', 'now')}\n"]

        for tf in ["D1", "H4", "H1", "M30", "M15", "M5", "W1", "M1"]:
            tf_data = ta_results.get(tf)
            if not tf_data:
                continue
            lines.append(f"### {tf} Timeframe\n")
            for indicator, data in tf_data.items():
                lines.append(f"**{indicator.upper()}**: {_format_indicator(data)}")
            lines.append("")

        return "\n".join(lines)


def _format_indicator(data: Any) -> str:
    """Format a single indicator result as readable text."""
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            if k in ("status", "insufficient_data"):
                continue
            if isinstance(v, float):
                parts.append(f"{k}={v:.4f}")
            elif isinstance(v, list):
                parts.append(f"{k}: {len(v)} items")
            elif isinstance(v, dict):
                parts.append(f"{k}: {v}")
            else:
                parts.append(f"{k}={v}")
        return " | ".join(parts) if parts else str(data)
    return str(data)


# ═══════════════════════════════════════════════════════════════════════
# COMPANION DATA FEEDS
# ═══════════════════════════════════════════════════════════════════════

class CompanionDataFeed:
    """Fetches companion market data: VIX, correlations, economic calendar, crypto metrics."""

    def __init__(self, db=None, executor=None):
        self.db = db
        self._executor = executor

    def get_vix_status(self) -> Dict:
        """Fetch VIX data for volatility context."""
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX")
            hist = vix.history(period="5d")
            if hist.empty:
                return {"value": None, "status": "unavailable"}
            current = round(float(hist["Close"].iloc[-1]), 2)
            prev = round(float(hist["Close"].iloc[-2]), 2) if len(hist) > 1 else current

            status = "normal"
            if current > 30:
                status = "extreme"
            elif current > 20:
                status = "elevated"

            return {
                "value": current, "previous": prev,
                "change": round(current - prev, 2),
                "status": status
            }
        except Exception as e:
            logger.debug(f"[DataScientist] VIX fetch failed: {e}")
            return {"value": None, "status": "unavailable", "error": str(e)}

    def get_correlation_data(self, symbol: str) -> Dict:
        """Fetch correlation companion data (DXY for gold, NASDAQ for BTC, etc)."""
        # Resolve aliases (XADUSD→XAGUSD, BTCUSD→BTCUSDT) for lookup
        if self.db:
            try:
                from db.market_symbols import resolve_symbol as _resolve
                symbol = _resolve(symbol, self.db)
            except Exception:
                pass
        companions = {
            "XAUUSD": [("DXY", "DX-Y.NYB", "inverse")],
            "XAGUSD": [("DXY", "DX-Y.NYB", "inverse")],
            "BTCUSD": [("NASDAQ", "^IXIC", "positive"), ("DXY", "DX-Y.NYB", "inverse")],
            "BTCUSDT": [("NASDAQ", "^IXIC", "positive"), ("DXY", "DX-Y.NYB", "inverse")],
            "NAS100": [("US10Y", "^TNX", "inverse"), ("VIX", "^VIX", "inverse")],
            "US30": [("US10Y", "^TNX", "inverse"), ("VIX", "^VIX", "inverse")],
            "SPX500": [("US10Y", "^TNX", "inverse"), ("VIX", "^VIX", "inverse")],
        }
        pairs = companions.get(symbol, [])
        if not pairs:
            return {"companions": []}

        results = []
        try:
            import yfinance as yf
            for name, ticker, correlation in pairs:
                try:
                    t = yf.Ticker(ticker)
                    hist = t.history(period="5d")
                    if not hist.empty:
                        current = round(float(hist["Close"].iloc[-1]), 4)
                        prev = round(float(hist["Close"].iloc[-2]), 4) if len(hist) > 1 else current
                        results.append({
                            "name": name, "value": current,
                            "change": round(current - prev, 4),
                            "correlation_type": correlation
                        })
                except Exception:
                    results.append({"name": name, "value": None, "error": "fetch_failed"})
        except ImportError:
            return {"companions": [], "error": "yfinance not installed"}

        return {"companions": results}

    def get_economic_calendar(self) -> Dict:
        """Fetch upcoming economic events for the next 24 hours."""
        try:
            import requests
            today = _utcnow().strftime("%Y-%m-%d")
            tomorrow = (_utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
            resp = requests.get(
                f"https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=10)
            if resp.status_code != 200:
                return {"events": [], "error": f"HTTP {resp.status_code}"}

            raw = resp.json()
            now = _utcnow()
            upcoming = []
            for ev in raw:
                ev_date = ev.get("date", "")
                try:
                    ev_dt = datetime.strptime(ev_date, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
                except (ValueError, TypeError):
                    continue
                if now - timedelta(hours=2) <= ev_dt <= now + timedelta(hours=24):
                    impact = ev.get("impact", "").lower()
                    upcoming.append({
                        "title": ev.get("title", "?"),
                        "country": ev.get("country", "?"),
                        "date": ev_dt.isoformat(),
                        "impact": impact,
                        "forecast": ev.get("forecast", ""),
                        "previous": ev.get("previous", ""),
                    })
            upcoming.sort(key=lambda x: x["date"])
            return {"events": upcoming[:20],
                    "high_impact_count": sum(1 for e in upcoming if e["impact"] == "high")}
        except Exception as e:
            logger.debug(f"[DataScientist] Calendar fetch failed: {e}")
            return {"events": [], "error": str(e)}

    def get_crypto_metrics(self, symbol: str) -> Dict:
        """Fetch crypto-specific metrics: funding rate, open interest, fear/greed."""
        # Resolve aliases (BTCUSD→BTCUSDT) for lookup
        if self.db:
            try:
                from db.market_symbols import resolve_symbol as _resolve
                symbol = _resolve(symbol, self.db)
            except Exception:
                pass
        sym_upper = symbol.upper()
        _non_crypto_prefixes = ("XAU", "XAG", "EUR", "GBP", "AUD", "NZD", "USD",
                                "CAD", "CHF", "JPY", "NAS", "US3", "SPX", "US50")
        is_crypto = (
            sym_upper.endswith(("USDT", "USDC", "BUSD"))
            or (sym_upper.endswith("USD")
                and not any(sym_upper.startswith(p) for p in _non_crypto_prefixes))
        )
        if not is_crypto:
            return {"applicable": False}

        result = {"applicable": True, "symbol": symbol}

        try:
            import requests
            fg_resp = requests.get(
                "https://api.alternative.me/fng/?limit=1", timeout=5)
            if fg_resp.status_code == 200:
                fg_data = fg_resp.json().get("data", [{}])[0]
                result["fear_greed"] = {
                    "value": int(fg_data.get("value", 0)),
                    "classification": fg_data.get("value_classification", "?")
                }
        except Exception as e:
            result["fear_greed"] = {"error": str(e)}

        if self._executor:
            try:
                fr = self._executor.fetch_funding_rate(symbol)
                if fr and "rate" in fr:
                    rate = fr["rate"]
                    result["funding_rate"] = rate
                    if rate > 0.0001:
                        result["funding_bias"] = "long_crowded"
                    elif rate < -0.0001:
                        result["funding_bias"] = "short_crowded"
                    else:
                        result["funding_bias"] = "neutral"
            except Exception as e:
                logger.debug(f"[DataScientist] Funding rate fetch failed for {symbol}: {e}")

        return result

    def get_full_companion_summary(self, symbol: str) -> Dict:
        """Build complete companion data summary for a dossier."""
        vix = self.get_vix_status()
        corr = self.get_correlation_data(symbol)
        calendar = self.get_economic_calendar()
        crypto = self.get_crypto_metrics(symbol)

        return {
            "vix": vix,
            "correlations": corr,
            "economic_calendar": calendar,
            "crypto_metrics": crypto if crypto.get("applicable") else None,
        }

    def format_companion_for_prompt(self, data: Dict) -> str:
        """Format companion data as text for LLM prompt inclusion."""
        lines = ["## MARKET COMPANION DATA\n"]
        vix = data.get("vix", {})
        if vix.get("value"):
            lines.append(f"**VIX**: {vix['value']} ({vix.get('status','?')}) | "
                        f"Change: {vix.get('change',0):+.2f}")

        corr = data.get("correlations", {})
        for c in corr.get("companions", []):
            if c.get("value"):
                lines.append(f"**{c['name']}**: {c['value']} | "
                            f"Change: {c.get('change',0):+.4f} | "
                            f"Correlation: {c.get('correlation_type','?')}")

        cal = data.get("economic_calendar", {})
        events = cal.get("events", [])
        if events:
            hi = cal.get("high_impact_count", 0)
            lines.append(f"\n**Economic Calendar** ({len(events)} events, "
                        f"{hi} HIGH impact):")
            for ev in events[:8]:
                lines.append(f"  - [{ev.get('impact','?').upper()}] "
                            f"{ev.get('title','?')} ({ev.get('country','?')}) "
                            f"@ {ev.get('date','')} | "
                            f"Forecast: {ev.get('forecast','?')} | "
                            f"Prev: {ev.get('previous','?')}")

        crypto = data.get("crypto_metrics")
        if crypto:
            fg = crypto.get("fear_greed", {})
            if fg.get("value"):
                lines.append(f"\n**Crypto Fear & Greed**: {fg['value']} "
                            f"({fg.get('classification','?')})")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════════

_ds_instance: Optional[DataScientist] = None
_cf_instance: Optional[CompanionDataFeed] = None


def get_data_scientist(db=None) -> DataScientist:
    global _ds_instance
    if _ds_instance is None:
        _ds_instance = DataScientist(db)
    return _ds_instance


def get_companion_feed(db=None, executor=None) -> CompanionDataFeed:
    global _cf_instance
    if _cf_instance is None:
        _cf_instance = CompanionDataFeed(db, executor)
    elif executor is not None and _cf_instance._executor is None:
        _cf_instance._executor = executor
    return _cf_instance
