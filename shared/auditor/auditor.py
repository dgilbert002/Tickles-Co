"""ContinuousAuditor — the loop that runs parity checks on a schedule.

Responsibilities:

  * Keep a catalogue of (strategy_id, candles_loader, strategy_fn,
    engines) tuples to audit.
  * On a configurable interval, run the ``ParityComparator`` on each
    entry and persist the records.
  * Emit ``HEARTBEAT`` records so a downstream dashboard can tell the
    auditor is alive even when nothing is failing.
  * Be trivially embeddable: you can run it from ``auditor_cli run``,
    from a systemd unit, or call ``tick()`` once from a cron job.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

import pandas as pd

from shared.auditor.comparator import ParityCheckInput, ParityComparator
from shared.auditor.schema import AuditEventType, AuditRecord, AuditSeverity
from shared.auditor.storage import AuditStore
from shared.backtest.engines.protocol import StrategyFn

log = logging.getLogger("tickles.auditor")


@dataclass
class AuditJob:
    """One auditable strategy slot.

    ``candles_loader`` must return a DataFrame whenever the auditor
    decides it's time to run. Keeping it a callable (rather than a
    DataFrame) means the auditor can request the latest candles
    lazily — either from the ``candles`` DB, a parquet file, or the
    synthetic test feed.
    """

    strategy_id: str
    strategy_fn: StrategyFn
    candles_loader: Callable[[], pd.DataFrame]
    cfg: Any
    engines: Optional[List[str]] = None


class ContinuousAuditor:
    def __init__(
        self,
        store: AuditStore,
        jobs: Optional[List[AuditJob]] = None,
        comparator: Optional[ParityComparator] = None,
        heartbeat_every_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.jobs: List[AuditJob] = list(jobs or [])
        self.comparator = comparator or ParityComparator()
        self.heartbeat_every_seconds = heartbeat_every_seconds
        self._last_heartbeat_ts: float = 0.0

    def add_job(self, job: AuditJob) -> None:
        self.jobs.append(job)

    def tick(self) -> List[AuditRecord]:
        """Run one auditing pass over every registered job.

        Returns the records that were persisted. Safe to call from a
        cron-style scheduler.
        """
        written: List[AuditRecord] = []

        if not self.jobs:
            rec = AuditRecord(
                event_type=AuditEventType.HEARTBEAT,
                severity=AuditSeverity.WARNING,
                subject="no_jobs_registered",
                passed=False,
            )
            self.store.record(rec)
            return [rec]

        per_job_records = 0
        for job in self.jobs:
            try:
                df = job.candles_loader()
                inp = ParityCheckInput(
                    strategy_id=job.strategy_id,
                    candles_df=df,
                    strategy=job.strategy_fn,
                    cfg=job.cfg,
                    engines=job.engines,
                )
                recs = self.comparator.compare(inp)
            except Exception as exc:  # pragma: no cover
                log.exception("audit job %s failed: %s", job.strategy_id, exc)
                recs = [
                    AuditRecord(
                        event_type=AuditEventType.PARITY_CHECK,
                        severity=AuditSeverity.CRITICAL,
                        subject=job.strategy_id,
                        strategy_id=job.strategy_id,
                        passed=False,
                        details={"error": str(exc), "type": type(exc).__name__},
                    )
                ]
            for r in recs:
                self.store.record(r)
                written.append(r)
                per_job_records += 1

        # Always emit a coverage event so dashboards can see the last run.
        cov = AuditRecord(
            event_type=AuditEventType.COVERAGE,
            severity=AuditSeverity.OK,
            subject="continuous_auditor",
            passed=True,
            metric=float(len(self.jobs)),
            details={
                "jobs_audited": len(self.jobs),
                "records_written": per_job_records,
            },
        )
        self.store.record(cov)
        written.append(cov)

        hb = self._maybe_heartbeat()
        if hb is not None:
            written.append(hb)
        return written

    def _maybe_heartbeat(self) -> Optional[AuditRecord]:
        now = time.time()
        if now - self._last_heartbeat_ts < self.heartbeat_every_seconds:
            return None
        self._last_heartbeat_ts = now
        rec = AuditRecord(
            event_type=AuditEventType.HEARTBEAT,
            severity=AuditSeverity.OK,
            subject="continuous_auditor",
            passed=True,
            details={"jobs": len(self.jobs)},
        )
        self.store.record(rec)
        return rec

    def run_forever(self, interval_seconds: float = 60.0) -> None:  # pragma: no cover
        """Blocking main loop. Meant for systemd / CLI `run` subcommand."""
        log.info("ContinuousAuditor starting; %d jobs, interval=%ss",
                 len(self.jobs), interval_seconds)
        while True:
            try:
                self.tick()
            except KeyboardInterrupt:
                log.info("interrupted — stopping auditor")
                return
            except Exception as exc:
                log.exception("auditor tick crashed: %s", exc)
            time.sleep(interval_seconds)
