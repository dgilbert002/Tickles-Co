"""
shared.cli.events_cli — operator CLI for Phase 30 Events Calendar.

Subcommands:
  apply-migration / migration-sql
  kinds             — list canonical event kinds.
  add               — upsert a single event.
  list              — list events (filters: kind, provider, symbol, etc).
  active            — events whose window includes NOW() (or --when).
  upcoming          — next N days.
  delete            — delete by id.
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

from shared.events import (
    EventRecord,
    EventsCalendarService,
    EventsStore,
    IMPORTANCE_LOW,
    InMemoryEventsPool,
    KIND_CUSTOM,
    KINDS,
    MIGRATION_PATH,
    read_migration_sql,
)

LOG = logging.getLogger("tickles.cli.events")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryEventsPool()
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


def _parse_dt(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def _handle_kinds(_args: argparse.Namespace) -> int:
    _dump({"ok": True, "kinds": sorted(KINDS)})
    return 0


async def _handle_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = EventsStore(pool)
    event = EventRecord(
        id=None,
        kind=args.kind,
        provider=args.provider,
        name=args.name,
        event_time=_parse_dt(args.event_time),
        window_before_minutes=args.window_before,
        window_after_minutes=args.window_after,
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        country=args.country,
        importance=args.importance,
        payload=json.loads(args.payload) if args.payload else {},
        metadata=json.loads(args.metadata) if args.metadata else {},
    )
    rid = await store.upsert_event(event)
    _dump({"ok": True, "id": rid, "event": event.to_dict()})
    return 0


async def _handle_list_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = EventsStore(pool)
    rows = await store.list_events(
        kind=args.kind, provider=args.provider,
        universe=args.universe, exchange=args.exchange, symbol=args.symbol,
        min_importance=args.min_importance,
        since=_parse_dt(args.since) if args.since else None,
        until=_parse_dt(args.until) if args.until else None,
        limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "events": [r.to_dict() for r in rows]})
    return 0


async def _handle_active_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = EventsCalendarService(EventsStore(pool))
    when = _parse_dt(args.when) if args.when else None
    rows = await svc.active_at(
        when=when, kind=args.kind, min_importance=args.min_importance,
    )
    _dump({"ok": True, "count": len(rows), "events": [r.to_dict() for r in rows]})
    return 0


async def _handle_upcoming_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = EventsCalendarService(EventsStore(pool))
    when = _parse_dt(args.when) if args.when else None
    rows = await svc.upcoming(
        when=when, horizon_days=args.horizon_days,
        kind=args.kind, min_importance=args.min_importance,
        limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "events": [r.to_dict() for r in rows]})
    return 0


async def _handle_delete_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = EventsStore(pool)
    n = await store.delete_event(args.id)
    _dump({"ok": True, "deleted": n})
    return 0


# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_EVENTS_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="events_cli",
        description="Operator CLI for Tickles Events Calendar (Phase 30).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 30 migration SQL.")
    sub.add_parser("kinds", help="List canonical event kinds.")

    sp = sub.add_parser("add", help="Upsert an event.")
    sp.add_argument("--kind", required=True, default=KIND_CUSTOM)
    sp.add_argument("--provider", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--event-time", required=True, help="ISO-8601 UTC")
    sp.add_argument("--window-before", type=int, default=0)
    sp.add_argument("--window-after", type=int, default=0)
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--country", default=None)
    sp.add_argument("--importance", type=int, default=IMPORTANCE_LOW)
    sp.add_argument("--payload", default=None)
    sp.add_argument("--metadata", default=None)
    _add_common(sp)

    sp = sub.add_parser("list", help="List events.")
    sp.add_argument("--kind", default=None)
    sp.add_argument("--provider", default=None)
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--min-importance", type=int, default=None)
    sp.add_argument("--since", default=None)
    sp.add_argument("--until", default=None)
    sp.add_argument("--limit", type=int, default=200)
    _add_common(sp)

    sp = sub.add_parser("active", help="Events whose window includes NOW() (or --when).")
    sp.add_argument("--when", default=None)
    sp.add_argument("--kind", default=None)
    sp.add_argument("--min-importance", type=int, default=None)
    _add_common(sp)

    sp = sub.add_parser("upcoming", help="Upcoming events.")
    sp.add_argument("--when", default=None)
    sp.add_argument("--horizon-days", type=int, default=7)
    sp.add_argument("--kind", default=None)
    sp.add_argument("--min-importance", type=int, default=None)
    sp.add_argument("--limit", type=int, default=200)
    _add_common(sp)

    sp = sub.add_parser("delete", help="Delete event by id.")
    sp.add_argument("--id", type=int, required=True)
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
    if args.cmd == "kinds":
        return _handle_kinds(args)
    if args.cmd == "add":
        return asyncio.run(_handle_add_async(args))
    if args.cmd == "list":
        return asyncio.run(_handle_list_async(args))
    if args.cmd == "active":
        return asyncio.run(_handle_active_async(args))
    if args.cmd == "upcoming":
        return asyncio.run(_handle_upcoming_async(args))
    if args.cmd == "delete":
        return asyncio.run(_handle_delete_async(args))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
