"""
shared.cli.altdata_cli — operator CLI for Phase 29 Alt-Data Ingestion.

Subcommands:

* ``apply-migration`` / ``migration-sql`` — DB migration helpers.
* ``sources``         — list built-in source names / categories.
* ``push``            — push a single manual item into an in-memory
                        or persisted pool (useful for seeding tests).
* ``ingest``          — one-shot tick using a JSON config of sources.
* ``latest``          — read alt_data_latest with filters.
* ``items``           — read recent alt_data_items with filters.
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

from shared.altdata import (
    AltDataIngestor,
    AltDataItem,
    AltDataStore,
    InMemoryAltDataPool,
    ManualAltDataSource,
    MIGRATION_PATH,
    SOURCE_TYPES,
    StaticAltDataSource,
    read_migration_sql,
)

LOG = logging.getLogger("tickles.cli.altdata")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryAltDataPool()
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


def _handle_sources(_args: argparse.Namespace) -> int:
    _dump({
        "ok": True,
        "source_types": sorted(SOURCE_TYPES),
        "built_in": [
            {"name": "static", "category": "custom"},
            {"name": "manual", "category": "custom"},
            {"name": "ccxt_funding", "category": "funding_rate"},
            {"name": "ccxt_open_interest", "category": "open_interest"},
        ],
    })
    return 0


def _parse_as_of(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _handle_push_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = AltDataStore(pool)
    item = AltDataItem(
        source=args.source,
        provider=args.provider,
        scope_key=args.scope_key,
        metric=args.metric,
        as_of=_parse_as_of(args.as_of),
        value_numeric=args.value,
        value_text=args.value_text,
        unit=args.unit,
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        payload=json.loads(args.payload) if args.payload else {},
        metadata=json.loads(args.metadata) if args.metadata else {},
    )
    rid = await store.insert_item(item)
    _dump({"ok": True, "id": rid, "inserted": bool(rid)})
    return 0


async def _handle_ingest_async(args: argparse.Namespace) -> int:
    items: List[AltDataItem] = []
    if args.fixture_file:
        with open(args.fixture_file, "r", encoding="utf-8") as f:
            fixture = json.load(f)
        for row in fixture:
            items.append(AltDataItem(
                source=row["source"],
                provider=row["provider"],
                scope_key=row["scope_key"],
                metric=row["metric"],
                as_of=_parse_as_of(row.get("as_of")),
                value_numeric=row.get("value_numeric"),
                value_text=row.get("value_text"),
                unit=row.get("unit"),
                universe=row.get("universe"),
                exchange=row.get("exchange"),
                symbol=row.get("symbol"),
                payload=row.get("payload") or {},
                metadata=row.get("metadata") or {},
            ))
    source = StaticAltDataSource(items=items)
    pool = await _get_pool(args.dsn, args.in_memory)
    store = AltDataStore(pool)
    ingestor = AltDataIngestor([source], store=store)
    report = await ingestor.tick(persist=not args.no_persist)
    _dump({"ok": True, "report": report.to_dict()})
    return 0


async def _handle_latest_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = AltDataStore(pool)
    rows = await store.list_latest(
        source=args.source, provider=args.provider,
        exchange=args.exchange, symbol=args.symbol,
        metric=args.metric, limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "rows": [r.to_dict() for r in rows]})
    return 0


async def _handle_items_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = AltDataStore(pool)
    since = _parse_as_of(args.since) if args.since else None
    rows = await store.list_items(
        source=args.source, provider=args.provider,
        exchange=args.exchange, symbol=args.symbol,
        metric=args.metric, since=since, limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "rows": [r.to_dict() for r in rows]})
    return 0


# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_ALTDATA_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="altdata_cli",
        description="Operator CLI for Tickles Alt-Data Ingestion (Phase 29).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 29 migration SQL.")
    sub.add_parser("sources", help="List built-in sources and categories.")

    sp = sub.add_parser("push", help="Insert a single alt-data item.")
    sp.add_argument("--source", required=True, choices=sorted(SOURCE_TYPES))
    sp.add_argument("--provider", required=True)
    sp.add_argument("--scope-key", required=True)
    sp.add_argument("--metric", required=True)
    sp.add_argument("--value", type=float, default=None)
    sp.add_argument("--value-text", default=None)
    sp.add_argument("--unit", default=None)
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--as-of", default=None)
    sp.add_argument("--payload", default=None, help="JSON object")
    sp.add_argument("--metadata", default=None, help="JSON object")
    _add_common(sp)

    sp = sub.add_parser("ingest", help="One-shot tick from a JSON fixture file.")
    sp.add_argument("--fixture-file", required=True)
    sp.add_argument("--no-persist", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("latest", help="Read alt_data_latest.")
    sp.add_argument("--source", default=None)
    sp.add_argument("--provider", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--metric", default=None)
    sp.add_argument("--limit", type=int, default=100)
    _add_common(sp)

    sp = sub.add_parser("items", help="Read alt_data_items.")
    sp.add_argument("--source", default=None)
    sp.add_argument("--provider", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--metric", default=None)
    sp.add_argument("--since", default=None)
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
    if args.cmd == "sources":
        return _handle_sources(args)
    if args.cmd == "push":
        return asyncio.run(_handle_push_async(args))
    if args.cmd == "ingest":
        return asyncio.run(_handle_ingest_async(args))
    if args.cmd == "latest":
        return asyncio.run(_handle_latest_async(args))
    if args.cmd == "items":
        return asyncio.run(_handle_items_async(args))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "ManualAltDataSource"]
