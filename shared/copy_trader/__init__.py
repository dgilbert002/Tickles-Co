"""Phase 33 — Copy-Trader."""
from __future__ import annotations

from pathlib import Path

from shared.copy_trader_trader.mapper import CopyMapper, MappingResult
from shared.copy_trader_trader.memory_pool import InMemoryCopyPool
from shared.copy_trader_trader.protocol import (
    SIDE_BUY,
    SIDE_SELL,
    CopySource,
    CopyTrade,
    SourceFill,
)
from shared.copy_trader_trader.service import CopyService
from shared.copy_trader_trader.sources import (
    BaseCopySource,
    CcxtCopySource,
    StaticCopySource,
)
from shared.copy_trader_trader.store import CopyStore

MIGRATION_PATH = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase33_copy.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "BaseCopySource",
    "CcxtCopySource",
    "CopyMapper",
    "CopyService",
    "CopySource",
    "CopyStore",
    "CopyTrade",
    "InMemoryCopyPool",
    "MIGRATION_PATH",
    "MappingResult",
    "SIDE_BUY",
    "SIDE_SELL",
    "SourceFill",
    "StaticCopySource",
    "read_migration_sql",
]
