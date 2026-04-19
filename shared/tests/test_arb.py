"""Phase 33 — arbitrage scanner tests."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Dict
from unittest.mock import patch


from shared.arb import (
    ArbOpportunity,
    ArbQuote,
    ArbScanner,
    ArbService,
    ArbStore,
    ArbVenue,
    InMemoryArbPool,
    MIGRATION_PATH,
    OfflineQuoteFetcher,
    ScannerConfig,
    read_migration_sql,
)
from shared.cli import arb_cli


# --------------------------------------------------------------------- fixtures


def _venues_fixture() -> list:
    return [
        ArbVenue(id=None, name="binance", kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0),
        ArbVenue(id=None, name="kraken", kind="spot",
                 taker_fee_bps=26.0, maker_fee_bps=16.0),
        ArbVenue(id=None, name="coinbase", kind="spot",
                 taker_fee_bps=40.0, maker_fee_bps=25.0),
    ]


def _quote_book(btc_binance_ask: float, btc_kraken_bid: float,
                coinbase_ask: float = 70_000.0) -> Dict[str, Dict[str, ArbQuote]]:
    return {
        "binance": {
            "BTC/USDT": ArbQuote(
                venue="binance", symbol="BTC/USDT",
                bid=btc_binance_ask - 1.0, ask=btc_binance_ask,
                bid_size=1.0, ask_size=1.0,
            ),
        },
        "kraken": {
            "BTC/USDT": ArbQuote(
                venue="kraken", symbol="BTC/USDT",
                bid=btc_kraken_bid, ask=btc_kraken_bid + 1.0,
                bid_size=1.0, ask_size=1.0,
            ),
        },
        "coinbase": {
            "BTC/USDT": ArbQuote(
                venue="coinbase", symbol="BTC/USDT",
                bid=coinbase_ask - 1.0, ask=coinbase_ask,
                bid_size=1.0, ask_size=1.0,
            ),
        },
    }


# ---------------------------------------------------------------------- migration


def test_migration_path_exists():
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS public.arb_venues" in sql
    assert "CREATE TABLE IF NOT EXISTS public.arb_opportunities" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS arb_venues_unique_idx" in sql


# ------------------------------------------------------------------------ scanner


def test_scanner_finds_positive_spread():
    # coinbase ask is higher than kraken bid → binance→kraken should
    # be the only pair that makes sense; use only two venues to keep
    # the math unambiguous.
    book = {
        "binance": {
            "BTC/USDT": ArbQuote(
                venue="binance", symbol="BTC/USDT",
                bid=68_999.0, ask=69_000.0, bid_size=1.0, ask_size=1.0,
            ),
        },
        "kraken": {
            "BTC/USDT": ArbQuote(
                venue="kraken", symbol="BTC/USDT",
                bid=69_500.0, ask=69_501.0, bid_size=1.0, ask_size=1.0,
            ),
        },
    }
    fetcher = OfflineQuoteFetcher(book)
    scanner = ArbScanner(
        _venues_fixture()[:2], fetcher,
        config=ScannerConfig(min_net_bps=5.0, max_size_usd=1_000_000.0),
    )
    opps = asyncio.run(scanner.scan("BTC/USDT"))
    assert any(
        o.buy_venue == "binance" and o.sell_venue == "kraken" for o in opps
    )
    top = opps[0]
    assert top.buy_venue == "binance" and top.sell_venue == "kraken"
    assert top.net_bps > 0
    assert top.est_profit_usd > 0
    # Gross bps ~ (69500 - 69000) / 69000 * 10_000 == ~72.46
    assert top.gross_bps > 60 and top.gross_bps < 80
    # Fees = binance taker (10) + kraken taker (26) = 36
    assert abs(top.fees_bps - 36.0) < 0.01


def test_scanner_skips_below_threshold():
    book = {
        "binance": {
            "BTC/USDT": ArbQuote(
                venue="binance", symbol="BTC/USDT",
                bid=68_999.0, ask=69_000.0, bid_size=1.0, ask_size=1.0,
            ),
        },
        "kraken": {
            "BTC/USDT": ArbQuote(
                venue="kraken", symbol="BTC/USDT",
                bid=69_010.0, ask=69_012.0, bid_size=1.0, ask_size=1.0,
            ),
        },
    }
    fetcher = OfflineQuoteFetcher(book)
    # Gap is tiny; after 36bps fees net_bps is negative → no opps.
    scanner = ArbScanner(
        _venues_fixture()[:2], fetcher,
        config=ScannerConfig(min_net_bps=5.0),
    )
    opps = asyncio.run(scanner.scan("BTC/USDT"))
    assert opps == []


def test_scanner_sorts_by_net_bps_desc():
    # Engineered so multiple (buy, sell) pairs yield net-positive spreads.
    fetcher = OfflineQuoteFetcher(_quote_book(
        btc_binance_ask=69_000.0, btc_kraken_bid=69_500.0,
        coinbase_ask=69_800.0))
    scanner = ArbScanner(
        _venues_fixture(), fetcher,
        config=ScannerConfig(min_net_bps=0.0, max_size_usd=1_000_000.0),
    )
    opps = asyncio.run(scanner.scan("BTC/USDT"))
    assert opps == sorted(opps, key=lambda o: (-o.net_bps, o.symbol))


def test_scanner_respects_max_size_usd():
    fetcher = OfflineQuoteFetcher(_quote_book(
        btc_binance_ask=50.0, btc_kraken_bid=55.0))
    scanner = ArbScanner(
        _venues_fixture(), fetcher,
        config=ScannerConfig(min_net_bps=0.0, max_size_usd=500.0),
    )
    opps = asyncio.run(scanner.scan("BTC/USDT"))
    assert opps
    # max_size_usd / buy_ask = 500/50 = 10 base units cap; book_size=1 caps tighter.
    assert opps[0].size_base <= 1.0 + 1e-9


def test_scanner_needs_two_quotes():
    fetcher = OfflineQuoteFetcher({
        "binance": {
            "BTC/USDT": ArbQuote(
                venue="binance", symbol="BTC/USDT",
                bid=69_000.0, ask=69_001.0, bid_size=1.0, ask_size=1.0,
            ),
        },
    })
    scanner = ArbScanner(_venues_fixture(), fetcher,
                        config=ScannerConfig(min_net_bps=0.0))
    assert asyncio.run(scanner.scan("BTC/USDT")) == []


# -------------------------------------------------------------------------- store


def test_store_upserts_venue_and_opportunity():
    pool = InMemoryArbPool()
    store = ArbStore(pool)
    v = ArbVenue(id=None, name="binance", kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0)
    vid1 = asyncio.run(store.upsert_venue(v))
    v.taker_fee_bps = 11.0
    vid2 = asyncio.run(store.upsert_venue(v))
    assert vid1 == vid2
    assert asyncio.run(store.list_venues(enabled_only=True))[0].taker_fee_bps == 11.0

    opp = ArbOpportunity(
        id=None, symbol="BTC/USDT", buy_venue="binance", sell_venue="kraken",
        buy_ask=69_000.0, sell_bid=69_500.0, size_base=0.5,
        gross_bps=72.5, net_bps=36.5, est_profit_usd=126.0,
        fees_bps=36.0, observed_at=datetime.now(timezone.utc),
    )
    oid = asyncio.run(store.record_opportunity(opp))
    assert oid > 0
    rows = asyncio.run(store.list_opportunities(symbol="BTC/USDT"))
    assert len(rows) == 1 and rows[0].buy_venue == "binance"


# ------------------------------------------------------------------------ service


def test_service_persists_opportunities():
    pool = InMemoryArbPool()
    store = ArbStore(pool)
    venues = _venues_fixture()
    for v in venues:
        asyncio.run(store.upsert_venue(v))
    fetcher = OfflineQuoteFetcher(_quote_book(
        btc_binance_ask=69_000.0, btc_kraken_bid=69_500.0))
    svc = ArbService(store, fetcher, venues=venues,
                     scanner_config=ScannerConfig(min_net_bps=5.0,
                                                   max_size_usd=1_000_000.0))
    opps = asyncio.run(svc.scan_symbols(["BTC/USDT"]))
    assert opps
    saved = asyncio.run(store.list_opportunities(symbol="BTC/USDT"))
    assert len(saved) == len(opps)
    assert saved[0].buy_venue == "binance"


def test_service_dry_run_does_not_persist():
    pool = InMemoryArbPool()
    store = ArbStore(pool)
    fetcher = OfflineQuoteFetcher(_quote_book(
        btc_binance_ask=69_000.0, btc_kraken_bid=69_500.0))
    svc = ArbService(store, fetcher, venues=_venues_fixture(),
                     scanner_config=ScannerConfig(min_net_bps=5.0,
                                                   max_size_usd=1_000_000.0))
    opps = asyncio.run(svc.scan_symbols(["BTC/USDT"], persist=False))
    assert opps
    assert asyncio.run(store.list_opportunities(symbol="BTC/USDT")) == []


# ---------------------------------------------------------------------------- CLI


def _run_cli(argv):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = arb_cli.main(argv)
    return rc, buf.getvalue()


def test_cli_migration_sql():
    rc, out = _run_cli(["migration-sql"])
    assert rc == 0
    assert "CREATE TABLE IF NOT EXISTS public.arb_opportunities" in out


def test_cli_apply_migration_path_only():
    rc, out = _run_cli(["apply-migration", "--path-only"])
    assert rc == 0
    assert "2026_04_19_phase33_arb.sql" in out


def test_cli_venue_add_and_venues_list():
    rc, _ = _run_cli([
        "venue-add", "--name", "binance",
        "--taker-fee-bps", "10.0", "--in-memory",
    ])
    assert rc == 0


def test_cli_scan_with_inline_quotes():
    quotes = {
        "binance": {
            "BTC/USDT": {"bid": 68_999.0, "ask": 69_000.0,
                          "bid_size": 1.0, "ask_size": 1.0}
        },
        "kraken": {
            "BTC/USDT": {"bid": 69_500.0, "ask": 69_501.0,
                          "bid_size": 1.0, "ask_size": 1.0}
        },
    }
    rc, out = _run_cli([
        "scan", "--quotes", json.dumps(quotes), "--symbols", "BTC/USDT",
        "--default-venues", "--min-net-bps", "5", "--in-memory",
    ])
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["count"] >= 1
    top = payload["opportunities"][0]
    assert top["buy_venue"] == "binance"
    assert top["sell_venue"] == "kraken"


def test_cli_scan_with_file_quotes(tmp_path: Path):
    quotes = {
        "binance": {
            "ETH/USDT": {"bid": 3499.0, "ask": 3500.0,
                          "bid_size": 10.0, "ask_size": 10.0}
        },
        "kraken": {
            "ETH/USDT": {"bid": 3520.0, "ask": 3521.0,
                          "bid_size": 10.0, "ask_size": 10.0}
        },
    }
    p = tmp_path / "q.json"
    p.write_text(json.dumps(quotes))
    rc, out = _run_cli([
        "scan", "--quotes", f"@{p}", "--symbols", "ETH/USDT",
        "--default-venues", "--min-net-bps", "5", "--in-memory",
    ])
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] >= 1


def test_cli_opportunities_empty():
    rc, out = _run_cli(["opportunities", "--in-memory"])
    assert rc == 0
    assert json.loads(out)["count"] == 0


# ------------------------------------------------------------------------- misc


def test_arb_service_is_registered():
    from shared.services.registry import (
        SERVICE_REGISTRY,
        register_builtin_services,
    )
    register_builtin_services()
    svc = SERVICE_REGISTRY.get("arb-scanner")
    assert svc.kind == "worker"
    assert svc.tags.get("phase") == "33"
    assert svc.enabled_on_vps is False


def test_arb_quote_mid_zero_when_missing_side():
    q = ArbQuote(venue="x", symbol="BTC/USDT", bid=0.0, ask=100.0)
    assert q.mid == 0.0
