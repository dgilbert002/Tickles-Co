"""Comparator implementations — translate signal into ``AuditRecord``s.

We currently ship three comparators:

  * ``ParityComparator`` — wraps Phase 19's ``parity_summary`` and
    emits one AuditRecord per engine examined.
  * ``LiveVsBacktestComparator`` — compares a single live fill to
    the corresponding backtest fill (stub + hooks for Phase 26).
  * ``FeeSlippageComparator`` — compares realised fee / slippage
    rates to the backtest assumptions.

Each comparator is pure in the sense that it returns the records it
produced; the ``ContinuousAuditor`` owns the persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

import pandas as pd

from shared.auditor.schema import AuditEventType, AuditRecord, AuditSeverity
from shared.backtest.engines.protocol import StrategyFn
from shared.backtest.parity import ParityTolerances


# ---------------------------------------------------------------- parity


@dataclass
class ParityCheckInput:
    strategy_id: str
    candles_df: pd.DataFrame
    strategy: StrategyFn
    cfg: Any  # BacktestConfig; avoid hard import here
    engines: Optional[List[str]] = None
    tolerances: Optional[ParityTolerances] = None


class ParityComparator:
    """Runs cross-engine parity; turns the report into audit records."""

    def __init__(self, source_of_truth: str = "classic") -> None:
        self.source_of_truth = source_of_truth

    def compare(self, inp: ParityCheckInput) -> List[AuditRecord]:
        from shared.backtest.parity import parity_summary

        report = parity_summary(
            inp.candles_df,
            inp.strategy,
            inp.cfg,
            engines=inp.engines,
            source_of_truth=self.source_of_truth,
            tolerances=inp.tolerances,
        )

        records: List[AuditRecord] = []
        digests_by_engine = {d.engine: d for d in report.digests}
        truth = digests_by_engine.get(self.source_of_truth)

        for engine_name, digest in digests_by_engine.items():
            if not digest.available:
                records.append(
                    AuditRecord(
                        event_type=AuditEventType.PARITY_CHECK,
                        severity=AuditSeverity.WARNING,
                        subject=f"{inp.strategy_id}@{engine_name}",
                        strategy_id=inp.strategy_id,
                        engine=engine_name,
                        passed=False,
                        details={
                            "reason": "engine_unavailable",
                            "error": digest.error,
                        },
                    )
                )
                continue

            if engine_name == self.source_of_truth:
                records.append(
                    AuditRecord(
                        event_type=AuditEventType.PARITY_CHECK,
                        severity=AuditSeverity.OK,
                        subject=f"{inp.strategy_id}@{engine_name}",
                        strategy_id=inp.strategy_id,
                        engine=engine_name,
                        passed=True,
                        details={
                            "note": "source_of_truth",
                            "num_trades": digest.num_trades,
                            "pnl_pct": digest.pnl_pct,
                            "sharpe": digest.sharpe,
                        },
                    )
                )
                continue

            passed = report.passes.get(engine_name, False)
            divergences = report.divergences.get(engine_name, [])
            severity = AuditSeverity.OK if passed else AuditSeverity.BREACH
            metric = None
            if truth is not None and truth.pnl_pct is not None and digest.pnl_pct is not None:
                metric = abs(digest.pnl_pct - truth.pnl_pct)

            records.append(
                AuditRecord(
                    event_type=AuditEventType.PARITY_CHECK,
                    severity=severity,
                    subject=f"{inp.strategy_id}@{engine_name}",
                    strategy_id=inp.strategy_id,
                    engine=engine_name,
                    passed=passed,
                    metric=metric,
                    tolerance=(
                        report.tolerances.pnl_pct_abs if hasattr(report, "tolerances") else None
                    ),
                    details={
                        "divergences": divergences,
                        "digest": {
                            "num_trades": digest.num_trades,
                            "pnl_pct": digest.pnl_pct,
                            "sharpe": digest.sharpe,
                            "winrate": digest.winrate,
                            "max_drawdown": digest.max_drawdown,
                            "runtime_ms": digest.runtime_ms,
                        },
                        "truth_digest": (
                            None
                            if truth is None
                            else {
                                "num_trades": truth.num_trades,
                                "pnl_pct": truth.pnl_pct,
                                "sharpe": truth.sharpe,
                                "winrate": truth.winrate,
                                "max_drawdown": truth.max_drawdown,
                            }
                        ),
                    },
                )
            )
        return records


# ----------------------------------------------------- live vs backtest


@dataclass
class LiveTrade:
    trade_id: str
    strategy_id: str
    engine: str
    side: str  # long/short
    qty: float
    entry_price: float
    exit_price: float
    fees_paid: float
    slippage_paid: float
    pnl_pct: float


@dataclass
class BacktestTrade:
    strategy_id: str
    engine: str
    entry_price: float
    exit_price: float
    fees: float
    slippage: float
    pnl_pct: float


@dataclass
class LiveVsBacktestTolerances:
    entry_price_bps: float = 10.0  # 10 basis points
    exit_price_bps: float = 10.0
    pnl_pct_abs: float = 0.25  # 25bp
    fee_bps: float = 5.0
    slippage_bps: float = 10.0


class LiveVsBacktestComparator:
    """Compares a single live trade to its backtest twin.

    Phase 26 will call ``compare(live, backtest)`` for every fill on a
    live account. Today the class is fully functional but nobody calls
    it in production yet — it's exercised only through unit tests and
    the CLI's ``simulate`` subcommand.
    """

    def __init__(self, tolerances: Optional[LiveVsBacktestTolerances] = None) -> None:
        self.tolerances = tolerances or LiveVsBacktestTolerances()

    def compare(self, live: LiveTrade, backtest: BacktestTrade) -> List[AuditRecord]:
        records: List[AuditRecord] = []
        tol = self.tolerances

        def bps(a: float, b: float) -> float:
            if a == 0:
                return 0.0
            return abs(a - b) / abs(a) * 10_000.0

        entry_bps = bps(live.entry_price, backtest.entry_price)
        exit_bps = bps(live.exit_price, backtest.exit_price)
        pnl_diff = abs(live.pnl_pct - backtest.pnl_pct)
        fee_bps = bps(live.fees_paid, backtest.fees) if backtest.fees else 0.0
        slip_bps = bps(live.slippage_paid, backtest.slippage) if backtest.slippage else 0.0

        records.append(
            AuditRecord(
                event_type=AuditEventType.LIVE_VS_BACKTEST,
                severity=(
                    AuditSeverity.OK
                    if entry_bps <= tol.entry_price_bps and exit_bps <= tol.exit_price_bps
                    and pnl_diff <= tol.pnl_pct_abs
                    else AuditSeverity.BREACH
                ),
                subject=f"trade:{live.trade_id}",
                strategy_id=live.strategy_id,
                engine=live.engine,
                passed=(
                    entry_bps <= tol.entry_price_bps
                    and exit_bps <= tol.exit_price_bps
                    and pnl_diff <= tol.pnl_pct_abs
                ),
                metric=pnl_diff,
                tolerance=tol.pnl_pct_abs,
                details={
                    "entry_bps": entry_bps,
                    "exit_bps": exit_bps,
                    "pnl_pct_diff": pnl_diff,
                    "fee_bps": fee_bps,
                    "slippage_bps": slip_bps,
                    "live": live.__dict__,
                    "backtest": backtest.__dict__,
                },
            )
        )
        return records


# ------------------------------------------------------ fees & slippage


@dataclass
class FeeSlippageObservation:
    strategy_id: str
    engine: str
    assumed_fee_bps: float
    realised_fee_bps: float
    assumed_slippage_bps: float
    realised_slippage_bps: float


class FeeSlippageComparator:
    """Flags drift between backtest fee/slippage assumptions and live reality."""

    def __init__(
        self, fee_tolerance_bps: float = 2.0, slippage_tolerance_bps: float = 5.0
    ) -> None:
        self.fee_tol = fee_tolerance_bps
        self.slip_tol = slippage_tolerance_bps

    def compare(self, obs: FeeSlippageObservation) -> List[AuditRecord]:
        records: List[AuditRecord] = []
        fee_drift = abs(obs.realised_fee_bps - obs.assumed_fee_bps)
        slip_drift = abs(obs.realised_slippage_bps - obs.assumed_slippage_bps)
        fee_passed = fee_drift <= self.fee_tol
        slip_passed = slip_drift <= self.slip_tol

        records.append(
            AuditRecord(
                event_type=AuditEventType.FEE_DRIFT,
                severity=AuditSeverity.OK if fee_passed else AuditSeverity.WARNING,
                subject=f"{obs.strategy_id}@{obs.engine}",
                strategy_id=obs.strategy_id,
                engine=obs.engine,
                passed=fee_passed,
                metric=fee_drift,
                tolerance=self.fee_tol,
                details={
                    "assumed_fee_bps": obs.assumed_fee_bps,
                    "realised_fee_bps": obs.realised_fee_bps,
                },
            )
        )
        records.append(
            AuditRecord(
                event_type=AuditEventType.SLIPPAGE_DRIFT,
                severity=AuditSeverity.OK if slip_passed else AuditSeverity.WARNING,
                subject=f"{obs.strategy_id}@{obs.engine}",
                strategy_id=obs.strategy_id,
                engine=obs.engine,
                passed=slip_passed,
                metric=slip_drift,
                tolerance=self.slip_tol,
                details={
                    "assumed_slippage_bps": obs.assumed_slippage_bps,
                    "realised_slippage_bps": obs.realised_slippage_bps,
                },
            )
        )
        return records
