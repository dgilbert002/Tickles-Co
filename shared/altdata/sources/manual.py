"""Manual alt-data source — items are pushed in via ``push()``.

Useful for operator-driven data (a macro release you paste into the
console, a manual sentiment override, etc.). The CLI / API layers own
the session; the ingestor just drains what's queued.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from shared.altdata.protocol import AltDataItem, SOURCE_CUSTOM


@dataclass
class ManualAltDataSource:
    name: str = "manual"
    category: str = SOURCE_CUSTOM
    queue: List[AltDataItem] = field(default_factory=list)

    def push(self, item: AltDataItem) -> None:
        self.queue.append(item)

    async def fetch(self) -> List[AltDataItem]:
        drained, self.queue = self.queue, []
        return drained


__all__ = ["ManualAltDataSource"]
