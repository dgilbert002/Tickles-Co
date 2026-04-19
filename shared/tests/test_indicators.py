"""
Phase 18 — indicator library smoke tests.

These tests are intentionally cheap: they import the registry, verify
that we passed the 250-indicator target, call a handful of the new
registrations against a small synthetic OHLCV frame, and smoke-test
the operator CLI.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _synthetic_ohlcv(n: int = 400, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 1.0, n).cumsum()
    base = 100.0 + steps
    high = base + rng.uniform(0.1, 1.0, n)
    low = base - rng.uniform(0.1, 1.0, n)
    open_ = base + rng.uniform(-0.5, 0.5, n)
    close = base + rng.uniform(-0.5, 0.5, n)
    volume = rng.uniform(1_000, 10_000, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


# ---------------------------------------------------------------------------
# catalog size
# ---------------------------------------------------------------------------


def test_registry_has_at_least_250_indicators() -> None:
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    assert len(INDICATORS) >= 250, f"expected >= 250 indicators, got {len(INDICATORS)}"


def test_registry_has_core_basics() -> None:
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    for name in ("sma", "ema", "rsi", "atr", "obv", "mfi"):
        assert name in INDICATORS, f"core indicator '{name}' missing"


def test_registry_has_bridge_and_extras() -> None:
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    bridge_count = sum(1 for n in INDICATORS if n.startswith("pta_"))
    extras_count = sum(1 for n in INDICATORS if n.startswith("ext_"))
    if bridge_count == 0:
        # bridge only registers when pandas_ta is importable; don't fail if missing
        import importlib.util
        if importlib.util.find_spec("pandas_ta") is not None:
            raise AssertionError("pandas_ta is installed but bridge registered 0 indicators")
    assert extras_count >= 40, f"expected >= 40 extras, got {extras_count}"


# ---------------------------------------------------------------------------
# run a few indicators against synthetic data
# ---------------------------------------------------------------------------


def _try(name: str, df: pd.DataFrame) -> bool:
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    spec = INDICATORS[name]
    result = spec.fn(df, dict(spec.defaults))
    if result is None:
        return False
    assert isinstance(result, pd.Series), f"{name} must return a Series"
    assert len(result) == len(df), f"{name} len {len(result)} != {len(df)}"
    return True


def test_core_indicators_execute() -> None:
    df = _synthetic_ohlcv()
    for name in ("sma", "ema", "rsi", "atr", "obv", "mfi", "bbands_pb", "roc"):
        assert _try(name, df)


def test_extras_indicators_execute() -> None:
    df = _synthetic_ohlcv()
    for name in (
        "ext_zscore",
        "ext_percentile_rank",
        "ext_true_range",
        "ext_atr_pct",
        "ext_log_return",
        "ext_rolling_sharpe",
        "ext_smma",
        "ext_bullish_streak",
        "ext_volume_zscore",
    ):
        assert _try(name, df)


def test_bridge_indicators_execute_when_pandas_ta_available() -> None:
    import importlib.util
    if importlib.util.find_spec("pandas_ta") is None:
        return  # skip silently
    df = _synthetic_ohlcv()
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    sample_names = [
        "pta_dema", "pta_hma", "pta_supertrend", "pta_adx_value",
        "pta_bbands_upper", "pta_macd_line", "pta_stoch_k", "pta_willr",
    ]
    ok = 0
    for name in sample_names:
        if name not in INDICATORS:
            continue
        try:
            if _try(name, df):
                ok += 1
        except Exception:
            # some pandas_ta functions may be sensitive to data size; skip
            continue
    assert ok >= 4, f"expected >= 4 bridge samples to execute, got {ok}"


# ---------------------------------------------------------------------------
# Spec field sanity
# ---------------------------------------------------------------------------


def test_every_spec_has_required_fields() -> None:
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    for name, spec in INDICATORS.items():
        assert spec.name == name
        assert callable(spec.fn)
        assert isinstance(spec.defaults, dict)
        assert isinstance(spec.param_ranges, dict)
        assert spec.category in {
            "trend", "momentum", "volatility", "volume",
            "statistical", "performance", "pattern",
            "smart_money", "crash_protection",
        }, f"{name} has unknown category '{spec.category}'"
        assert spec.direction in {"bullish", "bearish", "neutral"}, \
            f"{name} has unknown direction '{spec.direction}'"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_indicators_cli_count_runs_in_process() -> None:
    from shared.cli.indicators_cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["count"])
    assert rc == 0
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["total"] >= 250
    assert "by_category" in payload


def test_indicators_cli_describe_known_indicator() -> None:
    from shared.cli.indicators_cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["describe", "rsi"])
    assert rc == 0
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["name"] == "rsi"
    assert payload["category"] == "momentum"


def test_indicators_cli_describe_unknown_indicator_fails() -> None:
    from shared.cli.indicators_cli import main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["describe", "definitely_not_a_real_indicator"])
    assert rc == 1
    payload = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert payload["ok"] is False


def test_indicators_cli_entrypoint_importable() -> None:
    # Make sure ``python -m shared.cli.indicators_cli --help`` at least parses.
    proc = subprocess.run(
        [sys.executable, "-m", "shared.cli.indicators_cli", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "indicators_cli" in (proc.stdout + proc.stderr)
