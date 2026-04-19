"""Unit tests for the Phase 20 feature store.

We avoid real Redis by using ``InMemoryOnlineStore`` and point the
offline store at a temp dir. The tests exercise every public surface
(schema, registry, online + offline IO, high-level FeatureStore, and
the CLI subcommands).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
import pytest

from shared.features import (
    FEATURE_VIEWS,
    Entity,
    Feature,
    FeatureDtype,
    FeatureStore,
    FeatureView,
    get_feature_view,
    list_feature_views,
    register_feature_view,
)
from shared.features.offline_store import OfflineStore
from shared.features.online_store import InMemoryOnlineStore, _deserialize_row, _key


def _make_candles(n: int = 400, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n).cumsum()
    base = 100.0 + steps
    open_ = base + rng.uniform(-0.2, 0.2, n)
    high = np.maximum(open_, base) + rng.uniform(0.05, 0.5, n)
    low = np.minimum(open_, base) - rng.uniform(0.05, 0.5, n)
    close = base + rng.uniform(-0.2, 0.2, n)
    vol = rng.uniform(100, 1000, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )
    df.index.name = "ts"
    return df


def _tmp_store(tmp_path: Path) -> Tuple[FeatureStore, OfflineStore]:
    offline = OfflineStore(root=str(tmp_path / "features"))
    store = FeatureStore(online=InMemoryOnlineStore(), offline=offline)
    return store, offline


# ----------------------------- schema -----------------------------


def test_builtin_views_registered() -> None:
    names = {fv.name for fv in list_feature_views()}
    assert {"returns_basic", "volatility_basic", "microstructure_basic"}.issubset(names)


def test_feature_view_validate_output_rejects_missing_col() -> None:
    fv = get_feature_view("returns_basic")
    bad = pd.DataFrame(
        {"log_ret_1m": [0.0]},
        index=pd.DatetimeIndex(pd.to_datetime(["2025-01-01"], utc=True)),
    )
    with pytest.raises(ValueError):
        fv.validate_output(bad)


def test_feature_view_validate_output_requires_datetimeindex() -> None:
    fv = get_feature_view("returns_basic")
    df = pd.DataFrame({name: [0.0] for name in fv.feature_names()})
    with pytest.raises(ValueError):
        fv.validate_output(df)


def test_custom_feature_view_registers_and_resolves() -> None:
    fv = FeatureView(
        name="_test_custom_fv",
        entities=[Entity("asset", join_keys=["asset"])],
        features=[Feature("x", FeatureDtype.FLOAT)],
        compute=lambda df, ek, p: pd.DataFrame({"x": [1.0]}, index=df.index[:1]),
    )
    register_feature_view(fv)
    assert get_feature_view("_test_custom_fv") is fv
    register_feature_view(fv)  # idempotent
    assert FEATURE_VIEWS["_test_custom_fv"] is fv


# ---------------------------- online store ----------------------------


def test_online_store_roundtrip_in_memory() -> None:
    store = InMemoryOnlineStore()
    fv = get_feature_view("returns_basic")
    store.write(fv, "binance:BTC/USDT", {"log_ret_1m": 0.002, "log_ret_5m": -0.001,
                                           "log_ret_15m": 0.0, "mom_1h_pct": 0.01,
                                           "mom_4h_pct": -0.02})
    got = store.read(fv, "binance:BTC/USDT")
    assert got is not None
    assert pytest.approx(got["log_ret_1m"], rel=1e-9) == 0.002
    assert got["__timestamp"] is not None


def test_online_store_many_returns_nones() -> None:
    store = InMemoryOnlineStore()
    fv = get_feature_view("returns_basic")
    out = store.read_many(fv, ["a", "b"])
    assert out == {"a": None, "b": None}


def test_online_store_key_shape() -> None:
    fv = get_feature_view("returns_basic")
    assert _key(fv.name, "binance:BTC/USDT").startswith("tickles:fv:returns_basic:")


def test_deserialize_row_handles_empty_ts() -> None:
    fv = get_feature_view("returns_basic")
    row = _deserialize_row(fv, {"__timestamp": "", "__ts_unix": ""})
    assert row["__ts_unix"] is None


# --------------------------- offline store ----------------------------


def test_offline_store_write_read(tmp_path: Path) -> None:
    offline = OfflineStore(root=str(tmp_path))
    fv = get_feature_view("returns_basic")
    candles = _make_candles(300)
    feats = fv.compute(candles, "binance:BTC/USDT", {})  # type: ignore[misc]
    n = offline.write_batch(fv, "binance:BTC/USDT", feats)
    assert n == len(feats)
    back = offline.read_range(fv, "binance:BTC/USDT")
    assert not back.empty
    assert set(fv.feature_names()).issubset(back.columns)


def test_offline_store_dedupe(tmp_path: Path) -> None:
    offline = OfflineStore(root=str(tmp_path))
    fv = get_feature_view("returns_basic")
    candles = _make_candles(120)
    feats = fv.compute(candles, "binance:BTC/USDT", {})  # type: ignore[misc]
    offline.write_batch(fv, "binance:BTC/USDT", feats)
    offline.write_batch(fv, "binance:BTC/USDT", feats)
    back = offline.read_range(fv, "binance:BTC/USDT")
    assert len(back) == len(feats)


def test_offline_store_partitions(tmp_path: Path) -> None:
    offline = OfflineStore(root=str(tmp_path))
    fv = get_feature_view("returns_basic")
    candles = _make_candles(80)
    feats = fv.compute(candles, "binance:BTC/USDT", {})  # type: ignore[misc]
    offline.write_batch(fv, "binance:BTC/USDT", feats)
    offline.write_batch(fv, "binance:ETH/USDT", feats)
    parts = offline.partitions(fv)
    assert "binance_BTC_USDT" in parts
    assert "binance_ETH_USDT" in parts


# ---------------------------- high-level store ----------------------------


def test_feature_store_materialize_and_get_online(tmp_path: Path) -> None:
    store, _ = _tmp_store(tmp_path)
    candles = _make_candles(200)
    summary = store.materialize("returns_basic", "binance:BTC/USDT", candles)
    assert summary["rows_offline"] > 0
    assert summary["wrote_online"] is True

    vec = store.get_online("returns_basic", "binance:BTC/USDT")
    assert vec is not None
    assert "log_ret_1m" in vec


def test_feature_store_get_historical(tmp_path: Path) -> None:
    store, _ = _tmp_store(tmp_path)
    candles = _make_candles(300)
    store.materialize("volatility_basic", "binance:BTC/USDT", candles)
    hist = store.get_historical(
        "volatility_basic",
        ["binance:BTC/USDT"],
        start=candles.index[50],
        end=candles.index[-1],
    )
    assert not hist.empty
    assert "atr_14" in hist.columns
    assert hist["entity_key"].unique().tolist() == ["binance:BTC/USDT"]


def test_feature_store_unknown_view_raises(tmp_path: Path) -> None:
    store, _ = _tmp_store(tmp_path)
    with pytest.raises(KeyError):
        store.materialize("does_not_exist", "x", _make_candles(10))


# ------------------------------- CLI ------------------------------


def _run_cli(argv, env=None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "shared.cli.features_cli", *argv]
    repo_root = Path(__file__).resolve().parents[2]
    base_env = {**os.environ}
    if env:
        base_env.update(env)
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(repo_root), env=base_env, timeout=60
    )


def test_features_cli_list() -> None:
    r = _run_cli(["list"])
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True
    assert data["count"] >= 3


def test_features_cli_describe() -> None:
    r = _run_cli(["describe", "returns_basic"])
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["view"]["name"] == "returns_basic"
    assert len(data["view"]["features"]) >= 5


def test_features_cli_materialize_online_get(tmp_path: Path) -> None:
    candles = _make_candles(300)
    candles_path = tmp_path / "candles.parquet"
    candles_out = candles.reset_index().rename(columns={"ts": "ts"})
    candles_out.to_parquet(candles_path, index=False)

    env = {
        "TICKLES_FEATURE_OFFLINE_ROOT": str(tmp_path / "features"),
    }
    r = _run_cli(
        [
            "materialize",
            "--view",
            "returns_basic",
            "--entity",
            "binance:BTC/USDT",
            "--parquet",
            str(candles_path),
            "--in-memory",
        ],
        env=env,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True
    assert data["summary"]["rows_offline"] > 0

    # online-get reads a different process memory — with --in-memory we just
    # exercise the code path (will return a not-found, which exits 1).
    r2 = _run_cli(
        ["online-get", "--view", "returns_basic", "--entity", "binance:BTC/USDT", "--in-memory"],
        env=env,
    )
    assert r2.returncode in (0, 1)
    data2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert data2["view"] == "returns_basic"


def test_features_cli_historical_and_partitions(tmp_path: Path) -> None:
    candles = _make_candles(200)
    candles_path = tmp_path / "candles.parquet"
    candles.reset_index().to_parquet(candles_path, index=False)

    env = {
        "TICKLES_FEATURE_OFFLINE_ROOT": str(tmp_path / "features"),
    }
    # materialize first
    r = _run_cli(
        [
            "materialize",
            "--view",
            "volatility_basic",
            "--entity",
            "binance:BTC/USDT",
            "--parquet",
            str(candles_path),
            "--in-memory",
        ],
        env=env,
    )
    assert r.returncode == 0, r.stderr

    r2 = _run_cli(
        [
            "historical-get",
            "--view",
            "volatility_basic",
            "--entities",
            "binance:BTC/USDT",
            "--head",
            "3",
        ],
        env=env,
    )
    assert r2.returncode == 0, r2.stderr
    d2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert d2["ok"] is True
    assert d2["rows"] > 0

    r3 = _run_cli(["partitions", "--view", "volatility_basic"], env=env)
    assert r3.returncode == 0, r3.stderr
    d3 = json.loads(r3.stdout.strip().splitlines()[-1])
    assert d3["count"] >= 1
