"""
shared.backtest_submit.worker_hook — helpers for the existing
:mod:`shared.backtest.worker` process to update Phase 35 status.

The worker pulls an envelope from the Redis queue whose ``payload``
includes ``submission_id`` (set by :class:`BacktestSubmitter`). When
it starts, finishes, or fails a job, it calls the matching helper
here so the ``public.backtest_submissions`` row stays in sync.

Usage sketch (inside the worker loop)::

    env = queue.claim(worker_id)
    sid = (env.get("payload") or {}).get("submission_id")
    if sid:
        await hook.on_start(sid)
    try:
        summary = run(env)
        if sid:
            await hook.on_complete(sid, summary=summary)
    except Exception as e:
        if sid:
            await hook.on_fail(sid, error=str(e))
        raise
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from shared.backtest_submit.store import BacktestSubmissionStore

LOG = logging.getLogger("tickles.backtest_submit.worker_hook")


class SubmissionWorkerHook:
    def __init__(self, store: BacktestSubmissionStore) -> None:
        self._store = store

    async def on_start(self, submission_id: int) -> None:
        try:
            await self._store.mark_running(int(submission_id))
        except Exception:  # pragma: no cover - worker must not die
            LOG.exception("on_start(%s) failed", submission_id)

    async def on_complete(
        self,
        submission_id: int,
        *,
        summary: Dict[str, Any],
        artefacts: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            await self._store.mark_completed(
                int(submission_id),
                result_summary=summary, artefacts=artefacts,
            )
        except Exception:  # pragma: no cover
            LOG.exception("on_complete(%s) failed", submission_id)

    async def on_fail(
        self, submission_id: int, *, error: str,
    ) -> None:
        try:
            await self._store.mark_failed(
                int(submission_id), error=str(error),
            )
        except Exception:  # pragma: no cover
            LOG.exception("on_fail(%s) failed", submission_id)

    async def on_cancel(
        self, submission_id: int, *, reason: Optional[str] = None,
    ) -> None:
        try:
            await self._store.mark_cancelled(
                int(submission_id), reason=reason,
            )
        except Exception:  # pragma: no cover
            LOG.exception("on_cancel(%s) failed", submission_id)


__all__ = ["SubmissionWorkerHook"]
