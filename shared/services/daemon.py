"""
shared.services.daemon — generic long-running supervisor loop.

What it gives you
-----------------
A single async class (`ServiceDaemon`) that takes any "tick
function" (a coroutine returning a summary dict) and runs it on
a cadence with:

  * Graceful shutdown on SIGINT / SIGTERM.
  * Exponential backoff on consecutive failures (capped).
  * Jitter on the backoff so a zoo of daemons doesn't retry in
    lockstep against the same downstream (Redis, Postgres, an
    exchange).
  * Heartbeat events written to :class:`shared.auditor.AuditStore`
    so Phase 21's watchdog can prove that collectors are actually
    running — not just that systemd *thinks* they are.
  * A :class:`DaemonStats` snapshot exposed to the
    ``collectors_cli`` for ``status`` output.

Design choices
--------------
* We do not take a hard dependency on :mod:`shared.auditor` —
  heartbeats are best-effort. If the auditor tables don't exist
  yet, we log a debug line and continue. This keeps collectors
  workable even on a pristine box.
* We do not own signal handlers at import time. They are
  installed inside :meth:`ServiceDaemon.run_forever` so that
  tests (and `run_once`) don't steal SIGINT from pytest.
* Every tick returns a dict that gets merged into a rolling
  stats bag. The operator CLI reads that bag, not internal
  state.
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("tickles.services.daemon")

TickFn = Callable[[], Awaitable[Dict[str, Any]]]


@dataclass
class DaemonConfig:
    """Configuration for a :class:`ServiceDaemon`."""

    name: str
    interval_seconds: float = 60.0
    jitter_seconds: float = 2.0
    max_backoff_seconds: float = 300.0
    heartbeat_every_seconds: float = 30.0
    emit_heartbeats_to_auditor: bool = True

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must be >= 0")
        if self.max_backoff_seconds < self.interval_seconds:
            self.max_backoff_seconds = self.interval_seconds * 10.0


@dataclass
class DaemonStats:
    """Rolling stats exposed to operators."""

    name: str
    started_at: Optional[float] = None
    last_tick_at: Optional[float] = None
    last_tick_ok: Optional[bool] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_ticks: int = 0
    total_failures: int = 0
    last_summary: Dict[str, Any] = field(default_factory=dict)
    alive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "alive": self.alive,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "last_tick_ok": self.last_tick_ok,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_ticks": self.total_ticks,
            "total_failures": self.total_failures,
            "last_summary": self.last_summary,
        }


class ServiceDaemon:
    """Generic async supervisor loop.

    Parameters
    ----------
    config:
        Daemon configuration.
    tick:
        Coroutine that performs one unit of work. It must return
        a JSON-serialisable dict describing what happened (used
        for the last_summary exposed to the CLI).
    """

    def __init__(self, config: DaemonConfig, tick: TickFn) -> None:
        self.config = config
        self.tick = tick
        self.stats = DaemonStats(name=config.name)
        self._stop_event: Optional[asyncio.Event] = None
        self._last_heartbeat_ts: float = 0.0

    async def run_once(self) -> Dict[str, Any]:
        """Execute the tick exactly once. Used by tests + CLI."""
        start = time.time()
        try:
            summary = await self.tick()
            self.stats.total_ticks += 1
            self.stats.last_tick_at = time.time()
            self.stats.last_tick_ok = True
            self.stats.last_error = None
            self.stats.consecutive_failures = 0
            self.stats.last_summary = dict(summary or {})
            return self.stats.last_summary
        except Exception as exc:  # noqa: BLE001
            self.stats.total_ticks += 1
            self.stats.total_failures += 1
            self.stats.consecutive_failures += 1
            self.stats.last_tick_at = time.time()
            self.stats.last_tick_ok = False
            self.stats.last_error = repr(exc)
            logger.exception(
                "service '%s' tick failed (consecutive=%d)",
                self.config.name,
                self.stats.consecutive_failures,
            )
            return {"ok": False, "error": repr(exc), "elapsed": time.time() - start}

    async def run_forever(self) -> None:
        """Blocking supervisor loop. Honours SIGINT / SIGTERM."""
        self.stats.started_at = time.time()
        self.stats.alive = True
        self._stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            if self._stop_event and not self._stop_event.is_set():
                logger.info("service '%s' shutdown signal received", self.config.name)
                self._stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown)
            except (NotImplementedError, RuntimeError):
                pass

        try:
            while not self._stop_event.is_set():
                await self.run_once()
                self._maybe_heartbeat()
                wait_seconds = self._next_wait_seconds()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stats.alive = False
            logger.info("service '%s' stopped", self.config.name)

    def _next_wait_seconds(self) -> float:
        base = self.config.interval_seconds
        if self.stats.consecutive_failures > 0:
            backoff = min(
                self.config.max_backoff_seconds,
                self.config.interval_seconds * (2 ** self.stats.consecutive_failures),
            )
            base = max(base, backoff)
        jitter = random.uniform(0, self.config.jitter_seconds) if self.config.jitter_seconds else 0.0
        return float(base + jitter)

    def _maybe_heartbeat(self) -> None:
        if not self.config.emit_heartbeats_to_auditor:
            return
        now = time.time()
        if now - self._last_heartbeat_ts < self.config.heartbeat_every_seconds:
            return
        self._last_heartbeat_ts = now
        try:
            # Best-effort import so collectors work on boxes without the auditor.
            from shared.auditor import AuditStore
            from shared.auditor.schema import AuditEventType, AuditRecord, AuditSeverity

            severity = (
                AuditSeverity.BREACH if self.stats.consecutive_failures >= 3
                else AuditSeverity.WARNING if self.stats.consecutive_failures >= 1
                else AuditSeverity.OK
            )
            passed = severity == AuditSeverity.OK
            with AuditStore() as store:
                store.record(
                    AuditRecord(
                        event_type=AuditEventType.HEARTBEAT,
                        severity=severity,
                        subject=f"service:{self.config.name}",
                        passed=passed,
                        details={
                            "consecutive_failures": self.stats.consecutive_failures,
                            "total_ticks": self.stats.total_ticks,
                            "total_failures": self.stats.total_failures,
                            "last_summary": self.stats.last_summary,
                        },
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "service '%s' heartbeat to auditor failed (non-fatal): %s",
                self.config.name,
                exc,
            )
