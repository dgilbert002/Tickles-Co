"""Unit tests for the Phase 21 Rule-1 Continuous Auditor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from shared.auditor import (
    AuditStore,
    ContinuousAuditor,
    LiveVsBacktestComparator,
    ParityComparator,
)
from shared.auditor.auditor import AuditJob
from shared.auditor.comparator import (
    BacktestTrade,
    FeeSlippageComparator,
    FeeSlippageObservation,
    LiveTrade,
    LiveVsBacktestTolerances,
    ParityCheckInput,
)
from shared.auditor.schema import (
    AuditEventType,
    AuditRecord,
    AuditSeverity,
)


def _synth_candles(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n).cumsum()
    base = 100.0 + steps
    open_ = base + rng.uniform(-0.2, 0.2, n)
    high = np.maximum(open_, base) + rng.uniform(0.05, 0.5, n)
    low = np.minimum(open_, base) - rng.uniform(0.05, 0.5, n)
    close = base + rng.uniform(-0.2, 0.2, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
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


# ------------------------------- schema ---------------------------------


def test_audit_record_roundtrip_json() -> None:
    r = AuditRecord(
        event_type=AuditEventType.PARITY_CHECK,
        severity=AuditSeverity.BREACH,
        subject="strat-1@vectorbt",
        passed=False,
        metric=0.5,
        tolerance=0.25,
        strategy_id="strat-1",
        engine="vectorbt",
    )
    d = json.loads(r.to_json())
    assert d["event_type"] == "parity_check"
    assert d["severity"] == "breach"
    assert d["passed"] is False


# ------------------------------ storage ---------------------------------


def test_store_record_and_list(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        r = AuditRecord(
            event_type=AuditEventType.PARITY_CHECK,
            severity=AuditSeverity.OK,
            subject="x",
            passed=True,
        )
        out = store.record(r)
        assert out.id is not None
        recs = store.list_recent(limit=10)
        assert len(recs) == 1
        assert recs[0].event_type == AuditEventType.PARITY_CHECK


def test_store_summary(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        store.record(
            AuditRecord(event_type=AuditEventType.PARITY_CHECK, severity=AuditSeverity.OK,
                        subject="s1", passed=True)
        )
        store.record(
            AuditRecord(event_type=AuditEventType.PARITY_CHECK, severity=AuditSeverity.BREACH,
                        subject="s2", passed=False)
        )
        store.record(
            AuditRecord(event_type=AuditEventType.HEARTBEAT, severity=AuditSeverity.OK,
                        subject="x", passed=True)
        )
        summary = store.summary(window_seconds=3600)
    assert summary.total == 3
    assert summary.passed == 2
    assert summary.breaches == 1
    assert summary.by_event_type["parity_check"] == 2
    assert summary.by_event_type["heartbeat"] == 1
    assert 0.0 <= summary.pass_rate <= 1.0


def test_store_purge(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        old = AuditRecord(
            event_type=AuditEventType.HEARTBEAT, severity=AuditSeverity.OK,
            subject="x", passed=True, ts_unix=time.time() - 7 * 86400,
        )
        new = AuditRecord(
            event_type=AuditEventType.HEARTBEAT, severity=AuditSeverity.OK,
            subject="x", passed=True,
        )
        store.record(old)
        store.record(new)
        deleted = store.purge_older_than(time.time() - 86400)
        assert deleted == 1
        assert len(store.list_recent(limit=10)) == 1


def test_store_filters(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        store.record(
            AuditRecord(event_type=AuditEventType.PARITY_CHECK, severity=AuditSeverity.OK,
                        subject="s", passed=True)
        )
        store.record(
            AuditRecord(event_type=AuditEventType.FEE_DRIFT, severity=AuditSeverity.WARNING,
                        subject="s", passed=False)
        )
        parity = store.list_recent(limit=10, event_type=AuditEventType.PARITY_CHECK)
        assert len(parity) == 1
        warns = store.list_recent(limit=10, severity=AuditSeverity.WARNING)
        assert len(warns) == 1


# ------------------------------ comparators -----------------------------


def test_parity_comparator_emits_records() -> None:
    df = _synth_candles()
    cfg = _default_cfg()
    inp = ParityCheckInput(
        strategy_id="s1",
        candles_df=df,
        strategy=_sma_cross,
        cfg=cfg,
        engines=["classic"],
    )
    recs = ParityComparator().compare(inp)
    assert len(recs) >= 1
    engines = {r.engine for r in recs}
    assert "classic" in engines


def test_live_vs_backtest_comparator_pass_and_breach() -> None:
    comp = LiveVsBacktestComparator(
        LiveVsBacktestTolerances(
            entry_price_bps=10.0, exit_price_bps=10.0, pnl_pct_abs=0.25
        )
    )
    live = LiveTrade(
        trade_id="t1", strategy_id="s", engine="classic",
        side="long", qty=1.0,
        entry_price=100.0, exit_price=101.0,
        fees_paid=0.1, slippage_paid=0.05, pnl_pct=1.0,
    )
    bt_match = BacktestTrade(
        strategy_id="s", engine="classic",
        entry_price=100.0, exit_price=101.0,
        fees=0.1, slippage=0.05, pnl_pct=1.0,
    )
    ok = comp.compare(live, bt_match)
    assert ok[0].passed is True
    assert ok[0].severity == AuditSeverity.OK

    bt_breach = BacktestTrade(
        strategy_id="s", engine="classic",
        entry_price=90.0, exit_price=95.0,
        fees=0.5, slippage=0.5, pnl_pct=5.0,
    )
    breach = comp.compare(live, bt_breach)
    assert breach[0].passed is False
    assert breach[0].severity == AuditSeverity.BREACH


def test_fee_slippage_comparator_warnings() -> None:
    comp = FeeSlippageComparator(fee_tolerance_bps=2.0, slippage_tolerance_bps=5.0)
    obs = FeeSlippageObservation(
        strategy_id="s", engine="classic",
        assumed_fee_bps=5.0, realised_fee_bps=10.0,
        assumed_slippage_bps=2.0, realised_slippage_bps=20.0,
    )
    recs = comp.compare(obs)
    assert len(recs) == 2
    assert not recs[0].passed
    assert not recs[1].passed
    assert recs[0].severity == AuditSeverity.WARNING
    assert recs[1].severity == AuditSeverity.WARNING


# ------------------------------ auditor --------------------------------


def test_continuous_auditor_tick_records_coverage(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        job = AuditJob(
            strategy_id="s-sma",
            strategy_fn=_sma_cross,
            candles_loader=lambda: _synth_candles(),
            cfg=_default_cfg(),
            engines=["classic"],
        )
        auditor = ContinuousAuditor(store=store, jobs=[job], heartbeat_every_seconds=0.0)
        recs = auditor.tick()
        assert len(recs) >= 2
        coverage = [r for r in recs if r.event_type == AuditEventType.COVERAGE]
        assert len(coverage) == 1
        assert coverage[0].metric == 1.0
        # at least one heartbeat after the first tick
        heartbeats = [r for r in recs if r.event_type == AuditEventType.HEARTBEAT]
        assert len(heartbeats) >= 1


def test_continuous_auditor_tick_with_no_jobs_warns(tmp_path: Path) -> None:
    db = str(tmp_path / "a.sqlite3")
    with AuditStore(db) as store:
        auditor = ContinuousAuditor(store=store, jobs=[])
        recs = auditor.tick()
        assert len(recs) == 1
        assert recs[0].severity == AuditSeverity.WARNING
        assert recs[0].subject == "no_jobs_registered"


# ------------------------------- CLI -----------------------------------


def _run_cli(argv: List[str], env=None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "shared.cli.auditor_cli", *argv]
    repo_root = Path(__file__).resolve().parents[2]
    base_env = {**os.environ}
    if env:
        base_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(repo_root), env=base_env, timeout=120
    )


def test_cli_run_parity_check_and_status(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    r = _run_cli(["run-parity-check", "--db-path", db, "--engines", "classic"])
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["count"] >= 1

    r2 = _run_cli(["status", "--db-path", db, "--window", "3600"])
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert d2["summary"]["total"] >= 1


def test_cli_events_and_summary(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    r = _run_cli(["run-parity-check", "--db-path", db, "--engines", "classic"])
    assert r.returncode == 0, r.stderr

    r2 = _run_cli(["events", "--db-path", db, "--limit", "5"])
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert d2["count"] >= 1

    r3 = _run_cli(["summary", "--db-path", db, "--window", "3600"])
    assert r3.returncode == 0, r3.stderr
    d3 = json.loads(r3.stdout.strip().splitlines()[-1])
    assert d3["summary"]["total"] >= 1


def test_cli_simulate_live_trade(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    r = _run_cli(
        [
            "simulate-live-trade",
            "--db-path", db,
            "--trade-id", "t-cli",
            "--strategy-id", "test",
            "--engine", "classic",
            "--side", "long",
            "--qty", "1.0",
            "--live-entry", "100.0",
            "--live-exit", "101.0",
            "--live-fee", "0.1",
            "--live-slippage", "0.05",
            "--live-pnl-pct", "1.0",
            "--bt-entry", "100.0",
            "--bt-exit", "101.0",
            "--bt-fee", "0.1",
            "--bt-slippage", "0.05",
            "--bt-pnl-pct", "1.0",
        ]
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True


def test_cli_tick(tmp_path: Path) -> None:
    db = str(tmp_path / "cli.sqlite3")
    r = _run_cli(["tick", "--db-path", db])
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["records_written"] >= 2
