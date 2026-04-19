"""
shared.trading — Phase 25 Banker + Treasury + Capabilities (+ pure sizer).

The public surface is deliberately small: most callers only ever need
``TradeIntent``, ``Treasury``, and the read-only helpers.
"""

from pathlib import Path

from shared.trading.capabilities import (
    SCOPE_AGENT,
    SCOPE_COMPANY,
    SCOPE_STRATEGY,
    SCOPE_VENUE,
    Capability,
    CapabilityCheck,
    CapabilityChecker,
    CapabilityStore,
    TradeIntent,
    default_capability,
)
from shared.trading.sizer import (
    AccountSnapshot,
    MarketSnapshot,
    SizedIntent,
    StrategyConfig,
    size_intent,
)
from shared.trading.banker import BalanceSnapshot, Banker
from shared.trading.treasury import (
    Treasury,
    TreasuryDecision,
    account_snapshot_from_balance,
)

MIGRATION_PATH = str(
    Path(__file__).resolve().parent
    / "migrations"
    / "2026_04_19_phase25_banker_treasury.sql"
)


def read_migration_sql() -> str:
    with open(MIGRATION_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "SCOPE_COMPANY",
    "SCOPE_STRATEGY",
    "SCOPE_AGENT",
    "SCOPE_VENUE",
    "TradeIntent",
    "Capability",
    "CapabilityCheck",
    "CapabilityChecker",
    "CapabilityStore",
    "default_capability",
    "MarketSnapshot",
    "AccountSnapshot",
    "StrategyConfig",
    "SizedIntent",
    "size_intent",
    "BalanceSnapshot",
    "Banker",
    "Treasury",
    "TreasuryDecision",
    "account_snapshot_from_balance",
]
