"""
shared.regime.classifiers.volatility — volatility regime classifier.

Labels:

  * ``high_vol`` — realised vol over the window is above the long-run
    quantile threshold.
  * ``low_vol``  — realised vol is below the lower threshold.
  * ``sideways`` — in-between.
  * ``unknown``  — not enough candles.

Realised vol is computed from log returns; the classifier also
exposes drawdown so the composite classifier can re-use it.
"""
from __future__ import annotations

import math
from typing import List, Sequence

from shared.regime.protocol import (
    CLASSIFIER_VOLATILITY,
    Candle,
    REGIME_HIGH_VOL,
    REGIME_LOW_VOL,
    REGIME_SIDEWAYS,
    REGIME_UNKNOWN,
    RegimeSignal,
)


class VolatilityClassifier:
    """Realised volatility + drawdown classifier."""

    name: str = CLASSIFIER_VOLATILITY

    def __init__(
        self,
        *,
        window: int = 48,
        high_threshold: float = 0.04,   # 4 % stdev of log returns
        low_threshold: float = 0.01,    # 1 %
    ) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        if high_threshold <= low_threshold:
            raise ValueError("high_threshold must be > low_threshold")
        self.window = int(window)
        self.high_threshold = float(high_threshold)
        self.low_threshold = float(low_threshold)

    @staticmethod
    def _log_returns(closes: Sequence[float]) -> List[float]:
        out: List[float] = []
        prev = None
        for c in closes:
            if prev is not None and prev > 0.0 and c > 0.0:
                out.append(math.log(c / prev))
            prev = c
        return out

    @staticmethod
    def _stdev(values: Sequence[float]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        return math.sqrt(var)

    @staticmethod
    def _drawdown(closes: Sequence[float]) -> float:
        if not closes:
            return 0.0
        peak = closes[0]
        max_dd = 0.0
        for c in closes:
            if c > peak:
                peak = c
            if peak > 0.0:
                dd = (peak - c) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

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

        if n < self.window + 1:
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
                reason=f"need >= {self.window + 1} candles, got {n}",
                features={"window": self.window},
            )

        tail = closes[-(self.window + 1):]
        rets = self._log_returns(tail)
        vol = self._stdev(rets)
        dd = self._drawdown(tail)

        if vol >= self.high_threshold:
            regime = REGIME_HIGH_VOL
            reason = f"stdev {vol:.4f} >= high_threshold {self.high_threshold:.4f}"
            confidence = min(1.0, vol / (self.high_threshold * 2.0))
        elif vol <= self.low_threshold:
            regime = REGIME_LOW_VOL
            reason = f"stdev {vol:.4f} <= low_threshold {self.low_threshold:.4f}"
            confidence = min(1.0, self.low_threshold / max(1e-9, vol * 2.0))
        else:
            regime = REGIME_SIDEWAYS
            reason = "stdev inside the band"
            span = self.high_threshold - self.low_threshold
            mid = (self.high_threshold + self.low_threshold) / 2.0
            confidence = 1.0 - (abs(vol - mid) / max(1e-9, span))

        return RegimeSignal(
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            classifier=self.name,
            regime=regime,
            confidence=float(max(0.0, min(1.0, confidence))),
            volatility=float(vol),
            drawdown=float(dd),
            sample_size=n,
            as_of=last_ts,
            reason=reason,
            features={
                "window": self.window,
                "high_threshold": self.high_threshold,
                "low_threshold": self.low_threshold,
                "stdev_log_returns": float(vol),
                "max_drawdown": float(dd),
            },
        )


__all__ = ["VolatilityClassifier"]
