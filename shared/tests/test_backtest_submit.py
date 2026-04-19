"""Phase 35 — Local-to-VPS Backtest Submission tests."""
from __future__ import annotations

import asyncio
import json
from io import StringIO
from unittest.mock import patch

from shared.backtest_submit import (
    BacktestSpec,
    BacktestSubmissionStore,
    BacktestSubmitter,
    MIGRATION_PATH,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SUBMITTED,
    read_migration_sql,
)
from shared.backtest_submit.memory_pool import InMemoryBacktestSubmitPool
from shared.backtest_submit.submitter import InMemoryQueue
from shared.backtest_submit.worker_hook import SubmissionWorkerHook
from shared.cli import backtest_cli


# -------------------------------------------------------------- migration


def test_migration_path_exists():
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS public.backtest_submissions" in sql
    assert "backtest_submissions_hash_active_idx" in sql
    assert "public.backtest_submissions_active" in sql


# ------------------------------------------------------------------ spec


def test_spec_hash_is_canonical():
    a = BacktestSpec(
        strategy="rsi", symbols=["BTC/USDT", "ETH/USDT"],
        params={"b": 2, "a": 1}, capital_usd=10_000.0,
    )
    b = BacktestSpec(
        strategy="rsi", symbols=["ETH/USDT", "BTC/USDT"],
        params={"a": 1, "b": 2}, capital_usd=10_000.0,
    )
    assert a.hash() == b.hash()


def test_spec_hash_changes_with_params():
    a = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"], params={"x": 1})
    b = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"], params={"x": 2})
    assert a.hash() != b.hash()


# ----------------------------------------------------------------- store


def test_store_create_and_get():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        from shared.backtest_submit.protocol import BacktestSubmission
        sub = BacktestSubmission.from_spec(spec, client_id="local")
        sid = await store.create(sub)
        assert sid > 0
        fetched = await store.get(sid)
        assert fetched is not None
        assert fetched.spec.strategy == "rsi"
        assert fetched.status == STATUS_SUBMITTED

    asyncio.run(_run())


def test_store_dedupes_active_and_completed_specs():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        from shared.backtest_submit.protocol import BacktestSubmission
        first = BacktestSubmission.from_spec(spec)
        a = await store.create(first)
        dup = BacktestSubmission.from_spec(spec)
        b = await store.create(dup)
        assert a > 0
        assert b == 0  # partial unique index blocks while active

        # Cancelling the first should re-open the slot.
        await store.mark_cancelled(a, reason="testing")
        again = BacktestSubmission.from_spec(spec)
        c = await store.create(again)
        assert c > 0 and c != a

    asyncio.run(_run())


def test_store_transitions_to_running_completed():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        from shared.backtest_submit.protocol import BacktestSubmission
        sid = await store.create(BacktestSubmission.from_spec(spec))
        await store.mark_queued(sid, "q-123")
        mid = await store.get(sid)
        assert mid.status == STATUS_QUEUED
        assert mid.queue_job_id == "q-123"
        await store.mark_running(sid)
        await store.mark_completed(
            sid,
            result_summary={"pnl_usd": 42.0, "trades": 5},
            artefacts={"ch_table": "backtests.run_001"},
        )
        done = await store.get(sid)
        assert done.status == STATUS_COMPLETED
        assert done.result_summary["pnl_usd"] == 42.0
        assert done.artefacts["ch_table"] == "backtests.run_001"

    asyncio.run(_run())


def test_store_list_active_only_filters():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        from shared.backtest_submit.protocol import BacktestSubmission
        # Two active, one completed.
        a = await store.create(BacktestSubmission.from_spec(
            BacktestSpec(strategy="rsi", symbols=["BTC/USDT"]),
        ))
        b = await store.create(BacktestSubmission.from_spec(
            BacktestSpec(strategy="ma", symbols=["ETH/USDT"]),
        ))
        c = await store.create(BacktestSubmission.from_spec(
            BacktestSpec(strategy="don", symbols=["SOL/USDT"]),
        ))
        await store.mark_completed(
            c, result_summary={"pnl_usd": 1.0},
        )
        assert all(i > 0 for i in (a, b, c))
        active = await store.list(active_only=True)
        assert {r.id for r in active} == {a, b}
        completed = await store.list(status=STATUS_COMPLETED)
        assert [r.id for r in completed] == [c]

    asyncio.run(_run())


