"""Phase 29 — Alt-Data Ingestion tests."""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, List

from shared.altdata import (
    AltDataIngestor,
    AltDataItem,
    AltDataStore,
    InMemoryAltDataPool,
    MIGRATION_PATH,
    METRIC_FUNDING_RATE,
    METRIC_OI_CONTRACTS,
    METRIC_OI_USD,
    ManualAltDataSource,
    SOURCE_CUSTOM,
    SOURCE_FUNDING_RATE,
    StaticAltDataSource,
    read_migration_sql,
)
from shared.altdata.sources import (
    CcxtFundingRateSource,
    CcxtOpenInterestSource,
)
from shared.cli import altdata_cli
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_has_expected_objects() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for obj in (
        "public.alt_data_items",
        "public.alt_data_latest",
        "UNIQUE (source, provider, scope_key, metric, as_of)",
    ):
        assert obj in sql


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_alt_data_item_to_dict_roundtrips_datetime() -> None:
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item = AltDataItem(
        source=SOURCE_FUNDING_RATE, provider="ccxt:binance",
        scope_key="ccxt:binance/BTCUSDT/funding", metric=METRIC_FUNDING_RATE,
        as_of=t, value_numeric=0.0001, unit="ratio",
        exchange="binance", symbol="BTC/USDT",
    )
    d = item.to_dict()
    assert d["as_of"] == t.isoformat()
    assert d["value_numeric"] == 0.0001


# ---------------------------------------------------------------------------
# Static + Manual sources
# ---------------------------------------------------------------------------


def _item(as_of=None, metric=METRIC_FUNDING_RATE, scope="ccxt:binance/BTCUSDT/funding",
          value=0.0001) -> AltDataItem:
    return AltDataItem(
        source=SOURCE_FUNDING_RATE, provider="ccxt:binance",
        scope_key=scope, metric=metric,
        as_of=as_of or datetime.now(timezone.utc),
        value_numeric=value, unit="ratio",
        exchange="binance", symbol="BTC/USDT",
    )


def test_static_source_returns_copy_of_items() -> None:
    items = [_item()]
    src = StaticAltDataSource(items=items)
    out = _run(src.fetch())
    assert len(out) == 1
    out.append(_item())
    assert len(src.items) == 1  # didn't mutate


def test_manual_source_drains_queue() -> None:
    src = ManualAltDataSource()
    src.push(_item())
    src.push(_item(metric="whatever"))
    out = _run(src.fetch())
    assert len(out) == 2
    assert _run(src.fetch()) == []


# ---------------------------------------------------------------------------
# Store + InMemory pool
# ---------------------------------------------------------------------------


def test_store_insert_and_dedupes_on_unique_key() -> None:
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)

    async def go() -> None:
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rid = await store.insert_item(_item(as_of=t))
        assert rid == 1
        dup = await store.insert_item(_item(as_of=t))
        assert dup == 0  # deduped
        items = await store.list_items(source=SOURCE_FUNDING_RATE, limit=10)
        assert len(items) == 1

    _run(go())


def test_store_list_items_filters_by_exchange_and_metric() -> None:
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)

    async def go() -> None:
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await store.insert_item(_item(as_of=t, metric=METRIC_FUNDING_RATE))
        await store.insert_item(_item(as_of=t, metric=METRIC_OI_USD,
                                      scope="ccxt:binance/BTCUSDT/oi"))
        fr = await store.list_items(metric=METRIC_FUNDING_RATE, limit=10)
        oi = await store.list_items(metric=METRIC_OI_USD, limit=10)
        assert len(fr) == 1
        assert len(oi) == 1
        assert fr[0].metric == METRIC_FUNDING_RATE
        assert oi[0].metric == METRIC_OI_USD

    _run(go())


def test_store_list_items_filters_since() -> None:
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)

    async def go() -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(5):
            await store.insert_item(_item(
                as_of=base + timedelta(minutes=i),
                metric=f"m{i}",
                scope=f"scope/{i}",
            ))
        recent = await store.list_items(
            since=base + timedelta(minutes=3), limit=10,
        )
        assert len(recent) == 2

    _run(go())


def test_store_list_latest_dedupes_by_scope_and_metric() -> None:
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)

    async def go() -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(3):
            await store.insert_item(_item(
                as_of=base + timedelta(minutes=i),
                value=float(i),
            ))
        latest = await store.list_latest(source=SOURCE_FUNDING_RATE, limit=10)
        assert len(latest) == 1
        assert latest[0].value_numeric == 2.0  # most recent wins

    _run(go())


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------


def test_ingestor_persists_new_items_and_reports_counts() -> None:
    items = [
        _item(
            as_of=datetime(2026, 1, 1, tzinfo=timezone.utc),
            value=0.0001,
        ),
        _item(
            as_of=datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc),
            value=0.00012,
        ),
    ]
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)
    ingestor = AltDataIngestor([StaticAltDataSource(items=items)], store=store)

    async def go() -> None:
        r = await ingestor.tick()
        assert r.attempted == 2
        assert r.inserted == 2
        assert r.skipped == 0
        # Second tick — same items, nothing new
        r2 = await ingestor.tick()
        assert r2.attempted == 2
        assert r2.inserted == 0
        assert r2.skipped == 2

    _run(go())


def test_ingestor_no_persist_mode_skips_inserts() -> None:
    items = [_item()]
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)
    ingestor = AltDataIngestor([StaticAltDataSource(items=items)], store=store)

    async def go() -> None:
        r = await ingestor.tick(persist=False)
        assert r.inserted == 0
        assert r.skipped == 1
        assert await store.list_items(source=SOURCE_FUNDING_RATE) == []

    _run(go())


