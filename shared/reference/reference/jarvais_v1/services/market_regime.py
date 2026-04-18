"""
Market Regime Service — Pure-math market state classification.

Two operation modes:
  evaluate()   — FULL regime computation across multiple timeframes (~2-10s)
  fast_pulse() — FAST velocity check using cached BTC prices (~50-100ms)

Score range: -100 (BEARISH_SHOCK) to +100 (BULLISH_EUPHORIA)
Stateless: evaluate() needs no DB.  fast_pulse() reads market_regime_history.
"""

import logging
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger("market_regime")

# ── Regime Labels ─────────────────────────────────────────────────────
def _label_for_score(score: float) -> str:
    if score >= 70:
        return "BULLISH_EUPHORIA"
    if score >= 30:
        return "BULLISH_TREND"
    if score >= -29.99:
        return "NEUTRAL_RANGE"
    if score >= -69.99:
        return "BEARISH_TREND"
    return "BEARISH_SHOCK"


class MarketRegime:
    """Compute market regime from DataScientist indicators.
    Thread-safe: no mutable state between calls."""

    # ── FULL Regime ───────────────────────────────────────────────────

    def evaluate(self, symbol: str, data_scientist, executor=None,
                 altcoin_prices: Optional[Dict[str, list]] = None) -> Dict[str, Any]:
        """Compute a full regime score for *symbol* using multi-timeframe TA.

        Parameters
        ----------
        symbol : str
            Exchange symbol, e.g. "BTCUSDT"
        data_scientist : DataScientist
            Instance with `compute_all` and `get_candles_from_db` methods
        executor : CCXTExecutor, optional
            If provided, enriches result with funding rate, OI, L/S ratio
        altcoin_prices : dict, optional
            Dict mapping altcoin symbol -> list of recent OHLCV candles
            Used by AltcoinDivergenceDetector to check for divergence from BTC

        Returns
        -------
        dict with keys: score (-100..+100), label, components dict, enrichment dict
        """
        t0 = time.time()
        components = {
            "trend_alignment": 0.0,
            "momentum": 0.0,
            "volatility": 0.0,
            "volume_flow": 0.0,
        }

        timeframes_needed = {"M5": 24, "M15": 72, "M30": 96,
                             "H1": 264, "H4": 1080, "D1": 6240}
        try:
            candles = data_scientist.get_candles_from_db(symbol, timeframes_needed)
        except Exception as e:
            logger.warning(f"[Regime] Candle fetch failed for {symbol}: {e}")
            candles = {}

        ta = {}
        if candles:
            try:
                ta = data_scientist.compute_all(symbol, candles, requested=["ema", "rsi", "macd", "atr", "bollinger", "relative_volume", "volume_trend"])
            except Exception as e:
                logger.warning(f"[Regime] compute_all failed for {symbol}: {e}")

        # ── Component 1: Trend Alignment (-40 to +40) ────────────────
        trend_pts = 0.0
        trend_checks = 0
        for tf in ["M5", "M30", "H4", "D1"]:
            tf_data = ta.get(tf, {})
            ema = tf_data.get("ema", {})
            e9, e20, e50 = ema.get("ema_9"), ema.get("ema_20"), ema.get("ema_50")
            if e9 is not None and e20 is not None and e50 is not None:
                trend_checks += 1
                if e9 > e20 > e50:
                    trend_pts += 10.0
                elif e9 < e20 < e50:
                    trend_pts -= 10.0
                elif e9 > e20:
                    trend_pts += 3.0
                elif e9 < e20:
                    trend_pts -= 3.0
        components["trend_alignment"] = max(-40, min(40, trend_pts))

        # ── Component 2: Momentum (-30 to +30) ───────────────────────
        mom_pts = 0.0
        for tf in ["H1", "H4"]:
            tf_data = ta.get(tf, {})
            rsi = tf_data.get("rsi", {})
            rsi_val = rsi.get("value")
            if rsi_val is not None:
                if rsi_val > 70:
                    mom_pts += 8.0
                elif rsi_val > 55:
                    mom_pts += 4.0
                elif rsi_val < 30:
                    mom_pts -= 8.0
                elif rsi_val < 45:
                    mom_pts -= 4.0

            macd = tf_data.get("macd", {})
            hist = macd.get("histogram")
            if hist is not None and hist != 0:
                if hist > 0:
                    mom_pts += 3.0
                else:
                    mom_pts -= 3.0
        components["momentum"] = max(-30, min(30, mom_pts))

        # ── Component 3: Volatility (-20 to +20) ─────────────────────
        vol_pts = 0.0
        h1_data = ta.get("H1", {})
        atr = h1_data.get("atr", {})
        atr_val = atr.get("value")
        bb = h1_data.get("bollinger", {})
        bb_width = bb.get("width_pct")

        close = 0.0
        h1_candles = candles.get("H1", [])
        if h1_candles:
            close = float(h1_candles[-1].get("close", 0) or 0)

        if atr_val is not None and close > 0:
            atr_pct = (atr_val / close) * 100
            if atr_pct > 5.0:
                vol_pts -= 15.0
            elif atr_pct > 3.0:
                vol_pts -= 5.0
            elif atr_pct < 0.3:
                vol_pts -= 10.0
            else:
                vol_pts += 5.0

        if bb_width is not None:
            if bb_width > 8.0:
                vol_pts -= 5.0
            elif bb_width < 2.0:
                vol_pts += 5.0

        vol_regime = self._classify_volatility_regime(atr_val, close, bb_width)

        components["volatility"] = max(-20, min(20, vol_pts))

        # ── Component 4: Volume Flow (-10 to +10) ────────────────────
        vf_pts = 0.0
        for tf in ["M15", "H1"]:
            tf_data = ta.get(tf, {})
            rvol = tf_data.get("relative_volume", {})
            ratio = rvol.get("current_r_vol")
            if ratio is not None:
                if ratio >= 1.5:
                    vf_pts += 3.0
                elif ratio >= 1.0:
                    vf_pts += 1.0
                else:
                    vf_pts -= 2.0

            vtrend = tf_data.get("volume_trend", {})
            if vtrend.get("trend") == "increasing":
                vf_pts += 2.0
            elif vtrend.get("trend") == "decreasing":
                vf_pts -= 2.0
        components["volume_flow"] = max(-10, min(10, vf_pts))

        # ── Composite Score ───────────────────────────────────────────
        raw_score = sum(components.values())
        score = max(-100.0, min(100.0, round(raw_score, 2)))
        label = _label_for_score(score)

        data_quality = "full" if trend_checks >= 3 else ("partial" if trend_checks >= 1 else "no_data")

        # ── Optional enrichment via executor ──────────────────────────
        enrichment = {}
        if executor:
            try:
                fr = executor.fetch_funding_rate(symbol)
                if fr and "rate" in fr:
                    enrichment["funding_rate"] = fr["rate"]
            except Exception:
                pass
            try:
                oi = executor.fetch_open_interest(symbol)
                if oi:
                    enrichment["open_interest"] = oi
            except Exception:
                pass
            try:
                ls = executor.fetch_long_short_ratio(symbol)
                if ls:
                    enrichment["long_short_ratio"] = ls.get("ratio")
            except Exception:
                pass

        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info(f"[Regime] {symbol}: score={score} ({label}) "
                    f"trend={components['trend_alignment']:.0f} "
                    f"mom={components['momentum']:.0f} "
                    f"vol={components['volatility']:.0f} "
                    f"vflow={components['volume_flow']:.0f} "
                    f"elapsed={elapsed_ms}ms")

        duration_info = _duration_tracker.update(label, score)
        session_context = _get_session_context()
        divergence_info = _divergence_detector.evaluate(label, altcoin_prices or {})
        sizing_guidance = self._compute_sizing_guidance(
            label, vol_regime, divergence_info, components)

        return {
            "symbol": symbol,
            "score": score,
            "label": label,
            "data_quality": data_quality,
            "components": components,
            "enrichment": enrichment,
            "elapsed_ms": elapsed_ms,
            "volatility_regime": vol_regime,
            "duration": duration_info,
            "session": session_context,
            "altcoin_divergence": divergence_info,
            "sizing_guidance": sizing_guidance,
        }

    def _compute_sizing_guidance(self, label: str, vol_regime: str,
                                  divergence_info: Dict,
                                  components: Dict) -> Dict[str, Any]:
        """Compute position sizing guidance based on regime conditions.

        Returns a size_multiplier (0.25-1.5x) that scales the base risk_pct.
        Lower values = reduce position size (risky conditions).
        Higher values = can increase position size (favorable conditions).

        Behavioral rules enforced:
        - High volatility → reduce size
        - Altcoin divergence from BTC → reduce size
        - Extreme regimes (EUPHORIA/SHOCK) → reduce size
        - Strong trend with normal volatility → can increase size
        """
        multiplier = 1.0
        reasons = []

        if vol_regime == "EXPANSION":
            multiplier *= 0.5
            reasons.append("high_volatility_halves_size")
        elif vol_regime == "COMPRESSION":
            multiplier *= 1.2
            reasons.append("low_volatility_allows_20pct_increase")

        if divergence_info.get("divergence_detected"):
            multiplier *= 0.6
            reasons.append("altcoin_divergence_reduces_size_40pct")

        if label in ("BULLISH_EUPHORIA", "BEARISH_SHOCK"):
            multiplier *= 0.7
            reasons.append(f"extreme_regime_{label}_reduces_size_30pct")

        if label in ("BULLISH_TREND", "BEARISH_TREND") and vol_regime == "NORMAL":
            multiplier *= 1.3
            reasons.append("strong_trend_normal_vol_allows_30pct_increase")

        multiplier = max(0.25, min(1.5, round(multiplier, 2)))

        return {
            "size_multiplier": multiplier,
            "reasons": reasons,
            "regime_label": label,
            "volatility_regime": vol_regime,
        }

    # ── FAST Pulse ────────────────────────────────────────────────────

    def fast_pulse(self, symbol: str, db, executor=None,
                   threshold_pct: float = 2.0,
                   active_positions: list = None) -> Dict[str, Any]:
        """Lightweight velocity check using recent BTC prices from history table.

        Parameters
        ----------
        symbol : str
            Typically "BTCUSDT"
        db : Database
            Needed to read market_regime_history for historical BTC prices
        executor : CCXTExecutor, optional
            Used to get current BTC price. Falls back to DB last entry.
        threshold_pct : float
            Velocity threshold (default 2.0%)
        active_positions : list[dict], optional
            Active shadow positions with 'direction', 'entry_price', 'current_price' fields.
            Used for portfolio-aware alerting.

        Returns
        -------
        dict with velocity_alert, velocity_5m, velocity_15m, current_price, portfolio_health
        """
        result = {
            "velocity_alert": False,
            "velocity_5m": 0.0,
            "velocity_15m": 0.0,
            "current_price": 0.0,
            "alert_direction": None,
            "portfolio_health": None,
        }

        # Get current BTC price
        current_price = 0.0
        if executor:
            try:
                ticker = executor.get_ticker(symbol)
                if ticker:
                    current_price = float(ticker.get("last") or ticker.get("price") or 0)
            except Exception:
                pass

        if current_price <= 0 and db:
            try:
                row = db.fetch_one(
                    "SELECT btc_price FROM market_regime_history "
                    "WHERE symbol = %s AND btc_price IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 1", (symbol,))
                if row and row.get("btc_price"):
                    current_price = float(row["btc_price"])
            except Exception:
                pass

        if current_price <= 0:
            return result
        result["current_price"] = current_price

        # Get historical prices (5 min and 15 min ago)
        if not db:
            return result

        for window_min, key in [(5, "velocity_5m"), (15, "velocity_15m")]:
            try:
                row = db.fetch_one(
                    "SELECT btc_price FROM market_regime_history "
                    "WHERE symbol = %s AND btc_price IS NOT NULL "
                    "AND created_at <= DATE_SUB(NOW(), INTERVAL %s MINUTE) "
                    "ORDER BY created_at DESC LIMIT 1",
                    (symbol, window_min))
                if row and row.get("btc_price"):
                    old_price = float(row["btc_price"])
                    if old_price > 0:
                        pct_change = ((current_price - old_price) / old_price) * 100
                        result[key] = round(pct_change, 4)
            except Exception as e:
                logger.debug(f"[Regime] fast_pulse price lookup {window_min}min failed: {e}")

        # Check if velocity exceeds threshold
        v5 = abs(result["velocity_5m"])
        v15 = abs(result["velocity_15m"])
        max_velocity = max(v5, v15)

        if max_velocity < threshold_pct:
            return result

        # Direction of the move
        dominant_velocity = result["velocity_5m"] if v5 >= v15 else result["velocity_15m"]
        move_direction = "drop" if dominant_velocity < 0 else "pump"

        # Portfolio-aware check
        if active_positions:
            in_profit = 0
            in_loss = 0
            for pos in active_positions:
                entry = float(pos.get("entry_price") or 0)
                curr = float(pos.get("current_price") or 0)
                direction = (pos.get("direction") or "").upper()
                if entry <= 0 or curr <= 0:
                    continue
                if direction == "BUY":
                    is_profit = curr > entry
                else:
                    is_profit = curr < entry
                if is_profit:
                    in_profit += 1
                else:
                    in_loss += 1

            total = in_profit + in_loss
            if total > 0:
                profit_pct = in_profit / total
                result["portfolio_health"] = round(profit_pct * 100, 1)

                if move_direction == "drop" and profit_pct >= 0.6:
                    logger.info(f"[Regime] BTC velocity alert softened: {profit_pct*100:.0f}% "
                                f"of positions in profit (altcoin divergence)")
                    result["alert_direction"] = move_direction
                    return result

                if move_direction == "pump" and profit_pct >= 0.6:
                    logger.info(f"[Regime] BTC pump alert softened: {profit_pct*100:.0f}% "
                                f"of positions in profit")
                    result["alert_direction"] = move_direction
                    return result

        # Full velocity alert
        result["velocity_alert"] = True
        result["alert_direction"] = move_direction
        window = "5min" if v5 >= v15 else "15min"
        logger.warning(f"[Regime] VELOCITY ALERT: {symbol} {move_direction} "
                       f"{abs(dominant_velocity):.2f}% in {window}")
        return result


    def _classify_volatility_regime(self, atr_val: Optional[float], close: float, bb_width: Optional[float]) -> str:
        """Classify volatility into strategy-relevant regimes.

        Thresholds aligned with component scoring:
        - ATR < 0.3% is penalized in volatility component → COMPRESSION
        - ATR > 3.0% is penalized in volatility component → EXPANSION
        - BB width < 2.0% is rewarded in volatility component → COMPRESSION
        - BB width > 8.0% is penalized in volatility component → EXPANSION
        """
        if atr_val is None or close <= 0:
            atr_pct = None
        else:
            atr_pct = (atr_val / close) * 100

        compression_score = 0
        expansion_score = 0

        if atr_pct is not None:
            if atr_pct < 0.5:
                compression_score += 1
            elif atr_pct > 3.0:
                expansion_score += 1

        if bb_width is not None:
            if bb_width < 2.0:
                compression_score += 1
            elif bb_width > 5.0:
                expansion_score += 1

        if compression_score >= 2:
            return "COMPRESSION"
        elif expansion_score >= 2:
            return "EXPANSION"
        return "NORMAL"


