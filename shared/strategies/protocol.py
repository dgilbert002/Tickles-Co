"""
shared.strategies.protocol — dataclasses for the Phase 34 composer.

A :class:`StrategyDescriptor` is a registered producer (arb, copy,
souls, custom). A :class:`StrategyIntent` is one candidate trade
that a producer wants the composer to consider. The composer
evaluates a list of intents through optional gates (Treasury /
Guardrails) and optionally hands survivors to a submit callable
(ExecutionRouter). Every intent is audit-logged in
:table:`public.strategy_intents` regardless of outcome.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


KIND_ARB = "arb"
KIND_COPY = "copy"
KIND_SOULS = "souls"
KIND_CUSTOM = "custom"
STRATEGY_KINDS = (KIND_ARB, KIND_COPY, KIND_SOULS, KIND_CUSTOM)

SIDE_BUY = "buy"
SIDE_SELL = "sell"

# --------------------------------------------------------------- intent status
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_SKIPPED = "skipped"
STATUS_DUPLICATE = "duplicate"
STATUS_FAILED = "failed"

INTENT_STATUSES = (
    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_SUBMITTED,
    STATUS_FILLED, STATUS_SKIPPED, STATUS_DUPLICATE, STATUS_FAILED,
)


@dataclass
class StrategyDescriptor:
    id: Optional[int]
    name: str
    kind: str
    description: str = ""
    enabled: bool = True
    priority: int = 100
    company_id: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyIntent:
    """A single candidate trade proposed by a producer."""

    id: Optional[int]
    strategy_name: str
    strategy_kind: str
    symbol: str
    side: str
    size_base: float
    notional_usd: float = 0.0
    venue: Optional[str] = None
    reference_price: Optional[float] = None
    status: str = STATUS_PENDING
    decision_reason: Optional[str] = None
    order_id: Optional[int] = None
    correlation_id: Optional[str] = None
    source_ref: Optional[str] = None
    priority_score: float = 0.0
    company_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    proposed_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for key in ("proposed_at", "decided_at", "submitted_at"):
            val = getattr(self, key)
            d[key] = val.isoformat() if isinstance(val, datetime) else val
        return d


@dataclass
class CompositionResult:
    """Summary of one composer tick."""

    proposed: int
    approved: int
    rejected: int
    submitted: int
    duplicate: int
    skipped: int
    failed: int
    intents: List[StrategyIntent]

    @classmethod
    def empty(cls) -> "CompositionResult":
        return cls(0, 0, 0, 0, 0, 0, 0, [])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposed": self.proposed,
            "approved": self.approved,
            "rejected": self.rejected,
            "submitted": self.submitted,
            "duplicate": self.duplicate,
            "skipped": self.skipped,
            "failed": self.failed,
            "intents": [i.to_dict() for i in self.intents],
        }


__all__ = [
    "CompositionResult",
    "INTENT_STATUSES",
    "KIND_ARB",
    "KIND_COPY",
    "KIND_CUSTOM",
    "KIND_SOULS",
    "SIDE_BUY",
    "SIDE_SELL",
    "STATUS_APPROVED",
    "STATUS_DUPLICATE",
    "STATUS_FAILED",
    "STATUS_FILLED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
    "STATUS_SKIPPED",
    "STATUS_SUBMITTED",
    "STRATEGY_KINDS",
    "StrategyDescriptor",
    "StrategyIntent",
]
