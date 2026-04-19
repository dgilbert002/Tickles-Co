"""In-memory pool for Phase 29 alt-data tests and offline CLI usage."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


class InMemoryAltDataPool:
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
        raise NotImplementedError(f"InMemoryAltDataPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("INSERT INTO public.alt_data_items"):
            (
                source, provider, universe, exchange, symbol, scope_key,
                metric, value_numeric, value_text, unit, as_of, payload, metadata,
            ) = params
            key = (source, provider, scope_key, metric, as_of)
            for r in self.rows:
                if (
                    r["source"], r["provider"], r["scope_key"],
                    r["metric"], r["as_of"],
                ) == key:
                    return None
            rid = next(self._seq)
            self.rows.append({
                "id": rid,
                "source": source,
                "provider": provider,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "scope_key": scope_key,
                "metric": metric,
                "value_numeric": None if value_numeric is None else float(value_numeric),
                "value_text": value_text,
                "unit": unit,
                "as_of": as_of,
                "ingested_at": self._now(),
                "payload": self._loads(payload),
                "metadata": self._loads(metadata),
            })
            return {"id": rid}
        raise NotImplementedError(f"InMemoryAltDataPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()
        if sql.startswith("SELECT * FROM public.alt_data_items") or sql.startswith(
            "SELECT * FROM public.alt_data_latest"
        ):
            rows = list(self.rows)
            p_idx = 0
            for field_name in ("source", "provider", "exchange", "symbol", "metric"):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            if "as_of >= $" in sql:
                since = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["as_of"] >= since]
            limit = int(params[p_idx])
            if sql.startswith("SELECT * FROM public.alt_data_latest"):
                # Dedupe: latest per (scope_key, metric)
                latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
                for r in rows:
                    k = (r["scope_key"], r["metric"])
                    prev = latest.get(k)
                    if prev is None or (r["as_of"], r["id"]) > (prev["as_of"], prev["id"]):
                        latest[k] = r
                rows = list(latest.values())
            rows.sort(key=lambda r: r["as_of"], reverse=True)
            return [dict(r) for r in rows[:limit]]
        raise NotImplementedError(f"InMemoryAltDataPool.fetch_all: {sql!r}")


__all__ = ["InMemoryAltDataPool"]
