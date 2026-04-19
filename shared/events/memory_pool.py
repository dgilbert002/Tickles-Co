"""In-memory pool for Phase 30 events tests."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryEventsPool:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self._seq = itertools.count(1)

    @staticmethod
    def _loads(val: Any) -> Dict[str, Any]:
        if val is None:
            return {}
        if isinstance(val, dict):
            return val
        try:
            return json.loads(val)
        except Exception:
            return {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()
        if sql.startswith("DELETE FROM public.events_calendar"):
            (event_id,) = params
            before = len(self.rows)
            self.rows = [r for r in self.rows if r["id"] != int(event_id)]
            return before - len(self.rows)
        raise NotImplementedError(f"InMemoryEventsPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("INSERT INTO public.events_calendar"):
            (
                kind, provider, name, universe, exchange, symbol, country,
                importance, event_time, wb, wa, payload, metadata, dedupe_key,
            ) = params
            for r in self.rows:
                if r["provider"] == provider and r["dedupe_key"] == dedupe_key:
                    r.update({
                        "event_time": event_time,
                        "window_before_minutes": int(wb),
                        "window_after_minutes": int(wa),
                        "importance": int(importance),
                        "payload": self._loads(payload),
                        "metadata": self._loads(metadata),
                        "updated_at": self._now(),
                    })
                    return {"id": r["id"]}
            rid = next(self._seq)
            self.rows.append({
                "id": rid,
                "kind": kind,
                "provider": provider,
                "name": name,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "country": country,
                "importance": int(importance),
                "event_time": event_time,
                "window_before_minutes": int(wb),
                "window_after_minutes": int(wa),
                "payload": self._loads(payload),
                "metadata": self._loads(metadata),
                "dedupe_key": dedupe_key,
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            return {"id": rid}
        raise NotImplementedError(f"InMemoryEventsPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("SELECT * FROM public.events_calendar"):
            rows = list(self.rows)
            p_idx = 0
            for field_name in (
                "kind", "provider", "universe", "exchange", "symbol",
            ):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            if "importance >= $" in sql:
                minimp = int(params[p_idx])
                p_idx += 1
                rows = [r for r in rows if int(r.get("importance") or 1) >= minimp]
            if "event_time >= $" in sql:
                since = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["event_time"] >= since]
            if "event_time <= $" in sql:
                until = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["event_time"] <= until]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["event_time"])
            return [dict(r) for r in rows[:limit]]
        raise NotImplementedError(f"InMemoryEventsPool.fetch_all: {sql!r}")


__all__ = ["InMemoryEventsPool"]
