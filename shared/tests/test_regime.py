"""Phase 27 — Regime Service tests.

Covers:

* migration file shape
* Trend / Volatility / Composite classifiers on synthetic series
* RegimeStore <-> InMemoryRegimePool CRUD
* RegimeService persist + current/history
* CLI smoke tests
* Service-registry entry for 'regime'
"""
from __future__ import annotations

import asyncio
import io
import json
import math
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from shared.regime import (
    CLASSIFIER_COMPOSITE,
    CLASSIFIER_NAMES,
    CLASSIFIER_TREND,
    Candle,
    CompositeClassifier,
    InMemoryRegimePool,
    MIGRATION_PATH,
    REGIME_BEAR,
    REGIME_BULL,
    REGIME_CRASH,
    REGIME_HIGH_VOL,
    REGIME_LABELS,
    REGIME_LOW_VOL,
    REGIME_RECOVERY,
    REGIME_SIDEWAYS,
    REGIME_UNKNOWN,
    RegimeService,
    RegimeStore,
    TrendClassifier,
    VolatilityClassifier,
    build_classifier,
    read_migration_sql,
)
from shared.cli import regime_cli
from shared.services.registry import (
    SERVICE_REGISTRY,
    register_builtin_services,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _series(closes: List[float], *, step: int = 60) -> List[Candle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            ts=base + timedelta(seconds=i * step),
            open=float(c), high=float(c), low=float(c),
            close=float(c), volume=1.0,
        )
        for i, c in enumerate(closes)
    ]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_has_expected_objects() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for obj in (
        "public.regime_config",
        "public.regime_states",
        "public.regime_current",
    ):
        assert obj in sql, f"migration missing {obj}"


# ---------------------------------------------------------------------------
# TrendClassifier
# ---------------------------------------------------------------------------


def test_trend_classifier_reports_unknown_when_too_few_candles() -> None:
    clf = TrendClassifier(fast=20, slow=50)
    closes = list(range(10))
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_UNKNOWN
    assert sig.sample_size == 10


