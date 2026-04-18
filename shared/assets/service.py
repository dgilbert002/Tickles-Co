"""
shared.assets.service — async read/query API over the asset catalog tables.

Thin wrapper on top of shared.utils.db's DatabasePool so every caller gets the
same connection pool and the same row-to-pydantic mapping. No write methods —
loader.py owns upserts.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from shared.assets.schema import (
    Asset,
    AssetClass,
    InstrumentRef,
    Venue,
    VenueAssetRow,
)

log = logging.getLogger("tickles.assets.service")


class AssetCatalogService:
    """Async read-only accessor for venues / assets / instruments / v_asset_venues.

    Accepts any object exposing an async `fetch_all(sql, params=None)` and
    `fetch_one(sql, params=None)` returning row-dicts; the production pool in
    `shared.utils.db` matches that shape (asyncpg-based). Tests pass a
    FakePool.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        log.debug("AssetCatalogService initialised with pool=%r", pool)

    # ---- venues ---------------------------------------------------------

    async def list_venues(self, *, active_only: bool = True) -> List[Venue]:
        log.debug("list_venues(active_only=%s)", active_only)
        where = "WHERE is_active" if active_only else ""
        rows = await self._pool.fetch_all(
            f"SELECT * FROM venues {where} ORDER BY priority, code"
        )
        return [Venue.model_validate(dict(r)) for r in rows]

    async def venue_by_code(self, code: str) -> Optional[Venue]:
        log.debug("venue_by_code(code=%s)", code)
        row = await self._pool.fetch_one(
            "SELECT * FROM venues WHERE code=$1", (code,)
        )
        return Venue.model_validate(dict(row)) if row else None

    # ---- assets ---------------------------------------------------------

    async def list_assets(
        self,
        *,
        asset_class: Optional[AssetClass] = None,
        canonical_only: bool = True,
    ) -> List[Asset]:
        log.debug(
            "list_assets(asset_class=%s, canonical_only=%s)",
            asset_class, canonical_only,
        )
        clauses: List[str] = []
        params: List[Any] = []
        if canonical_only:
            clauses.append("alias_of_id IS NULL")
        if asset_class is not None:
            clauses.append(f"asset_class=${len(params) + 1}")
            params.append(asset_class.value)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._pool.fetch_all(
            f"SELECT * FROM assets {where} ORDER BY symbol",
            tuple(params) if params else None,
        )
        return [Asset.model_validate(dict(r)) for r in rows]

    async def asset_by_symbol(self, symbol: str) -> Optional[Asset]:
        log.debug("asset_by_symbol(symbol=%s)", symbol)
        row = await self._pool.fetch_one(
            "SELECT * FROM assets WHERE symbol=$1", (symbol.upper(),)
        )
        return Asset.model_validate(dict(row)) if row else None

    # ---- instrument resolution -----------------------------------------

    async def resolve_symbol(
        self,
        symbol: str,
        *,
        venue_code: Optional[str] = None,
    ) -> Optional[InstrumentRef]:
        """Resolve a symbol to an instrument, optionally scoped to one venue.

        Order of attempts:
          1. exact match on `instruments.symbol` (+ venue.code if given)
          2. match via `instrument_aliases.alias_value`
        """
        log.debug("resolve_symbol(symbol=%s, venue_code=%s)", symbol, venue_code)
        # 1. direct match
        sql = """
            SELECT i.*
              FROM instruments i
              LEFT JOIN venues v ON v.id = i.venue_id
             WHERE i.symbol = $1
        """
        params: List[Any] = [symbol]
        if venue_code is not None:
            sql += "  AND (v.code = $2 OR i.exchange = $2)\n"
            params.append(venue_code)
        sql += " LIMIT 1"
        row = await self._pool.fetch_one(sql, tuple(params))
        if row is not None:
            return InstrumentRef.model_validate(dict(row))

        # 2. alias match
        sql = """
            SELECT i.*
              FROM instrument_aliases ia
              JOIN instruments i ON i.id = ia.instrument_id
              LEFT JOIN venues v ON v.id = i.venue_id
             WHERE ia.alias_value = $1
        """
        params = [symbol]
        if venue_code is not None:
            sql += "  AND (v.code = $2 OR i.exchange = $2)\n"
            params.append(venue_code)
        sql += " LIMIT 1"
        row = await self._pool.fetch_one(sql, tuple(params))
        return InstrumentRef.model_validate(dict(row)) if row else None

    # ---- arbitrage-friendly roll-up ------------------------------------

    async def venues_for_asset(self, asset_symbol: str) -> List[VenueAssetRow]:
        """All venue+instrument rows for one logical asset, ordered by total
        cost (spread + taker fee) ascending, then by venue.priority.
        """
        log.debug("venues_for_asset(asset_symbol=%s)", asset_symbol)
        rows = await self._pool.fetch_all(
            """
            SELECT *
              FROM v_asset_venues
             WHERE asset_symbol = $1
             ORDER BY venue_priority, venue_code
            """,
            (asset_symbol.upper(),),
        )
        result = [VenueAssetRow.model_validate(dict(r)) for r in rows]
        # stable sort by cheapest-first
        result.sort(key=lambda r: (r.total_cost_one_side_pct, r.venue_priority))
        return result

    async def spread_snapshot(
        self, asset_symbol: str
    ) -> Dict[str, Any]:
        """High-level snapshot useful for the dashboard + arb agents.

        Returns {
            'asset': <symbol>,
            'venues': [VenueAssetRow, ...],
            'cheapest_total_cost_pct': Decimal | None,
            'spread_between_cheapest_and_most_expensive_pct': Decimal | None,
        }
        """
        log.debug("spread_snapshot(asset_symbol=%s)", asset_symbol)
        rows = await self.venues_for_asset(asset_symbol)
        costs = [r.total_cost_one_side_pct for r in rows if r.total_cost_one_side_pct is not None]
        out: Dict[str, Any] = {
            "asset": asset_symbol.upper(),
            "venues": [r.model_dump(mode="json") for r in rows],
            "cheapest_total_cost_pct": None,
            "spread_between_cheapest_and_most_expensive_pct": None,
        }
        if costs:
            cheapest = min(costs)
            most_expensive = max(costs)
            out["cheapest_total_cost_pct"] = cheapest
            out["spread_between_cheapest_and_most_expensive_pct"] = (
                most_expensive - cheapest
            )
        return out

    # ---- stats ----------------------------------------------------------

    async def stats(self) -> Dict[str, int]:
        log.debug("stats()")
        rows = await self._pool.fetch_all(
            """
            SELECT 'venues'              AS k, COUNT(*) AS v FROM venues
            UNION ALL SELECT 'venues_active',      COUNT(*) FROM venues WHERE is_active
            UNION ALL SELECT 'assets',             COUNT(*) FROM assets
            UNION ALL SELECT 'assets_canonical',   COUNT(*) FROM assets WHERE alias_of_id IS NULL
            UNION ALL SELECT 'instruments',        COUNT(*) FROM instruments
            UNION ALL SELECT 'instruments_linked', COUNT(*) FROM instruments
                                                   WHERE asset_id IS NOT NULL AND venue_id IS NOT NULL
            UNION ALL SELECT 'aliases',            COUNT(*) FROM instrument_aliases
            """
        )
        return {r["k"]: int(r["v"]) for r in rows}
