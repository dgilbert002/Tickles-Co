"""
shared.backtest_submit.submitter — ties the Phase 35 audit table to
the Phase 16 Redis queue.

Design note
-----------
We deliberately depend on a **protocol**, not the concrete
:class:`shared.backtest.queue.BacktestQueue`. This lets the local CLI
run with an in-memory queue (for smoke tests) and keeps the
submission layer importable even on machines where Redis isn't
available (the owner's laptop, for example).

The payload we enqueue is the canonical spec dict *plus* a
``submission_id`` field so the worker hook can correlate Redis
envelopes with submission rows.
"""
from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from shared.backtest_submit.protocol import (
    BacktestSpec,
    BacktestSubmission,
    STATUS_SUBMITTED,
)
from shared.backtest_submit.store import BacktestSubmissionStore

LOG = logging.getLogger("tickles.backtest_submit")


class QueueProtocol(Protocol):
    """Minimal Redis-queue interface the submitter expects."""

    def enqueue(
        self, job: Dict[str, Any], *, dedup: bool = True,
    ) -> Optional[str]: ...


class InMemoryQueue:
    """Test double that captures envelopes instead of touching Redis."""

    def __init__(self) -> None:
        self.jobs: List[Dict[str, Any]] = []
        self._seq = 0

    def enqueue(
        self, job: Dict[str, Any], *, dedup: bool = True,
    ) -> Optional[str]:
        self._seq += 1
        self.jobs.append(dict(job))
        return f"mem-{self._seq:06d}"

    def last(self) -> Optional[Dict[str, Any]]:
        return self.jobs[-1] if self.jobs else None


class BacktestSubmitter:
    def __init__(
        self,
        store: BacktestSubmissionStore,
        queue: Optional[QueueProtocol] = None,
        *,
        default_client_id: Optional[str] = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._default_client = (
            default_client_id or socket.gethostname() or "unknown"
        )

    @property
    def queue(self) -> Optional[QueueProtocol]:
        return self._queue

    async def submit(
        self,
        spec: BacktestSpec,
        *,
        client_id: Optional[str] = None,
        company_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        enqueue: bool = True,
    ) -> BacktestSubmission:
        sub = BacktestSubmission.from_spec(
            spec,
            client_id=client_id or self._default_client,
            company_id=company_id, metadata=metadata,
        )
        sub.status = STATUS_SUBMITTED
        sub.submitted_at = datetime.now(timezone.utc)

        new_id = await self._store.create(sub)
        if new_id == 0:
            existing = await self._store.get_by_hash(sub.spec_hash)
            if existing is not None:
                LOG.info(
                    "submit: hash %s already active/completed as id=%s",
                    sub.spec_hash[:12], existing.id,
                )
                return existing
            raise RuntimeError(
                "submit: insert returned 0 but no existing row found"
            )
        sub.id = new_id

        if enqueue and self._queue is not None:
            job = dict(spec.canonical_payload())
            job["submission_id"] = new_id
            try:
                qid = self._queue.enqueue(job, dedup=False)
            except Exception as e:
                await self._store.mark_failed(
                    new_id, error=f"enqueue_failed: {e!s}",
                )
                sub.status = "failed"
                sub.error = f"enqueue_failed: {e!s}"
                return sub
            if qid:
                await self._store.mark_queued(new_id, qid)
                sub.queue_job_id = qid
                sub.status = "queued"
        return sub


__all__ = ["BacktestSubmitter", "InMemoryQueue", "QueueProtocol"]
