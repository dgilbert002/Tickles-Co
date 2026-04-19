"""
shared.souls.protocol — dataclasses and constants for the Tickles
Apex / Quant / Ledger souls (Phase 31).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Canonical names
# ---------------------------------------------------------------------------

SOUL_APEX = "apex"
SOUL_QUANT = "quant"
SOUL_LEDGER = "ledger"

SOUL_NAMES = {SOUL_APEX, SOUL_QUANT, SOUL_LEDGER}

# Modes
MODE_DETERMINISTIC = "deterministic"
MODE_LLM = "llm"
MODE_HYBRID = "hybrid"

# Roles
ROLE_DECISION = "decision"
ROLE_RESEARCH = "research"
ROLE_BOOKKEEPER = "bookkeeper"

# Verdicts (per role)
VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"
VERDICT_PROPOSE = "propose"
VERDICT_JOURNAL = "journal"
VERDICT_OBSERVE = "observe"
VERDICT_DEFER = "defer"

VERDICTS = {
    VERDICT_APPROVE, VERDICT_REJECT, VERDICT_PROPOSE,
    VERDICT_JOURNAL, VERDICT_OBSERVE, VERDICT_DEFER,
}


@dataclass
class SoulPersona:
    id: Optional[int]
    name: str
    role: str
    description: Optional[str] = None
    default_llm: Optional[str] = None
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SoulPrompt:
    id: Optional[int]
    persona_id: int
    version: int
    template: str
    variables: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SoulContext:
    """Frozen snapshot of the world the soul is reasoning about.

    Kept deliberately generic so deterministic and LLM souls share the
    same envelope. ``fields`` is intentionally jsonable.
    """

    correlation_id: str
    company_id: Optional[str] = None
    fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "company_id": self.company_id,
            "fields": dict(self.fields),
        }


@dataclass
class SoulDecision:
    persona_name: str
    verdict: str
    confidence: float = 0.0
    rationale: Optional[str] = None
    outputs: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    mode: str = MODE_DETERMINISTIC
    correlation_id: Optional[str] = None
    company_id: Optional[str] = None
    persona_id: Optional[int] = None
    decided_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["decided_at"] = self.decided_at.isoformat() if self.decided_at else None
        return d


__all__ = [
    "SoulPersona",
    "SoulPrompt",
    "SoulContext",
    "SoulDecision",
    "SOUL_APEX",
    "SOUL_QUANT",
    "SOUL_LEDGER",
    "SOUL_NAMES",
    "MODE_DETERMINISTIC",
    "MODE_LLM",
    "MODE_HYBRID",
    "ROLE_DECISION",
    "ROLE_RESEARCH",
    "ROLE_BOOKKEEPER",
    "VERDICT_APPROVE",
    "VERDICT_REJECT",
    "VERDICT_PROPOSE",
    "VERDICT_JOURNAL",
    "VERDICT_OBSERVE",
    "VERDICT_DEFER",
    "VERDICTS",
]
