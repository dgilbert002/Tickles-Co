"""shared.events.store — DB wrapper for Phase 30 Events Calendar."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shared.events.protocol import EventRecord


def _coerce_json(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


class EventsStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------

    async def upsert_event(self, event: EventRecord) -> int:
        event.ensure_dedupe_key()
        sql = (
            "INSERT INTO public.events_calendar "
            "(kind, provider, name, universe, exchange, symbol, country, "
            " importance, event_time, window_before_minutes, "
            " window_after_minutes, payload, metadata, dedupe_key) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) "
            "ON CONFLICT (provider, dedupe_key) DO UPDATE SET "
            "  event_time = EXCLUDED.event_time, "
            "  window_before_minutes = EXCLUDED.window_before_minutes, "
            "  window_after_minutes = EXCLUDED.window_after_minutes, "
            "  importance = EXCLUDED.importance, "
            "  payload = EXCLUDED.payload, "
            "  metadata = EXCLUDED.metadata, "
            "  updated_at = NOW() "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                event.kind, event.provider, event.name, event.universe,
                event.exchange, event.symbol, event.country,
                int(event.importance),
                event.event_time,
                int(event.window_before_minutes),
                int(event.window_after_minutes),
                json.dumps(event.payload or {}),
                json.dumps(event.metadata or {}),
                event.dedupe_key,
            ),
        )
        rid = int(row["id"]) if row and "id" in row else 0
        if rid:
            event.id = rid
        return rid

    async def delete_event(self, event_id: int) -> int:
        sql = "DELETE FROM public.events_calendar WHERE id = $1"
        return await self._pool.execute(sql, (int(event_id),))

    # ------------------------------------------------------------------

    async def list_events(
        self,
        *,
        kind: Optional[str] = None,
        provider: Optional[str] = None,
        universe: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        min_importance: Optional[int] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 200,
    ) -> List[EventRecord]:
        sql = "SELECT * FROM public.events_calendar WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (
            ("kind", kind), ("provider", provider),
            ("universe", universe), ("exchange", exchange), ("symbol", symbol),
        ):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        if min_importance is not None:
            sql += f" AND importance >= ${idx}"
            params.append(int(min_importance))
            idx += 1
        if since is not None:
            sql += f" AND event_time >= ${idx}"
            params.append(since)
            idx += 1
        if until is not None:
            sql += f" AND event_time <= ${idx}"
            params.append(until)
            idx += 1
        sql += f" ORDER BY event_time ASC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._from_row(r) for r in rows]

    async def list_active(
        self,
        *,
        now: Optional[datetime] = None,
        kind: Optional[str] = None,
        min_importance: Optional[int] = None,
    ) -> List[EventRecord]:
        when = now or datetime.now(timezone.utc)
        # Pull a generous window around "when" then filter client-side so
        # the InMemory pool can match without a make_interval equivalent.
        since = when - timedelta(days=7)
        until = when + timedelta(days=7)
        events = await self.list_events(
            kind=kind, min_importance=min_importance,
            since=since, until=until, limit=1000,
        )
        return [e for e in events if e.is_active_at(when)]

    async def list_upcoming(
        self,
        *,
        now: Optional[datetime] = None,
        horizon_days: int = 7,
        kind: Optional[str] = None,
        min_importance: Optional[int] = None,
        limit: int = 200,
    ) -> List[EventRecord]:
        when = now or datetime.now(timezone.utc)
        return await self.list_events(
            kind=kind, min_importance=min_importance,
            since=when, until=when + timedelta(days=horizon_days),
            limit=limit,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _from_row(row: Dict[str, Any]) -> EventRecord:
        return EventRecord(
            id=int(row["id"]),
            kind=row["kind"],
            provider=row["provider"],
            name=row["name"],
            event_time=row["event_time"],
            window_before_minutes=int(row.get("window_before_minutes") or 0),
            window_after_minutes=int(row.get("window_after_minutes") or 0),
            universe=row.get("universe"),
            exchange=row.get("exchange"),
            symbol=row.get("symbol"),
            country=row.get("country"),
            importance=int(row.get("importance") or 1),
            payload=_coerce_json(row.get("payload")),
            metadata=_coerce_json(row.get("metadata")),
            dedupe_key=row.get("dedupe_key"),
        )


__all__ = ["EventsStore"]
