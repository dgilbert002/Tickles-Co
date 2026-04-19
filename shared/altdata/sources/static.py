"""Static / fixture alt-data source (deterministic; primarily for tests)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from shared.altdata.protocol import AltDataItem, SOURCE_CUSTOM


@dataclass
class StaticAltDataSource:
    """Return a fixed list of :class:`AltDataItem` every tick.

    Use this in tests, for seeded demo data, or for any source where the
    upstream push is handled outside the ingestor (and the items are
    later injected into the registry via ``items`` assignment).
    """

    name: str = "static"
    category: str = SOURCE_CUSTOM
    items: List[AltDataItem] = field(default_factory=list)

    async def fetch(self) -> List[AltDataItem]:
        return list(self.items)


__all__ = ["StaticAltDataSource"]
