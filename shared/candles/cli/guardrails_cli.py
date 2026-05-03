"""
shared.cli.guardrails_cli — operator CLI for Phase 28 Crash Protection.

Subcommands:

* ``apply-migration`` / ``migration-sql`` — DB migration helpers.
* ``rule-add``    — insert a new protection rule.
* ``rule-list``   — list rules.
* ``rule-disable`` / ``rule-enable`` — toggle a rule.
* ``evaluate``    — evaluate a snapshot JSON file against current rules
                    and print decisions (optionally ``--persist``).
* ``events``      — list recent events.
* ``active``      — read the ``crash_protection_active`` view.
* ``check-intent``— ask whether a specific (company, universe,
                    exchange, symbol) intent is currently blocked.
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

from shared.guardrails import (
    ACTION_TYPES,
    InMemoryGuardrailsPool,
    MIGRATION_PATH,
    PositionSummary,
    ProtectionRule,
    ProtectionSnapshot,
    RULE_TYPES,
    RegimeLabel,
    SEVERITIES,
    SEVERITY_WARNING,
    GuardrailsService,
    GuardrailsStore,
    read_migration_sql,
)

LOG = logging.getLogger("tickles.cli.guardrails")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryGuardrailsPool()
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


async def _handle_rule_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    params = json.loads(args.params) if args.params else {}
    rule = ProtectionRule(
        id=None,
        company_id=args.company_id,
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
        rule_type=args.rule_type,
        action=args.action,
        threshold=args.threshold,
        params=params,
        severity=args.severity,
        enabled=not args.disabled,
    )
    rid = await store.insert_rule(rule)
    _dump({"ok": True, "id": rid})
    return 0


async def _handle_rule_list_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    rules = await store.list_rules(
        company_id=args.company_id,
        rule_type=args.rule_type,
        enabled_only=args.enabled_only,
    )
    _dump({"ok": True, "count": len(rules), "rules": [r.to_dict() for r in rules]})
    return 0


async def _handle_rule_enable_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    n = await store.set_enabled(args.id, not args.disable)
    _dump({"ok": True, "updated": n, "id": args.id, "enabled": not args.disable})
    return 0


def _snapshot_from_json(payload: Dict[str, Any]) -> ProtectionSnapshot:
    def _dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    positions = [
        PositionSummary(
            company_id=p["company_id"],
            exchange=p["exchange"],
            symbol=p["symbol"],
            direction=p.get("direction", "long"),
            quantity=float(p.get("quantity", 0)),
            notional_usd=float(p.get("notional_usd", 0)),
            realized_pnl_usd=float(p.get("realized_pnl_usd", 0)),
            unrealised_pnl_usd=float(p.get("unrealised_pnl_usd", 0)),
        )
        for p in payload.get("positions", [])
    ]
    regimes = [
        RegimeLabel(
            universe=r["universe"],
            exchange=r["exchange"],
            symbol=r["symbol"],
            timeframe=r.get("timeframe", "1h"),
            classifier=r.get("classifier", "composite"),
            regime=r["regime"],
            as_of=_dt(r.get("as_of")) or datetime.now(timezone.utc),
        )
        for r in payload.get("regimes", [])
    ]
    return ProtectionSnapshot(
        company_id=payload["company_id"],
        universe=payload.get("universe"),
        equity_usd=payload.get("equity_usd"),
        equity_peak_usd=payload.get("equity_peak_usd"),
        equity_daily_start_usd=payload.get("equity_daily_start_usd"),
        positions=positions,
        regimes=regimes,
        last_tick_at=_dt(payload.get("last_tick_at")),
        now=_dt(payload.get("now")),
        metadata=payload.get("metadata", {}),
    )


async def _handle_evaluate_async(args: argparse.Namespace) -> int:
    if args.snapshot_file:
        with open(args.snapshot_file, "r", encoding="utf-8") as f:
            snap_payload = json.load(f)
    elif args.snapshot:
        snap_payload = json.loads(args.snapshot)
    else:
        raise SystemExit("--snapshot or --snapshot-file is required")
    snapshot = _snapshot_from_json(snap_payload)

    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    service = GuardrailsService(store)
    decisions = await service.tick(
        snapshot,
        rule_type=args.rule_type,
        persist=args.persist,
        only_triggered=args.only_triggered,
    )
    _dump({
        "ok": True,
        "count": len(decisions),
        "decisions": [d.to_dict() for d in decisions],
    })
    return 0


async def _handle_events_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    rows = await store.list_events(
        company_id=args.company_id,
        status=args.status,
        limit=args.limit,
    )
    _dump({"ok": True, "count": len(rows), "events": [r.to_dict() for r in rows]})
    return 0


async def _handle_active_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    rows = await store.list_active(
        company_id=args.company_id, triggered_only=not args.all,
    )
    _dump({"ok": True, "count": len(rows), "active": [r.to_dict() for r in rows]})
    return 0


async def _handle_check_intent_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = GuardrailsStore(pool)
    service = GuardrailsService(store)
    blockers = await service.is_intent_blocked(
        company_id=args.company_id,
        universe=args.universe,
        exchange=args.exchange,
        symbol=args.symbol,
    )
    _dump({
        "ok": True,
        "blocked": bool(blockers),
        "blockers": [r.to_dict() for r in blockers],
    })
    return 0


# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_GUARDRAILS_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guardrails_cli",
        description="Operator CLI for Tickles Crash Protection (Phase 28).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 28 migration SQL.")

    sp = sub.add_parser("rule-add", help="Insert a new protection rule.")
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", default=None)
    sp.add_argument("--symbol", default=None)
    sp.add_argument("--rule-type", required=True, choices=sorted(RULE_TYPES))
    sp.add_argument("--action", required=True, choices=sorted(ACTION_TYPES))
    sp.add_argument("--threshold", type=float, default=None)
    sp.add_argument("--params", default=None, help="JSON params dict")
    sp.add_argument("--severity", default=SEVERITY_WARNING, choices=sorted(SEVERITIES))
    sp.add_argument("--disabled", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("rule-list", help="List protection rules.")
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--rule-type", default=None, choices=sorted(RULE_TYPES))
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("rule-toggle", help="Enable or disable a rule.")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--disable", action="store_true",
                    help="Disable instead of enable (default: enable)")
    _add_common(sp)

    sp = sub.add_parser("evaluate", help="Evaluate a snapshot against current rules.")
    sp.add_argument("--snapshot", default=None, help="Inline JSON snapshot")
    sp.add_argument("--snapshot-file", default=None, help="Path to JSON snapshot file")
    sp.add_argument("--rule-type", default=None, choices=sorted(RULE_TYPES))
    sp.add_argument("--persist", action="store_true",
                    help="Also persist decisions as events.")
    sp.add_argument("--only-triggered", action="store_true",
                    help="If persisting, skip resolved decisions.")
    _add_common(sp)

    sp = sub.add_parser("events", help="List recent events.")
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--status", default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("active", help="Read crash_protection_active view.")
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--all", action="store_true",
                    help="Include resolved rows (default: triggered only).")
    _add_common(sp)

    sp = sub.add_parser("check-intent", help="Is an intent currently blocked?")
    sp.add_argument("--company-id", required=True)
    sp.add_argument("--universe", default=None)
    sp.add_argument("--exchange", required=True)
    sp.add_argument("--symbol", required=True)
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
    if args.cmd == "rule-add":
        return asyncio.run(_handle_rule_add_async(args))
    if args.cmd == "rule-list":
        return asyncio.run(_handle_rule_list_async(args))
    if args.cmd == "rule-toggle":
        return asyncio.run(_handle_rule_enable_async(args))
    if args.cmd == "evaluate":
        return asyncio.run(_handle_evaluate_async(args))
    if args.cmd == "events":
        return asyncio.run(_handle_events_async(args))
    if args.cmd == "active":
        return asyncio.run(_handle_active_async(args))
    if args.cmd == "check-intent":
        return asyncio.run(_handle_check_intent_async(args))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
