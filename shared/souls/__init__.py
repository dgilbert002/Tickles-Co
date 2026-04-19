"""Phase 31 — Apex / Quant / Ledger modernised souls."""
from __future__ import annotations

from pathlib import Path

from shared.souls.memory_pool import InMemorySoulsPool
from shared.souls.personas import (
    ApexSoul,
    CuriositySoul,
    LedgerSoul,
    OptimiserSoul,
    QuantSoul,
    RegimeWatcherSoul,
    ScoutSoul,
)
from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    MODE_HYBRID,
    MODE_LLM,
    ROLE_BOOKKEEPER,
    ROLE_DECISION,
    ROLE_EXPLORER,
    ROLE_OPTIMISER,
    ROLE_REGIME_WATCHER,
    ROLE_RESEARCH,
    ROLE_SCOUT,
    SOUL_APEX,
    SOUL_CURIOSITY,
    SOUL_LEDGER,
    SOUL_NAMES,
    SOUL_OPTIMISER,
    SOUL_QUANT,
    SOUL_REGIME_WATCHER,
    SOUL_SCOUT,
    VERDICT_ALERT,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    VERDICT_EXPLORE,
    VERDICT_JOURNAL,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
    VERDICT_REJECT,
    VERDICTS,
    SoulContext,
    SoulDecision,
    SoulPersona,
    SoulPrompt,
)
from shared.souls.service import SoulsConfig, SoulsService
from shared.souls.store import SoulDecisionRow, SoulsStore
from shared.souls.store_phase32 import (
    OptimiserCandidate,
    OptimiserStore,
    RegimeTransition,
    RegimeTransitionStore,
    ScoutCandidate,
    ScoutStore,
)

MIGRATION_PATH = Path(__file__).parent / "migrations" / "2026_04_19_phase31_souls.sql"
MIGRATION_PATH_PHASE32 = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase32_scout.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def read_migration_sql_phase32() -> str:
    return MIGRATION_PATH_PHASE32.read_text(encoding="utf-8")


__all__ = [
    "ApexSoul",
    "CuriositySoul",
    "LedgerSoul",
    "OptimiserSoul",
    "QuantSoul",
    "RegimeWatcherSoul",
    "ScoutSoul",
    "SoulContext",
    "SoulDecision",
    "SoulDecisionRow",
    "SoulPersona",
    "SoulPrompt",
    "SoulsConfig",
    "SoulsService",
    "SoulsStore",
    "InMemorySoulsPool",
    "ScoutCandidate",
    "ScoutStore",
    "OptimiserCandidate",
    "OptimiserStore",
    "RegimeTransition",
    "RegimeTransitionStore",
    "SOUL_APEX",
    "SOUL_QUANT",
    "SOUL_LEDGER",
    "SOUL_SCOUT",
    "SOUL_CURIOSITY",
    "SOUL_OPTIMISER",
    "SOUL_REGIME_WATCHER",
    "SOUL_NAMES",
    "ROLE_DECISION",
    "ROLE_RESEARCH",
    "ROLE_BOOKKEEPER",
    "ROLE_SCOUT",
    "ROLE_EXPLORER",
    "ROLE_OPTIMISER",
    "ROLE_REGIME_WATCHER",
    "MODE_DETERMINISTIC",
    "MODE_LLM",
    "MODE_HYBRID",
    "VERDICT_APPROVE",
    "VERDICT_REJECT",
    "VERDICT_PROPOSE",
    "VERDICT_JOURNAL",
    "VERDICT_OBSERVE",
    "VERDICT_DEFER",
    "VERDICT_EXPLORE",
    "VERDICT_ALERT",
    "VERDICTS",
    "MIGRATION_PATH",
    "MIGRATION_PATH_PHASE32",
    "read_migration_sql",
    "read_migration_sql_phase32",
]
