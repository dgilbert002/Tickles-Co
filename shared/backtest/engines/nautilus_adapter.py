"""
shared.backtest.engines.nautilus_adapter — scaffolded NautilusTrader engine.

Phase 19 lays the scaffolding; the full implementation lands in
**Phase 26 (Execution Layer on NautilusTrader)**. We deliberately
keep this file small and honest:

  * ``available()`` returns True iff ``nautilus_trader`` imports, but
  * ``run()`` raises RuntimeError pointing to Phase 26.

Putting this stub in the registry today means Dean's dashboards and
agent tooling can already call ``engines.list_engines()`` and see
that Nautilus is planned — no surprise integrations later.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from shared.backtest.engine import BacktestConfig, BacktestResult
from shared.backtest.engines.protocol import (
    BacktestEngine,
    EngineCapabilities,
    StrategyFn,
)


class NautilusEngine(BacktestEngine):
    """Scaffolding only — real implementation lands in Phase 26."""

    name = "nautilus"

    capabilities = EngineCapabilities(
        supports_intrabar_sl_tp=True,
        supports_funding=True,
        supports_fees=True,
        supports_slippage=True,
        supports_vectorised_sweep=False,
        supports_walk_forward=True,
        notes=(
            "Scaffolded in Phase 19; full implementation lands in "
            "Phase 26 (Execution Layer). Once active this engine will "
            "provide exact live/backtest parity via NautilusTrader's "
            "event loop and a shared strategy runner."
        ),
    )

    def available(self) -> bool:
        try:
            import nautilus_trader  # noqa: F401
            return True
        except Exception:
            return False

    def run(
        self,
        candles_df: pd.DataFrame,
        strategy: StrategyFn,
        cfg: BacktestConfig,
        crash_protection_series: Optional[pd.Series] = None,
    ) -> BacktestResult:
        raise RuntimeError(
            "NautilusEngine.run: scaffolded in Phase 19; implementation "
            "lands in Phase 26 (Execution Layer on NautilusTrader). "
            "Use engine 'classic' or 'vectorbt' until then."
        )


__all__ = ["NautilusEngine"]
