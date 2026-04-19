"""
shared.strategies — Phase 34 Strategy Composer.

Aggregates candidate trade signals from every configured producer
(arb, copy, souls, custom) into the :table:`public.strategy_intents`
audit trail, applies an optional gate (Treasury + Guardrails in later
phases), and optionally hands survivors to a submit callable
(ExecutionRouter wiring).
"""
from __future__ import annotations

from pathlib import Path

MIGRATION_PATH = (
    Path(__file__).parent / "migrations"
    / "2026_04_19_phase34_strategies.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


from shared.strategies.composer import (  # noqa: E402
    ComposerConfig,
    GateDecision,
    StrategyComposer,
)
from shared.strategies.protocol import (  # noqa: E402
    CompositionResult,
    StrategyDescriptor,
    StrategyIntent,
)
from shared.strategies.store import StrategyStore  # noqa: E402


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "ComposerConfig",
    "CompositionResult",
    "GateDecision",
    "StrategyComposer",
    "StrategyDescriptor",
    "StrategyIntent",
    "StrategyStore",
]
