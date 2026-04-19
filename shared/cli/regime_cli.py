"""
shared.cli.regime_cli — operator CLI for Phase 27 Regime Service.

Subcommands:

* ``apply-migration`` / ``migration-sql`` — DB migration helpers.
* ``classifiers`` — list known classifiers.
* ``config-set`` — upsert a ``regime_config`` row.
* ``config-list`` — list ``regime_config`` rows.
* ``classify`` — classify a (universe, exchange, symbol, timeframe)
  with synthetic closes passed on the CLI (``--closes 1,2,3,...``)
  or via ``--closes-file <path>``. Useful for parity diffs + quick
  smoke tests without DB candles.
* ``tick`` — pull configs, classify each, persist signals.
* ``current`` — read ``regime_current`` view.
* ``history`` — read recent ``regime_states``.

All DB interactions go through either an in-memory pool (default
or ``--in-memory``) or ``TICKLES_REGIME_DSN`` / ``--dsn`` for
Postgres.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from shared.regime import (
    CLASSIFIER_NAMES,
    Candle,
    InMemoryRegimePool,
    MIGRATION_PATH,
    RegimeService,
    RegimeStore,
    read_migration_sql,
)

LOG = logging.getLogger("tickles.cli.regime")


# ---------------------------------------------------------------------------
# Pool acquisition
# ---------------------------------------------------------------------------


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryRegimePool()
    try:
        import asyncpg  # type: ignore
    except ImportError as exc:
        raise RuntimeError("asyncpg not installed; cannot connect") from exc

    class _AsyncpgPool:
        def __init__(self, pool: Any) -> None:
            self._pool = pool

        async def execute(self, sql: str, params: Sequence[Any]) -> int:
            async with self._pool.acquire() as conn:
                res = await conn.execute(sql, *params)
            return int(res.split()[-1]) if res and res[-1].isdigit() else 0

        async def fetch_one(self, sql: str, params: Sequence[Any]) -> Optional[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
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
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


# ---------------------------------------------------------------------------
# Candle helpers (synthetic, for --closes)
# ---------------------------------------------------------------------------


def _synthetic_candles(closes: Sequence[float], *, start: Optional[datetime] = None, step_seconds: int = 60) -> List[Candle]:
    base = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: List[Candle] = []
    for i, c in enumerate(closes):
        ts = base + timedelta(seconds=i * step_seconds)
        out.append(Candle(ts=ts, open=float(c), high=float(c), low=float(c), close=float(c), volume=1.0))
    return out


def _parse_closes(args: argparse.Namespace) -> List[float]:
    if args.closes_file:
        with open(args.closes_file, "r", encoding="utf-8") as f:
            return [float(x.strip()) for x in f if x.strip()]
    if not args.closes:
        raise ValueError("--closes or --closes-file is required")
    return [float(x) for x in args.closes.split(",") if x.strip()]


# ---------------------------------------------------------------------------
# Subcommands
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


def _handle_classifiers(_args: argparse.Namespace) -> int:
    _dump({"ok": True, "classifiers": sorted(CLASSIFIER_NAMES)})
    return 0


async def _handle_config_set_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = RegimeStore(pool)
    params = json.loads(args.params) if args.params else {}
    rid = await store.upsert_config(
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        classifier=args.classifier,
        params=params,
        enabled=not args.disabled,
    )
    _dump({"ok": True, "id": rid})
    return 0


async def _handle_config_list_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = RegimeStore(pool)
    rows = await store.list_configs(
        universe=args.universe, enabled_only=args.enabled_only,
    )
    _dump({"ok": True, "count": len(rows), "configs": [r.to_dict() for r in rows]})
    return 0


async def _handle_classify_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = RegimeStore(pool)
    service = RegimeService(store)
    closes = _parse_closes(args)
    candles = _synthetic_candles(closes)
    params = json.loads(args.params) if args.params else None
    sig = await service.classify_from_candles(
        candles,
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        classifier=args.classifier,
        params=params,
        persist=args.persist,
    )
    _dump({"ok": True, "signal": sig.to_dict()})
    return 0


async def _handle_current_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = RegimeStore(pool)
    rows = await store.list_current(
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        classifier=args.classifier,
    )
    _dump({"ok": True, "count": len(rows), "rows": [r.to_dict() for r in rows]})
    return 0


async def _handle_history_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = RegimeStore(pool)
    rows = await store.list_history(
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        classifier=args.classifier,
        limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "rows": [r.to_dict() for r in rows]})
    return 0


# ---------------------------------------------------------------------------
# Arg parser
# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_REGIME_DSN"),
                   help="Postgres DSN (default: env TICKLES_REGIME_DSN)")
    p.add_argument("--in-memory", action="store_true",
                   help="Use the in-memory pool (default if DSN is empty)")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="regime_cli",
        description="Operator CLI for the Tickles Regime Service (Phase 27).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")

    sub.add_parser("migration-sql", help="Print the Phase 27 migration SQL.")
    sub.add_parser("classifiers", help="List known classifiers.")

    sp = sub.add_parser("config-set", help="Upsert a regime_config row.")
    sp.add_argument("--universe", required=True)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--timeframe", required=True)
    sp.add_argument("--classifier", required=True, choices=sorted(CLASSIFIER_NAMES))
    sp.add_argument("--params", default=None, help="JSON params, e.g. '{\"fast\":20,\"slow\":50}'")
    sp.add_argument("--disabled", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("config-list", help="List regime_config rows.")
    sp.add_argument("--universe", default=None)
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("classify", help="Classify a window of closes via one classifier.")
    sp.add_argument("--universe", required=True)
    sp.add_argument("--exchange", required=True)
    sp.add_argument("--symbol", required=True)
    sp.add_argument("--timeframe", required=True)
    sp.add_argument("--classifier", default="composite", choices=sorted(CLASSIFIER_NAMES))
    sp.add_argument("--params", default=None)
    closes_group = sp.add_mutually_exclusive_group()
    closes_group.add_argument("--closes", default=None,
                              help="Comma-separated closes, e.g. 100,101,102,...")
    closes_group.add_argument("--closes-file", default=None,
                              help="File with one close per line.")
    sp.add_argument("--persist", action="store_true",
                    help="Also persist to regime_states.")
    _add_common(sp)

    sp = sub.add_parser("current", help="Read regime_current.")
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--timeframe", default=None)
    sp.add_argument("--classifier", default=None)
    _add_common(sp)

    sp = sub.add_parser("history", help="Read recent regime_states.")
    sp.add_argument("--universe", required=True)
    sp.add_argument("--exchange", required=True)
    sp.add_argument("--symbol", required=True)
    sp.add_argument("--timeframe", required=True)
    sp.add_argument("--classifier", default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.cmd == "apply-migration":
        return _handle_apply_migration(args)
    if args.cmd == "migration-sql":
        return _handle_migration_sql(args)
    if args.cmd == "classifiers":
        return _handle_classifiers(args)
    if args.cmd == "config-set":
        return asyncio.run(_handle_config_set_async(args))
    if args.cmd == "config-list":
        return asyncio.run(_handle_config_list_async(args))
    if args.cmd == "classify":
        return asyncio.run(_handle_classify_async(args))
    if args.cmd == "current":
        return asyncio.run(_handle_current_async(args))
    if args.cmd == "history":
        return asyncio.run(_handle_history_async(args))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
