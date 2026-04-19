"""
shared.backtest.engines — Backtest Engine 2.0 (Phase 19).

The 21-year-old version
=======================
``shared/backtest/engine.py`` already gives us a tight, deterministic,
bar-by-bar event-loop engine (the one Dean audited on 2026-04-17).
That engine is *accurate* but not *fast*: it's a Python for-loop over
every bar, every run. Running 1,000 param combinations for Scout
would take hours.

Phase 19 adds two alternative backends that plug into the same
strategy / config contract:

  * ``classic`` — wraps the existing engine.py. Our source-of-truth.
  * ``vectorbt`` — wraps vectorbt's vectorised ``Portfolio.from_signals``
    for fast parameter sweeps. Approximate fill model (fills at same
    bar close by default in vbt; we translate our "next-bar open"
    rule onto vbt by shifting entries forward by one bar).
  * ``nautilus`` — scaffolded adapter for NautilusTrader. Full
    implementation lands in Phase 26 when the execution layer goes
    live; today the factory raises NotImplementedError with a clear
    message.

Downstream code (``cli/engines_cli.py``, ``parity.py``) looks up an
engine by name via ``get(name)`` so strategies and sweeps can target
different engines without code changes.
"""
from __future__ import annotations

from typing import Dict, List

from shared.backtest.engines.protocol import (
    BacktestEngine,
    EngineCapabilities,
)
from shared.backtest.engines.classic import ClassicEngine
from shared.backtest.engines.vectorbt_adapter import VectorBTEngine
from shared.backtest.engines.nautilus_adapter import NautilusEngine


_REGISTRY: Dict[str, BacktestEngine] = {}


def _register(engine: BacktestEngine) -> None:
    _REGISTRY[engine.name] = engine


_register(ClassicEngine())
_register(VectorBTEngine())
_register(NautilusEngine())


def get(name: str) -> BacktestEngine:
    """Return the engine registered under ``name``."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown backtest engine {name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def list_engines() -> List[str]:
    return sorted(_REGISTRY.keys())


def capabilities() -> Dict[str, EngineCapabilities]:
    return {name: eng.capabilities for name, eng in _REGISTRY.items()}


__all__ = [
    "BacktestEngine",
    "EngineCapabilities",
    "get",
    "list_engines",
    "capabilities",
]
