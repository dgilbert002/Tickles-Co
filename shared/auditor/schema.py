"""Audit schema primitives.

Every auditor check produces an ``AuditRecord``. Records are keyed by
``event_type`` (what kind of check), ``subject`` (what was checked —
strategy id, engine name, trade id, …) and ``severity`` (ok / warning
/ breach / critical). The ``details`` dict carries whatever extra
structured data the comparator emitted.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class AuditEventType(str, Enum):
    """What kind of check produced this record."""

    PARITY_CHECK = "parity_check"  # Phase 19 cross-engine parity
    LIVE_VS_BACKTEST = "live_vs_backtest"  # single live trade vs its replay
    FEE_DRIFT = "fee_drift"  # realised fee rate != backtest assumption
    SLIPPAGE_DRIFT = "slippage_drift"  # realised slippage != backtest assumption
    FILL_DRIFT = "fill_drift"  # fill price drifted from model
    HEARTBEAT = "heartbeat"  # continuous auditor liveness pulse
    COVERAGE = "coverage"  # how many strategies/engines were audited this run


class AuditSeverity(str, Enum):
    OK = "ok"
    WARNING = "warning"
    BREACH = "breach"
    CRITICAL = "critical"


@dataclass
class AuditRecord:
    """A single audit event.

    Stored verbatim in SQLite. ``details`` is JSON-encoded on the way
    in and decoded on the way out.
    """

    event_type: AuditEventType
    severity: AuditSeverity
    subject: str
    passed: bool
    metric: Optional[float] = None
    tolerance: Optional[float] = None
    strategy_id: Optional[str] = None
    engine: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    ts_unix: float = field(default_factory=time.time)
    id: Optional[int] = None  # populated after insert

    def to_json(self) -> str:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["severity"] = self.severity.value
        return json.dumps(d, sort_keys=True, default=str)

    def to_row(self) -> Dict[str, Any]:
        return {
            "ts_unix": self.ts_unix,
            "event_type": self.event_type.value,
            "severity": self.severity.value,
            "subject": self.subject,
            "strategy_id": self.strategy_id,
            "engine": self.engine,
            "passed": 1 if self.passed else 0,
            "metric": self.metric,
            "tolerance": self.tolerance,
            "details_json": json.dumps(self.details, sort_keys=True, default=str),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "AuditRecord":
        return cls(
            id=row.get("id"),
            ts_unix=float(row["ts_unix"]),
            event_type=AuditEventType(row["event_type"]),
            severity=AuditSeverity(row["severity"]),
            subject=row["subject"],
            strategy_id=row.get("strategy_id"),
            engine=row.get("engine"),
            passed=bool(row.get("passed", 0)),
            metric=row.get("metric"),
            tolerance=row.get("tolerance"),
            details=json.loads(row["details_json"]) if row.get("details_json") else {},
        )


@dataclass
class DivergenceSummary:
    """Rolling summary of audit activity over a window."""

    window_seconds: int
    total: int
    passed: int
    warnings: int
    breaches: int
    critical: int
    pass_rate: float
    last_event_ts: Optional[float] = None
    by_event_type: Dict[str, int] = field(default_factory=dict)
    by_engine: Dict[str, int] = field(default_factory=dict)
