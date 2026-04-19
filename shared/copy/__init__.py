"""Phase 33 — Copy-Trader."""
from __future__ import annotations

from pathlib import Path

from shared.copy.mapper import CopyMapper, MappingResult
from shared.copy.memory_pool import InMemoryCopyPool
from shared.copy.protocol import (
    SIDE_BUY,
    SIDE_SELL,
    CopySource,
    CopyTrade,
    SourceFill,
)
from shared.copy.service import CopyService
from shared.copy.sources import (
    BaseCopySource,
    CcxtCopySource,
    StaticCopySource,
)
from shared.copy.store import CopyStore

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
