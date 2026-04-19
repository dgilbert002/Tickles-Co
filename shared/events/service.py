"""
shared.events.service — EventsCalendarService.

Orchestrates the :class:`EventsStore` and provides:

* ``upsert``           — persist a single :class:`EventRecord`.
* ``upsert_many``      — bulk upsert.
* ``active_at``        — list events whose window is active at ``when``.
* ``upcoming``         — next N days.
* ``active_windows``   — deduped :class:`EventWindow` list.
* ``any_active``       — True if any event blocks at ``when`` (scope filter
                         + min_importance).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional

from shared.events.protocol import EventRecord, EventWindow
from shared.events.store import EventsStore


class EventsCalendarService:
    def __init__(self, store: EventsStore) -> None:
        self._store = store

    async def upsert(self, event: EventRecord) -> int:
        return await self._store.upsert_event(event)

    async def upsert_many(self, events: Iterable[EventRecord]) -> int:
        n = 0
        for e in events:
            if await self._store.upsert_event(e):
                n += 1
        return n

    async def delete(self, event_id: int) -> int:
        return await self._store.delete_event(event_id)

    async def list(self, **kwargs) -> List[EventRecord]:
        return await self._store.list_events(**kwargs)

    async def active_at(
        self,
        when: Optional[datetime] = None,
        *,
        kind: Optional[str] = None,
        min_importance: Optional[int] = None,
    ) -> List[EventRecord]:
        return await self._store.list_active(
            now=when, kind=kind, min_importance=min_importance,
        )

    async def upcoming(
        self,
        when: Optional[datetime] = None,
        *,
        horizon_days: int = 7,
        kind: Optional[str] = None,
        min_importance: Optional[int] = None,
        limit: int = 200,
    ) -> List[EventRecord]:
        return await self._store.list_upcoming(
            now=when, horizon_days=horizon_days,
            kind=kind, min_importance=min_importance, limit=limit,
        )

    async def active_windows(
        self,
        when: Optional[datetime] = None,
        *,
        kind: Optional[str] = None,
        min_importance: Optional[int] = None,
    ) -> List[EventWindow]:
        events = await self.active_at(
            when=when, kind=kind, min_importance=min_importance,
        )
        return [e.window() for e in events]

    async def any_active(
        self,
        when: Optional[datetime] = None,
        *,
        universe: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        min_importance: Optional[int] = None,
    ) -> List[EventWindow]:
        """Return windows matching the scope filter."""
        whenu = when or datetime.now(timezone.utc)
        events = await self.active_at(
            when=whenu, min_importance=min_importance,
        )
        out: List[EventWindow] = []
        for ev in events:
            if universe is not None and ev.universe not in (None, universe):
                continue
            if exchange is not None and ev.exchange not in (None, exchange):
                continue
            if symbol is not None and ev.symbol not in (None, symbol):
                continue
            out.append(ev.window())
        return out


__all__ = ["EventsCalendarService"]
