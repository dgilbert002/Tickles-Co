"""Phase 29 — Alt-data ingestion package."""
from __future__ import annotations

from pathlib import Path

from shared.altdata.memory_pool import InMemoryAltDataPool
from shared.altdata.protocol import (
    AltDataItem,
    AltDataSource,
    METRIC_ACTIVE_ADDRESSES,
    METRIC_FUNDING_RATE,
    METRIC_MACRO_VALUE,
    METRIC_NETFLOW_USD,
    METRIC_OI_CONTRACTS,
    METRIC_OI_USD,
    METRIC_SENTIMENT,
    SOURCE_CUSTOM,
    SOURCE_FUNDING_RATE,
    SOURCE_MACRO,
    SOURCE_ONCHAIN,
    SOURCE_OPEN_INTEREST,
    SOURCE_SOCIAL,
    SOURCE_TYPES,
)
from shared.altdata.service import AltDataIngestor, IngestReport
from shared.altdata.sources import (
    CcxtFundingRateSource,
    CcxtOpenInterestSource,
    ManualAltDataSource,
    StaticAltDataSource,
)
from shared.altdata.store import AltDataRow, AltDataStore

MIGRATION_PATH = Path(__file__).parent / "migrations" / "2026_04_19_phase29_altdata.sql"


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


__all__ = [
    "AltDataItem",
    "AltDataSource",
    "AltDataStore",
    "AltDataRow",
    "AltDataIngestor",
    "IngestReport",
    "InMemoryAltDataPool",
    "CcxtFundingRateSource",
    "CcxtOpenInterestSource",
    "ManualAltDataSource",
    "StaticAltDataSource",
    "METRIC_ACTIVE_ADDRESSES",
    "METRIC_FUNDING_RATE",
    "METRIC_MACRO_VALUE",
    "METRIC_NETFLOW_USD",
    "METRIC_OI_CONTRACTS",
    "METRIC_OI_USD",
    "METRIC_SENTIMENT",
    "SOURCE_CUSTOM",
    "SOURCE_FUNDING_RATE",
    "SOURCE_MACRO",
    "SOURCE_ONCHAIN",
    "SOURCE_OPEN_INTEREST",
    "SOURCE_SOCIAL",
    "SOURCE_TYPES",
    "MIGRATION_PATH",
    "read_migration_sql",
]
