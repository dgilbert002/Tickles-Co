"""
shared.cli.auditor_cli — Rule-1 Continuous Auditor operator CLI (Phase 21).

Subcommands:

* ``status``                 — last heartbeat + rolling divergence summary.
* ``summary [--window S]``   — detailed rollup over a window.
* ``events [--limit N] [--severity BREACH]`` — tail recent events.
* ``run-parity-check``       — one-shot parity check on a synthetic
                               candle feed + SMA-cross strategy.
* ``simulate-live-trade``    — feed a fabricated (live, backtest)
                               trade pair through the comparator; used
                               for smoke tests of Phase 26 hooks.
* ``tick``                   — run one pass of the ContinuousAuditor
                               against the built-in synthetic job.
* ``run``                    — start the ContinuousAuditor forever
                               (for systemd). Blocking.
* ``purge-older --days N``   — delete events older than N days.

All stdout is single-line JSON.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from shared.auditor import (
    AuditStore,
    ContinuousAuditor,
    LiveVsBacktestComparator,
    ParityComparator,
)
from shared.auditor.auditor import AuditJob
from shared.auditor.comparator import (
    BacktestTrade,
    LiveTrade,
    LiveVsBacktestTolerances,
    ParityCheckInput,
)
from shared.auditor.schema import AuditEventType, AuditSeverity
from shared.cli._common import (
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)

log = logging.getLogger("tickles.auditor.cli")


# -------------------------------------------------- synthetic helpers


def _synthetic_candles(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n).cumsum()
    base = 100.0 + steps
    open_ = base + rng.uniform(-0.2, 0.2, n)
    high = np.maximum(open_, base) + rng.uniform(0.05, 0.5, n)
    low = np.minimum(open_, base) - rng.uniform(0.05, 0.5, n)
    close = base + rng.uniform(-0.2, 0.2, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {
            "openPrice": open_,
            "highPrice": high,
            "lowPrice": low,
            "closePrice": close,
            "openBid": open_ - 0.05,
            "closeAsk": close + 0.05,
            "volume": rng.uniform(100, 1000, n),
            "snapshotTime": idx,
        },
        index=idx,
    )
    return df


def _sma_cross(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    fast = int(params.get("fast", 5))
    slow = int(params.get("slow", 20))
    c = df["closePrice"].astype(float)
    f = c.rolling(fast).mean()
    s = c.rolling(slow).mean()
    state = pd.Series(0.0, index=df.index)
    state = state.mask(f > s, 1.0).mask(f < s, -1.0)
    prev = state.shift(1).fillna(0.0)
    cross = (state != prev).astype(float)
    out = pd.Series(0.0, index=df.index)
    out[(cross.astype(bool)) & (state != 0.0)] = state[(cross.astype(bool)) & (state != 0.0)]
    return out


def _default_cfg() -> Any:
    from shared.backtest.engine import BacktestConfig

    return BacktestConfig(
        symbol="SYNTH/USD",
        source="synthetic",
        timeframe="1m",
        start_date="2025-01-01",
        end_date="2025-01-02",
        direction="long",
        initial_capital=10_000.0,
        position_pct=95.0,
        fee_taker_bps=5.0,
        slippage_bps=2.0,
        strategy_name="sma_cross",
        indicator_name="sma",
        indicator_params={"fast": 5, "slow": 20},
    )


# ------------------------------------------------------- commands


def _make_store(args: argparse.Namespace) -> AuditStore:
    path = getattr(args, "db_path", None)
    return AuditStore(path=path)


def cmd_status(args: argparse.Namespace) -> int:
    with _make_store(args) as store:
        summary = store.summary(window_seconds=getattr(args, "window", 3600))
        last_hb = store.list_recent(
            limit=1, event_type=AuditEventType.HEARTBEAT
        )
        last_cov = store.list_recent(
            limit=1, event_type=AuditEventType.COVERAGE
        )
    emit(
        {
            "ok": True,
            "summary": asdict(summary),
            "last_heartbeat": last_hb[0].to_json() if last_hb else None,
            "last_coverage": last_cov[0].to_json() if last_cov else None,
        }
    )
    return EXIT_OK


def cmd_summary(args: argparse.Namespace) -> int:
    with _make_store(args) as store:
        summary = store.summary(window_seconds=args.window)
    emit({"ok": True, "summary": asdict(summary)})
    return EXIT_OK


def cmd_events(args: argparse.Namespace) -> int:
    sev = None
    if args.severity:
        sev = AuditSeverity(args.severity)
    etype = None
    if args.event_type:
        etype = AuditEventType(args.event_type)
    with _make_store(args) as store:
        recs = store.list_recent(limit=args.limit, event_type=etype, severity=sev)
    emit(
        {
            "ok": True,
            "count": len(recs),
            "events": [
                {
                    "id": r.id,
                    "ts_unix": r.ts_unix,
                    "event_type": r.event_type.value,
                    "severity": r.severity.value,
                    "subject": r.subject,
                    "strategy_id": r.strategy_id,
                    "engine": r.engine,
                    "passed": r.passed,
                    "metric": r.metric,
                    "tolerance": r.tolerance,
                    "details": r.details,
                }
                for r in recs
            ],
        }
    )
    return EXIT_OK


def cmd_run_parity_check(args: argparse.Namespace) -> int:
    from shared.backtest.parity import ParityTolerances

    df = _synthetic_candles()
    cfg = _default_cfg()
    comp = ParityComparator()
    tol_kwargs: Dict[str, Any] = {}
    if getattr(args, "pnl_pct_abs", None) is not None:
        tol_kwargs["pnl_pct_abs"] = float(args.pnl_pct_abs)
    if getattr(args, "sharpe_abs", None) is not None:
        tol_kwargs["sharpe_abs"] = float(args.sharpe_abs)
    if getattr(args, "winrate_abs", None) is not None:
        tol_kwargs["winrate_abs"] = float(args.winrate_abs)
    if getattr(args, "max_drawdown_abs", None) is not None:
        tol_kwargs["max_drawdown_abs"] = float(args.max_drawdown_abs)
    tolerances: Optional[ParityTolerances] = (
        ParityTolerances(**tol_kwargs) if tol_kwargs else None
    )
    inp = ParityCheckInput(
        strategy_id=args.strategy_id,
        candles_df=df,
        strategy=_sma_cross,
        cfg=cfg,
        engines=args.engines.split(",") if args.engines else None,
        tolerances=tolerances,
    )
    recs = comp.compare(inp)
    with _make_store(args) as store:
        for r in recs:
            store.record(r)
    emit(
        {
            "ok": all(r.passed for r in recs),
            "strategy_id": args.strategy_id,
            "count": len(recs),
            "events": [
                {
                    "engine": r.engine,
                    "severity": r.severity.value,
                    "passed": r.passed,
                    "metric": r.metric,
                    "tolerance": r.tolerance,
                }
                for r in recs
            ],
        }
    )
    return EXIT_OK


def cmd_simulate_live_trade(args: argparse.Namespace) -> int:
    live = LiveTrade(
        trade_id=args.trade_id,
        strategy_id=args.strategy_id,
        engine=args.engine,
        side=args.side,
        qty=args.qty,
        entry_price=args.live_entry,
        exit_price=args.live_exit,
        fees_paid=args.live_fee,
        slippage_paid=args.live_slippage,
        pnl_pct=args.live_pnl_pct,
    )
    bt = BacktestTrade(
        strategy_id=args.strategy_id,
        engine=args.engine,
        entry_price=args.bt_entry,
        exit_price=args.bt_exit,
        fees=args.bt_fee,
        slippage=args.bt_slippage,
        pnl_pct=args.bt_pnl_pct,
    )
    tol = LiveVsBacktestTolerances(
        entry_price_bps=args.entry_bps,
        exit_price_bps=args.exit_bps,
        pnl_pct_abs=args.pnl_pct_abs,
    )
    recs = LiveVsBacktestComparator(tolerances=tol).compare(live, bt)
    with _make_store(args) as store:
        for r in recs:
            store.record(r)
    emit(
        {
            "ok": all(r.passed for r in recs),
            "count": len(recs),
            "events": [
                {
                    "severity": r.severity.value,
                    "passed": r.passed,
                    "metric": r.metric,
                    "tolerance": r.tolerance,
                    "details": r.details,
                }
                for r in recs
            ],
        }
    )
    return EXIT_OK


def _default_job() -> AuditJob:
    return AuditJob(
        strategy_id="synthetic_sma_cross",
        strategy_fn=_sma_cross,
        candles_loader=lambda: _synthetic_candles(),
        cfg=_default_cfg(),
        engines=["classic", "vectorbt"],
    )


def cmd_tick(args: argparse.Namespace) -> int:
    with _make_store(args) as store:
        auditor = ContinuousAuditor(store=store, jobs=[_default_job()])
        recs = auditor.tick()
    emit(
        {
            "ok": True,
            "records_written": len(recs),
            "summary": {
                "passed": sum(1 for r in recs if r.passed),
                "failed": sum(1 for r in recs if not r.passed),
            },
        }
    )
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:  # pragma: no cover
    with _make_store(args) as store:
        auditor = ContinuousAuditor(store=store, jobs=[_default_job()])
        auditor.run_forever(interval_seconds=args.interval)
    return EXIT_OK


def cmd_purge(args: argparse.Namespace) -> int:
    cutoff = time.time() - (args.days * 86400.0)
    with _make_store(args) as store:
        n = store.purge_older_than(cutoff)
    emit({"ok": True, "deleted": n, "cutoff_ts_unix": cutoff})
    return EXIT_OK


# ---------------------------------------------------- subcommand wiring


def _common_db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db-path", default=None, help="override SQLite path")


def _build_status(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--window", type=int, default=3600)


def _build_summary(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--window", type=int, default=3600)


def _build_events(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--severity", default=None, choices=[s.value for s in AuditSeverity])
    p.add_argument("--event-type", default=None, choices=[e.value for e in AuditEventType])


def _build_run_parity(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--strategy-id", default="synthetic_sma_cross")
    p.add_argument("--engines", default="")
    p.add_argument("--pnl-pct-abs", type=float, default=None,
                   help="PnL percentage tolerance (absolute).")
    p.add_argument("--sharpe-abs", type=float, default=8.0,
                   help="Sharpe tolerance (absolute). Default 8.0 for synthetic.")
    p.add_argument("--winrate-abs", type=float, default=None,
                   help="Win-rate tolerance (absolute percentage points).")
    p.add_argument("--max-drawdown-abs", type=float, default=None,
                   help="Max-drawdown tolerance (absolute percentage points).")


def _build_simulate_trade(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--trade-id", default="sim-1")
    p.add_argument("--strategy-id", default="simulated")
    p.add_argument("--engine", default="classic")
    p.add_argument("--side", default="long")
    p.add_argument("--qty", type=float, default=1.0)
    p.add_argument("--live-entry", type=float, required=True)
    p.add_argument("--live-exit", type=float, required=True)
    p.add_argument("--live-fee", type=float, default=0.0)
    p.add_argument("--live-slippage", type=float, default=0.0)
    p.add_argument("--live-pnl-pct", type=float, required=True)
    p.add_argument("--bt-entry", type=float, required=True)
    p.add_argument("--bt-exit", type=float, required=True)
    p.add_argument("--bt-fee", type=float, default=0.0)
    p.add_argument("--bt-slippage", type=float, default=0.0)
    p.add_argument("--bt-pnl-pct", type=float, required=True)
    p.add_argument("--entry-bps", type=float, default=10.0)
    p.add_argument("--exit-bps", type=float, default=10.0)
    p.add_argument("--pnl-pct-abs", type=float, default=0.25)


def _build_tick(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)


def _build_run(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--interval", type=float, default=60.0)


def _build_purge(p: argparse.ArgumentParser) -> None:
    _common_db_arg(p)
    p.add_argument("--days", type=int, default=30)


def main(argv: Optional[List[str]] = None) -> int:
    subs = [
        Subcommand("status", "Latest heartbeat + rolling divergence summary.",
                   cmd_status, build=_build_status),
        Subcommand("summary", "Rollup over a window.", cmd_summary, build=_build_summary),
        Subcommand("events", "Tail recent audit events.", cmd_events, build=_build_events),
        Subcommand("run-parity-check", "One-shot parity check.",
                   cmd_run_parity_check, build=_build_run_parity),
        Subcommand("simulate-live-trade", "Feed a synthetic live-vs-bt trade pair.",
                   cmd_simulate_live_trade, build=_build_simulate_trade),
        Subcommand("tick", "Run one ContinuousAuditor pass.", cmd_tick, build=_build_tick),
        Subcommand("run", "Run ContinuousAuditor forever.", cmd_run, build=_build_run),
        Subcommand("purge-older", "Delete events older than N days.",
                   cmd_purge, build=_build_purge),
    ]
    parser = build_parser(
        prog="auditor_cli",
        description="Rule-1 Continuous Auditor — operator CLI (Phase 21).",
        subcommands=subs,
    )
    if argv is not None:
        import sys
        sys.argv = ["auditor_cli", *argv]
    return run(parser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
