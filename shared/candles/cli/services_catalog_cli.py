"""
shared.cli.services_catalog_cli — Phase 24 DB-backed Services Catalog CLI.

Subcommands (all stdout is single-line JSON):

* ``apply-migration`` — print path + ready-to-paste psql command for
  the Phase 24 migration SQL (no writes).
* ``migration-sql`` — print the raw SQL so callers can pipe it directly
  into psql.
* ``sync`` — upsert every service in the Phase 22 ``SERVICE_REGISTRY``
  into ``public.services_catalog`` (idempotent).
* ``snapshot [--names a,b,c]`` — shell out to ``systemctl show`` for
  each service and update the ``last_systemd_*`` columns plus append
  a row to ``public.services_catalog_snapshots``.
* ``attach-heartbeats [--window-seconds N]`` — read the most recent
  heartbeat per service from the Phase 21 auditor SQLite store and
  stamp ``last_heartbeat_ts`` / ``last_heartbeat_severity``.
* ``list [--kind KIND]`` — read catalog rows from the DB.
* ``describe <name>`` — read one catalog row from the DB.
* ``refresh`` — one-shot convenience = ``sync`` + ``snapshot`` +
  ``attach-heartbeats``.

This CLI deliberately uses an async DB pool (asyncpg-compatible) from
:mod:`shared.utils.db` so the operator's view matches whatever the
services themselves would see. A ``--dsn`` override is available for
local dev.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)
from shared.services import SERVICE_REGISTRY
from shared.services.catalog import (
    MIGRATION_PATH,
    ServicesCatalog,
    extract_heartbeats_from_audit,
    read_migration_sql,
)


# ---------------------------------------------------------------------------
# Pool acquisition — supports both real asyncpg (preferred) and a dev DSN.
# ---------------------------------------------------------------------------


async def _acquire_pool(dsn: Optional[str]) -> Any:
    """Return a DatabasePool-compatible object.

    Priority:
      1. ``--dsn`` flag (or ``TICKLES_SHARED_DSN`` env) → thin asyncpg wrapper.
      2. ``shared.utils.db.get_shared_pool()`` — the canonical path.
    """
    if dsn:
        import asyncpg  # type: ignore[import-not-found]  # lazy import so the CLI imports cleanly off-box

        class _DsnPool:
            def __init__(self, pool: Any) -> None:
                self._pool = pool

            async def execute(self, sql: str, params: Sequence[Any]) -> int:
                async with self._pool.acquire() as conn:
                    status = await conn.execute(sql, *params)
                try:
                    return int(str(status).rsplit(" ", 1)[-1])
                except (ValueError, AttributeError):
                    return 0

            async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
                if not rows:
                    return 0
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(sql, rows)
                return len(rows)

            async def fetch_all(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
                async with self._pool.acquire() as conn:
                    records = await conn.fetch(sql, *params)
                return [dict(r) for r in records]

            async def fetch_one(
                self, sql: str, params: Sequence[Any]
            ) -> Optional[Dict[str, Any]]:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(sql, *params)
                return dict(row) if row is not None else None

        asyncpg_pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
        return _DsnPool(asyncpg_pool)

    # Canonical path — same pool every other tickles service uses.
    from shared.utils import db

    return await db.get_shared_pool()


def _dsn_from_args(args: argparse.Namespace) -> Optional[str]:
    return getattr(args, "dsn", None) or os.environ.get("TICKLES_SHARED_DSN")


def _resolve_names(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_apply_migration(args: argparse.Namespace) -> int:
    del args
    emit(
        {
            "ok": True,
            "migration_path": MIGRATION_PATH,
            "apply_example": (
                f"psql -h 127.0.0.1 -U admin -d tickles_shared -f {MIGRATION_PATH}"
            ),
        }
    )
    return EXIT_OK


def cmd_migration_sql(args: argparse.Namespace) -> int:
    del args
    sys.stdout.write(read_migration_sql())
    sys.stdout.flush()
    return EXIT_OK


async def _do_sync(dsn: Optional[str]) -> int:
    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)
    count = await catalog.sync_registry(pool)
    emit({"ok": True, "synced": count})
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    return asyncio.run(_do_sync(_dsn_from_args(args)))


async def _do_snapshot(
    dsn: Optional[str], names: Optional[List[str]], history: bool
) -> int:
    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)
    results = await catalog.snapshot_systemd(
        pool, names=names, also_append_history=history
    )
    emit({"ok": True, "count": len(results), "snapshots": results})
    return EXIT_OK


def cmd_snapshot(args: argparse.Namespace) -> int:
    return asyncio.run(
        _do_snapshot(
            _dsn_from_args(args),
            _resolve_names(getattr(args, "names", None)),
            not getattr(args, "no_history", False),
        )
    )


async def _do_attach_heartbeats(
    dsn: Optional[str], names: Optional[List[str]], window_seconds: int
) -> int:
    try:
        from shared.auditor import AuditStore
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": f"auditor unavailable: {exc}"})
        return EXIT_FAIL

    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)

    target_names = names or [d.name for d in SERVICE_REGISTRY.list_services()]
    subjects = [f"service:{n}" for n in target_names]
    with AuditStore() as store:
        heartbeats = extract_heartbeats_from_audit(
            store, subjects, window_seconds=window_seconds
        )
    # Remap subject -> service name (strip "service:" prefix).
    for hb in heartbeats:
        if hb.service.startswith("service:"):
            hb.service = hb.service.split(":", 1)[1]

    updated = await catalog.attach_heartbeats(pool, heartbeats)
    emit(
        {
            "ok": True,
            "window_seconds": window_seconds,
            "updated": updated,
            "services": [
                {"name": hb.service, "ts": hb.ts.isoformat(), "severity": hb.severity}
                for hb in heartbeats
            ],
        }
    )
    return EXIT_OK


def cmd_attach_heartbeats(args: argparse.Namespace) -> int:
    return asyncio.run(
        _do_attach_heartbeats(
            _dsn_from_args(args),
            _resolve_names(getattr(args, "names", None)),
            int(getattr(args, "window_seconds", 3600)),
        )
    )


async def _do_list(dsn: Optional[str], kind: Optional[str]) -> int:
    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)
    rows = await catalog.list_services(pool, kind=kind)
    emit({"ok": True, "count": len(rows), "services": rows})
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    return asyncio.run(_do_list(_dsn_from_args(args), getattr(args, "kind", None)))


async def _do_describe(dsn: Optional[str], name: str) -> int:
    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)
    row = await catalog.describe_service(pool, name)
    if row is None:
        emit({"ok": False, "error": f"service not in catalog: {name}"})
        return EXIT_FAIL
    emit({"ok": True, "service": row})
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    return asyncio.run(_do_describe(_dsn_from_args(args), args.name))


async def _do_refresh(dsn: Optional[str], window_seconds: int) -> int:
    catalog = ServicesCatalog(registry=SERVICE_REGISTRY)
    pool = await _acquire_pool(dsn)
    synced = await catalog.sync_registry(pool)
    snapshots = await catalog.snapshot_systemd(pool, names=None)
    hb_count = 0
    try:
        from shared.auditor import AuditStore

        names = [d.name for d in SERVICE_REGISTRY.list_services()]
        subjects = [f"service:{n}" for n in names]
        with AuditStore() as store:
            hbs = extract_heartbeats_from_audit(store, subjects, window_seconds)
        for hb in hbs:
            if hb.service.startswith("service:"):
                hb.service = hb.service.split(":", 1)[1]
        hb_count = await catalog.attach_heartbeats(pool, hbs)
    except Exception as exc:  # noqa: BLE001
        emit(
            {
                "ok": True,
                "synced": synced,
                "snapshots": len(snapshots),
                "heartbeats_updated": 0,
                "heartbeats_error": str(exc),
            }
        )
        return EXIT_OK
    emit(
        {
            "ok": True,
            "synced": synced,
            "snapshots": len(snapshots),
            "heartbeats_updated": hb_count,
        }
    )
    return EXIT_OK


def cmd_refresh(args: argparse.Namespace) -> int:
    return asyncio.run(
        _do_refresh(
            _dsn_from_args(args),
            int(getattr(args, "window_seconds", 3600)),
        )
    )


# ---------------------------------------------------------------------------
# Arg builders
# ---------------------------------------------------------------------------


def _add_dsn_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dsn",
        default=None,
        help="Postgres DSN. Defaults to TICKLES_SHARED_DSN or shared.utils.db pool.",
    )


def _build_apply_migration(p: argparse.ArgumentParser) -> None:
    del p


def _build_migration_sql(p: argparse.ArgumentParser) -> None:
    del p


def _build_sync(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)


def _build_snapshot(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)
    p.add_argument("--names", default=None, help="comma-separated service names")
    p.add_argument("--no-history", action="store_true",
                   help="skip append to services_catalog_snapshots")


def _build_attach_heartbeats(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)
    p.add_argument("--names", default=None, help="comma-separated service names")
    p.add_argument("--window-seconds", type=int, default=3600)


def _build_list(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)
    p.add_argument("--kind", default=None)


def _build_describe(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)
    p.add_argument("name")


def _build_refresh(p: argparse.ArgumentParser) -> None:
    _add_dsn_flag(p)
    p.add_argument("--window-seconds", type=int, default=3600)


def main() -> int:
    subs = [
        Subcommand("apply-migration", "Print migration path + psql example.",
                   cmd_apply_migration, build=_build_apply_migration),
        Subcommand("migration-sql", "Print the raw Phase 24 migration SQL.",
                   cmd_migration_sql, build=_build_migration_sql),
        Subcommand("sync", "Upsert Phase 22 registry into services_catalog.",
                   cmd_sync, build=_build_sync),
        Subcommand("snapshot", "Capture systemd state for registered services.",
                   cmd_snapshot, build=_build_snapshot),
        Subcommand("attach-heartbeats", "Stamp last auditor heartbeat per service.",
                   cmd_attach_heartbeats, build=_build_attach_heartbeats),
        Subcommand("list", "List services_catalog rows from the DB.",
                   cmd_list, build=_build_list),
        Subcommand("describe", "Describe one services_catalog row.",
                   cmd_describe, build=_build_describe),
        Subcommand("refresh", "sync + snapshot + attach-heartbeats in one call.",
                   cmd_refresh, build=_build_refresh),
    ]
    parser = build_parser(
        "services_catalog_cli",
        "Operator CLI for the Tickles Services Catalog (Phase 24).",
        subs,
    )
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
