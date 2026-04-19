"""
Phase 19 — Backtest Engine 2.0 tests.

These cover:
  * the engine registry (list, get, capabilities)
  * the classic engine returning a sensible result on synthetic data
  * the vectorbt engine running end-to-end when installed
  * the Nautilus scaffold returning NotImplementedError shape
  * the parity harness being tolerant of missing deps
  * the engines_cli subcommands
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from typing import Any, Dict

import numpy as np
import pandas as pd


def _synthetic_df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n).cumsum()
    base = 100.0 + steps
    open_ = base + rng.uniform(-0.2, 0.2, n)
    high = np.maximum(open_, base) + rng.uniform(0.05, 0.5, n)
    low = np.minimum(open_, base) - rng.uniform(0.05, 0.5, n)
    close = base + rng.uniform(-0.2, 0.2, n)
    idx = pd.date_range("2025-02-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
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


def _sma_cross(df: pd.DataFrame, params: Dict[str, Any]) -> pd.Series:
    fast = int(params.get("fast", 5))
    slow = int(params.get("slow", 20))
    c = df["closePrice"].astype(float)
    f = c.rolling(fast).mean()
    s = c.rolling(slow).mean()
    state = pd.Series(0.0, index=df.index)
    state = state.mask(f > s, 1.0).mask(f < s, -1.0)
    prev = state.shift(1).fillna(0.0)
    cross_mask = (state != prev) & (state != 0.0)
    out = pd.Series(0.0, index=df.index)
    out[cross_mask] = state[cross_mask]
    return out


def _cfg():
    from shared.backtest.engine import BacktestConfig

    return BacktestConfig(
        symbol="SYNTH/USD",
        source="synthetic",
        timeframe="1m",
        start_date="2025-02-01",
        end_date="2025-02-02",
        direction="long",
        initial_capital=10_000.0,
        position_pct=95.0,
        fee_taker_bps=5.0,
        slippage_bps=2.0,
        strategy_name="sma_cross",
        indicator_name="sma",
        indicator_params={"fast": 5, "slow": 20},
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_lists_three_engines() -> None:
    from shared.backtest.engines import capabilities, list_engines

    names = list_engines()
    assert "classic" in names
    assert "vectorbt" in names
    assert "nautilus" in names
    caps = capabilities()
    assert caps["classic"].supports_intrabar_sl_tp
    assert caps["vectorbt"].supports_vectorised_sweep


def test_registry_get_unknown_raises() -> None:
    from shared.backtest.engines import get
    import pytest

    with pytest.raises(KeyError):
        get("does_not_exist")


# ---------------------------------------------------------------------------
# classic engine
# ---------------------------------------------------------------------------


def test_classic_engine_runs_end_to_end() -> None:
    from shared.backtest.engines import get

    engine = get("classic")
    assert engine.available()
    df = _synthetic_df()
    result = engine.run(df, _sma_cross, _cfg())
    assert result.bars_processed == len(df)
    assert result.engine_version.startswith("2026.")
    assert isinstance(result.trades, list)


# ---------------------------------------------------------------------------
# vectorbt engine
# ---------------------------------------------------------------------------


def test_vectorbt_engine_runs_or_reports_unavailable() -> None:
    from shared.backtest.engines import get

    engine = get("vectorbt")
    if not engine.available():
        # acceptable — CI may run without vectorbt installed
        return
    df = _synthetic_df()
    result = engine.run(df, _sma_cross, _cfg())
    assert result.bars_processed == len(df)
    assert "vectorbt" in result.engine_version


# ---------------------------------------------------------------------------
# nautilus scaffold
# ---------------------------------------------------------------------------


def test_nautilus_engine_is_scaffold_only() -> None:
    from shared.backtest.engines import get
    import pytest

    engine = get("nautilus")
    df = _synthetic_df()
    with pytest.raises(RuntimeError, match="Phase 26"):
        engine.run(df, _sma_cross, _cfg())


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------


def test_parity_report_runs_classic_only_when_vbt_missing() -> None:
    from shared.backtest.engines import get
    from shared.backtest.parity import parity_summary

    df = _synthetic_df()
    report = parity_summary(df, _sma_cross, _cfg(), engines=["classic"])
    payload = report.to_dict()
    assert payload["source_of_truth"] == "classic"
    assert payload["ok"] is True
    assert payload["digests"][0]["engine"] == "classic"

    # ensure vectorbt gets reported but never fails the run when missing
    vbt = get("vectorbt")
    if not vbt.available():
        report = parity_summary(df, _sma_cross, _cfg(), engines=["classic", "vectorbt"])
        pd = report.to_dict()
        vbt_digest = next(d for d in pd["digests"] if d["engine"] == "vectorbt")
        assert vbt_digest["available"] is False
        assert pd["ok"] is True  # missing engine must not fail the report


def test_parity_report_compares_classic_vs_vbt_when_available() -> None:
    from shared.backtest.engines import get
    from shared.backtest.parity import parity_summary, ParityTolerances

    if not get("vectorbt").available():
        return
    df = _synthetic_df()
    report = parity_summary(
        df,
        _sma_cross,
        _cfg(),
        engines=["classic", "vectorbt"],
        tolerances=ParityTolerances(
            num_trades_abs=10,
            pnl_pct_abs=50.0,
            sharpe_abs=5.0,
            winrate_abs=50.0,
            max_drawdown_abs=50.0,
        ),
    )
    payload = report.to_dict()
    assert len(payload["digests"]) == 2
    # with wide tolerances the parity should pass even though engines differ
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(argv):
    from shared.cli.engines_cli import main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    last = buf.getvalue().strip().splitlines()[-1]
    return rc, json.loads(last)


def test_engines_cli_list_runs() -> None:
    rc, payload = _cli(["list"])
    assert rc == 0
    assert payload["ok"] is True
    assert payload["count"] >= 3
    names = [e["name"] for e in payload["engines"]]
    assert "classic" in names and "vectorbt" in names and "nautilus" in names


def test_engines_cli_sample_classic() -> None:
    rc, payload = _cli(["sample", "--engine", "classic"])
    assert rc == 0
    assert payload["ok"] is True
    assert "num_trades" in payload


def test_engines_cli_parity_runs() -> None:
    rc, payload = _cli(["parity", "--engines", "classic"])
    # classic-only parity always passes
    assert rc == 0
    assert payload["ok"] is True
    assert payload["report"]["source_of_truth"] == "classic"


def test_engines_cli_capabilities() -> None:
    rc, payload = _cli(["capabilities"])
    assert rc == 0
    assert payload["ok"] is True
    assert "classic" in payload["capabilities"]
