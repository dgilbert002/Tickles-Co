"""Phase 33 — Cross-exchange arbitrage scanner."""
from __future__ import annotations

from pathlib import Path

from shared.arb.fetchers import (
    CcxtQuoteFetcher,
    OfflineQuoteFetcher,
    QuoteFetcher,
)
from shared.arb.memory_pool import InMemoryArbPool
from shared.arb.protocol import (
    ArbOpportunity,
    ArbQuote,
    ArbVenue,
    Side,
)
from shared.arb.scanner import ArbScanner, ScannerConfig
from shared.arb.service import ArbService
from shared.arb.store import ArbStore

MIGRATION_PATH = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase33_arb.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "ArbOpportunity",
    "ArbQuote",
    "ArbScanner",
    "ArbService",
    "ArbStore",
    "ArbVenue",
    "CcxtQuoteFetcher",
    "InMemoryArbPool",
    "MIGRATION_PATH",
    "OfflineQuoteFetcher",
    "QuoteFetcher",
    "ScannerConfig",
    "Side",
    "read_migration_sql",
]
