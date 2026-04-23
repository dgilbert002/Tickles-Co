"""
shared.copy_trader.store — async wrappers around the Phase 33 copy tables.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from shared.copy_trader_trader.protocol import CopySource, CopyTrade


class CopyStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ---------------------------------------------------------------- sources

    async def upsert_source(self, source: CopySource) -> int:
        sql = (
            "INSERT INTO public.copy_sources "
            "(company_id, name, kind, venue, identifier, size_mode, "
            " size_value, max_notional_usd, symbol_whitelist, "
            " symbol_blacklist, enabled, metadata, last_checked_at, "
            " updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW()) "
            "ON CONFLICT (kind, COALESCE(venue,''), identifier, "
            "             COALESCE(company_id,'')) "
            "DO UPDATE SET "
            "  name              = EXCLUDED.name, "
            "  size_mode         = EXCLUDED.size_mode, "
            "  size_value        = EXCLUDED.size_value, "
            "  max_notional_usd  = EXCLUDED.max_notional_usd, "
            "  symbol_whitelist  = EXCLUDED.symbol_whitelist, "
            "  symbol_blacklist  = EXCLUDED.symbol_blacklist, "
            "  enabled           = EXCLUDED.enabled, "
            "  metadata          = EXCLUDED.metadata, "
            "  updated_at        = NOW() "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                source.company_id, source.name, source.kind, source.venue,
                source.identifier, source.size_mode,
                float(source.size_value or 0.0),
                None if source.max_notional_usd is None
                else float(source.max_notional_usd),
                json.dumps(list(source.symbol_whitelist or [])),
                json.dumps(list(source.symbol_blacklist or [])),
                bool(source.enabled),
                json.dumps(source.metadata or {}),
                source.last_checked_at,
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_sources(
        self, *, enabled_only: bool = False,
    ) -> List[CopySource]:
        sql = "SELECT * FROM public.copy_sources"
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY name"
        rows = await self._pool.fetch_all(sql, ())
        return [_source_row(r) for r in rows]

    async def get_source(self, source_id: int) -> Optional[CopySource]:
        sql = "SELECT * FROM public.copy_sources WHERE id = $1"
        row = await self._pool.fetch_one(sql, (int(source_id),))
        return _source_row(row) if row else None

    async def touch_source(self, source_id: int) -> None:
        sql = (
            "UPDATE public.copy_sources SET last_checked_at = NOW(), "
            " updated_at = NOW() WHERE id = $1"
        )
        await self._pool.execute(sql, (int(source_id),))

    # ------------------------------------------------------------------ trades

    async def record_trade(self, trade: CopyTrade) -> int:
        sql = (
            "INSERT INTO public.copy_trades "
            "(source_id, company_id, source_fill_id, source_trade_ts, "
            " symbol, side, source_price, source_qty_base, "
            " source_notional_usd, mapped_qty_base, mapped_notional_usd, "
            " status, skip_reason, correlation_id, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
            "ON CONFLICT (source_id, source_fill_id) DO NOTHING "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                int(trade.source_id), trade.company_id, trade.source_fill_id,
                trade.source_trade_ts, trade.symbol, trade.side,
                None if trade.source_price is None else float(trade.source_price),
                None if trade.source_qty_base is None else float(trade.source_qty_base),
                None if trade.source_notional_usd is None else float(trade.source_notional_usd),
                float(trade.mapped_qty_base),
                float(trade.mapped_notional_usd),
                trade.status, trade.skip_reason, trade.correlation_id,
                json.dumps(trade.metadata or {}),
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_trades(
        self,
        *,
        source_id: Optional[int] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[CopyTrade]:
        sql = "SELECT * FROM public.copy_trades WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if source_id is not None:
            sql += f" AND source_id = ${idx}"
            params.append(int(source_id))
            idx += 1
        if status is not None:
            sql += f" AND status = ${idx}"
            params.append(status)
            idx += 1
        if symbol is not None:
            sql += f" AND symbol = ${idx}"
            params.append(symbol)
            idx += 1
        sql += f" ORDER BY created_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_trade_row(r) for r in rows]


# ---------------------------------------------------------------------------


def _source_row(row: Dict[str, Any]) -> CopySource:
    wl = row.get("symbol_whitelist")
    bl = row.get("symbol_blacklist")
    if isinstance(wl, str):
        try:
            wl = json.loads(wl)
        except Exception:
            wl = []
    if isinstance(bl, str):
        try:
            bl = json.loads(bl)
        except Exception:
            bl = []
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return CopySource(
        id=int(row["id"]), name=row["name"], kind=row["kind"],
        venue=row.get("venue"), identifier=row["identifier"],
        size_mode=row.get("size_mode") or "ratio",
        size_value=float(row.get("size_value") or 0.0),
        max_notional_usd=(
            None if row.get("max_notional_usd") is None
            else float(row["max_notional_usd"])
        ),
        symbol_whitelist=list(wl or []),
        symbol_blacklist=list(bl or []),
        enabled=bool(row.get("enabled", True)),
        company_id=row.get("company_id"),
        metadata=md or {},
        last_checked_at=row.get("last_checked_at"),
    )


def _trade_row(row: Dict[str, Any]) -> CopyTrade:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return CopyTrade(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        source_fill_id=row["source_fill_id"],
        source_trade_ts=row["source_trade_ts"],
        symbol=row["symbol"],
        side=row["side"],
        source_price=(
            None if row.get("source_price") is None
            else float(row["source_price"])
        ),
        source_qty_base=(
            None if row.get("source_qty_base") is None
            else float(row["source_qty_base"])
        ),
        source_notional_usd=(
            None if row.get("source_notional_usd") is None
            else float(row["source_notional_usd"])
        ),
        mapped_qty_base=float(row.get("mapped_qty_base") or 0.0),
        mapped_notional_usd=float(row.get("mapped_notional_usd") or 0.0),
        status=row.get("status") or "pending",
        skip_reason=row.get("skip_reason"),
        company_id=row.get("company_id"),
        correlation_id=row.get("correlation_id"),
        metadata=md or {},
    )


__all__ = ["CopyStore"]
