"""
shared.copy_trader.sources — ``BaseCopySource`` + concrete implementations.

Each source knows how to produce a list of :class:`SourceFill` objects
that happened **after** ``since``. The copy service keeps the water
mark (last_checked_at) so repeated polls don't re-emit the same fills.

* :class:`StaticCopySource` — in-memory list of fills (tests, demos).
* :class:`CcxtCopySource`   — live ``ccxt.async_support.fetch_my_trades``
                              against a configured account.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol

from shared.copy_trader_trader.protocol import SIDE_BUY, SIDE_SELL, CopySource, SourceFill

LOG = logging.getLogger("tickles.copy.sources")


class BaseCopySource(Protocol):
    async def fetch_fills(
        self, source: CopySource, since: Optional[datetime] = None,
    ) -> List[SourceFill]:
        ...


class StaticCopySource:
    """Serves a fixed list of fills — for tests and demo runs."""

    def __init__(self, fills: Optional[List[SourceFill]] = None) -> None:
        self._fills: List[SourceFill] = list(fills or [])

    def set_fills(self, fills: List[SourceFill]) -> None:
        self._fills = list(fills)

    def append(self, fill: SourceFill) -> None:
        self._fills.append(fill)

    async def fetch_fills(
        self, source: CopySource, since: Optional[datetime] = None,
    ) -> List[SourceFill]:
        if since is None:
            return list(self._fills)
        out: List[SourceFill] = []
        for f in self._fills:
            if f.ts is None:
                out.append(f)
            elif f.ts > since:
                out.append(f)
        return out


class CcxtCopySource:
    """Live source: polls ``fetch_my_trades`` on the configured account.

    Expects :attr:`CopySource.identifier` to hold a JSON-serialised
    credential blob? No. We only consume *public* data in Phase 33.
    ``fetch_my_trades`` requires API keys, so live CcxtCopySource is
    implemented here for Phase 34 wiring — until then it gracefully
    returns an empty list if no exchange is provided.
    """

    def __init__(
        self, *, exchange: Optional[Any] = None, limit: int = 100,
    ) -> None:
        self._ex = exchange
        self._limit = int(limit)

    async def fetch_fills(
        self, source: CopySource, since: Optional[datetime] = None,
    ) -> List[SourceFill]:
        if self._ex is None:
            LOG.debug("CcxtCopySource has no exchange; returning [] for %s",
                      source.name)
            return []
        since_ms = int(since.timestamp() * 1000) if since else None
        try:
            trades = await self._ex.fetch_my_trades(
                None, since=since_ms, limit=self._limit,
            )
        except Exception as e:
            LOG.warning("fetch_my_trades failed for %s: %s", source.name, e)
            return []
        out: List[SourceFill] = []
        for t in trades or []:
            try:
                side = (t.get("side") or "").lower()
                if side not in (SIDE_BUY, SIDE_SELL):
                    continue
                price = float(t.get("price") or 0.0)
                qty = float(t.get("amount") or 0.0)
                if price <= 0 or qty <= 0:
                    continue
                ts_ms = t.get("timestamp")
                ts = (
                    datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    if ts_ms else None
                )
                out.append(SourceFill(
                    fill_id=str(t.get("id") or ""),
                    symbol=t.get("symbol") or "",
                    side=side,
                    price=price,
                    qty_base=qty,
                    notional_usd=price * qty,
                    ts=ts,
                    raw=dict(t),
                ))
            except Exception as e:  # pragma: no cover
                LOG.debug("skip malformed trade %s: %s", t, e)
                continue
        return out


__all__ = [
    "BaseCopySource",
    "CcxtCopySource",
    "StaticCopySource",
]
