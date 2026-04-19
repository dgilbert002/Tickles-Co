"""Tickles-Co Feature Store (Phase 20).

A lightweight, Feast-style feature-store layer that sits on top of the
existing Redis + DuckDB + candle infrastructure. It exists so that
strategies / agents / the optimiser never have to re-derive
indicators at call-site — they ask the feature store for a vector
and the store returns either the latest online value (Redis) or a
point-in-time historical range (DuckDB / parquet).

This module intentionally does NOT depend on the `feast` package.
The API mirrors Feast's core primitives (Entity, Feature,
FeatureView, FeatureStore) so we can migrate later with minimal
churn, but the implementation uses our own Redis / DuckDB clients so
the install footprint stays small.

Public surface::

    from shared.features import (
        Entity, Feature, FeatureView, FeatureStore,
        list_feature_views, get_feature_view, register_feature_view,
        feature_sets,
    )
"""

from shared.features.schema import (  # noqa: F401
    Entity,
    Feature,
    FeatureView,
    FeatureDtype,
)
from shared.features.registry import (  # noqa: F401
    register_feature_view,
    get_feature_view,
    list_feature_views,
    FEATURE_VIEWS,
)
from shared.features.store import FeatureStore  # noqa: F401
from shared.features import feature_sets  # noqa: F401

# Register built-in feature views on import.
try:
    feature_sets.register_all()
except Exception:  # pragma: no cover
    pass
