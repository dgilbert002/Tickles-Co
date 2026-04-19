"""
shared.backtest.engines.protocol — common interface for Phase 19 engines.

The 21-year-old version
=======================
Every backtest engine in the Phase 19 family must answer three
questions:

  1. What's your name? (for registry lookup + logging)
  2. What can you do? (capabilities — SL/TP intrabar, funding,
     vectorised sweeps, walk-forward, etc.)
  3. Given candles + strategy + config, return a BacktestResult.

Keeping the contract tiny means we can add more engines later (a Rust
FFI version, a Cython version, a NautilusTrader version) without the
callers changing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

import pandas as pd

from shared.backtest.engine import BacktestConfig, BacktestResult


StrategyFn = Callable[[pd.DataFrame, Dict[str, Any]], pd.Series]


@dataclass(frozen=True)
class EngineCapabilities:
    """What a given engine implementation is able to model."""

    supports_intrabar_sl_tp: bool = False
    supports_funding: bool = False
    supports_fees: bool = True
    supports_slippage: bool = True
    supports_vectorised_sweep: bool = False
    supports_walk_forward: bool = False
    notes: str = ""


@runtime_checkable
class BacktestEngine(Protocol):
    """Uniform protocol every engine registered in Phase 19 implements."""

    name: str
    capabilities: EngineCapabilities

    def available(self) -> bool:
        """Return True if this engine's deps are importable in the current venv."""
        ...

    def run(
        self,
        candles_df: pd.DataFrame,
        strategy: StrategyFn,
        cfg: BacktestConfig,
        crash_protection_series: Optional[pd.Series] = None,
    ) -> BacktestResult:
        """Execute a single backtest and return our unified result object."""
        ...


@dataclass
class EngineUnavailable:
    """Raised via RuntimeError when an engine's deps are missing at run time."""

    engine: str
    reason: str
    install_hint: str = ""


def raise_unavailable(u: EngineUnavailable) -> None:
    raise RuntimeError(
        f"engine '{u.engine}' unavailable: {u.reason}"
        + (f" (install hint: {u.install_hint})" if u.install_hint else "")
    )


__all__ = [
    "BacktestEngine",
    "EngineCapabilities",
    "EngineUnavailable",
    "StrategyFn",
    "raise_unavailable",
]
