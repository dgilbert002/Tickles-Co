"""
shared.altdata.protocol — core types for Phase 29 Alt-Data Ingestion.

Everything flows as :class:`AltDataItem` rows. Sources are plugins that
implement the :class:`AltDataSource` protocol and yield items from an
async ``fetch`` coroutine. The service persists them into
``public.alt_data_items`` deduping on (source, provider, scope_key,
metric, as_of).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Dict, List, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Source categories. Freeform strings, but we keep a canonical list so
# that analytics / strategies can filter cleanly.
# ---------------------------------------------------------------------------

SOURCE_FUNDING_RATE = "funding_rate"
SOURCE_OPEN_INTEREST = "open_interest"
SOURCE_SOCIAL = "social"
SOURCE_ONCHAIN = "onchain"
SOURCE_MACRO = "macro"
SOURCE_CUSTOM = "custom"

SOURCE_TYPES = {
    SOURCE_FUNDING_RATE,
    SOURCE_OPEN_INTEREST,
    SOURCE_SOCIAL,
    SOURCE_ONCHAIN,
    SOURCE_MACRO,
    SOURCE_CUSTOM,
}

# Common metric names (optional; free-form allowed)
METRIC_FUNDING_RATE = "funding_rate"
METRIC_OI_USD = "oi_usd"
METRIC_OI_CONTRACTS = "oi_contracts"
METRIC_SENTIMENT = "sentiment_score"
METRIC_ACTIVE_ADDRESSES = "active_addresses"
METRIC_NETFLOW_USD = "netflow_usd"
METRIC_MACRO_VALUE = "macro_value"


@dataclass
class AltDataItem:
    """A single alt-data measurement."""

    source: str
    provider: str
    scope_key: str
    metric: str
    as_of: datetime
    value_numeric: Optional[float] = None
    value_text: Optional[str] = None
    unit: Optional[str] = None
    universe: Optional[str] = None
    exchange: Optional[str] = None
    symbol: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


@runtime_checkable
class AltDataSource(Protocol):
    """Contract for an alt-data source plugin.

    A source has a stable ``name`` (used in ``provider``) and ``category``
    (one of ``SOURCE_TYPES``). ``fetch`` returns a list of items and
    may be called repeatedly by the service (once per tick).
    """

    name: str
    category: str

    def fetch(self) -> Awaitable[List[AltDataItem]]:  # pragma: no cover - protocol
        ...


__all__ = [
    "AltDataItem",
    "AltDataSource",
    "SOURCE_FUNDING_RATE",
    "SOURCE_OPEN_INTEREST",
    "SOURCE_SOCIAL",
    "SOURCE_ONCHAIN",
    "SOURCE_MACRO",
    "SOURCE_CUSTOM",
    "SOURCE_TYPES",
    "METRIC_FUNDING_RATE",
    "METRIC_OI_USD",
    "METRIC_OI_CONTRACTS",
    "METRIC_SENTIMENT",
    "METRIC_ACTIVE_ADDRESSES",
    "METRIC_NETFLOW_USD",
    "METRIC_MACRO_VALUE",
]