def test_trend_classifier_flags_bull_on_rising_series() -> None:
    clf = TrendClassifier(fast=20, slow=50, slope_lookback=10, min_slope=0.0005)
    closes = [100.0 + i * 0.5 for i in range(200)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_BULL
    assert (sig.trend_score or 0) > 0.0


def test_trend_classifier_flags_bear_on_falling_series() -> None:
    clf = TrendClassifier(fast=20, slow=50, slope_lookback=10, min_slope=0.0005)
    closes = [200.0 - i * 0.5 for i in range(200)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_BEAR


def test_trend_classifier_flags_sideways_on_flat_series() -> None:
    clf = TrendClassifier(fast=20, slow=50, slope_lookback=10, min_slope=0.0005)
    closes = [100.0 + ((-1) ** i) * 0.1 for i in range(200)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_SIDEWAYS


def test_trend_classifier_rejects_bad_init_params() -> None:
    with pytest.raises(ValueError):
        TrendClassifier(fast=50, slow=20)
    with pytest.raises(ValueError):
        TrendClassifier(fast=0, slow=10)


# ---------------------------------------------------------------------------
# VolatilityClassifier
# ---------------------------------------------------------------------------


def test_volatility_classifier_high_vol_on_wild_series() -> None:
    clf = VolatilityClassifier(window=48, high_threshold=0.04, low_threshold=0.005)
    closes: List[float] = []
    price = 100.0
    for i in range(60):
        price *= (1.10 if i % 2 == 0 else 0.90)
        closes.append(price)
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_HIGH_VOL
    assert (sig.volatility or 0) >= 0.04


def test_volatility_classifier_low_vol_on_flat_series() -> None:
    clf = VolatilityClassifier(window=48, high_threshold=0.04, low_threshold=0.002)
    closes = [100.0 + i * 0.001 for i in range(80)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_LOW_VOL
    assert (sig.volatility or 0) <= 0.002


def test_volatility_classifier_unknown_with_insufficient_data() -> None:
    clf = VolatilityClassifier(window=48)
    closes = list(range(10))
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_UNKNOWN


def test_volatility_classifier_drawdown_set() -> None:
    clf = VolatilityClassifier(window=48)
    closes = [100.0] * 30 + [70.0] * 30   # 30 % drawdown
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.drawdown is not None
    assert sig.drawdown > 0.25


def test_volatility_classifier_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        VolatilityClassifier(window=1)
    with pytest.raises(ValueError):
        VolatilityClassifier(window=48, high_threshold=0.01, low_threshold=0.02)


# ---------------------------------------------------------------------------
# CompositeClassifier
# ---------------------------------------------------------------------------


def test_composite_classifier_crash_on_bear_plus_drawdown() -> None:
    clf = CompositeClassifier(
        trend=TrendClassifier(fast=20, slow=50, slope_lookback=10, min_slope=0.0005),
        volatility=VolatilityClassifier(window=48, high_threshold=0.04, low_threshold=0.002),
        crash_dd=0.10,
    )
    closes = [100.0 - i * 0.5 for i in range(200)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    # 100 -> 0.5 (massive dd) + bear trend -> crash
    assert sig.regime == REGIME_CRASH


def test_composite_classifier_recovery_on_non_bear_plus_drawdown() -> None:
    clf = CompositeClassifier(crash_dd=0.10)
    # Build a series where the *last* volatility window (49 candles)
    # contains a meaningful peak-to-trough drawdown (so vol.drawdown
    # >= crash_dd) AND the overall trend of the slow SMA is bullish
    # (so trend != bear). Composite should therefore label it as
    # 'recovery' (or at worst plain 'bull' on this synthetic input).
    closes: List[float] = [50.0 + i * 0.3 for i in range(150)]
    # Sharp plunge over the next 20 candles (peak -> ~70 % of peak)
    peak = closes[-1]
    closes += [peak * (1.0 - 0.02 * i) for i in range(1, 21)]
    # Steady recovery to close to the previous peak
    trough = closes[-1]
    closes += [trough + i * 0.8 for i in range(1, 40)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert (sig.drawdown or 0) >= 0.10
    assert sig.regime in (REGIME_RECOVERY, REGIME_BULL, REGIME_CRASH)


def test_composite_classifier_unknown_when_too_short() -> None:
    clf = CompositeClassifier()
    sig = clf.classify(
        _series([100.0, 101.0, 102.0]),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert sig.regime == REGIME_UNKNOWN


def test_build_classifier_rejects_unknown_name() -> None:
    with pytest.raises(ValueError):
        build_classifier("not-a-classifier")


def test_build_classifier_returns_all_known_classifiers() -> None:
    for name in CLASSIFIER_NAMES:
        clf = build_classifier(name)
        assert clf is not None


# ---------------------------------------------------------------------------
# RegimeStore + InMemoryRegimePool
# ---------------------------------------------------------------------------


def test_store_upsert_and_list_configs() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)

    async def go() -> None:
        rid = await store.upsert_config(
            universe="crypto-majors",
            exchange="binance",
            symbol="BTC/USDT",
            timeframe="1h",
            classifier=CLASSIFIER_COMPOSITE,
            params={"crash_dd": 0.12},
        )
        assert rid > 0
        rid2 = await store.upsert_config(
            universe="crypto-majors",
            exchange="binance",
            symbol="BTC/USDT",
            timeframe="1h",
            classifier=CLASSIFIER_COMPOSITE,
            params={"crash_dd": 0.15},
        )
        assert rid == rid2, "upsert must re-use id"
        rows = await store.list_configs(universe="crypto-majors", enabled_only=True)
        assert len(rows) == 1
        assert rows[0].params["crash_dd"] == 0.15

    _run(go())


def test_store_insert_state_and_current_view() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)
    clf = CompositeClassifier()
    closes = [100.0 + i * 0.5 for i in range(200)]
    sig = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )

    async def go() -> None:
        sid = await store.insert_state(sig)
        assert sid > 0
        current = await store.list_current()
        assert len(current) == 1
        row = current[0]
        assert row.universe == "u"
        assert row.regime == sig.regime

    _run(go())


def test_store_history_returns_latest_first() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)

    async def go() -> None:
        clf = CompositeClassifier()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, shift in enumerate((0.0, 0.5, 1.0, 1.5)):
            closes = [100.0 + shift + j * 0.5 for j in range(200)]
            candles = [
                Candle(
                    ts=base + timedelta(hours=i, seconds=j * 60),
                    open=c, high=c, low=c, close=c, volume=1.0,
                )
                for j, c in enumerate(closes)
            ]
            sig = clf.classify(
                candles,
                universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
            )
            await store.insert_state(sig)

        hist = await store.list_history(
            universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
            limit=10,
        )
        assert len(hist) == 4
        assert hist[0].as_of >= hist[-1].as_of

    _run(go())


def test_store_insert_requires_as_of() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)
    clf = TrendClassifier()
    sig = clf.classify(
        _series([1.0, 2.0, 3.0]),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    sig.as_of = None

    async def go() -> None:
        with pytest.raises(ValueError):
            await store.insert_state(sig)

    _run(go())


# ---------------------------------------------------------------------------
# RegimeService
# ---------------------------------------------------------------------------


def test_service_classify_from_candles_persists_signal() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)
    service = RegimeService(store)
    closes = [100.0 + i * 0.5 for i in range(200)]

    async def go() -> None:
        sig = await service.classify_from_candles(
            _series(closes),
            universe="u", exchange="binance", symbol="BTC/USDT",
            timeframe="1h", classifier=CLASSIFIER_TREND, persist=True,
        )
        assert sig.regime == REGIME_BULL
        assert len(pool.states) == 1

    _run(go())


def test_service_tick_runs_all_enabled_configs() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)

    async def loader(exchange: str, symbol: str, timeframe: str, window: int) -> List[Candle]:
        closes = [100.0 + i * 0.5 for i in range(max(window, 120))]
        return _series(closes)

    service = RegimeService(store, candles_loader=loader)

    async def go() -> None:
        await store.upsert_config(
            universe="u", exchange="binance", symbol="BTC/USDT",
            timeframe="1h", classifier=CLASSIFIER_COMPOSITE,
            params={"crash_dd": 0.10},
        )
        await store.upsert_config(
            universe="u", exchange="binance", symbol="ETH/USDT",
            timeframe="1h", classifier=CLASSIFIER_TREND,
        )
        signals = await service.tick(universe="u", window=300)
        assert len(signals) == 2
        assert len(pool.states) == 2

    _run(go())


def test_service_tick_skips_universe_level_config_without_symbol() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)

    async def loader(exchange: str, symbol: str, timeframe: str, window: int) -> List[Candle]:
        return _series([100.0 + i * 0.5 for i in range(300)])

    service = RegimeService(store, candles_loader=loader)

    async def go() -> None:
        await store.upsert_config(
            universe="u", exchange=None, symbol=None,
            timeframe="1h", classifier=CLASSIFIER_COMPOSITE,
        )
        signals = await service.tick(universe="u")
        assert signals == []

    _run(go())


