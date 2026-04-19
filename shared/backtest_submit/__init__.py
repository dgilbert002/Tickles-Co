"""
shared.backtest_submit — Phase 35 Local-to-VPS Backtest Submission.

Durable audit + status layer on top of the Phase-16 Redis backtest
queue. The local CLI inserts a row into
:table:`public.backtest_submissions`, optionally enqueues the job
through :class:`shared.backtest.queue.BacktestQueue`, and then polls
the submission table for progress/results.
"""
from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parent / "migrations"
    / "2026_04_19_phase35_submissions.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


from shared.backtest_submit.protocol import (  # noqa: E402
    ACTIVE_STATUSES,
    BacktestSpec,
    BacktestSubmission,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUBMITTED,
    SUBMISSION_STATUSES,
    TERMINAL_STATUSES,
)
from shared.backtest_submit.store import (  # noqa: E402
    BacktestSubmissionStore,
)
from shared.backtest_submit.submitter import (  # noqa: E402
    BacktestSubmitter,
    QueueProtocol,
)

__all__ = [
    "ACTIVE_STATUSES",
    "BacktestSpec",
    "BacktestSubmission",
    "BacktestSubmissionStore",
    "BacktestSubmitter",
    "MIGRATION_PATH",
    "QueueProtocol",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUBMITTED",
    "SUBMISSION_STATUSES",
    "TERMINAL_STATUSES",
    "read_migration_sql",
]
