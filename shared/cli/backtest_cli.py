"""
shared.cli.backtest_cli — operator CLI for the Phase 35 Local-to-VPS
Backtest Submission layer.

Subcommands:
  apply-migration / migration-sql   DB migration helpers.
  submit                            Submit a new backtest (reads JSON spec).
  status                            Inspect a single submission by id.
  list                              List submissions (filter by status/client).
  wait                              Block until the submission terminates.
  cancel                            Mark a submission cancelled.
  demo                              End-to-end in-memory demo.

The CLI talks to :table:`public.backtest_submissions` through
:class:`BacktestSubmissionStore`, and (optionally) enqueues into the
Phase-16 Redis queue via :class:`shared.backtest.queue.BacktestQueue`
(``--with-queue``). Without ``--with-queue`` the CLI still records
submissions — useful on laptops that don't have Redis reachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from shared.backtest_submit import (
    BacktestSpec,
    BacktestSubmissionStore,
    BacktestSubmitter,
    MIGRATION_PATH,
    QueueProtocol,
    read_migration_sql,
)
from shared.backtest_submit.memory_pool import InMemoryBacktestSubmitPool
from shared.backtest_submit.submitter import InMemoryQueue

LOG = logging.getLogger("tickles.cli.backtest")


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryBacktestSubmitPool()
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
            self, sql: str, params: Sequence[Any],
        ) -> Optional[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(
            self, sql: str, params: Sequence[Any],
        ) -> List[Dict[str, Any]]:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    return _AsyncpgPool(pool)


def _get_queue(enabled: bool) -> Optional[QueueProtocol]:
    if not enabled:
        return None
    try:
        from backtest.queue import BacktestQueue  # type: ignore
    except ImportError:
        try:
            from shared.backtest.queue import BacktestQueue  # type: ignore
        except ImportError:
            LOG.warning("backtest.queue unavailable, running without queue")
            return None
    try:
        return BacktestQueue()
    except Exception as e:  # pragma: no cover - network dependent
        LOG.warning("BacktestQueue init failed (%s); running without queue", e)
        return None


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


# ----------------------------------------------------------------- handlers


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


def _load_spec(source: str) -> BacktestSpec:
    if source.startswith("@"):
        with open(source[1:], "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    else:
        raw = json.loads(source)
    return BacktestSpec(
        strategy=str(raw["strategy"]),
        symbols=list(raw.get("symbols") or []),
        timeframe=str(raw.get("timeframe", "1m")),
        start=raw.get("start"), end=raw.get("end"),
        engine=str(raw.get("engine", "classic")),
        params=dict(raw.get("params") or {}),
        capital_usd=float(raw.get("capital_usd") or 10_000.0),
        metadata=dict(raw.get("metadata") or {}),
    )


async def _handle_submit_async(args: argparse.Namespace) -> int:
    if not args.spec:
        _dump({"ok": False, "error": "--spec is required"})
        return 1
    spec = _load_spec(args.spec)
    pool = await _get_pool(args.dsn, args.in_memory)
    store = BacktestSubmissionStore(pool)
    queue = _get_queue(args.with_queue)
    submitter = BacktestSubmitter(
        store, queue, default_client_id=args.client_id,
    )
    sub = await submitter.submit(
        spec, client_id=args.client_id,
        company_id=args.company_id,
        metadata=json.loads(args.metadata) if args.metadata else None,
        enqueue=args.with_queue,
    )
    _dump({"ok": True, "submission": sub.to_dict()})
    return 0


async def _handle_status_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = BacktestSubmissionStore(pool)
    sub = await store.get(args.id)
    if sub is None:
        _dump({"ok": False, "error": f"submission {args.id} not found"})
        return 1
    _dump({"ok": True, "submission": sub.to_dict()})
    return 0


async def _handle_list_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = BacktestSubmissionStore(pool)
    rows = await store.list(
        status=args.status, client_id=args.client_id,
        active_only=args.active_only, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "submissions": [r.to_dict() for r in rows],
    })
    return 0


async def _handle_wait_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = BacktestSubmissionStore(pool)
    deadline = time.time() + args.timeout_s
    last_status: Optional[str] = None
    while True:
        sub = await store.get(args.id)
        if sub is None:
            _dump({"ok": False, "error": f"submission {args.id} not found"})
            return 1
        if sub.status != last_status:
            LOG.info("wait(%s): status=%s", args.id, sub.status)
            last_status = sub.status
        if sub.is_terminal:
            _dump({"ok": True, "submission": sub.to_dict()})
            return 0 if sub.status == "completed" else 2
        if time.time() >= deadline:
            _dump({
                "ok": False, "error": "timeout",
                "submission": sub.to_dict(),
            })
            return 3
        await asyncio.sleep(args.poll_s)


async def _handle_cancel_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = BacktestSubmissionStore(pool)
    await store.mark_cancelled(args.id, reason=args.reason)
    sub = await store.get(args.id)
    _dump({
        "ok": True, "submission": sub.to_dict() if sub else None,
    })
    return 0


async def _handle_demo_async(args: argparse.Namespace) -> int:
    """End-to-end demo: submit, pretend-worker runs, mark completed."""

    pool = InMemoryBacktestSubmitPool()
    store = BacktestSubmissionStore(pool)
    queue = InMemoryQueue()
    submitter = BacktestSubmitter(store, queue, default_client_id="demo-cli")

    print("[demo] submitting three backtests ...")
    subs = []
    for strat, sym, tf in (
        ("rsi_crossover", "BTC/USDT", "1h"),
        ("donchian_breakout", "ETH/USDT", "15m"),
        ("ma_revert", "SOL/USDT", "5m"),
    ):
        spec = BacktestSpec(
            strategy=strat, symbols=[sym], timeframe=tf,
            start="2026-01-01", end="2026-02-01",
            engine="classic",
            params={"lookback": 14, "threshold": 1.5},
            capital_usd=10_000.0,
        )
        sub = await submitter.submit(spec)
        subs.append(sub)
        print(
            f"  submitted id={sub.id} hash={sub.spec_hash[:12]} "
            f"status={sub.status} queue_job={sub.queue_job_id}"
        )

    # Dedupe demo: re-submitting the first spec hits the partial
    # unique index and the submitter returns the existing row.
    repeat = await submitter.submit(subs[0].spec)
    print()
    print(
        f"[demo] resubmit of same spec returns id={repeat.id} "
        f"status={repeat.status} (idempotent)"
    )

    # "Worker" picks jobs off the queue and drives status transitions.
    from shared.backtest_submit.worker_hook import SubmissionWorkerHook
    hook = SubmissionWorkerHook(store)
    print()
    print("[demo] fake worker processing queue ...")
    for job in queue.jobs:
        sid = job["submission_id"]
        await hook.on_start(sid)
        pnl = round((sid * 123.45) % 250.0 - 50, 2)
        trades = 20 + (sid * 3 % 40)
        sharpe = round(((sid * 7) % 25) / 10.0, 2)
        await hook.on_complete(sid, summary={
            "pnl_usd": pnl, "trades": trades, "sharpe": sharpe,
        }, artefacts={"ch_table": f"backtests.run_{sid:04d}"})
        print(
            f"  worker finished id={sid}  pnl=${pnl:>7.2f}  "
            f"trades={trades:<3}  sharpe={sharpe}"
        )

    print()
    print("[demo] final statuses:")
    rows = await store.list(limit=20)
    for r in rows:
        summary = r.result_summary or {}
        print(
            f"  id={r.id:<3} status={r.status:<10} strategy={r.spec.strategy:<20}"
            f" pnl=${summary.get('pnl_usd', 0):>7.2f}"
            f" trades={summary.get('trades', 0):<3} "
            f"sharpe={summary.get('sharpe', 0)}"
        )
    return 0


# ----------------------------------------------------------------- parser


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_BT_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_cli",
        description="Phase 35 Local-to-VPS Backtest Submission CLI.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration",
                        help="Print migration path + psql example.")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql", help="Print the Phase 35 migration SQL.")

    sp = sub.add_parser("submit", help="Submit a new backtest job.")
    sp.add_argument("--spec", required=True,
                    help="JSON literal OR @path/to/spec.json")
    sp.add_argument("--client-id", default=None)
    sp.add_argument("--company-id", default=None)
    sp.add_argument("--metadata", default=None)
    sp.add_argument("--with-queue", action="store_true",
                    help="Enqueue into the Phase-16 Redis queue.")
    _add_common(sp)

    sp = sub.add_parser("status", help="Show one submission by id.")
    sp.add_argument("id", type=int)
    _add_common(sp)

    sp = sub.add_parser("list", help="List submissions.")
    sp.add_argument("--status", default=None)
    sp.add_argument("--client-id", default=None)
    sp.add_argument("--active-only", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("wait", help="Poll until a submission terminates.")
    sp.add_argument("id", type=int)
    sp.add_argument("--timeout-s", type=float, default=600.0)
    sp.add_argument("--poll-s", type=float, default=2.0)
    _add_common(sp)

    sp = sub.add_parser("cancel", help="Mark a submission cancelled.")
    sp.add_argument("id", type=int)
    sp.add_argument("--reason", default=None)
    _add_common(sp)

    sub.add_parser("demo", help="End-to-end in-memory demo.")

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
    if args.cmd == "submit":
        return asyncio.run(_handle_submit_async(args))
    if args.cmd == "status":
        return asyncio.run(_handle_status_async(args))
    if args.cmd == "list":
        return asyncio.run(_handle_list_async(args))
    if args.cmd == "wait":
        return asyncio.run(_handle_wait_async(args))
    if args.cmd == "cancel":
        return asyncio.run(_handle_cancel_async(args))
    if args.cmd == "demo":
        return asyncio.run(_handle_demo_async(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
