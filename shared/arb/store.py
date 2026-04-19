"""
shared.arb.store — async wrappers around the Phase 33 arb tables.

Symbol-agnostic. Every write is append-only except venues, which are
upserted by the ``(name, kind, company_id)`` tuple exposed by the
migration's unique index.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from shared.arb.protocol import ArbOpportunity, ArbVenue


class ArbStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------ venues

    async def upsert_venue(self, venue: ArbVenue) -> int:
        sql = (
            "INSERT INTO public.arb_venues "
            "(company_id, name, kind, taker_fee_bps, maker_fee_bps, "
            " enabled, metadata, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7, NOW()) "
            "ON CONFLICT (name, kind, COALESCE(company_id,'')) "
            "DO UPDATE SET "
            "  taker_fee_bps = EXCLUDED.taker_fee_bps, "
            "  maker_fee_bps = EXCLUDED.maker_fee_bps, "
            "  enabled       = EXCLUDED.enabled, "
            "  metadata      = EXCLUDED.metadata, "
            "  updated_at    = NOW() "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                venue.company_id, venue.name, venue.kind,
                float(venue.taker_fee_bps), float(venue.maker_fee_bps),
                bool(venue.enabled), json.dumps(venue.metadata or {}),
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_venues(
        self, *, enabled_only: bool = False,
    ) -> List[ArbVenue]:
        sql = "SELECT * FROM public.arb_venues"
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY name, kind"
        rows = await self._pool.fetch_all(sql, ())
        return [_venue_row(r) for r in rows]

    # ----------------------------------------------------------- opportunities

    async def record_opportunity(self, opp: ArbOpportunity) -> int:
        sql = (
            "INSERT INTO public.arb_opportunities "
            "(company_id, symbol, buy_venue, sell_venue, buy_ask, sell_bid, "
            " size_base, gross_bps, net_bps, est_profit_usd, fees_bps, "
            " correlation_id, metadata, observed_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                opp.company_id, opp.symbol, opp.buy_venue, opp.sell_venue,
                float(opp.buy_ask), float(opp.sell_bid), float(opp.size_base),
                float(opp.gross_bps), float(opp.net_bps),
                float(opp.est_profit_usd), float(opp.fees_bps),
                opp.correlation_id, json.dumps(opp.metadata or {}),
                opp.observed_at,
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_opportunities(
        self,
        *,
        symbol: Optional[str] = None,
        min_net_bps: Optional[float] = None,
        limit: int = 50,
    ) -> List[ArbOpportunity]:
        sql = "SELECT * FROM public.arb_opportunities WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if symbol is not None:
            sql += f" AND symbol = ${idx}"
            params.append(symbol)
            idx += 1
        if min_net_bps is not None:
            sql += f" AND net_bps >= ${idx}"
            params.append(float(min_net_bps))
            idx += 1
        sql += f" ORDER BY observed_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_opp_row(r) for r in rows]


# ---------------------------------------------------------------------------


def _venue_row(row: Dict[str, Any]) -> ArbVenue:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return ArbVenue(
        id=int(row["id"]), name=row["name"], kind=row["kind"],
        taker_fee_bps=float(row.get("taker_fee_bps") or 0.0),
        maker_fee_bps=float(row.get("maker_fee_bps") or 0.0),
        enabled=bool(row.get("enabled", True)),
        company_id=row.get("company_id"),
        metadata=md or {},
    )


def _opp_row(row: Dict[str, Any]) -> ArbOpportunity:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return ArbOpportunity(
        id=int(row["id"]),
        symbol=row["symbol"],
        buy_venue=row["buy_venue"], sell_venue=row["sell_venue"],
        buy_ask=float(row["buy_ask"]), sell_bid=float(row["sell_bid"]),
        size_base=float(row.get("size_base") or 0.0),
        gross_bps=float(row.get("gross_bps") or 0.0),
        net_bps=float(row.get("net_bps") or 0.0),
        est_profit_usd=float(row.get("est_profit_usd") or 0.0),
        fees_bps=float(row.get("fees_bps") or 0.0),
        company_id=row.get("company_id"),
        correlation_id=row.get("correlation_id"),
        metadata=md or {},
        observed_at=row.get("observed_at"),
    )


__all__ = ["ArbStore"]