def _get_session_context() -> Dict[str, Any]:
    """Determine current trading session and typical behavior.

    NOTE: Session hours are forex-market heuristics applied to crypto.
    Crypto trades 24/7 and doesn't have real exchange open/close times,
    but volume patterns still roughly follow traditional session flows
    as institutional traders operate on these schedules.
    """
    utc_hour = datetime.now(timezone.utc).hour

    sessions = [
        {
            "session": "Asian Session",
            "hours": "00:00-08:00 UTC",
            "range_start": 0,
            "range_end": 8,
            "characteristics": "Lower volume, range-bound, false breakouts common",
            "guidance": "Prefer range trading. Avoid breakout entries. Tight stops, quick targets.",
        },
        {
            "session": "London Open",
            "hours": "08:00-12:00 UTC",
            "range_start": 8,
            "range_end": 12,
            "characteristics": "Volume surge, trend initiation, Asian range break",
            "guidance": "Watch for Asian range breakout. First move often reverses — wait 30min confirmation.",
        },
        {
            "session": "London Midday",
            "hours": "12:00-13:00 UTC",
            "range_start": 12,
            "range_end": 13,
            "characteristics": "Consolidation, lower momentum between London open and NY overlap",
            "guidance": "Reduced conviction period. Wait for NY overlap for entries.",
        },
        {
            "session": "London/NY Overlap",
            "hours": "13:00-17:00 UTC",
            "range_start": 13,
            "range_end": 17,
            "characteristics": "Highest volume, strongest trends, institutional flow",
            "guidance": "Best session for trend following. Breakouts most reliable. Standard sizing.",
        },
        {
            "session": "NY Afternoon",
            "hours": "17:00-21:00 UTC",
            "range_start": 17,
            "range_end": 21,
            "characteristics": "Trend continuation or reversal, profit-taking",
            "guidance": "Watch for NY session reversals. Reduce size after 19:00 UTC.",
        },
        {
            "session": "NY Close / Roll-over",
            "hours": "21:00-00:00 UTC",
            "range_start": 21,
            "range_end": 24,
            "characteristics": "Volume decline, position squaring, choppy",
            "guidance": "Avoid new entries. Close intraday positions. Funding rate arbitrage window.",
        },
    ]

    for s in sessions:
        if s["range_start"] <= utc_hour < s["range_end"]:
            return {
                "session": s["session"],
                "hours": s["hours"],
                "characteristics": s["characteristics"],
                "guidance": s["guidance"],
            }

    return {
        "session": "Unknown",
        "hours": "N/A",
        "characteristics": "Session detection failed",
        "guidance": "Use standard risk management",
    }