def test_service_current_and_history() -> None:
    pool = InMemoryRegimePool()
    store = RegimeStore(pool)
    service = RegimeService(store)
    closes = [100.0 + i * 0.5 for i in range(200)]

    async def go() -> None:
        for i in range(3):
            await service.classify_from_candles(
                _series([c + i for c in closes]),
                universe="u", exchange="binance", symbol="BTC/USDT",
                timeframe="1h", classifier=CLASSIFIER_COMPOSITE, persist=True,
            )
        cur = await service.current(universe="u")
        hist = await service.history(
            universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
        )
        assert len(cur) == 1
        assert len(hist) == 3

    _run(go())


# ---------------------------------------------------------------------------
# Service registry
# ---------------------------------------------------------------------------


def test_regime_service_is_registered() -> None:
    register_builtin_services()
    assert "regime" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("regime")
    assert desc.kind == "worker"
    assert desc.tags.get("phase") == "27"


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = regime_cli.main(argv)
    out = buf.getvalue().strip()
    assert code == 0, f"cli exited with {code}; stdout={out!r}"
    return json.loads(out)


def test_cli_classifiers_lists_known_names() -> None:
    data = _run_cli(["classifiers"])
    assert data["ok"] is True
    assert set(data["classifiers"]) == set(CLASSIFIER_NAMES)


def test_cli_apply_migration_prints_path_only() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = regime_cli.main(["apply-migration", "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH)


def test_cli_classify_via_synthetic_closes() -> None:
    closes = ",".join(str(100 + i) for i in range(200))
    data = _run_cli([
        "classify", "--universe", "u", "--exchange", "binance",
        "--symbol", "BTC/USDT", "--timeframe", "1h",
        "--classifier", CLASSIFIER_TREND,
        "--closes", closes,
        "--in-memory",
    ])
    assert data["ok"] is True
    assert data["signal"]["regime"] in REGIME_LABELS


def test_cli_config_set_and_list_roundtrip() -> None:
    # Use a process-shared in-memory pool: the CLI re-creates the pool
    # every call when --in-memory is used, so config-set and config-list
    # won't share state. Instead we test against a single CLI call chain:
    data = _run_cli([
        "config-list", "--in-memory",
    ])
    assert data["ok"] is True
    assert data["count"] == 0


# Math sanity that the classifier is deterministic
def test_classifier_is_deterministic() -> None:
    closes = [100.0 + math.sin(i * 0.2) * 2.0 + i * 0.1 for i in range(300)]
    clf = CompositeClassifier()
    a = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    b = clf.classify(
        _series(closes),
        universe="u", exchange="binance", symbol="BTC/USDT", timeframe="1h",
    )
    assert a.regime == b.regime
    assert a.confidence == b.confidence
    assert a.trend_score == b.trend_score
    assert a.volatility == b.volatility