def test_ingestor_normalises_naive_datetime_to_utc() -> None:
    naive = datetime(2026, 1, 2, 0, 0, 0)
    item = AltDataItem(
        source=SOURCE_CUSTOM, provider="seed", scope_key="seed/1",
        metric="x", as_of=naive, value_numeric=1.0,
    )
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)
    ingestor = AltDataIngestor([StaticAltDataSource(items=[item])], store=store)

    async def go() -> None:
        await ingestor.tick()
        rows = await store.list_items(source=SOURCE_CUSTOM, limit=10)
        assert rows[0].as_of.tzinfo is not None

    _run(go())


def test_ingestor_add_source_is_reflected_in_next_tick() -> None:
    pool = InMemoryAltDataPool()
    store = AltDataStore(pool)
    ingestor = AltDataIngestor([], store=store)

    async def go() -> None:
        r = await ingestor.tick()
        assert r.attempted == 0

        ingestor.add_source(StaticAltDataSource(items=[_item()]))
        r2 = await ingestor.tick()
        assert r2.attempted == 1

    _run(go())


# ---------------------------------------------------------------------------
# CCXT sources (with fake exchanges)
# ---------------------------------------------------------------------------


class _FakeFundingExchange:
    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def fetch_funding_rate(self, symbol: str) -> Any:
        return self._mapping[symbol]


class _FakeOIExchange:
    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def fetch_open_interest(self, symbol: str) -> Any:
        return self._mapping[symbol]


def test_ccxt_funding_source_converts_rows() -> None:
    ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ex = _FakeFundingExchange({
        "BTC/USDT": {"symbol": "BTC/USDT", "fundingRate": 0.0002, "timestamp": ts},
        "ETH/USDT": {"symbol": "ETH/USDT", "fundingRate": 0.0001, "timestamp": ts},
    })
    src = CcxtFundingRateSource(
        exchange_id="binance",
        symbols=("BTC/USDT", "ETH/USDT"),
        exchange_obj=ex,
    )
    out = _run(src.fetch())
    assert len(out) == 2
    btc = next(x for x in out if x.symbol == "BTC/USDT")
    assert btc.metric == METRIC_FUNDING_RATE
    assert btc.value_numeric == 0.0002
    assert btc.scope_key == "ccxt:binance/BTCUSDT/funding"


def test_ccxt_funding_source_skips_bad_rows() -> None:
    ex = _FakeFundingExchange({
        "BTC/USDT": {"symbol": "BTC/USDT"},  # no fundingRate
    })
    src = CcxtFundingRateSource(symbols=("BTC/USDT",), exchange_obj=ex)
    assert _run(src.fetch()) == []


def test_ccxt_open_interest_source_emits_contracts_and_usd() -> None:
    ts = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    ex = _FakeOIExchange({
        "BTC/USDT": {
            "symbol": "BTC/USDT", "openInterestAmount": 12345.0,
            "openInterestValue": 1e9, "timestamp": ts,
        },
    })
    src = CcxtOpenInterestSource(symbols=("BTC/USDT",), exchange_obj=ex)
    out = _run(src.fetch())
    metrics = sorted([x.metric for x in out])
    assert metrics == sorted([METRIC_OI_CONTRACTS, METRIC_OI_USD])


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_altdata_ingestor_service_is_registered() -> None:
    register_builtin_services()
    assert "altdata-ingestor" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("altdata-ingestor")
    assert desc.tags.get("phase") == "29"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> Any:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = altdata_cli.main(argv)
    out = buf.getvalue().strip()
    assert code == 0, f"cli exit={code}; stdout={out!r}"
    return json.loads(out)


def test_cli_apply_migration_path_only() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = altdata_cli.main(["apply-migration", "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH)


def test_cli_sources_lists_built_in() -> None:
    data = _run_cli(["sources"])
    names = {b["name"] for b in data["built_in"]}
    assert {"static", "manual", "ccxt_funding", "ccxt_open_interest"} <= names


def test_cli_push_and_items_in_memory(tmp_path) -> None:
    data = _run_cli([
        "push", "--in-memory",
        "--source", SOURCE_CUSTOM,
        "--provider", "seed",
        "--scope-key", "seed/abc",
        "--metric", "x",
        "--value", "1.0",
        "--as-of", "2026-01-01T00:00:00+00:00",
    ])
    assert data["ok"] is True
    # push and items use different pool instances in-memory so count is 0 on reread
    data_items = _run_cli(["items", "--in-memory", "--limit", "10"])
    assert data_items["ok"] is True
    assert data_items["count"] == 0


def test_cli_ingest_from_fixture_file(tmp_path) -> None:
    fixture = [
        {
            "source": SOURCE_FUNDING_RATE,
            "provider": "ccxt:binance",
            "scope_key": "ccxt:binance/BTCUSDT/funding",
            "metric": METRIC_FUNDING_RATE,
            "as_of": "2026-01-01T00:00:00+00:00",
            "value_numeric": 0.0001,
            "unit": "ratio",
            "exchange": "binance",
            "symbol": "BTC/USDT",
        }
    ]
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    data = _run_cli(["ingest", "--in-memory", "--fixture-file", str(path)])
    assert data["ok"] is True
    assert data["report"]["attempted"] == 1
    assert data["report"]["inserted"] == 1


def test_cli_latest_empty_in_memory() -> None:
    data = _run_cli(["latest", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0
