"""Phase 27 Regime Service — public API."""
from __future__ import annotations

from pathlib import Path

from shared.regime.classifiers import (
    CompositeClassifier,
    TrendClassifier,
    VolatilityClassifier,
)
from shared.regime.memory_pool import InMemoryRegimePool
from shared.regime.protocol import (
    CLASSIFIER_COMPOSITE,
    CLASSIFIER_NAMES,
    CLASSIFIER_TREND,
    CLASSIFIER_VOLATILITY,
    Candle,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_CRASH,
    REGIME_HIGH_VOL,
    REGIME_LABELS,
    REGIME_LOW_VOL,
    REGIME_RECOVERY,
    REGIME_SIDEWAYS,
    REGIME_UNKNOWN,
    RegimeClassifier,
    RegimeSignal,
)
from shared.regime.service import CandlesLoader, RegimeService, build_classifier
from shared.regime.store import RegimeConfigRow, RegimeStateRow, RegimeStore

MIGRATION_PATH: Path = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase27_regime.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "Candle",
    "RegimeClassifier",
    "RegimeSignal",
    "RegimeService",
    "RegimeStore",
    "RegimeStateRow",
    "RegimeConfigRow",
    "TrendClassifier",
    "VolatilityClassifier",
    "CompositeClassifier",
    "InMemoryRegimePool",
    "CandlesLoader",
    "build_classifier",
    "CLASSIFIER_TREND",
    "CLASSIFIER_VOLATILITY",
    "CLASSIFIER_COMPOSITE",
    "CLASSIFIER_NAMES",
    "REGIME_BULL", "REGIME_BEAR", "REGIME_SIDEWAYS",
    "REGIME_CRASH", "REGIME_RECOVERY",
    "REGIME_HIGH_VOL", "REGIME_LOW_VOL", "REGIME_UNKNOWN",
    "REGIME_LABELS",
]
