"""
shared.cli.souls_cli — operator CLI for Phase 31 Apex/Quant/Ledger souls.

Subcommands:
  apply-migration / migration-sql   DB migration helpers.
  seed-personas                     Upsert canonical Apex/Quant/Ledger personas.
  personas                          List registered personas.
  prompts-add                       Add a prompt version for a persona.
  prompts                           List prompts for a persona.
  run-apex / run-quant / run-ledger Evaluate one soul against a JSON context.
  decisions                         List recent decisions (filter by persona / correlation).
  latest                            Latest decision per (persona, correlation_id).

Every soul runs deterministically by default — ``--in-memory`` produces
stable output without touching PostgreSQL. When a DSN is supplied the
CLI persists verdicts via :class:`SoulsStore`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from shared.souls import (
    InMemorySoulsPool,
    MIGRATION_PATH,
    MIGRATION_PATH_PHASE32,
    SoulContext,
    SoulPrompt,
    SoulsService,
    SoulsStore,
    read_migration_sql,
    read_migration_sql_phase32,
)

LOG = logging.getLogger("tickles.cli.souls")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemorySoulsPool()
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
            self, sql: str, params: Sequence[Any]
        ) -> Optional[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(
            self, sql: str, params: Sequence[Any]
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


def _parse_fields(value: Optional[str]) -> Dict[str, Any]:
    if not value:
        return {}
    if value.startswith("@"):
        with open(value[1:], "r", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(value)


# ---------------------------------------------------------------------------


def _handle_migration_sql(args: argparse.Namespace) -> int:
    phase = getattr(args, "phase", 31)
    if phase == 32:
        print(read_migration_sql_phase32())
    else:
        print(read_migration_sql())
    return 0


def _handle_apply_migration(args: argparse.Namespace) -> int:
    phase = getattr(args, "phase", 31)
    path = MIGRATION_PATH_PHASE32 if phase == 32 else MIGRATION_PATH
    if args.path_only:
        print(str(path))
        return 0
    _dump({
        "migration_path": str(path),
        "apply_with": (
            "psql -h 127.0.0.1 -U admin -d tickles_shared "
            f"-f {path}"
        ),
        "ok": True,
    })
    return 0


async def _handle_seed_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = SoulsService(SoulsStore(pool))
    ids = await svc.seed_personas()
    _dump({"ok": True, "persona_ids": ids})
    return 0


async def _handle_personas_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = SoulsStore(pool)
    rows = await store.list_personas(enabled_only=args.enabled_only)
    _dump({"ok": True, "count": len(rows), "personas": [r.to_dict() for r in rows]})
    return 0


async def _handle_prompts_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = SoulsStore(pool)
    persona = await store.get_persona(args.persona)
    if not persona or not persona.id:
        _dump({"ok": False, "error": f"persona {args.persona!r} not found (seed first)"})
        return 1
    prompt = SoulPrompt(
        id=None,
        persona_id=int(persona.id),
        version=int(args.version),
        template=args.template,
        variables=json.loads(args.variables) if args.variables else [],
    )
    pid = await store.add_prompt(prompt)
    _dump({"ok": True, "id": pid, "prompt": prompt.to_dict()})
    return 0


async def _handle_prompts_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = SoulsStore(pool)
    persona = await store.get_persona(args.persona)
    if not persona or not persona.id:
        _dump({"ok": False, "error": f"persona {args.persona!r} not found"})
        return 1
    rows = await store.list_prompts(int(persona.id))
    _dump({"ok": True, "count": len(rows), "prompts": [r.to_dict() for r in rows]})
    return 0


async def _run_soul(args: argparse.Namespace, which: str) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = SoulsService(SoulsStore(pool))
    if not args.no_persist:
        await svc.seed_personas()
    fields = _parse_fields(args.fields)
    ctx = SoulContext(
        correlation_id=args.correlation_id,
        company_id=args.company_id,
        fields=fields,
    )
    persist = not args.no_persist
    if which == "apex":
        decision = await svc.run_apex(ctx, persist=persist)
    elif which == "quant":
        decision = await svc.run_quant(ctx, persist=persist)
    elif which == "ledger":
        decision = await svc.run_ledger(ctx, persist=persist)
    elif which == "scout":
        decision = await svc.run_scout(ctx, persist=persist)
    elif which == "curiosity":
        decision = await svc.run_curiosity(ctx, persist=persist)
    elif which == "optimiser":
        decision = await svc.run_optimiser(ctx, persist=persist)
    elif which == "regime-watcher":
        decision = await svc.run_regime_watcher(ctx, persist=persist)
    else:  # pragma: no cover - argparse enforces this
        raise ValueError(which)
    _dump({"ok": True, "soul": which, "decision": decision.to_dict()})
    return 0


async def _handle_decisions_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = SoulsService(SoulsStore(pool))
    persona_id: Optional[int] = None
    if args.persona:
        persona = await svc._store.get_persona(args.persona)  # type: ignore[attr-defined]
        if persona and persona.id:
            persona_id = int(persona.id)
    rows = await svc.decisions(
        persona_id=persona_id,
        correlation_id=args.correlation_id,
        company_id=args.company_id,
        verdict=args.verdict,
        limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "decisions": [r.to_dict() for r in rows],
    })
    return 0


async def _handle_latest_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    svc = SoulsService(SoulsStore(pool))
    rows = await svc.latest_decisions(
        persona=args.persona, company_id=args.company_id, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "decisions": [r.to_dict() for r in rows],
    })
    return 0


# ---------------------------------------------------------------------------


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_SOULS_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _add_run_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--correlation-id", required=True)
    p.add_argument("--company-id", default=None)
    p.add_argument("--fields", default=None,
                   help="JSON dict OR @path/to/file.json with soul inputs.")
    p.add_argument("--no-persist", action="store_true",
                   help="Do not write the decision to agent_decisions.")
    _add_common(p)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="souls_cli",
        description="Operator CLI for Tickles Apex/Quant/Ledger souls (Phase 31).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration", help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sp.add_argument("--phase", type=int, choices=[31, 32], default=31)
    sp = sub.add_parser("migration-sql", help="Print the Phase 31/32 migration SQL.")
    sp.add_argument("--phase", type=int, choices=[31, 32], default=31)

    sp = sub.add_parser("seed-personas",
                        help="Upsert canonical Apex/Quant/Ledger personas.")
    _add_common(sp)

    sp = sub.add_parser("personas", help="List registered personas.")
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("prompts-add", help="Add a prompt version for a persona.")
    sp.add_argument("--persona", required=True)
    sp.add_argument("--version", type=int, required=True)
    sp.add_argument("--template", required=True)
    sp.add_argument("--variables", default=None,
                    help='JSON list of variable names, e.g. \'["regime","trend"]\'')
    _add_common(sp)

    sp = sub.add_parser("prompts", help="List prompts for a persona.")
    sp.add_argument("--persona", required=True)
    _add_common(sp)

    sp = sub.add_parser("run-apex", help="Evaluate the Apex soul against a context.")
    _add_run_common(sp)
    sp = sub.add_parser("run-quant", help="Evaluate the Quant soul against a context.")
    _add_run_common(sp)
    sp = sub.add_parser("run-ledger", help="Evaluate the Ledger soul against a context.")
    _add_run_common(sp)
    sp = sub.add_parser("run-scout", help="Evaluate the Scout soul against a context.")
    _add_run_common(sp)
    sp = sub.add_parser("run-curiosity", help="Evaluate the Curiosity soul.")
    _add_run_common(sp)
    sp = sub.add_parser("run-optimiser", help="Evaluate the Optimiser soul.")
    _add_run_common(sp)
    sp = sub.add_parser("run-regime-watcher",
                        help="Evaluate the Regime-Watcher soul.")
    _add_run_common(sp)

    sp = sub.add_parser("decisions", help="List recent decisions.")
    sp.add_argument("--persona", default=None)
    sp.add_argument("--correlation-id", default=None)
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--verdict", default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("latest", help="Latest decision per (persona, correlation_id).")
    sp.add_argument("--persona", default=None)
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--limit", type=int, default=50)
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
    if args.cmd == "seed-personas":
        return asyncio.run(_handle_seed_async(args))
    if args.cmd == "personas":
        return asyncio.run(_handle_personas_async(args))
    if args.cmd == "prompts-add":
        return asyncio.run(_handle_prompts_add_async(args))
    if args.cmd == "prompts":
        return asyncio.run(_handle_prompts_async(args))
    if args.cmd == "run-apex":
        return asyncio.run(_run_soul(args, "apex"))
    if args.cmd == "run-quant":
        return asyncio.run(_run_soul(args, "quant"))
    if args.cmd == "run-ledger":
        return asyncio.run(_run_soul(args, "ledger"))
    if args.cmd == "run-scout":
        return asyncio.run(_run_soul(args, "scout"))
    if args.cmd == "run-curiosity":
        return asyncio.run(_run_soul(args, "curiosity"))
    if args.cmd == "run-optimiser":
        return asyncio.run(_run_soul(args, "optimiser"))
    if args.cmd == "run-regime-watcher":
        return asyncio.run(_run_soul(args, "regime-watcher"))
    if args.cmd == "decisions":
        return asyncio.run(_handle_decisions_async(args))
    if args.cmd == "latest":
        return asyncio.run(_handle_latest_async(args))

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
