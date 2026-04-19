"""
shared.cli.engines_cli — Backtest Engine 2.0 operator CLI (Phase 19).

Subcommands:

* ``list`` — enumerate every registered engine + its capabilities +
  availability (deps installed or not).
* ``capabilities`` — dump the EngineCapabilities dataclass for each.
* ``sample`` — run a tiny synthetic-data backtest against a named
  engine (smoke-test).
* ``parity`` — run the synthetic-data backtest through all engines
  and emit a parity report (JSON).

All output is single-line JSON on stdout.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)


def _load() -> Any:
    import shared.backtest.engines as eng_mod  # noqa: F401
    return eng_mod


def _synthetic_ohlcv(n: int = 400, seed: int = 42) -> pd.DataFrame:
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
        }
    )
    df.index = idx
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


def _default_cfg():
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


def cmd_list(args: argparse.Namespace) -> int:
    mod = _load()
    items = []
    for name in mod.list_engines():
        eng = mod.get(name)
        items.append(
            {
                "name": name,
                "available": eng.available(),
                "capabilities": asdict(eng.capabilities),
            }
        )
    emit({"ok": True, "engines": items, "count": len(items)})
    return EXIT_OK


def cmd_capabilities(args: argparse.Namespace) -> int:
    mod = _load()
    caps = {name: asdict(c) for name, c in mod.capabilities().items()}
    emit({"ok": True, "capabilities": caps})
    return EXIT_OK


def cmd_sample(args: argparse.Namespace) -> int:
    mod = _load()
    try:
        engine = mod.get(args.engine)
    except KeyError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    if not engine.available():
        emit(
            {
                "ok": False,
                "error": f"engine '{args.engine}' unavailable (deps missing)",
            }
        )
        return EXIT_FAIL
    df = _synthetic_ohlcv()
    cfg = _default_cfg()
    try:
        result = engine.run(df, _sma_cross, cfg)
    except Exception as exc:
        emit({"ok": False, "engine": args.engine, "error": str(exc)})
        return EXIT_FAIL
    emit(
        {
            "ok": True,
            "engine": args.engine,
            "num_trades": result.num_trades,
            "pnl_pct": round(float(result.pnl_pct), 4),
            "sharpe": round(float(result.sharpe), 4),
            "winrate": round(float(result.winrate), 4),
            "max_drawdown": round(float(result.max_drawdown), 4),
            "runtime_ms": round(float(result.runtime_ms), 2),
        }
    )
    return EXIT_OK


def cmd_parity(args: argparse.Namespace) -> int:
    from shared.backtest.parity import parity_summary

    df = _synthetic_ohlcv()
    cfg = _default_cfg()
    report = parity_summary(
        df, _sma_cross, cfg, engines=args.engines.split(",") if args.engines else None
    )
    emit({"ok": report.to_dict()["ok"], "report": report.to_dict()})
    return EXIT_OK if report.to_dict()["ok"] else EXIT_FAIL


def _build_sample(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engine", default="classic", help="engine name")


def _build_parity(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engines", default="", help="comma-separated list; default = classic,vectorbt")


def main(argv: Optional[List[str]] = None) -> int:
    subs = [
        Subcommand("list", "List engines and availability.", cmd_list),
        Subcommand("capabilities", "Capability matrix.", cmd_capabilities),
        Subcommand("sample", "Run a tiny synthetic backtest.", cmd_sample, build=_build_sample),
        Subcommand("parity", "Cross-engine parity report.", cmd_parity, build=_build_parity),
    ]
    parser = build_parser(
        prog="engines_cli",
        description="Backtest Engine 2.0 — inspector + parity harness.",
        subcommands=subs,
    )
    if argv is not None:
        import sys
        sys.argv = ["engines_cli", *argv]
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
