"""
shared.altdata.service — AltDataIngestor.

Given a list of :class:`AltDataSource` instances and a
:class:`AltDataStore`, a tick:

1. Calls ``source.fetch()`` on each source.
2. Validates + normalises items (freezing ``as_of`` to UTC).
3. Persists via :class:`AltDataStore.insert_item` (ON CONFLICT DO NOTHING).
4. Returns an ingest report (attempted / inserted / skipped per source).

The ingestor is deterministic for a fixed input and safe to call from a
service loop or CLI one-shot.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from shared.altdata.protocol import AltDataItem, AltDataSource
from shared.altdata.store import AltDataRow, AltDataStore

LOG = logging.getLogger("tickles.altdata.service")


@dataclass
class IngestReport:
    attempted: int = 0
    inserted: int = 0
    skipped: int = 0
    per_source: Dict[str, Dict[str, int]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempted": self.attempted,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "per_source": self.per_source,
            "errors": list(self.errors),
        }


class AltDataIngestor:
    def __init__(
        self,
        sources: Sequence[AltDataSource],
        store: Optional[AltDataStore] = None,
    ) -> None:
        self._sources: List[AltDataSource] = list(sources)
        self._store = store

    # ------------------------------------------------------------------

    def sources(self) -> List[AltDataSource]:
        return list(self._sources)

    def add_source(self, source: AltDataSource) -> None:
        self._sources.append(source)

    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(item: AltDataItem) -> AltDataItem:
        as_of = item.as_of
        if as_of is None:
            as_of = datetime.now(timezone.utc)
        elif as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        item.as_of = as_of
        return item

    async def tick(self, persist: bool = True) -> IngestReport:
        report = IngestReport()

        for src in self._sources:
            bucket = report.per_source.setdefault(src.name, {
                "attempted": 0, "inserted": 0, "skipped": 0,
            })
            try:
                raw = await src.fetch()
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("altdata source %s failed", src.name)
                report.errors.append(f"{src.name}: {exc!r}")
                continue

            items = [self._normalise(it) for it in raw or []]
            bucket["attempted"] += len(items)
            report.attempted += len(items)

            if not persist or self._store is None:
                bucket["skipped"] += len(items)
                report.skipped += len(items)
                continue

            for it in items:
                rid = await self._store.insert_item(it)
                if rid:
                    bucket["inserted"] += 1
                    report.inserted += 1
                else:
                    bucket["skipped"] += 1
                    report.skipped += 1

        return report

    # ------------------------------------------------------------------

    async def latest(self, **kwargs: Any) -> List[AltDataRow]:
        if self._store is None:
            return []
        return await self._store.list_latest(**kwargs)

    async def history(self, **kwargs: Any) -> List[AltDataRow]:
        if self._store is None:
            return []
        return await self._store.list_items(**kwargs)


__all__ = ["AltDataIngestor", "IngestReport"]