# ------------------------------------------------------------- submitter


def test_submitter_enqueues_and_updates_status():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        q = InMemoryQueue()
        submitter = BacktestSubmitter(store, q, default_client_id="unit-test")
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        sub = await submitter.submit(spec)
        assert sub.id and sub.id > 0
        assert sub.status == "queued"
        assert sub.queue_job_id and sub.queue_job_id.startswith("mem-")
        assert q.jobs and q.jobs[0]["submission_id"] == sub.id
        assert q.jobs[0]["strategy"] == "rsi"

    asyncio.run(_run())


def test_submitter_returns_existing_on_dedupe():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        q = InMemoryQueue()
        submitter = BacktestSubmitter(store, q)
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        first = await submitter.submit(spec)
        repeat = await submitter.submit(spec)
        assert repeat.id == first.id
        # Queue must NOT have been enqueued a second time.
        assert len(q.jobs) == 1

    asyncio.run(_run())


def test_submitter_without_queue_records_submitted_only():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        submitter = BacktestSubmitter(store, queue=None)
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        sub = await submitter.submit(spec, enqueue=False)
        assert sub.id
        assert sub.status == STATUS_SUBMITTED
        assert sub.queue_job_id is None

    asyncio.run(_run())


# ---------------------------------------------------------------- hook


def test_worker_hook_drives_status_to_completion():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        submitter = BacktestSubmitter(store, InMemoryQueue())
        spec = BacktestSpec(strategy="rsi", symbols=["BTC/USDT"])
        sub = await submitter.submit(spec)
        hook = SubmissionWorkerHook(store)
        await hook.on_start(sub.id)
        r = await store.get(sub.id)
        assert r.status == "running"
        await hook.on_complete(
            sub.id, summary={"pnl_usd": 10.0},
            artefacts={"equity_curve": "/tmp/eq.csv"},
        )
        r = await store.get(sub.id)
        assert r.status == STATUS_COMPLETED
        assert r.artefacts["equity_curve"] == "/tmp/eq.csv"

    asyncio.run(_run())


def test_worker_hook_records_failure():
    async def _run():
        pool = InMemoryBacktestSubmitPool()
        store = BacktestSubmissionStore(pool)
        submitter = BacktestSubmitter(store, InMemoryQueue())
        sub = await submitter.submit(
            BacktestSpec(strategy="rsi", symbols=["BTC/USDT"]),
        )
        hook = SubmissionWorkerHook(store)
        await hook.on_fail(sub.id, error="boom")
        r = await store.get(sub.id)
        assert r.status == STATUS_FAILED
        assert r.error == "boom"

    asyncio.run(_run())


# ----------------------------------------------------------------- CLI


def _stdout(fn, *args, **kwargs):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def test_cli_migration_sql_smoke():
    rc, out = _stdout(backtest_cli.main, ["migration-sql"])
    assert rc == 0
    assert "public.backtest_submissions" in out


def test_cli_apply_migration_path_only():
    rc, out = _stdout(backtest_cli.main, ["apply-migration", "--path-only"])
    assert rc == 0
    assert str(MIGRATION_PATH) in out


def test_cli_submit_in_memory_returns_id():
    spec = json.dumps({
        "strategy": "rsi",
        "symbols": ["BTC/USDT"],
        "params": {"lookback": 14},
    })
    rc, out = _stdout(backtest_cli.main, [
        "submit", "--in-memory", "--spec", spec,
    ])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["submission"]["id"] >= 1
    assert parsed["submission"]["status"] == STATUS_SUBMITTED


def test_cli_demo_end_to_end():
    rc, out = _stdout(backtest_cli.main, ["demo"])
    assert rc == 0
    assert "submitting three backtests" in out
    assert "worker finished" in out
    assert "final statuses" in out
