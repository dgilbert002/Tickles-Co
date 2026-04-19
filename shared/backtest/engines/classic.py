"""
shared.backtest.engines.classic — our deterministic event-loop engine.

This is a *thin* wrapper around ``shared/backtest/engine.py`` so the
event-loop engine can participate in the Phase 19 registry without
touching the audited implementation itself.

The classic engine is the source of truth for Rule 1 (backtests must
equal live). Any new engine is evaluated against it via the parity
harness.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from shared.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    run_backtest,
)
from shared.backtest.engines.protocol import (
    BacktestEngine,
    EngineCapabilities,
    StrategyFn,
)


class ClassicEngine(BacktestEngine):
    """Deterministic, bar-by-bar, audited — source of truth for parity."""

    name = "classic"

    capabilities = EngineCapabilities(
        supports_intrabar_sl_tp=True,
        supports_funding=True,
        supports_fees=True,
        supports_slippage=True,
        supports_vectorised_sweep=False,
        supports_walk_forward=False,
        notes=(
            "Python event loop — O(n_bars). Matches live execution "
            "(next-bar-open fills, intrabar SL/TP). Slowest engine but "
            "source of truth for the parity harness."
        ),
    )

    def available(self) -> bool:
        return True

    def run(
        self,
        candles_df: pd.DataFrame,
        strategy: StrategyFn,
        cfg: BacktestConfig,
        crash_protection_series: Optional[pd.Series] = None,
    ) -> BacktestResult:
        return run_backtest(
            candles_df=candles_df,
            strategy=strategy,
            cfg=cfg,
            crash_protection_series=crash_protection_series,
        )


__all__ = ["ClassicEngine"]
