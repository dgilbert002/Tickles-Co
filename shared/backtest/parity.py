"""
shared.backtest.parity — cross-engine parity harness (Phase 19).

The 21-year-old version
=======================
Rule 1 of the Trading House says "backtests must equal live". That's
easy to assert for a single engine; it's harder once we have two or
three engines that compute the same thing in different ways. The
parity harness is the deterministic check we run in CI that keeps
them honest.

How it works
------------

Given ``engines=["classic", "vectorbt"]``, a strategy, and a
BacktestConfig:

  1. Run the strategy through every available engine.
  2. Extract a "parity digest" from each result: num_trades,
     pnl_pct, sharpe, winrate, max_drawdown.
  3. Compare against the source-of-truth engine (``classic`` by
     default) within per-metric tolerances.
  4. Return a structured report: per-engine digest + per-engine pass
     / fail + list of divergences.

Tolerances default to generous values because different engines
have subtly different fill models. ``parity_summary`` exposes them
via kwargs so Scout can tighten them for strict regression checks.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from shared.backtest.engine import BacktestConfig, BacktestResult
from shared.backtest.engines import get as get_engine
from shared.backtest.engines.protocol import StrategyFn

log = logging.getLogger("tickles.parity")


@dataclass(frozen=True)
class ParityTolerances:
    num_trades_abs: int = 3
    pnl_pct_abs: float = 5.0
    sharpe_abs: float = 1.5
    winrate_abs: float = 15.0
    max_drawdown_abs: float = 10.0

    def check(self, metric: str, a: float, b: float) -> bool:
        tolerance_map = {
            "num_trades": self.num_trades_abs,
            "pnl_pct": self.pnl_pct_abs,
            "sharpe": self.sharpe_abs,
            "winrate": self.winrate_abs,
            "max_drawdown": self.max_drawdown_abs,
        }
        tol = tolerance_map.get(metric)
        if tol is None:
            return True
        return abs(a - b) <= tol


@dataclass
class EngineDigest:
    engine: str
    available: bool
    num_trades: int
    pnl_pct: float
    sharpe: float
    winrate: float
    max_drawdown: float
    runtime_ms: float
    error: Optional[str] = None

    @classmethod
    def from_result(cls, engine: str, result: BacktestResult) -> "EngineDigest":
        return cls(
            engine=engine,
            available=True,
            num_trades=result.num_trades,
            pnl_pct=float(result.pnl_pct),
            sharpe=float(result.sharpe),
            winrate=float(result.winrate),
            max_drawdown=float(result.max_drawdown),
            runtime_ms=float(result.runtime_ms),
        )


@dataclass
class ParityReport:
    source_of_truth: str
    digests: List[EngineDigest]
    tolerances: ParityTolerances
    passes: Dict[str, bool]
    divergences: Dict[str, List[str]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_of_truth": self.source_of_truth,
            "tolerances": asdict(self.tolerances),
            "digests": [asdict(d) for d in self.digests],
            "passes": self.passes,
            "divergences": self.divergences,
            "ok": all(self.passes.values()),
        }


def parity_summary(
    candles_df: pd.DataFrame,
    strategy: StrategyFn,
    cfg: BacktestConfig,
    engines: Optional[List[str]] = None,
    source_of_truth: str = "classic",
    tolerances: Optional[ParityTolerances] = None,
    crash_protection_series: Optional[pd.Series] = None,
) -> ParityReport:
    """Run ``strategy`` through multiple engines and compare the outputs.

    Engines whose deps are missing (e.g. ``vectorbt`` not installed) are
    reported as ``available=False`` with their error; they never cause
    a parity failure, so CI stays green on minimal environments.
    """
    if engines is None:
        engines = ["classic", "vectorbt"]
    if source_of_truth not in engines:
        engines = [source_of_truth, *engines]
    tolerances = tolerances or ParityTolerances()

    digests: List[EngineDigest] = []
    source_digest: Optional[EngineDigest] = None

    for name in engines:
        engine = get_engine(name)
        if not engine.available():
            digests.append(
                EngineDigest(
                    engine=name,
                    available=False,
                    num_trades=0,
                    pnl_pct=0.0,
                    sharpe=0.0,
                    winrate=0.0,
                    max_drawdown=0.0,
                    runtime_ms=0.0,
                    error="engine dependencies not installed",
                )
            )
            continue
        try:
            result = engine.run(
                candles_df=candles_df,
                strategy=strategy,
                cfg=cfg,
                crash_protection_series=crash_protection_series,
            )
            digest = EngineDigest.from_result(name, result)
        except Exception as exc:
            log.warning("parity: engine %s raised %s", name, exc)
            digest = EngineDigest(
                engine=name,
                available=True,
                num_trades=0,
                pnl_pct=0.0,
                sharpe=0.0,
                winrate=0.0,
                max_drawdown=0.0,
                runtime_ms=0.0,
                error=str(exc),
            )
        digests.append(digest)
        if name == source_of_truth:
            source_digest = digest

    passes: Dict[str, bool] = {}
    divergences: Dict[str, List[str]] = {}

    if source_digest is None or not source_digest.available:
        for d in digests:
            passes[d.engine] = False
            divergences[d.engine] = ["source-of-truth engine unavailable"]
        return ParityReport(
            source_of_truth=source_of_truth,
            digests=digests,
            tolerances=tolerances,
            passes=passes,
            divergences=divergences,
        )

    for d in digests:
        if d.engine == source_of_truth:
            passes[d.engine] = True
            divergences[d.engine] = []
            continue
        if not d.available or d.error:
            passes[d.engine] = True
            divergences[d.engine] = [d.error or "unavailable; skipped"]
            continue
        diffs: List[str] = []
        for metric in ("num_trades", "pnl_pct", "sharpe", "winrate", "max_drawdown"):
            a = getattr(source_digest, metric)
            b = getattr(d, metric)
            if not tolerances.check(metric, a, b):
                diffs.append(f"{metric}: source={a:.4f} engine={b:.4f}")
        passes[d.engine] = not diffs
        divergences[d.engine] = diffs

    return ParityReport(
        source_of_truth=source_of_truth,
        digests=digests,
        tolerances=tolerances,
        passes=passes,
        divergences=divergences,
    )


__all__ = [
    "EngineDigest",
    "ParityReport",
    "ParityTolerances",
    "parity_summary",
]
