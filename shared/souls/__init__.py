"""Phase 31 — Apex / Quant / Ledger modernised souls."""
from __future__ import annotations

from pathlib import Path

from shared.souls.memory_pool import InMemorySoulsPool
from shared.souls.personas import ApexSoul, LedgerSoul, QuantSoul
from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    MODE_HYBRID,
    MODE_LLM,
    ROLE_BOOKKEEPER,
    ROLE_DECISION,
    ROLE_RESEARCH,
    SOUL_APEX,
    SOUL_LEDGER,
    SOUL_NAMES,
    SOUL_QUANT,
    VERDICT_APPROVE,
    VERDICT_DEFER,
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

MIGRATION_PATH = Path(__file__).parent / "migrations" / "2026_04_19_phase31_souls.sql"


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "ApexSoul",
    "LedgerSoul",
    "QuantSoul",
    "SoulContext",
    "SoulDecision",
    "SoulDecisionRow",
    "SoulPersona",
    "SoulPrompt",
    "SoulsConfig",
    "SoulsService",
    "SoulsStore",
    "InMemorySoulsPool",
    "SOUL_APEX",
    "SOUL_QUANT",
    "SOUL_LEDGER",
    "SOUL_NAMES",
    "ROLE_DECISION",
    "ROLE_RESEARCH",
    "ROLE_BOOKKEEPER",
    "MODE_DETERMINISTIC",
    "MODE_LLM",
    "MODE_HYBRID",
    "VERDICT_APPROVE",
    "VERDICT_REJECT",
    "VERDICT_PROPOSE",
    "VERDICT_JOURNAL",
    "VERDICT_OBSERVE",
    "VERDICT_DEFER",
    "VERDICTS",
    "MIGRATION_PATH",
    "read_migration_sql",
]
