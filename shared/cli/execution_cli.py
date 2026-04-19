"""
shared.cli.execution_cli — operator CLI for Phase 26 Execution Layer.

Subcommands:

* ``apply-migration`` / ``migration-sql`` — DB migration.
* ``adapters`` — list registered adapters and availability flags.
* ``submit``    — submit an ExecutionIntent via a chosen adapter.
* ``cancel``    — cancel a previously-submitted client_order_id.
* ``orders``    — list open orders for a company.
* ``fills``     — list recent fills for a company.
* ``positions`` — list the current positions view for a company.
* ``paper-simulate`` — offline submit -> tick -> fill walk-through.

Uses :class:`shared.execution.memory_pool.InMemoryExecutionPool` when
``--in-memory`` is supplied or ``TICKLES_EXECUTION_DSN`` is unset.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.execution import (
    ADAPTER_PAPER,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    ExecutionIntent,
    ExecutionRouter,
    MIGRATION_PATH,
    MarketTick,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    ORDER_TYPE_STOP_LIMIT,
    OrderSnapshot,
    PaperExecutionAdapter,
    default_adapters,
    read_migration_sql,
)
from shared.execution.memory_pool import InMemoryExecutionPool

LOG = logging.getLogger("tickles.cli.execution")


# ---------------------------------------------------------------------------
# Pool acquisition
# ---------------------------------------------------------------------------


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryExecutionPool()
    try:
        import asyncpg  # type: ignore
    except ImportError as exc:
        raise RuntimeError("asyncpg not installed; cannot connect") from exc

    class _AsyncpgPool:
        def __init__(self, pool: Any) -> None:
            self._pool = pool

        async def execute(self, sql: str, params: list) -> int:
            async with self._pool.acquire() as conn:
                res = await conn.execute(sql, *params)
            return int(res.split()[-1]) if res and res[-1].isdigit() else 0

        async def fetch_one(self, sql: str, params: list) -> Optional[dict]:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(self, sql: str, params: list) -> list:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    return _AsyncpgPool(pool)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


def _snap_to_dict(snap: OrderSnapshot) -> Dict[str, Any]:
    return asdict(snap)


# ---------------------------------------------------------------------------
# Subcommand handlers
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


def _handle_adapters(_args: argparse.Namespace) -> int:
    adapters = default_adapters()
    out = {}
    for name, adapter in adapters.items():
        available = True
        reason: Optional[str] = None
        if hasattr(adapter, "available"):
            available = bool(adapter.available)
            if not available:
                reason = "adapter flag set to False"
        out[name] = {"name": name, "available": available, "reason": reason}
    _dump({"adapters": out, "ok": True})
    return 0


def _intent_from_args(args: argparse.Namespace) -> ExecutionIntent:
    direction = args.direction.lower()
    if direction not in (DIRECTION_LONG, DIRECTION_SHORT):
        raise ValueError(f"direction must be long/short, got {direction!r}")
    order_type = args.order_type.lower()
    if order_type not in (ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT, ORDER_TYPE_STOP, ORDER_TYPE_STOP_LIMIT):
        raise ValueError(f"invalid order_type {order_type!r}")
    metadata: Dict[str, Any] = {}
    if args.metadata:
        metadata.update(json.loads(args.metadata))
    return ExecutionIntent(
        company_id=args.company,
        strategy_id=args.strategy_id,
        agent_id=args.agent_id,
        exchange=args.exchange,
        account_id_external=args.account_id,
        symbol=args.symbol,
        direction=direction,
        order_type=order_type,
        quantity=float(args.quantity),
        requested_price=float(args.price) if args.price is not None else None,
        stop_price=float(args.stop_price) if args.stop_price is not None else None,
        time_in_force=args.tif,
        client_order_id=args.client_order_id,
        requested_notional_usd=(
            float(args.notional) if args.notional is not None else None
        ),
        treasury_decision_id=args.treasury_decision_id,
        intent_hash=args.intent_hash,
        metadata=metadata,
    )


async def _submit_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    adapters = default_adapters()
    router = ExecutionRouter(pool, adapters=adapters, default_adapter=args.adapter)

    try:
        intent = _intent_from_args(args)
    except ValueError as exc:
        _dump({"ok": False, "error": str(exc)})
        return 2

    market = None
    if args.market_last is not None:
        market = MarketTick(
            symbol=intent.symbol,
            bid=float(args.market_bid or args.market_last),
            ask=float(args.market_ask or args.market_last),
            last=float(args.market_last),
        )

    snap = await router.submit(intent, adapter=args.adapter, market=market)
    _dump({"ok": True, "order": _snap_to_dict(snap)})
    return 0


async def _cancel_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=args.adapter)
    try:
        snap = await router.cancel(args.client_order_id, adapter=args.adapter)
    except ValueError as exc:
        _dump({"ok": False, "error": str(exc)})
        return 2
    _dump({"ok": True, "order": _snap_to_dict(snap)})
    return 0


async def _orders_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=args.adapter)
    open_orders = await router._store.list_open_orders(args.company, limit=args.limit)  # noqa: SLF001
    _dump({"ok": True, "count": len(open_orders), "orders": [_snap_to_dict(o) for o in open_orders]})
    return 0


async def _fills_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    from shared.execution.store import ExecutionStore
    store = ExecutionStore(pool)
    fills = await store.list_fills(args.company, limit=args.limit)
    _dump({"ok": True, "count": len(fills), "fills": [asdict(f) for f in fills]})
    return 0


async def _positions_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    from shared.execution.store import ExecutionStore
    store = ExecutionStore(pool)
    positions = await store.list_current_positions(args.company)
    _dump({"ok": True, "count": len(positions), "positions": [asdict(p) for p in positions]})
    return 0


async def _paper_simulate_async(args: argparse.Namespace) -> int:
    """Fully offline walk-through: submit through paper, re-tick, fill."""
    pool = InMemoryExecutionPool()
    paper = PaperExecutionAdapter(
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        slippage_bps=args.slippage_bps,
    )
    router = ExecutionRouter(pool, adapters={ADAPTER_PAPER: paper}, default_adapter=ADAPTER_PAPER)

    try:
        intent = _intent_from_args(args)
    except ValueError as exc:
        _dump({"ok": False, "error": str(exc)})
        return 2

    market = MarketTick(
        symbol=intent.symbol,
        bid=float(args.market_bid or args.market_last),
        ask=float(args.market_ask or args.market_last),
        last=float(args.market_last),
    )

    snap = await router.submit(intent, market=market)
    _dump({
        "ok": True,
        "adapter": ADAPTER_PAPER,
        "order": _snap_to_dict(snap),
        "fills": [asdict(f) for f in await router._store.list_fills(intent.company_id, limit=10)],  # noqa: SLF001
        "positions": [
            asdict(p)
            for p in await router._store.list_current_positions(intent.company_id)  # noqa: SLF001
        ],
    })
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_intent_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--company", required=True)
    p.add_argument("--strategy-id", default=None)
    p.add_argument("--agent-id", default=None)
    p.add_argument("--exchange", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--direction", required=True, choices=["long", "short"])
    p.add_argument("--order-type", default=ORDER_TYPE_MARKET)
    p.add_argument("--quantity", required=True, type=float)
    p.add_argument("--price", default=None, type=float)
    p.add_argument("--stop-price", default=None, type=float)
    p.add_argument("--tif", default="gtc", choices=["gtc", "ioc", "fok", "gtd"])
    p.add_argument("--client-order-id", default=None)
    p.add_argument("--notional", default=None, type=float)
    p.add_argument("--treasury-decision-id", default=None, type=int)
    p.add_argument("--intent-hash", default=None)
    p.add_argument("--metadata", default=None, help="JSON-encoded metadata")


def _add_market_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--market-last", default=None, type=float)
    p.add_argument("--market-bid", default=None, type=float)
    p.add_argument("--market-ask", default=None, type=float)


def _add_global_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_EXECUTION_DSN"))
    p.add_argument("--in-memory", action="store_true",
                   help="Use in-memory pool (ignore --dsn).")
    p.add_argument("--adapter", default=ADAPTER_PAPER,
                   choices=["paper", "ccxt", "nautilus"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execution_cli",
        description="Operator CLI for the Tickles Execution Layer (Phase 26).",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", metavar="SUBCOMMAND", required=True)

    p = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    p.add_argument("--path-only", action="store_true")
    p.set_defaults(func=_handle_apply_migration)

    p = sub.add_parser("migration-sql", help="Print the Phase 26 migration SQL.")
    p.set_defaults(func=_handle_migration_sql)

    p = sub.add_parser("adapters", help="List registered execution adapters + availability.")
    p.set_defaults(func=_handle_adapters)

    p = sub.add_parser("submit", help="Submit an ExecutionIntent via an adapter.")
    _add_global_args(p)
    _add_intent_args(p)
    _add_market_args(p)
    p.set_defaults(func=lambda a: asyncio.run(_submit_async(a)))

    p = sub.add_parser("cancel", help="Cancel a client_order_id via an adapter.")
    _add_global_args(p)
    p.add_argument("--client-order-id", required=True)
    p.set_defaults(func=lambda a: asyncio.run(_cancel_async(a)))

    p = sub.add_parser("orders", help="List open orders for a company.")
    _add_global_args(p)
    p.add_argument("--company", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: asyncio.run(_orders_async(a)))

    p = sub.add_parser("fills", help="List recent fills for a company.")
    _add_global_args(p)
    p.add_argument("--company", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=lambda a: asyncio.run(_fills_async(a)))

    p = sub.add_parser("positions", help="List current positions for a company.")
    _add_global_args(p)
    p.add_argument("--company", required=True)
    p.set_defaults(func=lambda a: asyncio.run(_positions_async(a)))

    p = sub.add_parser("paper-simulate", help="Offline paper submit walk-through.")
    _add_intent_args(p)
    _add_market_args(p)
    p.add_argument("--taker-fee-bps", type=float, default=4.0)
    p.add_argument("--maker-fee-bps", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.set_defaults(func=lambda a: asyncio.run(_paper_simulate_async(a)))

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else (logging.INFO if args.verbose else logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
