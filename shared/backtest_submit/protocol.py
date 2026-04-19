"""
shared.backtest_submit.protocol — dataclasses for Phase 35.

Two types:

* :class:`BacktestSpec` — canonical job description. It's whatever the
  existing worker (:mod:`shared.backtest.worker`) already understands,
  wrapped in a dataclass with :meth:`canonical_payload` for reliable
  hashing.
* :class:`BacktestSubmission` — one row in :table:`public.backtest_submissions`.

The submission status lifecycle is:

  submitted → queued → running → completed
                              ↘ failed
                              ↘ cancelled
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


STATUS_SUBMITTED = "submitted"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_SUBMITTED, STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)
SUBMISSION_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES


@dataclass
class BacktestSpec:
    """Canonical backtest job description."""

    strategy: str
    symbols: List[str]
    timeframe: str = "1m"
    start: Optional[str] = None          # ISO date or timestamp
    end: Optional[str] = None
    engine: str = "classic"              # classic | vectorbt | nautilus
    params: Dict[str, Any] = field(default_factory=dict)
    capital_usd: float = 10_000.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def canonical_payload(self) -> Dict[str, Any]:
        """Deterministic dict representation used for hashing."""
        return {
            "strategy": self.strategy,
            "symbols": sorted(self.symbols),
            "timeframe": self.timeframe,
            "start": self.start, "end": self.end,
            "engine": self.engine,
            "params": _sort_dict(self.params),
            "capital_usd": float(self.capital_usd),
        }

    def hash(self) -> str:
        payload = self.canonical_payload()
        blob = json.dumps(payload, sort_keys=True,
                          separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestSubmission:
    id: Optional[int]
    spec: BacktestSpec
    spec_hash: str
    status: str = STATUS_SUBMITTED
    client_id: Optional[str] = None
    queue_job_id: Optional[str] = None
    result_summary: Optional[Dict[str, Any]] = None
    artefacts: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    company_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    submitted_at: Optional[datetime] = None
    queued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @classmethod
    def from_spec(
        cls,
        spec: BacktestSpec,
        *,
        client_id: Optional[str] = None,
        company_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "BacktestSubmission":
        return cls(
            id=None, spec=spec, spec_hash=spec.hash(),
            client_id=client_id, company_id=company_id,
            metadata=dict(metadata or {}),
        )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "spec_hash": self.spec_hash,
            "status": self.status,
            "client_id": self.client_id,
            "queue_job_id": self.queue_job_id,
            "result_summary": self.result_summary,
            "artefacts": self.artefacts,
            "error": self.error,
            "company_id": self.company_id,
            "metadata": self.metadata,
        }
        for key in ("submitted_at", "queued_at", "started_at",
                    "completed_at"):
            val = getattr(self, key)
            d[key] = val.isoformat() if isinstance(val, datetime) else val
        return d


def _sort_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: d[k] for k in sorted(d)}


__all__ = [
    "ACTIVE_STATUSES",
    "BacktestSpec",
    "BacktestSubmission",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "STATUS_SUBMITTED",
    "SUBMISSION_STATUSES",
    "TERMINAL_STATUSES",
]
