"""Schema primitives for the feature store.

These mirror Feast's core types. We keep them small and dependency-
free so they can be imported from anywhere without dragging the full
feature-store machinery with them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import pandas as pd


class FeatureDtype(str, Enum):
    """Supported feature value types.

    Kept deliberately narrow — our online store (Redis) serialises
    everything to strings, and DuckDB auto-casts primitive types
    cleanly.
    """

    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    STRING = "string"


@dataclass(frozen=True)
class Entity:
    """An addressable thing we attach features to.

    Examples: ``asset`` (symbol like BTC/USDT), ``venue`` (binance),
    ``strategy`` (strategy_id), ``account`` (account_id).
    """

    name: str
    description: str = ""
    join_keys: List[str] = field(default_factory=list)

    def primary_key(self) -> str:
        return self.join_keys[0] if self.join_keys else self.name


@dataclass(frozen=True)
class Feature:
    """A single named feature within a FeatureView."""

    name: str
    dtype: FeatureDtype
    description: str = ""


@dataclass
class FeatureView:
    """A group of related features computed together.

    ``compute`` receives (candles_df, entity_key, params) and must
    return a DataFrame indexed by timestamp with one column per
    feature in ``features``. The materializer calls this function for
    each entity key and writes the result to the online + offline
    stores.
    """

    name: str
    entities: List[Entity]
    features: List[Feature]
    source: str = "candles"
    ttl_seconds: int = 24 * 60 * 60
    description: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    compute: Optional[Callable[[pd.DataFrame, str, Dict[str, Any]], pd.DataFrame]] = None

    def feature_names(self) -> List[str]:
        return [f.name for f in self.features]

    def primary_entity(self) -> Entity:
        if not self.entities:
            raise ValueError(f"FeatureView {self.name!r} has no entities")
        return self.entities[0]

    def validate_output(self, df: pd.DataFrame) -> None:
        """Sanity-check a compute() output before storage."""
        expected = set(self.feature_names())
        actual = set(df.columns)
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"{self.name}: compute output missing features {sorted(missing)}")
        if extra:
            raise ValueError(f"{self.name}: compute output has unexpected features {sorted(extra)}")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(f"{self.name}: compute output must have DatetimeIndex")
