"""Phase 33 — copy-trader tests."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

from shared.copy import (
    CopyMapper,
    CopyService,
    CopySource,
    CopyStore,
    CopyTrade,
    InMemoryCopyPool,
    MIGRATION_PATH,
    SourceFill,
    StaticCopySource,
    read_migration_sql,
)
from shared.copy.protocol import (
    SIZE_MODE_FIXED_NOTIONAL_USD,
    SIZE_MODE_RATIO,
    SIZE_MODE_REPLICATE,
    SOURCE_KIND_STATIC,
)
from shared.cli import copy_cli


# -------------------------------------------------------------------- fixtures


def _mk_source(**kw) -> CopySource:
    defaults: dict = dict(
        id=None, name="leader1", kind=SOURCE_KIND_STATIC,
        identifier="leader1-test", venue=None,
        size_mode=SIZE_MODE_RATIO, size_value=0.1,
        max_notional_usd=None, symbol_whitelist=[],
        symbol_blacklist=[], enabled=True, metadata={},
    )
    defaults.update(kw)
    return CopySource(**defaults)


def _mk_fill(fill_id: str, price: float, qty: float, *,
             symbol: str = "BTC/USDT", side: str = "buy",
             when: datetime | None = None) -> SourceFill:
    ts = when or datetime.now(timezone.utc)
    return SourceFill(
        fill_id=fill_id, symbol=symbol, side=side,
        price=price, qty_base=qty, notional_usd=price * qty, ts=ts,
    )


# ------------------------------------------------------------------- migration


def test_migration_sql_present():
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS public.copy_sources" in sql
    assert "CREATE TABLE IF NOT EXISTS public.copy_trades" in sql
    assert "UNIQUE (source_id, source_fill_id)" in sql


# ---------------------------------------------------------------------- mapper


def test_mapper_ratio():
    m = CopyMapper()
    src = _mk_source(id=7, size_mode=SIZE_MODE_RATIO, size_value=0.25)
    r = m.map(src, _mk_fill("f1", 70_000.0, 0.4))
    assert r.kept and r.trade is not None
    assert r.trade.mapped_qty_base == 0.1  # 0.4 * 0.25
    assert r.trade.mapped_notional_usd == 70_000.0 * 0.1
    assert r.trade.status == "pending"


def test_mapper_fixed_notional():
    m = CopyMapper()
    src = _mk_source(id=7, size_mode=SIZE_MODE_FIXED_NOTIONAL_USD,
                     size_value=250.0)
    r = m.map(src, _mk_fill("f2", 50_000.0, 1.0))
    assert r.kept
    assert r.trade.mapped_notional_usd == 250.0
    assert abs(r.trade.mapped_qty_base - 250.0 / 50_000.0) < 1e-12


def test_mapper_replicate():
    m = CopyMapper()
    src = _mk_source(id=7, size_mode=SIZE_MODE_REPLICATE)
    r = m.map(src, _mk_fill("f3", 1000.0, 2.0))
    assert r.kept
    assert r.trade.mapped_qty_base == 2.0
    assert r.trade.mapped_notional_usd == 2000.0


def test_mapper_caps_at_max_notional():
    m = CopyMapper()
    src = _mk_source(id=7, size_mode=SIZE_MODE_RATIO, size_value=1.0,
                     max_notional_usd=500.0)
    r = m.map(src, _mk_fill("f4", 1_000.0, 5.0))  # 5000 notional
    assert r.kept
    assert r.trade.mapped_notional_usd == 500.0
    assert abs(r.trade.mapped_qty_base - 0.5) < 1e-12


def test_mapper_whitelist_blocks_non_matching_symbol():
    m = CopyMapper()
    src = _mk_source(id=7, symbol_whitelist=["BTC/USDT"])
    r = m.map(src, _mk_fill("f5", 1.0, 1.0, symbol="DOGE/USDT"))
    assert not r.kept
    assert r.skip_reason == "symbol_not_whitelisted"
    assert r.trade.status == "skipped"


def test_mapper_blacklist_blocks_symbol():
    m = CopyMapper()
    src = _mk_source(id=7, symbol_blacklist=["LUNA/USDT"])
    r = m.map(src, _mk_fill("f6", 1.0, 1.0, symbol="LUNA/USDT"))
    assert not r.kept
    assert r.skip_reason == "symbol_blacklisted"


def test_mapper_rejects_bad_price_or_qty():
    m = CopyMapper()
    src = _mk_source(id=7)
    r = m.map(src, _mk_fill("f7", 0.0, 1.0))
    assert r.skip_reason == "invalid_price_or_qty"


def test_mapper_rejects_disabled_source():
    m = CopyMapper()
    src = _mk_source(id=7, enabled=False)
    r = m.map(src, _mk_fill("f8", 1.0, 1.0))
    assert r.skip_reason == "source_disabled"


# ----------------------------------------------------------------------- store


def test_store_upserts_source():
    pool = InMemoryCopyPool()
    store = CopyStore(pool)
    s = _mk_source()
    sid1 = asyncio.run(store.upsert_source(s))
    s.size_value = 0.5
    sid2 = asyncio.run(store.upsert_source(s))
    assert sid1 == sid2
    got = asyncio.run(store.get_source(sid1))
    assert got and got.size_value == 0.5


def test_store_trades_unique_per_fill_id():
    pool = InMemoryCopyPool()
    store = CopyStore(pool)
    sid = asyncio.run(store.upsert_source(_mk_source()))
    trade = CopyTrade(
        id=None, source_id=sid, source_fill_id="x1",
        source_trade_ts=datetime.now(timezone.utc),
        symbol="BTC/USDT", side="buy",
        source_price=69_000.0, source_qty_base=0.1,
        source_notional_usd=6_900.0,
        mapped_qty_base=0.01, mapped_notional_usd=690.0,
        status="pending",
    )
    tid1 = asyncio.run(store.record_trade(trade))
    tid2 = asyncio.run(store.record_trade(trade))
    assert tid1 != 0
    assert tid2 == 0  # ON CONFLICT DO NOTHING


# --------------------------------------------------------------------- service


def test_service_tick_dedupes_and_respects_since():
    pool = InMemoryCopyPool()
    store = CopyStore(pool)
    sid = asyncio.run(store.upsert_source(_mk_source(size_value=0.1)))
    src = asyncio.run(store.get_source(sid))
    assert src is not None

    now = datetime.now(timezone.utc)
    fills = [
        _mk_fill("fA", 70_000.0, 0.5, when=now - timedelta(minutes=10)),
        _mk_fill("fB", 70_100.0, 0.5, when=now - timedelta(minutes=5)),
    ]
    fetcher = StaticCopySource(fills)
    svc = CopyService(store, fetcher)

    result = asyncio.run(svc.tick_one(src))
    assert result.fills_fetched == 2
    assert result.trades_kept == 2
    assert len(asyncio.run(store.list_trades(source_id=sid))) == 2

    # Re-tick: the service updated last_checked_at on the first tick,
    # so older fills are filtered out by `since`. Either way, the
    # store's unique constraint would also dedupe.
    src2 = asyncio.run(store.get_source(sid))
    assert src2 is not None
    assert src2.last_checked_at is not None
    result2 = asyncio.run(svc.tick_one(src2))
    assert result2.fills_fetched == 0
    assert len(asyncio.run(store.list_trades(source_id=sid))) == 2

    # Add a new fill after the watermark; it should be picked up.
    fetcher.append(
        _mk_fill("fC", 70_200.0, 0.5, when=now + timedelta(minutes=1)))
    src3 = asyncio.run(store.get_source(sid))
    assert src3 is not None
    result3 = asyncio.run(svc.tick_one(src3))
    assert result3.fills_fetched == 1
    assert result3.trades_kept == 1
    assert len(asyncio.run(store.list_trades(source_id=sid))) == 3


def test_service_skipped_trades_are_audited():
    pool = InMemoryCopyPool()
    store = CopyStore(pool)
    sid = asyncio.run(store.upsert_source(
        _mk_source(symbol_whitelist=["BTC/USDT"])))
    src = asyncio.run(store.get_source(sid))
    fetcher = StaticCopySource([
        _mk_fill("g1", 100.0, 10.0, symbol="BTC/USDT"),
        _mk_fill("g2", 100.0, 10.0, symbol="SHIB/USDT"),
    ])
    svc = CopyService(store, fetcher)
    r = asyncio.run(svc.tick_one(src))
    assert r.trades_kept == 1
    assert r.trades_skipped == 1
    skipped = asyncio.run(store.list_trades(source_id=sid, status="skipped"))
    assert len(skipped) == 1 and skipped[0].skip_reason == "symbol_not_whitelisted"


# ---------------------------------------------------------------------- CLI


def _run_cli(argv):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = copy_cli.main(argv)
    return rc, buf.getvalue()


def test_cli_migration_sql():
    rc, out = _run_cli(["migration-sql"])
    assert rc == 0
    assert "CREATE TABLE IF NOT EXISTS public.copy_trades" in out


def test_cli_apply_migration_path_only():
    rc, out = _run_cli(["apply-migration", "--path-only"])
    assert rc == 0
    assert "2026_04_19_phase33_copy.sql" in out


def test_cli_source_add_and_tick_flow(tmp_path):
    tmp = tmp_path / "fills.json"
    now = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps([
        {
            "fill_id": "x1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "price": 70_000.0,
            "qty_base": 0.1,
            "notional_usd": 7_000.0,
            "ts": now,
        },
    ]))
    # We need state across CLI calls, so use one in-memory pool by
    # monkeypatching _get_pool to return a shared instance.
    shared_pool = InMemoryCopyPool()

    async def _shared_pool(dsn, in_memory):  # noqa: ARG001
        return shared_pool

    with patch.object(copy_cli, "_get_pool", _shared_pool):
        rc, _ = _run_cli([
            "source-add", "--name", "leader1",
            "--identifier", "leader-xyz", "--size-value", "0.25",
            "--in-memory",
        ])
        assert rc == 0
        rc, out = _run_cli([
            "sources", "--in-memory",
        ])
        assert rc == 0
        payload = json.loads(out)
        assert payload["count"] == 1
        sid = payload["sources"][0]["id"]

        rc, out = _run_cli([
            "tick", "--source-id", str(sid),
            "--fills", f"@{tmp}", "--in-memory",
        ])
        assert rc == 0
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["result"]["trades_kept"] == 1
        assert payload["result"]["trades"][0]["mapped_notional_usd"] == 1_750.0

        rc, out = _run_cli([
            "trades", "--source-id", str(sid), "--in-memory",
        ])
        assert rc == 0
        payload = json.loads(out)
        assert payload["count"] == 1


# -------------------------------------------------------------------- registry


def test_copy_service_is_registered():
    from shared.services.registry import (
        SERVICE_REGISTRY,
        register_builtin_services,
    )
    register_builtin_services()
    svc = SERVICE_REGISTRY.get("copy-trader")
    assert svc.kind == "worker"
    assert svc.tags.get("phase") == "33"
    assert svc.enabled_on_vps is False
