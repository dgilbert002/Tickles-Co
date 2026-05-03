"""
shared.cli.sufficiency_cli — operator CLI for the Data Sufficiency Engine
(Phase 15).

Subcommands:
    stats        — cache row counts (pass/warn/fail, instruments covered)
    profiles     — list effective profiles (DB overrides on top of built-ins)
    check        — grade one (instrument, profile) pair; prints report
    report       — fetch the most recent cached report (no refresh)
    invalidate   — drop cached rows for an instrument / timeframe
    bulk         — grade every active instrument against a profile

All commands emit single-line JSON on stdout so they pipe cleanly into
Paperclip agents or shell pipelines.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any, Dict, List, Optional

from shared.data_sufficiency.schema import Timeframe

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
    """Acquire the shared DB pool and wrap it in a SufficiencyService."""
    from shared.data_sufficiency.service import SufficiencyService
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    return SufficiencyService(pool)


def _needs_db_or_error() -> Optional[int]:
    """Short-circuit with a helpful message when run off-box without a DB."""
    if is_on_vps():
        return None
    emit({
        "ok": False,
        "environment": "local",
        "message": (
            "sufficiency_cli commands that read the DB require VPS Postgres "
            "access. Run inside /opt/tickles or set the DB_* env vars before "
            "invoking locally."
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


def cmd_profiles(args: argparse.Namespace) -> int:
    """List effective profiles. Works off-box (falls back to built-ins)."""
    async def _run() -> Any:
        if is_on_vps():
            svc = await _service()
            return await svc.list_profiles()
        # off-box: built-ins only
        from shared.data_sufficiency.profiles import BUILTIN_PROFILES
        return sorted(BUILTIN_PROFILES.values(), key=lambda p: p.name)

    try:
        profiles = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    if args.names_only:
        emit({
            "ok": True,
            "count": len(profiles),
            "names": [p.name for p in profiles],
        })
    else:
        emit({
            "ok": True,
            "count": len(profiles),
            "profiles": [p.model_dump(mode="json") for p in profiles],
        })
    return EXIT_OK


def _build_profiles(p: argparse.ArgumentParser) -> None:
    p.add_argument("--names-only", action="store_true",
                   help="emit only the profile names")


def cmd_check(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        svc = await _service()
        return await svc.check(
            args.instrument_id,
            args.profile,
            use_cache=not args.no_cache,
        )

    try:
        report = asyncio.run(_run())
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL

    payload = {"ok": True, "report": report.model_dump(mode="json")}
    emit(payload)
    return EXIT_OK if report.ok else EXIT_FAIL


def _build_check(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, required=True,
                   help="instruments.id to grade")
    p.add_argument("--profile", required=True,
                   help='profile name (see `sufficiency_cli profiles`)')
    p.add_argument("--no-cache", action="store_true",
                   help="force a fresh scan (ignore cached report)")


def cmd_report(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Any:
        svc = await _service()
        return await svc.report_for(
            args.instrument_id,
            Timeframe(args.timeframe),
            args.profile,
        )

    try:
        report = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    if report is None:
        emit({"ok": False, "cached": False,
              "message": "no cached report for this triplet"})
        return EXIT_FAIL
    emit({"ok": True, "cached": True,
          "report": report.model_dump(mode="json")})
    return EXIT_OK


def _build_report(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, required=True)
    p.add_argument("--timeframe", required=True,
                   choices=[t.value for t in Timeframe])
    p.add_argument("--profile", required=True)


def cmd_invalidate(args: argparse.Namespace) -> int:
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> int:
        svc = await _service()
        tf = Timeframe(args.timeframe) if args.timeframe else None
        return await svc.invalidate(
            instrument_id=args.instrument_id,
            timeframe=tf,
        )

    try:
        deleted = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "deleted": deleted})
    return EXIT_OK


def _build_invalidate(p: argparse.ArgumentParser) -> None:
    p.add_argument("--instrument-id", type=int, default=None)
    p.add_argument("--timeframe", default=None,
                   choices=[t.value for t in Timeframe])


def cmd_bulk(args: argparse.Namespace) -> int:
    """Grade every active instrument against a profile and print summary."""
    rc = _needs_db_or_error()
    if rc is not None:
        return rc

    async def _run() -> Dict[str, Any]:
        from shared.utils.db import get_shared_pool
        from shared.data_sufficiency.service import SufficiencyService
        pool = await get_shared_pool()
        svc = SufficiencyService(pool)
        rows = await pool.fetch_all(
            "SELECT id FROM instruments WHERE is_active=TRUE ORDER BY id",
        )
        ids = [int(r["id"]) for r in rows]
        if args.limit:
            ids = ids[: args.limit]
        results: Dict[str, int] = {"pass": 0, "pass_with_warnings": 0, "fail": 0, "error": 0}
        failures: List[Dict[str, Any]] = []
        for iid in ids:
            try:
                rep = await svc.check(iid, args.profile, use_cache=not args.no_cache)
                results[rep.verdict.value] = results.get(rep.verdict.value, 0) + 1
                if rep.verdict.value == "fail":
                    failures.append({
                        "instrument_id": iid,
                        "reasons": rep.reasons_fail[:3],
                    })
            except Exception as exc:
                results["error"] += 1
                failures.append({"instrument_id": iid, "error": str(exc)})
        return {
            "profile": args.profile,
            "scanned": len(ids),
            "results": results,
            "failures_sample": failures[:20],
        }

    try:
        summary = asyncio.run(_run())
    except Exception as exc:
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "summary": summary})
    return EXIT_OK


def _build_bulk(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", required=True)
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of instruments scanned")
    p.add_argument("--no-cache", action="store_true",
                   help="force fresh scan on every instrument")


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="sufficiency_cli",
        description="Operator CLI for the Data Sufficiency Engine (Phase 15).",
        subcommands=[
            Subcommand("stats", "cache row counts + pass/warn/fail split", cmd_stats),
            Subcommand("profiles", "list effective profiles",
                       cmd_profiles, build=_build_profiles),
            Subcommand("check", "grade one (instrument, profile)",
                       cmd_check, build=_build_check),
            Subcommand("report", "fetch cached report without refresh",
                       cmd_report, build=_build_report),
            Subcommand("invalidate", "drop cached rows",
                       cmd_invalidate, build=_build_invalidate),
            Subcommand("bulk", "grade every active instrument",
                       cmd_bulk, build=_build_bulk),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
