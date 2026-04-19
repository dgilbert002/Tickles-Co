"""Process-global registry for feature views.

Feature views register themselves at import time (see
``feature_sets.py``). Other code imports ``FEATURE_VIEWS`` or calls
``list_feature_views`` to discover what's available.
"""

from __future__ import annotations

from typing import Dict, List

from shared.features.schema import FeatureView

FEATURE_VIEWS: Dict[str, FeatureView] = {}


def register_feature_view(fv: FeatureView, *, overwrite: bool = False) -> None:
    if fv.name in FEATURE_VIEWS and not overwrite:
        return
    FEATURE_VIEWS[fv.name] = fv


def get_feature_view(name: str) -> FeatureView:
    if name not in FEATURE_VIEWS:
        raise KeyError(f"feature view {name!r} not registered")
    return FEATURE_VIEWS[name]


def list_feature_views() -> List[FeatureView]:
    return sorted(FEATURE_VIEWS.values(), key=lambda fv: fv.name)