class RegimeDurationTracker:
    """Tracks how long the market has been in each regime state."""

    def __init__(self, max_history: int = 200):
        self._history: OrderedDict = OrderedDict()
        self._max_history = max_history
        self._current_label: Optional[str] = None
        self._current_start_tick: Optional[int] = None
        self._tick_count = 0
        self._previous_label: Optional[str] = None
        self._last_flip_time: Optional[datetime] = None

    def update(self, label: str, score: float) -> Dict[str, Any]:
        self._tick_count += 1
        now = datetime.now(timezone.utc)

        flipped = label != self._current_label
        flip_direction = None

        if flipped:
            self._previous_label = self._current_label
            self._current_label = label
            self._current_start_tick = self._tick_count
            self._last_flip_time = now
            flip_direction = f"{self._previous_label} -> {label}"

        duration_ticks = (self._tick_count - self._current_start_tick + 1
                         if self._current_start_tick else 1)
        duration_hours = duration_ticks * 0.167

        self._history[self._tick_count] = {
            "label": label,
            "score": score,
            "flipped": flipped,
        }

        while len(self._history) > self._max_history:
            self._history.popitem(last=False)

        return {
            "duration_ticks": duration_ticks,
            "duration_hours": round(duration_hours, 1),
            "previous_regime": self._previous_label or "UNKNOWN",
            "flipped_this_tick": flipped,
            "flip_direction": flip_direction,
            "flip_time_iso": self._last_flip_time.isoformat() if self._last_flip_time else None,
        }


