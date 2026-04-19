"""
shared.regime.protocol — public interfaces and data classes for the
Phase 27 Regime Service.

A classifier takes a window of OHLCV candles and returns a
:class:`RegimeSignal`. All classifiers must be *deterministic* —
same input, same output — so regression tests can pin the output
and the Rule-1 auditor can replay them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable


REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_SIDEWAYS = "sideways"
REGIME_CRASH = "crash"
REGIME_RECOVERY = "recovery"
REGIME_HIGH_VOL = "high_vol"
REGIME_LOW_VOL = "low_vol"
REGIME_UNKNOWN = "unknown"

REGIME_LABELS = {
    REGIME_BULL,
    REGIME_BEAR,
    REGIME_SIDEWAYS,
    REGIME_CRASH,
    REGIME_RECOVERY,
    REGIME_HIGH_VOL,
    REGIME_LOW_VOL,
    REGIME_UNKNOWN,
}


CLASSIFIER_TREND = "trend"
CLASSIFIER_VOLATILITY = "volatility"
CLASSIFIER_COMPOSITE = "composite"

CLASSIFIER_NAMES = {
    CLASSIFIER_TREND,
    CLASSIFIER_VOLATILITY,
    CLASSIFIER_COMPOSITE,
}


@dataclass(frozen=True)
class Candle:
    """Minimal OHLCV row the classifiers consume."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class RegimeSignal:
    """Output of a classifier for a single (symbol, timeframe, tick)."""

    universe: str
    exchange: str
    symbol: str
    timeframe: str
    classifier: str
    regime: str
    confidence: float = 0.0
    trend_score: Optional[float] = None
    volatility: Optional[float] = None
    drawdown: Optional[float] = None
    sample_size: int = 0
    as_of: Optional[datetime] = None
    reason: Optional[str] = None
    features: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "universe": self.universe,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "classifier": self.classifier,
            "regime": self.regime,
            "confidence": float(self.confidence),
            "trend_score": self.trend_score,
            "volatility": self.volatility,
            "drawdown": self.drawdown,
            "sample_size": int(self.sample_size),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "reason": self.reason,
            "features": dict(self.features),
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class RegimeClassifier(Protocol):
    """Pure classifier protocol.

    Implementations *must not* touch the filesystem, network, or
    database. All state comes from ``candles`` and the instance's
    constructor-time params.
    """

    name: str

    def classify(
        self,
        candles: Sequence[Candle],
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
    ) -> RegimeSignal:
        ...


__all__ = [
    "Candle",
    "RegimeSignal",
    "RegimeClassifier",
    "REGIME_BULL",
    "REGIME_BEAR",
    "REGIME_SIDEWAYS",
    "REGIME_CRASH",
    "REGIME_RECOVERY",
    "REGIME_HIGH_VOL",
    "REGIME_LOW_VOL",
    "REGIME_UNKNOWN",
    "REGIME_LABELS",
    "CLASSIFIER_TREND",
    "CLASSIFIER_VOLATILITY",
    "CLASSIFIER_COMPOSITE",
    "CLASSIFIER_NAMES",
]


# ``List`` re-export for static-type checkers that read this module's
# public surface. The classifiers subpackage avoids the import cycle.
_List = List
