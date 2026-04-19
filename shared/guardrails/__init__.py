"""Phase 28 Crash Protection — public API."""
from __future__ import annotations

from pathlib import Path

from shared.guardrails.evaluator import (
    REGIME_CRASH_LABELS,
    decisions_block_intent,
    evaluate,
)
from shared.guardrails.memory_pool import InMemoryGuardrailsPool
from shared.guardrails.protocol import (
    ACTION_ALERT,
    ACTION_FLATTEN_POSITIONS,
    ACTION_HALT_NEW_ORDERS,
    ACTION_TYPES,
    PositionSummary,
    ProtectionDecision,
    ProtectionRule,
    ProtectionSnapshot,
    RULE_DAILY_LOSS,
    RULE_EQUITY_DRAWDOWN,
    RULE_POSITION_NOTIONAL,
    RULE_REGIME_CRASH,
    RULE_STALE_DATA,
    RULE_TYPES,
    RegimeLabel,
    SEVERITIES,
    SEVERITY_CRITICAL,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    STATUS_OVERRIDDEN,
    STATUS_RESOLVED,
    STATUS_TRIGGERED,
    STATUSES,
)
from shared.guardrails.service import GuardrailsService
from shared.guardrails.store import GuardrailsStore, ProtectionEventRow

MIGRATION_PATH: Path = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase28_crash.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "evaluate",
    "decisions_block_intent",
    "REGIME_CRASH_LABELS",
    "ProtectionRule",
    "ProtectionSnapshot",
    "ProtectionDecision",
    "PositionSummary",
    "RegimeLabel",
    "GuardrailsService",
    "GuardrailsStore",
    "ProtectionEventRow",
    "InMemoryGuardrailsPool",
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
]
