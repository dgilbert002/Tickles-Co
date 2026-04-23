"""
shared.cli.copy_cli — operator CLI for the Phase 33 copy-trader.

Subcommands:
  apply-migration / migration-sql       DB migration helpers.
  source-add                            Register / update a leader source.
  sources                               List registered sources.
  tick                                  Run one copy tick against a source.
  trades                                List mirrored trades.

The static source kind is handy for dry-runs and the demo subcommand;
the ``ccxt_account`` kind requires API keys and is wired in Phase 34.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from shared.copy_trader_trader import (
    CopyService,
    CopyStore,
    CopySource,
    InMemoryCopyPool,
    MIGRATION_PATH,
    SourceFill,
    StaticCopySource,
    read_migration_sql,
)

LOG = logging.getLogger("tickles.cli.copy")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryCopyPool()
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
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


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


async def _handle_source_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = CopyStore(pool)
    sid = await store.upsert_source(CopySource(
        id=None, name=args.name, kind=args.kind,
        identifier=args.identifier, venue=args.venue,
        size_mode=args.size_mode, size_value=args.size_value,
        max_notional_usd=args.max_notional_usd,
        symbol_whitelist=args.whitelist or [],
        symbol_blacklist=args.blacklist or [],
        enabled=not args.disabled,
        company_id=args.company_id,
    ))
    _dump({"ok": True, "id": sid, "name": args.name})
    return 0


async def _handle_sources_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = CopyStore(pool)
    sources = await store.list_sources(enabled_only=args.enabled_only)
    _dump({
        "ok": True, "count": len(sources),
        "sources": [s.to_dict() for s in sources],
    })
    return 0


async def _handle_tick_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = CopyStore(pool)
    fills: List[SourceFill] = []
    if args.fills:
        raw = json.loads(
            open(args.fills[1:], "r", encoding="utf-8").read()
            if args.fills.startswith("@") else args.fills
        )
        for f in raw:
            ts = f.get("ts")
            ts_dt = (
                datetime.fromisoformat(ts).astimezone(timezone.utc)
                if isinstance(ts, str) else
                (ts or datetime.now(timezone.utc))
            )
            fills.append(SourceFill(
                fill_id=str(f["fill_id"]),
                symbol=f["symbol"],
                side=f["side"],
                price=float(f["price"]),
                qty_base=float(f["qty_base"]),
                notional_usd=(
                    None if f.get("notional_usd") is None
                    else float(f["notional_usd"])
                ),
                ts=ts_dt,
            ))
    svc = CopyService(store, StaticCopySource(fills))
    source = await store.get_source(args.source_id)
    if source is None:
        _dump({"ok": False, "error": f"source {args.source_id} not found"})
        return 1
    result = await svc.tick_one(
        source, correlation_id=args.correlation_id,
        persist=not args.no_persist,
    )
    _dump({"ok": True, "result": result.to_dict()})
    return 0


async def _handle_demo_async(args: argparse.Namespace) -> int:
    """Live demo: treat Binance's public trade tape as a leader feed.

    We don't have a real account to copy from without API keys — so the
    demo uses the *public* trade tape on a liquid pair and shows what
    our mirrored trades would look like if a trader posting these fills
    were our leader. Purely illustrative; no orders are ever sent.
    """
    try:
        import ccxt.async_support as ccxt  # type: ignore
    except ImportError:
        print("ccxt is not installed — pip install ccxt")
        return 1

    ex = ccxt.binance({"enableRateLimit": True,
                        "timeout": int(args.timeout_s * 1000)})
    pool = InMemoryCopyPool()
    store = CopyStore(pool)
    source = CopySource(
        id=None, name=args.source_name, kind="static",
        identifier=f"public-tape:{args.symbol}",
        size_mode=args.size_mode, size_value=args.size_value,
        max_notional_usd=args.max_notional_usd,
        enabled=True,
    )
    sid = await store.upsert_source(source)
    saved = await store.get_source(sid)
    assert saved is not None

    try:
        print(f"[demo] fetching last {args.limit} public trades of "
              f"{args.symbol} from binance ...")
        trades = await ex.fetch_trades(args.symbol, limit=args.limit)
    finally:
        await ex.close()

    fills: List[SourceFill] = []
    for t in trades or []:
        try:
            side = (t.get("side") or "").lower()
            if side not in ("buy", "sell"):
                continue
            price = float(t.get("price") or 0.0)
            qty = float(t.get("amount") or 0.0)
            if price <= 0 or qty <= 0:
                continue
            ts_ms = t.get("timestamp")
            ts = (
                datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                if ts_ms else datetime.now(timezone.utc)
            )
            fills.append(SourceFill(
                fill_id=str(t.get("id") or ""),
                symbol=args.symbol, side=side,
                price=price, qty_base=qty,
                notional_usd=price * qty, ts=ts,
            ))
        except Exception:  # pragma: no cover
            continue

    from shared.copy_trader_trader.sources import StaticCopySource as _Static
    svc = CopyService(store, _Static(fills))
    result = await svc.tick_one(saved, correlation_id="copy-demo")

    print()
    print(f"== source: {source.name} ({source.size_mode}, "
          f"{source.size_value}, cap="
          f"{source.max_notional_usd}) ==")
    print(f"  fills fetched : {result.fills_fetched}")
    print(f"  trades kept   : {result.trades_kept}")
    print(f"  trades skipped: {result.trades_skipped}")
    print()
    print(
        f"  {'ts':<25} {'side':<4} {'src_qty':>12} {'src_px':>12} "
        f"{'mapped_qty':>12} {'mapped_notional_usd':>22}"
    )
    print("  " + "-" * 95)
    shown = 0
    for t in result.trades:
        if shown >= args.show:
            break
        ts_iso = (
            t.source_trade_ts.isoformat()
            if isinstance(t.source_trade_ts, datetime)
            else str(t.source_trade_ts)
        )
        print(
            f"  {ts_iso:<25} {t.side:<4} "
            f"{(t.source_qty_base or 0):>12.6f} "
            f"{(t.source_price or 0):>12.4f} "
            f"{t.mapped_qty_base:>12.6f} "
            f"{t.mapped_notional_usd:>22.4f}"
        )
        shown += 1
    if len(result.trades) > args.show:
        print(f"  ... {len(result.trades) - args.show} more "
              "(full audit row stored in copy_trades)")
    return 0


async def _handle_trades_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = CopyStore(pool)
    trades = await store.list_trades(
        source_id=args.source_id, status=args.status,
        symbol=args.symbol, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(trades),
        "trades": [t.to_dict() for t in trades],
    })
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_COPY_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copy_cli",
                                description="Phase 33 copy-trader CLI.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration",
                        help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql",
                    help="Print the Phase 33 copy-trader migration SQL.")

    sp = sub.add_parser("source-add", help="Upsert a leader source.")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", default="static",
                    choices=["ccxt_account", "wallet", "feed", "static"])
    sp.add_argument("--identifier", required=True)
    sp.add_argument("--venue", default=None)
    sp.add_argument("--size-mode", default="ratio",
                    choices=["ratio", "fixed_notional_usd", "replicate"])
    sp.add_argument("--size-value", type=float, default=0.1)
    sp.add_argument("--max-notional-usd", type=float, default=None)
    sp.add_argument("--whitelist", nargs="*")
    sp.add_argument("--blacklist", nargs="*")
    sp.add_argument("--disabled", action="store_true")
    sp.add_argument("--company-id", default=None)
    _add_common(sp)

    sp = sub.add_parser("sources", help="List registered sources.")
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("tick",
                        help="Run one copy tick against a static fill list.")
    sp.add_argument("--source-id", type=int, required=True)
    sp.add_argument("--fills",
                    help=("JSON list OR @path to file of "
                          "{fill_id,symbol,side,price,qty_base,notional_usd,ts}."))
    sp.add_argument("--correlation-id", default=None)
    sp.add_argument("--no-persist", action="store_true")
    _add_common(sp)

    sp = sub.add_parser(
        "demo",
        help=("Live copy-trader demo against a public Binance trade tape. "
              "Illustrative only; no orders are sent."),
    )
    sp.add_argument("--symbol", default="BTC/USDT")
    sp.add_argument("--size-mode", default="ratio",
                    choices=["ratio", "fixed_notional_usd", "replicate"])
    sp.add_argument("--size-value", type=float, default=0.1)
    sp.add_argument("--max-notional-usd", type=float, default=None)
    sp.add_argument("--source-name", default="public-tape-leader")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--show", type=int, default=10)
    sp.add_argument("--timeout-s", type=float, default=8.0)

    sp = sub.add_parser("trades", help="List mirrored trades.")
    sp.add_argument("--source-id", type=int, default=None)
    sp.add_argument("--status", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--limit", type=int, default=100)
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
    if args.cmd == "source-add":
        return asyncio.run(_handle_source_add_async(args))
    if args.cmd == "sources":
        return asyncio.run(_handle_sources_async(args))
    if args.cmd == "tick":
        return asyncio.run(_handle_tick_async(args))
    if args.cmd == "demo":
        return asyncio.run(_handle_demo_async(args))
    if args.cmd == "trades":
        return asyncio.run(_handle_trades_async(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
