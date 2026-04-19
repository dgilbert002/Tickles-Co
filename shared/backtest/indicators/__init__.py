"""
Indicator package entry point — imports all modules so that their register()
calls populate the INDICATORS dict.

Agents and services should import from here:

    from backtest.indicators import INDICATORS, get, list_all

    sma_fn = get("sma")
    rsi = sma_fn(df, {"period": 20})
"""
from __future__ import annotations

from typing import Dict, List

# Import every indicator module to trigger their register() side effects.
from . import core       # noqa: F401
from . import smart_money  # noqa: F401
from . import crash_protect  # noqa: F401

from .core import INDICATORS, IndicatorSpec

# Phase 18 — wire the pandas_ta bridge and the hand-rolled extras.
# Both modules are no-ops if their dependencies are missing.
try:
    from . import extras as _extras_mod
    _extras_mod.register_all()
except Exception:  # noqa: BLE001 — indicators are best-effort additions
    pass

try:
    from . import pandas_ta_bridge as _pta_mod
    _pta_mod.register_all()
except Exception:  # noqa: BLE001 — bridge is optional (pandas_ta may be absent)
    pass


def get(name: str) -> IndicatorSpec:
    """Look up an indicator spec by name. Raises KeyError if unknown."""
    return INDICATORS[name]


def list_all() -> List[str]:
    """Alphabetical list of all registered indicator names."""
    return sorted(INDICATORS.keys())


def by_category(category: str) -> List[str]:
    return sorted(n for n, s in INDICATORS.items() if s.category == category)


def by_direction(direction: str) -> List[str]:
    return sorted(n for n, s in INDICATORS.items() if s.direction == direction)


def summary() -> Dict[str, Dict]:
    """Return a dict suitable for JSON export (catalog, UI)."""
    return {
        name: {
            "category":    spec.category,
            "direction":   spec.direction,
            "description": spec.description,
            "defaults":    spec.defaults,
            "param_ranges": spec.param_ranges,
            "asset_class": spec.asset_class,
        }
        for name, spec in INDICATORS.items()
    }
