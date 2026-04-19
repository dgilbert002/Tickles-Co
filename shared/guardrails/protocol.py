"""
shared.guardrails.protocol — public dataclasses, enums, and
constants for Phase 28 Crash Protection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# Rule types ---------------------------------------------------------------
RULE_REGIME_CRASH = "regime_crash"
RULE_EQUITY_DRAWDOWN = "equity_drawdown"
RULE_POSITION_NOTIONAL = "position_notional"
RULE_DAILY_LOSS = "daily_loss"
RULE_STALE_DATA = "stale_data"

RULE_TYPES = {
    RULE_REGIME_CRASH,
    RULE_EQUITY_DRAWDOWN,
    RULE_POSITION_NOTIONAL,
    RULE_DAILY_LOSS,
    RULE_STALE_DATA,
}

# Actions -----------------------------------------------------------------
ACTION_HALT_NEW_ORDERS = "halt_new_orders"
ACTION_FLATTEN_POSITIONS = "flatten_positions"
ACTION_ALERT = "alert"

ACTION_TYPES = {
    ACTION_HALT_NEW_ORDERS,
    ACTION_FLATTEN_POSITIONS,
    ACTION_ALERT,
}

# Severities --------------------------------------------------------------
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"

SEVERITIES = {
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SEVERITY_ERROR,
    SEVERITY_CRITICAL,
}

# Statuses ----------------------------------------------------------------
STATUS_TRIGGERED = "triggered"
STATUS_RESOLVED = "resolved"
STATUS_OVERRIDDEN = "overridden"

STATUSES = {STATUS_TRIGGERED, STATUS_RESOLVED, STATUS_OVERRIDDEN}


# Dataclasses -------------------------------------------------------------


@dataclass
class ProtectionRule:
    """Operator-configured trigger rule (matches the DB row)."""

    id: Optional[int]
    company_id: Optional[str]
    universe: Optional[str]
    exchange: Optional[str]
    symbol: Optional[str]
    rule_type: str
    action: str
    threshold: Optional[float]
    params: Dict[str, Any] = field(default_factory=dict)
    severity: str = SEVERITY_WARNING
    enabled: bool = True

    def scope_key(self) -> str:
        return "|".join([
            self.company_id or "*",
            self.universe or "*",
            self.exchange or "*",
            self.symbol or "*",
            self.rule_type,
        ])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PositionSummary:
    """Snapshot view of an open position used by the evaluator."""

    company_id: str
    exchange: str
    symbol: str
    direction: str
    quantity: float
    notional_usd: float
    realized_pnl_usd: float = 0.0
    unrealised_pnl_usd: float = 0.0


@dataclass
class RegimeLabel:
    """Latest regime classification feeding the evaluator."""

    universe: str
    exchange: str
    symbol: str
    timeframe: str
    classifier: str
    regime: str
    as_of: datetime


@dataclass
class ProtectionSnapshot:
    """Inputs the evaluator consumes on every tick."""

    company_id: str
    universe: Optional[str] = None
    equity_usd: Optional[float] = None
    equity_peak_usd: Optional[float] = None
    equity_daily_start_usd: Optional[float] = None
    positions: List[PositionSummary] = field(default_factory=list)
    regimes: List[RegimeLabel] = field(default_factory=list)
    last_tick_at: Optional[datetime] = None
    now: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProtectionDecision:
    """Evaluator output for a single rule evaluation."""

    rule: ProtectionRule
    status: str            # triggered / resolved
    reason: str
    metric: Optional[float] = None
    company_id: Optional[str] = None
    universe: Optional[str] = None
    exchange: Optional[str] = None
    symbol: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            **asdict(self),
            "rule": self.rule.to_dict(),
        }
        return payload


__all__ = [
    "RULE_REGIME_CRASH",
    "RULE_EQUITY_DRAWDOWN",
    "RULE_POSITION_NOTIONAL",
    "RULE_DAILY_LOSS",
    "RULE_STALE_DATA",
    "RULE_TYPES",
    "ACTION_HALT_NEW_ORDERS",
    "ACTION_FLATTEN_POSITIONS",
    "ACTION_ALERT",
    "ACTION_TYPES",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "SEVERITY_CRITICAL",
    "SEVERITIES",
    "STATUS_TRIGGERED",
    "STATUS_RESOLVED",
    "STATUS_OVERRIDDEN",
    "STATUSES",
    "ProtectionRule",
    "PositionSummary",
    "RegimeLabel",
    "ProtectionSnapshot",
    "ProtectionDecision",
]
