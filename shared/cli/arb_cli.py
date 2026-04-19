"""
shared.cli.arb_cli — operator CLI for the Phase 33 arbitrage scanner.

Subcommands:
  apply-migration / migration-sql       DB migration helpers.
  venues / venue-add                    Inspect / register venues.
  scan                                  Deterministic scan over static quotes.
  live                                  Live scan using CCXT public tickers.
  opportunities                         List recent opportunities.
  demo                                  Print a friendly demo table from CCXT.

The ``live`` and ``demo`` subcommands use public endpoints only — no
API keys required. They're the tangible part the owner can inspect
while the system is being built.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from shared.arb import (
    ArbService,
    ArbStore,
    ArbVenue,
    CcxtQuoteFetcher,
    InMemoryArbPool,
    MIGRATION_PATH,
    OfflineQuoteFetcher,
    ScannerConfig,
    read_migration_sql,
)
from shared.arb.protocol import ArbQuote

LOG = logging.getLogger("tickles.cli.arb")


# ---------------------------------------------------------------------------
# Pool helpers (same pattern as souls_cli)
# ---------------------------------------------------------------------------


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryArbPool()
    try:
        import asyncpg  # type: ignore
    except ImportError as exc:
        raise RuntimeError("asyncpg not installed") from exc

    class _AsyncpgPool:
        def __init__(self, pool: Any) -> None:
            self._pool = pool

        async def execute(self, sql: str, params: Sequence[Any]) -> int:
            async with self._pool.acquire() as conn:
                res = await conn.execute(sql, *params)
            return int(res.split()[-1]) if res and res[-1].isdigit() else 0

        async def fetch_one(
            self, sql: str, params: Sequence[Any],
        ) -> Optional[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(
            self, sql: str, params: Sequence[Any],
        ) -> List[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    return _AsyncpgPool(pool)


def _default(obj: Any) -> Any:
    from datetime import datetime
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _handle_migration_sql(_args: argparse.Namespace) -> int:
    print(read_migration_sql())
    return 0


def _handle_apply_migration(args: argparse.Namespace) -> int:
    if args.path_only:
        print(str(MIGRATION_PATH))
        return 0
    _dump({
        "migration_path": str(MIGRATION_PATH),
        "apply_with": (
            "psql -h 127.0.0.1 -U admin -d tickles_shared "
            f"-f {MIGRATION_PATH}"
        ),
        "ok": True,
    })
    return 0


async def _handle_venue_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = ArbStore(pool)
    vid = await store.upsert_venue(ArbVenue(
        id=None, name=args.name, kind=args.kind,
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        enabled=not args.disabled,
        company_id=args.company_id,
    ))
    _dump({"ok": True, "id": vid, "name": args.name})
    return 0


async def _handle_venues_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = ArbStore(pool)
    venues = await store.list_venues(enabled_only=args.enabled_only)
    _dump({
        "ok": True, "count": len(venues),
        "venues": [v.to_dict() for v in venues],
    })
    return 0


async def _handle_scan_async(args: argparse.Namespace) -> int:
    """Deterministic scan using a supplied JSON quote book."""

    quote_book = _load_quote_book(args.quotes)
    if not quote_book:
        _dump({"ok": False, "error": "no quotes supplied (use --quotes)"})
        return 1
    pool = await _get_pool(args.dsn, args.in_memory)
    store = ArbStore(pool)
    venues = _default_demo_venues() if args.default_venues else (
        await store.list_venues(enabled_only=True) or _default_demo_venues()
    )
    fetcher = OfflineQuoteFetcher(book=quote_book)
    svc = ArbService(
        store, fetcher, venues=venues,
        scanner_config=ScannerConfig(
            min_net_bps=args.min_net_bps,
            max_size_usd=args.max_size_usd,
            max_opportunities=args.max,
        ),
    )
    symbols = args.symbols or sorted({
        s for quotes in quote_book.values() for s in quotes.keys()
    })
    opps = await svc.scan_symbols(
        symbols, correlation_id=args.correlation_id, persist=not args.no_persist,
    )
    _dump({
        "ok": True, "count": len(opps), "symbols": symbols,
        "opportunities": [o.to_dict() for o in opps],
    })
    return 0


async def _handle_live_async(args: argparse.Namespace) -> int:
    """Live CCXT scan — public endpoints only."""

    pool = await _get_pool(args.dsn, args.in_memory)
    store = ArbStore(pool)
    venues = (
        _default_demo_venues()
        if args.default_venues or not await store.list_venues(enabled_only=True)
        else await store.list_venues(enabled_only=True)
    )
    fetcher = CcxtQuoteFetcher(timeout_s=args.timeout_s)
    try:
        svc = ArbService(
            store, fetcher, venues=venues,
            scanner_config=ScannerConfig(
                min_net_bps=args.min_net_bps,
                max_size_usd=args.max_size_usd,
                max_opportunities=args.max,
            ),
        )
        opps = await svc.scan_symbols(
            args.symbols, correlation_id=args.correlation_id,
            persist=not args.no_persist,
        )
        _dump({
            "ok": True, "count": len(opps), "symbols": args.symbols,
            "venues_tried": [v.name for v in venues],
            "opportunities": [o.to_dict() for o in opps],
        })
        return 0
    finally:
        await fetcher.close()


async def _handle_demo_async(args: argparse.Namespace) -> int:
    """Human-friendly live demo: prints a pretty table of the spread."""

    venues = _default_demo_venues()
    fetcher = CcxtQuoteFetcher(timeout_s=args.timeout_s)
    pool = InMemoryArbPool()
    store = ArbStore(pool)
    try:
        print(f"[demo] scanning {args.symbols} across "
              f"{[v.name for v in venues]} ...")
        # Fetch raw quotes for display
        tables: Dict[str, Dict[str, ArbQuote]] = {}
        for symbol in args.symbols:
            quotes = await fetcher.fetch_top_of_book(symbol,
                                                     [v.name for v in venues])
            tables[symbol] = quotes
            print()
            print(f"== {symbol} ==")
            if not quotes:
                print("  (no venue returned a valid quote)")
                continue
            # pick precision so tiny-price coins (PEPE, SHIB) print usefully
            sample_price = max(
                (q.ask for q in quotes.values() if q.ask > 0),
                default=1.0,
            )
            if sample_price >= 100:
                dp = 4
            elif sample_price >= 1:
                dp = 6
            elif sample_price >= 0.01:
                dp = 8
            else:
                dp = 10
            for v in venues:
                q = quotes.get(v.name)
                if q is None:
                    print(f"  {v.name:<10} (unavailable)")
                else:
                    best_ask = min(
                        (qq.ask for qq in quotes.values() if qq.ask > 0),
                        default=q.ask,
                    )
                    best_bid = max(
                        (qq.bid for qq in quotes.values() if qq.bid > 0),
                        default=q.bid,
                    )
                    gross_gap = (
                        (q.bid - best_ask) / best_ask * 10_000.0
                        if best_ask and q.bid else 0.0
                    )
                    best_bid_mark = " *" if q.bid == best_bid else "  "
                    best_ask_mark = " *" if q.ask == best_ask else "  "
                    print(
                        f"  {v.name:<10} bid {q.bid:>16.{dp}f}{best_bid_mark}  "
                        f"ask {q.ask:>16.{dp}f}{best_ask_mark}  "
                        f"gap-vs-best-ask {gross_gap:>+7.2f}bps"
                    )
        # Run the scanner deterministically over whatever we fetched.
        offline = OfflineQuoteFetcher({
            v: {s: q for s, q in ((sym, tables[sym].get(v))
                                   for sym in tables)
                if q is not None}
            for v in {vn.name for vn in venues}
        })
        svc_off = ArbService(
            store, offline, venues=venues,
            scanner_config=ScannerConfig(
                min_net_bps=args.min_net_bps,
                max_size_usd=args.max_size_usd,
                max_opportunities=args.max,
            ),
        )
        opps = await svc_off.scan_symbols(
            args.symbols, correlation_id="demo", persist=False,
        )
        print()
        print(
            f"== opportunities (min_net_bps={args.min_net_bps}, "
            f"max_size_usd={args.max_size_usd}) =="
        )
        if not opps:
            print("  (no net-positive gap after fees — normal in liquid markets)")
        for o in opps:
            print(
                f"  {o.symbol:<10} BUY@{o.buy_venue:<9} {o.buy_ask:>12.4f}"
                f"  |  SELL@{o.sell_venue:<9} {o.sell_bid:>12.4f}"
                f"  net={o.net_bps:>7.2f}bps  ~${o.est_profit_usd:>10.2f}"
            )
        return 0
    finally:
        await fetcher.close()


async def _handle_opportunities_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = ArbStore(pool)
    rows = await store.list_opportunities(
        symbol=args.symbol, min_net_bps=args.min_net_bps, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "opportunities": [r.to_dict() for r in rows],
    })
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_demo_venues() -> List[ArbVenue]:
    # Four very liquid venues — all public REST endpoints work without
    # any API keys. Fees are taker-ish defaults; operators can override
    # them via ``arb_cli venue-add`` once a real book of business exists.
    return [
        ArbVenue(id=None, name="binance",  kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0),
        ArbVenue(id=None, name="kraken",   kind="spot",
                 taker_fee_bps=26.0, maker_fee_bps=16.0),
        ArbVenue(id=None, name="coinbase", kind="spot",
                 taker_fee_bps=40.0, maker_fee_bps=25.0),
        ArbVenue(id=None, name="bybit",    kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0),
    ]


def _load_quote_book(value: Optional[str]) -> Optional[Dict[str, Dict[str, ArbQuote]]]:
    if not value:
        return None
    if value.startswith("@"):
        with open(value[1:], "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = json.loads(value)
    book: Dict[str, Dict[str, ArbQuote]] = {}
    for venue, symbols in raw.items():
        book[venue] = {}
        for sym, q in symbols.items():
            book[venue][sym] = ArbQuote(
                venue=venue, symbol=sym,
                bid=float(q.get("bid", 0.0)), ask=float(q.get("ask", 0.0)),
                bid_size=float(q.get("bid_size", 0.0)),
                ask_size=float(q.get("ask_size", 0.0)),
            )
    return book


# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_ARB_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arb_cli",
                                description="Phase 33 arbitrage scanner CLI.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration",
                        help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 33 arb migration SQL.")

    sp = sub.add_parser("venue-add", help="Upsert a venue.")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", default="spot")
    sp.add_argument("--taker-fee-bps", type=float, default=10.0)
    sp.add_argument("--maker-fee-bps", type=float, default=2.0)
    sp.add_argument("--disabled", action="store_true")
    sp.add_argument("--company-id", default=None)
    _add_common(sp)

    sp = sub.add_parser("venues", help="List registered venues.")
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("scan",
                        help="Deterministic scan over a supplied quote book.")
    sp.add_argument("--quotes", required=True,
                    help="JSON dict OR @path/to/file.json with "
                         "{venue:{symbol:{bid,ask,bid_size,ask_size}}}.")
    sp.add_argument("--symbols", nargs="*")
    sp.add_argument("--default-venues", action="store_true",
                    help="Use built-in demo venues instead of the store.")
    sp.add_argument("--min-net-bps", type=float, default=5.0)
    sp.add_argument("--max-size-usd", type=float, default=10_000.0)
    sp.add_argument("--max", type=int, default=50)
    sp.add_argument("--correlation-id", default=None)
    sp.add_argument("--no-persist", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("live",
                        help="Live scan using CCXT public tickers.")
    sp.add_argument("--symbols", nargs="+", required=True)
    sp.add_argument("--default-venues", action="store_true",
                    help="Use built-in demo venues instead of the store.")
    sp.add_argument("--min-net-bps", type=float, default=5.0)
    sp.add_argument("--max-size-usd", type=float, default=10_000.0)
    sp.add_argument("--max", type=int, default=50)
    sp.add_argument("--timeout-s", type=float, default=5.0)
    sp.add_argument("--correlation-id", default=None)
    sp.add_argument("--no-persist", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("demo",
                        help="Pretty, human-friendly live CCXT demo table.")
    sp.add_argument("--symbols", nargs="+",
                    default=["BTC/USDT", "ETH/USDT"])
    sp.add_argument("--min-net-bps", type=float, default=5.0)
    sp.add_argument("--max-size-usd", type=float, default=10_000.0)
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--timeout-s", type=float, default=8.0)

    sp = sub.add_parser("opportunities", help="List recorded opportunities.")
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--min-net-bps", type=float, default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.cmd == "migration-sql":
        return _handle_migration_sql(args)
    if args.cmd == "apply-migration":
        return _handle_apply_migration(args)
    if args.cmd == "venue-add":
        return asyncio.run(_handle_venue_add_async(args))
    if args.cmd == "venues":
        return asyncio.run(_handle_venues_async(args))
    if args.cmd == "scan":
        return asyncio.run(_handle_scan_async(args))
    if args.cmd == "live":
        return asyncio.run(_handle_live_async(args))
    if args.cmd == "demo":
        return asyncio.run(_handle_demo_async(args))
    if args.cmd == "opportunities":
        return asyncio.run(_handle_opportunities_async(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
