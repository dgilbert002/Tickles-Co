"""
shared.cli.assets_cli — operator CLI for the Universal Asset Catalog (Phase 14).

Subcommands:
    stats               — row counts across venues / assets / instruments / aliases
    list-venues         — list configured venues (code, adapter, priority, active)
    list-assets         — list assets (optionally filtered by class)
    resolve             — resolve a symbol to an instrument (with optional --venue)
    spread              — dump v_asset_venues rows + cheapest/spread for one asset
    load                — run the loader (wrapper over `python -m shared.assets.loader`)

All commands emit single-line JSON on stdout so they pipe cleanly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, Optional

from ._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    is_on_vps,
    run,
)


async def _service() -> Any:
    """Acquire the shared DB pool and wrap it in an AssetCatalogService."""
    from shared.assets.service import AssetCatalogService
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    return AssetCatalogService(pool)


def _needs_db_or_error() -> Optional[int]:
    """Short-circuit with a helpful message when run off-box without a DB."""
    if is_on_vps():
        return None
    emit({
        "ok": False,
        "environment": "local",
        "message": (
            "assets_cli commands that read the DB require VPS Postgres access. "
            "Run inside /opt/tickles or set DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/"
            "DB_NAME_SHARED env vars before invoking locally."
        ),
    })
    return EXIT_FAIL


# ---- command handlers --------------------------------------------------

def cmd_stats(_args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Dict[str, int]:
        svc = await _service()
        return await svc.stats()

    try:
        stats = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "stats": stats})
    return EXIT_OK


def cmd_list_venues(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        svc = await _service()
        return await svc.list_venues(active_only=not args.all)

    try:
        venues = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({
        "ok": True,
        "count": len(venues),
        "venues": [v.model_dump(mode="json") for v in venues],
    })
    return EXIT_OK


def _build_list_venues(p: argparse.ArgumentParser) -> None:
    p.add_argument("--all", action="store_true", help="include inactive venues")


def cmd_list_assets(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc
    from shared.assets.schema import AssetClass
    ac: Optional[AssetClass] = None
    if args.asset_class:
        try:
            ac = AssetClass(args.asset_class)
        except ValueError:
            emit({"ok": False, "error": f"unknown asset_class: {args.asset_class}"})
            return EXIT_FAIL

    async def _run() -> Any:
        svc = await _service()
        return await svc.list_assets(asset_class=ac, canonical_only=not args.include_aliases)

    try:
        assets = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({
        "ok": True,
        "count": len(assets),
        "assets": [a.model_dump(mode="json") for a in assets],
    })
    return EXIT_OK


def _build_list_assets(p: argparse.ArgumentParser) -> None:
    p.add_argument("--asset-class", default=None,
                   help="filter: crypto|cfd|commodity|forex|index|stock")
    p.add_argument("--include-aliases", action="store_true",
                   help="include non-canonical (aliased) assets")


def cmd_resolve(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        svc = await _service()
        return await svc.resolve_symbol(args.symbol, venue_code=args.venue)

    try:
        ref = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    if ref is None:
        emit({"ok": False, "symbol": args.symbol, "venue": args.venue, "resolved": None})
        return EXIT_FAIL
    emit({"ok": True, "resolved": ref.model_dump(mode="json")})
    return EXIT_OK


def _build_resolve(p: argparse.ArgumentParser) -> None:
    p.add_argument("symbol", help="venue-native symbol or alias")
    p.add_argument("--venue", default=None, help="optional venue code filter")


def cmd_spread(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        svc = await _service()
        return await svc.spread_snapshot(args.asset)

    try:
        snap = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "snapshot": snap})
    return EXIT_OK


def _build_spread(p: argparse.ArgumentParser) -> None:
    p.add_argument("asset", help="canonical asset symbol, e.g. BTC or GOLD")


def cmd_load(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None and not args.dry_run:
        return rc
    from shared.assets import loader as asset_loader

    async def _run() -> Any:
        if args.dry_run:
            return await asset_loader.run_load(
                None, asset_loader._venue_codes(args.venue),
                limit=args.limit, dry_run=True,
            )
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()
        return await asset_loader.run_load(
            pool, asset_loader._venue_codes(args.venue),
            limit=args.limit, dry_run=False,
        )

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    sys.stdout.write(json.dumps({"ok": True, "result": result}, sort_keys=True, default=str) + "\n")
    return EXIT_OK


def _build_load(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", default="all",
                   help='comma-separated venue codes or "all"')
    p.add_argument("--limit", type=int, default=None,
                   help="max instruments per venue (smoke-test mode)")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch only; no DB writes")


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="assets_cli",
        description="Operator CLI for the Universal Asset Catalog.",
        subcommands=[
            Subcommand("stats", "row counts across venues/assets/instruments/aliases", cmd_stats),
            Subcommand("list-venues", "list configured venues",
                       cmd_list_venues, build=_build_list_venues),
            Subcommand("list-assets", "list logical assets",
                       cmd_list_assets, build=_build_list_assets),
            Subcommand("resolve", "resolve a symbol to an instrument",
                       cmd_resolve, build=_build_resolve),
            Subcommand("spread", "spread snapshot for a logical asset",
                       cmd_spread, build=_build_spread),
            Subcommand("load", "run the loader (wrapper)",
                       cmd_load, build=_build_load),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
