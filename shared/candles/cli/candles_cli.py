"""
shared.cli.candles_cli — operator CLI for the Candle Hub (Phase 16).

Subcommands:
    status        — systemd state of tickles-candle-daemon
    stats         — per-timeframe candle row counts across the table
    coverage      — per-(instrument, timeframe, source) coverage rows
    resample      — 1m -> Nm/Nh/Nd rollups for one (instrument, source)
    backfill      — historical fetch via CCXT for one instrument
    invalidate    — drop Phase 15 sufficiency cache rows for an instrument

Non-interactive. Emits single-line JSON on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any, Dict, List, Optional

from shared.candles.schema import RESAMPLE_CHAIN, Timeframe

from ._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    is_on_vps,
    run,
    systemctl_status,
)

CANDLE_DAEMON_UNIT = "tickles-candle-daemon"


def _needs_db_or_error() -> Optional[int]:
    if is_on_vps():
        return None
    emit({
        "ok": False,
        "environment": "local",
        "message": (
            "candles_cli commands that read the DB require VPS Postgres "
            "access. Run inside /opt/tickles or set the DB_* env vars "
            "before invoking locally."
        ),
    })
    return EXIT_FAIL


async def _pool() -> Any:
    from shared.utils.db import get_shared_pool
    return await get_shared_pool()


# ---- command handlers --------------------------------------------------

def cmd_status(_args: argparse.Namespace) -> int:
    """Return systemd state of the candle daemon. Works off-VPS too."""
    status = systemctl_status(CANDLE_DAEMON_UNIT)
    emit({
        "ok": True,
        "unit": status["unit"],
        "active": status["active"],
        "sub": status["sub"],
        "enabled": status["enabled"],
    })
    return EXIT_OK


def cmd_stats(_args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Dict[str, int]:
        from shared.candles.coverage import coverage_stats
        return await coverage_stats(await _pool())

    try:
        stats = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "stats": stats})
    return EXIT_OK


def cmd_coverage(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        from shared.candles.coverage import list_coverage
        tf = Timeframe(args.timeframe) if args.timeframe else None
        return await list_coverage(
            await _pool(),
            instrument_id=args.instrument_id,
            timeframe=tf,
            source=args.source,
            active_only=not args.all,
        )

    try:
        rows = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({
        "ok": True,
        "count": len(rows),
        "coverage": [r.model_dump(mode="json") for r in rows],
    })
    return EXIT_OK


def _build_coverage(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, default=None)
    p.add_argument("--timeframe", default=None,
                   choices=[t.value for t in Timeframe])
    p.add_argument("--source", default=None)
    p.add_argument("--all", action="store_true",
                   help="include rows for inactive instruments")


def cmd_resample(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Dict[str, Any]:
        from shared.candles.resample import (
            invalidate_sufficiency_for,
            resample_chain,
            resample_one,
        )
        pool = await _pool()
        if args.chain:
            reports = await resample_chain(
                pool, args.instrument_id, args.source,
                dry_run=args.dry_run,
            )
            touched_tfs: List[Timeframe] = [Timeframe(tf) for tf in reports]
            invalidated = 0
            if not args.dry_run:
                invalidated = await invalidate_sufficiency_for(
                    pool, args.instrument_id, touched_tfs,
                )
            return {
                "chain": True,
                "invalidated_sufficiency_rows": invalidated,
                "reports": {tf: r.model_dump(mode="json")
                            for tf, r in reports.items()},
            }
        target = Timeframe(args.to)
        report = await resample_one(
            pool, args.instrument_id, args.source, target,
            dry_run=args.dry_run,
        )
        invalidated = 0
        if not args.dry_run and report.rows_written > 0:
            invalidated = await invalidate_sufficiency_for(
                pool, args.instrument_id, [target],
            )
        return {
            "chain": False,
            "invalidated_sufficiency_rows": invalidated,
            "report": report.model_dump(mode="json"),
        }

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "result": result})
    return EXIT_OK


def _build_resample(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, required=True)
    p.add_argument("--source", required=True,
                   help="exchange / source tag, e.g. binance")
    p.add_argument("--to", default=None,
                   choices=[t.value for t in RESAMPLE_CHAIN],
                   help="target timeframe for a single rollup")
    p.add_argument("--chain", action="store_true",
                   help="build the full 5m..1w chain (ignores --to)")
    p.add_argument("--dry-run", action="store_true",
                   help="plan only; do not INSERT")


def cmd_backfill(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Dict[str, Any]:
        from shared.candles.backfill import backfill_instrument, parse_window
        start_dt, end_dt = parse_window(args.start, args.end)
        report = await backfill_instrument(
            await _pool(),
            args.instrument_id,
            start_dt, end_dt,
            timeframe=Timeframe(args.timeframe),
            source_override=args.source,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
        return report.model_dump(mode="json")

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "report": result})
    return EXIT_OK


def _build_backfill(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, required=True)
    p.add_argument("--timeframe", default=Timeframe.M1.value,
                   choices=[Timeframe.M1.value],
                   help="only 1m supported in Phase 16")
    p.add_argument("--start", default=None,
                   help="ISO8601 date/time or '7d' relative")
    p.add_argument("--end", default=None,
                   help="ISO8601 date/time; defaults to now")
    p.add_argument("--source", default=None,
                   help="override 'source' column value")
    p.add_argument("--max-pages", type=int, default=None,
                   help="cap page count for smoke-testing")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch only; no DB writes")


def cmd_invalidate(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> int:
        from shared.candles.resample import invalidate_sufficiency_for
        tfs = [Timeframe(t) for t in args.timeframes] if args.timeframes else RESAMPLE_CHAIN + [Timeframe.M1]
        return await invalidate_sufficiency_for(
            await _pool(), args.instrument_id, tfs,
        )

    try:
        deleted = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "invalidated_sufficiency_rows": deleted})
    return EXIT_OK


def _build_invalidate(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, required=True)
    p.add_argument("--timeframes", nargs="*",
                   choices=[t.value for t in Timeframe],
                   help="subset of timeframes; defaults to all")


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="candles_cli",
        description="Operator CLI for the Candle Hub (Phase 16).",
        subcommands=[
            Subcommand("status", "systemd state of tickles-candle-daemon", cmd_status),
            Subcommand("stats", "per-timeframe row counts", cmd_stats),
            Subcommand("coverage", "per-(instrument, timeframe, source) coverage",
                       cmd_coverage, build=_build_coverage),
            Subcommand("resample", "1m -> Nm/Nh/Nd rollups",
                       cmd_resample, build=_build_resample),
            Subcommand("backfill", "CCXT historical backfill",
                       cmd_backfill, build=_build_backfill),
            Subcommand("invalidate", "drop Phase 15 sufficiency cache",
                       cmd_invalidate, build=_build_invalidate),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
