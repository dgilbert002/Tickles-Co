"""
shared.regime.classifiers.composite — trend + volatility + crash.

The composite classifier runs the trend and volatility classifiers
and combines their outputs with an extra crash/recovery check.

Decision table (priority top-down):

  * drawdown >= ``crash_dd`` AND trend == bear → ``crash``
  * drawdown >= ``crash_dd`` AND trend != bear → ``recovery``
  * vol == high_vol AND trend == bull            → ``bull``    (leave trend on top; vol metadata kept)
  * vol == high_vol AND trend == bear            → ``bear``
  * vol == high_vol AND trend == sideways        → ``high_vol``
  * vol == low_vol                               → ``low_vol``
  * else → trend.regime

Confidence is the min of the two sub-classifier confidences, with
a small boost for crash/recovery when drawdown is unambiguous.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

from shared.regime.classifiers.trend import TrendClassifier
from shared.regime.classifiers.volatility import VolatilityClassifier
from shared.regime.protocol import (
    CLASSIFIER_COMPOSITE,
    Candle,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_CRASH,
    REGIME_HIGH_VOL,
    REGIME_LOW_VOL,
    REGIME_RECOVERY,
    REGIME_SIDEWAYS,
    REGIME_UNKNOWN,
    RegimeSignal,
)


class CompositeClassifier:
    """Composite classifier combining trend + volatility + crash/recovery."""

    name: str = CLASSIFIER_COMPOSITE

    def __init__(
        self,
        *,
        trend: TrendClassifier | None = None,
        volatility: VolatilityClassifier | None = None,
        crash_dd: float = 0.10,   # 10 % drawdown triggers crash/recovery gate
    ) -> None:
        self.trend = trend or TrendClassifier()
        self.volatility = volatility or VolatilityClassifier()
        self.crash_dd = float(crash_dd)

    def classify(
        self,
        candles: Sequence[Candle],
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
    ) -> RegimeSignal:
        trend_sig = self.trend.classify(
            candles,
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
        )
        vol_sig = self.volatility.classify(
            candles,
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
        )

        if trend_sig.regime == REGIME_UNKNOWN or vol_sig.regime == REGIME_UNKNOWN:
            return RegimeSignal(
                universe=universe,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                classifier=self.name,
                regime=REGIME_UNKNOWN,
                confidence=0.0,
                trend_score=trend_sig.trend_score,
                volatility=vol_sig.volatility,
                drawdown=vol_sig.drawdown,
                sample_size=max(trend_sig.sample_size, vol_sig.sample_size),
                as_of=trend_sig.as_of or vol_sig.as_of,
                reason="sub-classifier returned unknown",
                features={
                    "trend": trend_sig.features,
                    "volatility": vol_sig.features,
                },
            )

        dd = float(vol_sig.drawdown or 0.0)

        regime: str
        reason: str

        if dd >= self.crash_dd and trend_sig.regime == REGIME_BEAR:
            regime = REGIME_CRASH
            reason = f"dd {dd:.3f} >= {self.crash_dd:.3f} and trend bear"
        elif dd >= self.crash_dd and trend_sig.regime != REGIME_BEAR:
            regime = REGIME_RECOVERY
            reason = f"dd {dd:.3f} >= {self.crash_dd:.3f}, trend not bear"
        elif vol_sig.regime == REGIME_HIGH_VOL and trend_sig.regime == REGIME_BULL:
            regime = REGIME_BULL
            reason = "trend bull with high vol"
        elif vol_sig.regime == REGIME_HIGH_VOL and trend_sig.regime == REGIME_BEAR:
            regime = REGIME_BEAR
            reason = "trend bear with high vol"
        elif vol_sig.regime == REGIME_HIGH_VOL and trend_sig.regime == REGIME_SIDEWAYS:
            regime = REGIME_HIGH_VOL
            reason = "sideways trend but high vol dominates"
        elif vol_sig.regime == REGIME_LOW_VOL:
            regime = REGIME_LOW_VOL
            reason = "low vol dominates"
        else:
            regime = trend_sig.regime
            reason = f"trend classifier: {trend_sig.reason}"

        base_conf = min(trend_sig.confidence, vol_sig.confidence)
        if regime in (REGIME_CRASH, REGIME_RECOVERY):
            # explicit drawdown trigger -> boosted confidence
            base_conf = max(base_conf, min(1.0, dd / self.crash_dd))

        features: Dict[str, Any] = {
            "crash_dd": self.crash_dd,
            "trend": {
                "regime": trend_sig.regime,
                "score": trend_sig.trend_score,
                "features": trend_sig.features,
            },
            "volatility": {
                "regime": vol_sig.regime,
                "stdev": vol_sig.volatility,
                "drawdown": vol_sig.drawdown,
                "features": vol_sig.features,
            },
        }

        return RegimeSignal(
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            classifier=self.name,
            regime=regime,
            confidence=float(base_conf),
            trend_score=trend_sig.trend_score,
            volatility=vol_sig.volatility,
            drawdown=vol_sig.drawdown,
            sample_size=max(trend_sig.sample_size, vol_sig.sample_size),
            as_of=trend_sig.as_of or vol_sig.as_of,
            reason=reason,
            features=features,
        )


__all__ = ["CompositeClassifier"]