class AltcoinDivergenceDetector:
    """Detects when altcoins diverge from BTC regime direction."""

    def __init__(self, divergence_threshold: float = 0.7):
        self._threshold = divergence_threshold

    def evaluate(self, btc_regime_label: str, price_data: Dict[str, list]) -> Dict[str, Any]:
        btc_direction = self._label_to_direction(btc_regime_label)
        if btc_direction == "NEUTRAL":
            return {
                "divergence_detected": False,
                "reason": "BTC is neutral — no divergence to detect",
                "altcoin_trend": "N/A",
                "divergence_pct": 0.0,
            }

        diverging_count = 0
        total_alts = 0
        alt_trends = {}

        for symbol, candles in price_data.items():
            if symbol == "BTCUSDT" or len(candles) < 3:
                continue
            total_alts += 1
            alt_direction = self._candle_trend_direction(candles)
            alt_trends[symbol] = alt_direction
            if alt_direction != btc_direction and alt_direction != "FLAT":
                diverging_count += 1

        if total_alts == 0:
            return {
                "divergence_detected": False,
                "reason": "No altcoin data",
                "altcoin_trend": "UNKNOWN",
                "divergence_pct": 0.0,
            }

        divergence_pct = diverging_count / total_alts
        detected = divergence_pct >= self._threshold

        return {
            "divergence_detected": detected,
            "reason": (f"{diverging_count}/{total_alts} alts diverging from BTC "
                      f"({divergence_pct:.0%})" if total_alts > 0 else "No data"),
            "altcoin_trend": self._dominant_alt_trend(alt_trends),
            "divergence_pct": round(divergence_pct, 2),
            "diverging_symbols": [s for s, d in alt_trends.items()
                                 if d != btc_direction and d != "FLAT"],
        }

    @staticmethod
    def _label_to_direction(label: str) -> str:
        if "BULLISH" in label:
            return "UP"
        elif "BEARISH" in label:
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _candle_trend_direction(candles: list) -> str:
        if len(candles) < 2:
            return "FLAT"
        first_close = candles[0].get("close", 0)
        last_close = candles[-1].get("close", 0)
        if first_close == 0:
            return "FLAT"
        roc = (last_close - first_close) / first_close
        if roc > 0.02:
            return "UP"
        elif roc < -0.02:
            return "DOWN"
        return "FLAT"

    @staticmethod
    def _dominant_alt_trend(alt_trends: Dict[str, str]) -> str:
        if not alt_trends:
            return "UNKNOWN"
        counts = {}
        for trend in alt_trends.values():
            counts[trend] = counts.get(trend, 0) + 1
        return max(counts, key=counts.get)


_duration_tracker = RegimeDurationTracker()
_divergence_detector = AltcoinDivergenceDetector()


def store_regime_history(db, regime: Dict, btc_price: float = None,
                         source: str = "full") -> bool:
    """Write a regime result to market_regime_history table."""
    try:
        import json
        db.execute(
            "INSERT INTO market_regime_history "
            "(symbol, regime_score, regime_label, btc_price, components, source) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (regime.get("symbol", "BTCUSDT"),
             regime.get("score", 0),
             regime.get("label", "NEUTRAL_RANGE"),
             btc_price,
             json.dumps(regime.get("components", {})),
             source))
        return True
    except Exception as e:
        logger.warning(f"[Regime] Failed to store regime history: {e}")
        return False


def cleanup_regime_history(db, retention_days: int = 30) -> int:
    """Delete regime history rows older than retention_days."""
    try:
        result = db.execute(
            "DELETE FROM market_regime_history "
            "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)",
            (retention_days,))
        deleted = result if isinstance(result, int) else 0
        if deleted > 0:
            logger.info(f"[Regime] Cleaned up {deleted} old regime history rows")
        return deleted
    except Exception as e:
        logger.debug(f"[Regime] Regime history cleanup failed: {e}")
        return 0
