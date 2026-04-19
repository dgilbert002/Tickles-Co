"""
shared.regime.classifiers.trend — deterministic trend classifier.

The trend classifier looks at fast/slow simple moving averages and
the slope of the slow SMA to decide bull/bear/sideways.

Rules (in priority order):

  1. If we do not have at least ``slow`` candles → ``unknown``.
  2. Slope-normalised by price must exceed ``min_slope`` (as a
     fraction of the slow SMA) in one direction, AND the fast SMA
     must agree with that direction, to call bull/bear.
  3. Otherwise → sideways.

All numbers are pure Python (no numpy / pandas) so the classifier
can run inside systemd services without heavy deps.
"""
from __future__ import annotations

from typing import Sequence

from shared.regime.protocol import (
    CLASSIFIER_TREND,
    Candle,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_SIDEWAYS,
    REGIME_UNKNOWN,
    RegimeSignal,
)


class TrendClassifier:
    """Fast vs slow SMA + slope of the slow SMA."""

    name: str = CLASSIFIER_TREND

    def __init__(
        self,
        *,
        fast: int = 20,
        slow: int = 50,
        slope_lookback: int = 10,
        min_slope: float = 0.0005,   # 0.05 % per candle of price
    ) -> None:
        if fast <= 0 or slow <= 0 or slope_lookback <= 0:
            raise ValueError("fast/slow/slope_lookback must be > 0")
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self.fast = int(fast)
        self.slow = int(slow)
        self.slope_lookback = int(slope_lookback)
        self.min_slope = float(min_slope)

    # ------------------------------------------------------------------

    @staticmethod
    def _sma(values: Sequence[float], window: int) -> float:
        if window <= 0 or len(values) < window:
            return float("nan")
        tail = values[-window:]
        return sum(tail) / float(window)

    def classify(
        self,
        candles: Sequence[Candle],
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
    ) -> RegimeSignal:
        closes = [float(c.close) for c in candles]
        n = len(closes)
        last_ts = candles[-1].ts if candles else None

        if n < self.slow:
            return RegimeSignal(
                universe=universe,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                classifier=self.name,
                regime=REGIME_UNKNOWN,
                confidence=0.0,
                sample_size=n,
                as_of=last_ts,
                reason=f"need >= {self.slow} candles, got {n}",
                features={"fast": self.fast, "slow": self.slow},
            )

        fast_sma = self._sma(closes, self.fast)
        slow_sma_now = self._sma(closes, self.slow)
        slow_sma_prev = self._sma(
            closes[: max(1, n - self.slope_lookback)], self.slow
        )

        slope = (slow_sma_now - slow_sma_prev) / self.slope_lookback
        slope_pct = slope / slow_sma_now if slow_sma_now else 0.0

        above_slow = fast_sma > slow_sma_now
        below_slow = fast_sma < slow_sma_now

        # Decision
        if slope_pct >= self.min_slope and above_slow:
            regime = REGIME_BULL
            confidence = min(1.0, abs(slope_pct) / (self.min_slope * 3.0))
            reason = "fast>slow and slow slope > +min_slope"
        elif slope_pct <= -self.min_slope and below_slow:
            regime = REGIME_BEAR
            confidence = min(1.0, abs(slope_pct) / (self.min_slope * 3.0))
            reason = "fast<slow and slow slope < -min_slope"
        else:
            regime = REGIME_SIDEWAYS
            confidence = 1.0 - min(1.0, abs(slope_pct) / self.min_slope)
            reason = "slope/|direction| below threshold"

        return RegimeSignal(
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            classifier=self.name,
            regime=regime,
            confidence=float(confidence),
            trend_score=float(slope_pct),
            sample_size=n,
            as_of=last_ts,
            reason=reason,
            features={
                "fast": self.fast,
                "slow": self.slow,
                "slope_lookback": self.slope_lookback,
                "min_slope": self.min_slope,
                "fast_sma": float(fast_sma),
                "slow_sma_now": float(slow_sma_now),
                "slow_sma_prev": float(slow_sma_prev),
                "slope_abs": float(slope),
                "slope_pct": float(slope_pct),
            },
        )


__all__ = ["TrendClassifier"]
