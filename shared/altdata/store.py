"""
shared.altdata.store — DB wrapper for alt-data items.

Rows are append-only. Inserts dedupe on
(source, provider, scope_key, metric, as_of) and return ``0`` on
``ON CONFLICT DO NOTHING``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.altdata.protocol import AltDataItem


@dataclass
class AltDataRow:
    id: int
    source: str
    provider: str
    universe: Optional[str]
    exchange: Optional[str]
    symbol: Optional[str]
    scope_key: str
    metric: str
    value_numeric: Optional[float]
    value_text: Optional[str]
    unit: Optional[str]
    as_of: datetime
    ingested_at: datetime
    payload: Dict[str, Any]
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "provider": self.provider,
            "universe": self.universe,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "scope_key": self.scope_key,
            "metric": self.metric,
            "value_numeric": self.value_numeric,
            "value_text": self.value_text,
            "unit": self.unit,
            "as_of": self.as_of.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }


def _coerce_json(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


def _coerce_float(val: Any) -> Optional[float]:
    return None if val is None else float(val)


class AltDataStore:
    """Async DB wrapper for ``public.alt_data_items``."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------

    async def insert_item(self, item: AltDataItem) -> int:
        sql = (
            "INSERT INTO public.alt_data_items "
            "(source, provider, universe, exchange, symbol, scope_key, "
            " metric, value_numeric, value_text, unit, as_of, payload, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
            "ON CONFLICT (source, provider, scope_key, metric, as_of) DO NOTHING "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                item.source,
                item.provider,
                item.universe,
                item.exchange,
                item.symbol,
                item.scope_key,
                item.metric,
                item.value_numeric,
                item.value_text,
                item.unit,
                item.as_of,
                json.dumps(item.payload or {}),
                json.dumps(item.metadata or {}),
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def insert_items(self, items: List[AltDataItem]) -> int:
        n = 0
        for it in items:
            rid = await self.insert_item(it)
            if rid:
                n += 1
        return n

    # ------------------------------------------------------------------

    async def list_items(
        self,
        *,
        source: Optional[str] = None,
        provider: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        metric: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[AltDataRow]:
        sql = "SELECT * FROM public.alt_data_items WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (
            ("source", source),
            ("provider", provider),
            ("exchange", exchange),
            ("symbol", symbol),
            ("metric", metric),
        ):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        if since is not None:
            sql += f" AND as_of >= ${idx}"
            params.append(since)
            idx += 1
        sql += f" ORDER BY as_of DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._from_row(r) for r in rows]

    async def list_latest(
        self,
        *,
        source: Optional[str] = None,
        provider: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        metric: Optional[str] = None,
        limit: int = 100,
    ) -> List[AltDataRow]:
        sql = "SELECT * FROM public.alt_data_latest WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (
            ("source", source),
            ("provider", provider),
            ("exchange", exchange),
            ("symbol", symbol),
            ("metric", metric),
        ):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        sql += f" ORDER BY as_of DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._from_row(r) for r in rows]

    # ------------------------------------------------------------------

    @staticmethod
    def _from_row(row: Dict[str, Any]) -> AltDataRow:
        return AltDataRow(
            id=int(row["id"]),
            source=row["source"],
            provider=row["provider"],
            universe=row.get("universe"),
            exchange=row.get("exchange"),
            symbol=row.get("symbol"),
            scope_key=row["scope_key"],
            metric=row["metric"],
            value_numeric=_coerce_float(row.get("value_numeric")),
            value_text=row.get("value_text"),
            unit=row.get("unit"),
            as_of=row["as_of"],
            ingested_at=row["ingested_at"],
            payload=_coerce_json(row.get("payload")),
            metadata=_coerce_json(row.get("metadata")),
        )


__all__ = ["AltDataStore", "AltDataRow"]
