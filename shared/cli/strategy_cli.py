"""
shared.cli.strategy_cli — operator CLI for the Phase 34 Strategy Composer.

Subcommands:
  apply-migration / migration-sql   DB migration helpers.
  descriptor-add / descriptors      Register / list strategy descriptors.
  tick                              Run one composer pass (persists intents).
  intents                           List intents (filter by status/strategy).
  latest                            Latest intent per (strategy, symbol, side).
  demo                              Live demo chaining arb_scanner into
                                    the composer using CCXT public tickers.
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

from shared.arb import (
    ArbService,
    ArbStore,
    CcxtQuoteFetcher,
    InMemoryArbPool,
    ScannerConfig,
)
from shared.arb.protocol import ArbVenue
from shared.strategies import (
    MIGRATION_PATH,
    StrategyDescriptor,
    StrategyStore,
    read_migration_sql,
)
from shared.strategies.composer import GateDecision
from shared.strategies.memory_pool import InMemoryStrategyPool
from shared.strategies.producers import ArbProducer
from shared.strategies.service import StrategyComposerService

LOG = logging.getLogger("tickles.cli.strategy")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryStrategyPool()
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


# ----------------------------------------------------------------- handlers


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


async def _handle_descriptor_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = StrategyStore(pool)
    did = await store.upsert_descriptor(StrategyDescriptor(
        id=None, name=args.name, kind=args.kind,
        description=args.description or "",
        enabled=not args.disabled, priority=args.priority,
        company_id=args.company_id,
        config=json.loads(args.config) if args.config else {},
    ))
    _dump({"ok": True, "id": did, "name": args.name, "kind": args.kind})
    return 0


async def _handle_descriptors_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = StrategyStore(pool)
    rows = await store.list_descriptors(enabled_only=args.enabled_only)
    _dump({
        "ok": True, "count": len(rows),
        "descriptors": [r.to_dict() for r in rows],
    })
    return 0


async def _handle_tick_async(args: argparse.Namespace) -> int:
    """Run one composer pass against the configured producers.

    For the CLI we default to the ArbProducer wired into whatever pool
    the operator points us at (via --dsn or --in-memory). This is
    mostly a smoke-test; a real deployment will run
    :class:`StrategyComposerService` from a service daemon.
    """

    strat_pool = await _get_pool(args.dsn, args.in_memory)
    strat_store = StrategyStore(strat_pool)
    arb_pool = InMemoryArbPool()
    arb_store = ArbStore(arb_pool)
    producer = ArbProducer(arb_store, min_net_bps=args.min_net_bps)
    svc = StrategyComposerService(
        strat_store, [producer],
    )
    res = await svc.tick(
        limit_per_producer=args.limit,
        correlation_id=args.correlation_id, persist=not args.no_persist,
    )
    _dump({"ok": True, **res.to_dict()})
    return 0


async def _handle_intents_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = StrategyStore(pool)
    rows = await store.list_intents(
        strategy_name=args.strategy, status=args.status,
        symbol=args.symbol, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "intents": [r.to_dict() for r in rows],
    })
    return 0


async def _handle_latest_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = StrategyStore(pool)
    rows = await store.list_latest(limit=args.limit)
    _dump({
        "ok": True, "count": len(rows),
        "intents": [r.to_dict() for r in rows],
    })
    return 0


async def _handle_demo_async(args: argparse.Namespace) -> int:
    """Human-friendly live composer demo."""

    venues = [
        ArbVenue(id=None, name="binance",  kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0),
        ArbVenue(id=None, name="kraken",   kind="spot",
                 taker_fee_bps=26.0, maker_fee_bps=16.0),
        ArbVenue(id=None, name="coinbase", kind="spot",
                 taker_fee_bps=40.0, maker_fee_bps=25.0),
        ArbVenue(id=None, name="bybit",    kind="spot",
                 taker_fee_bps=10.0, maker_fee_bps=2.0),
    ]
    fetcher = CcxtQuoteFetcher(timeout_s=args.timeout_s)
    arb_pool = InMemoryArbPool()
    arb_store = ArbStore(arb_pool)
    strat_pool = InMemoryStrategyPool()
    strat_store = StrategyStore(strat_pool)
    arb_svc = ArbService(
        arb_store, fetcher, venues=venues,
        scanner_config=ScannerConfig(
            min_net_bps=args.min_net_bps,
            max_size_usd=args.max_size_usd,
            max_opportunities=args.max,
        ),
    )
    try:
        print(
            f"[demo] scanning {args.symbols} on "
            f"{[v.name for v in venues]} ..."
        )
        opps = await arb_svc.scan_symbols(
            args.symbols, correlation_id="composer-demo", persist=True,
        )
        print(f"[demo] arb opportunities found: {len(opps)}")

        async def _gate(intent):  # pragma: no cover - simple stub
            # Gate stub: approve everything for the demo. Real gates
            # come in later phases (Treasury + Guardrails).
            return GateDecision(approved=True, reason="demo-approve-all")

        producer = ArbProducer(arb_store, min_net_bps=args.min_net_bps)
        svc = StrategyComposerService(
            strat_store, [producer],
            gate=_gate if args.gate else None,
        )
        res = await svc.tick(
            limit_per_producer=args.max,
            correlation_id="composer-demo",
        )
        print()
        print("== composition result ==")
        print(
            f"  proposed={res.proposed}  approved={res.approved}  "
            f"rejected={res.rejected}  duplicate={res.duplicate}  "
            f"submitted={res.submitted}  failed={res.failed}  "
            f"skipped={res.skipped}"
        )
        for i in res.intents[: args.show]:
            print(
                f"  [{i.status:<9}] {i.strategy_name:<12} "
                f"{i.symbol:<10} {i.side:<4} "
                f"size={i.size_base:>12.6f}  "
                f"notional=${i.notional_usd:>10.2f}  "
                f"venue={i.venue or '-':<8}  "
                f"priority={i.priority_score:>6.2f}"
            )
        return 0
    finally:
        await fetcher.close()


# ----------------------------------------------------------------- parser


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_STRAT_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="strategy_cli",
        description="Phase 34 Strategy Composer CLI.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration",
                        help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 34 migration SQL.")

    sp = sub.add_parser("descriptor-add",
                        help="Upsert a strategy descriptor.")
    sp.add_argument("--name", required=True)
    sp.add_argument("--kind", required=True,
                    choices=("arb", "copy", "souls", "custom"))
    sp.add_argument("--description", default="")
    sp.add_argument("--disabled", action="store_true")
    sp.add_argument("--priority", type=int, default=100)
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--config", default=None,
                    help="JSON string of config overrides.")
    _add_common(sp)

    sp = sub.add_parser("descriptors", help="List registered descriptors.")
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("tick", help="Run one composer pass.")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--min-net-bps", type=float, default=5.0)
    sp.add_argument("--correlation-id", default=None)
    sp.add_argument("--no-persist", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("intents", help="List intents.")
    sp.add_argument("--strategy", default=None)
    sp.add_argument("--status", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("latest",
                        help="Latest intent per strategy+symbol+side.")
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("demo",
                        help="Live CCXT composer demo (no API keys needed).")
    sp.add_argument("--symbols", nargs="+",
                    default=["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    sp.add_argument("--min-net-bps", type=float, default=1.0)
    sp.add_argument("--max-size-usd", type=float, default=10_000.0)
    sp.add_argument("--max", type=int, default=20)
    sp.add_argument("--timeout-s", type=float, default=8.0)
    sp.add_argument("--show", type=int, default=10)
    sp.add_argument("--gate", action="store_true",
                    help="Wire a permissive approve-all gate stub.")

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
    if args.cmd == "descriptor-add":
        return asyncio.run(_handle_descriptor_add_async(args))
    if args.cmd == "descriptors":
        return asyncio.run(_handle_descriptors_async(args))
    if args.cmd == "tick":
        return asyncio.run(_handle_tick_async(args))
    if args.cmd == "intents":
        return asyncio.run(_handle_intents_async(args))
    if args.cmd == "latest":
        return asyncio.run(_handle_latest_async(args))
    if args.cmd == "demo":
        return asyncio.run(_handle_demo_async(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    _ = datetime.now(timezone.utc)  # keep timezone import used
    sys.exit(main())
