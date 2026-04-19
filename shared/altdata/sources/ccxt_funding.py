"""CCXT-based funding-rate source.

Fetches the current funding rate from a CCXT exchange for a list of
perpetual symbols. CCXT is optional; if it isn't installed the source
returns an empty list (the service logs a warning and moves on).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from shared.altdata.protocol import (
    AltDataItem,
    METRIC_FUNDING_RATE,
    SOURCE_FUNDING_RATE,
)

LOG = logging.getLogger("tickles.altdata.funding")


@dataclass
class CcxtFundingRateSource:
    """Emit one ``AltDataItem`` per configured symbol, per tick."""

    exchange_id: str = "binance"
    symbols: Sequence[str] = field(default_factory=lambda: ("BTC/USDT", "ETH/USDT"))
    name: str = "ccxt_funding"
    category: str = SOURCE_FUNDING_RATE
    exchange_obj: Optional[Any] = None  # injected in tests

    async def _fetch_one(self, ex: Any, symbol: str) -> Optional[AltDataItem]:
        try:
            raw = await asyncio.to_thread(ex.fetch_funding_rate, symbol)
        except Exception as exc:  # pragma: no cover - network-path
            LOG.warning("ccxt.fetch_funding_rate %s failed: %s", symbol, exc)
            return None

        if not isinstance(raw, dict):
            return None

        ts_ms = raw.get("timestamp") or raw.get("fundingTimestamp")
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            as_of = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        else:
            as_of = datetime.now(timezone.utc)
        rate = raw.get("fundingRate")
        if rate is None:
            return None

        normalised = symbol.replace("/", "")
        scope = f"ccxt:{self.exchange_id}/{normalised}/funding"
        return AltDataItem(
            source=SOURCE_FUNDING_RATE,
            provider=f"ccxt:{self.exchange_id}",
            scope_key=scope,
            metric=METRIC_FUNDING_RATE,
            as_of=as_of,
            value_numeric=float(rate),
            unit="ratio",
            exchange=self.exchange_id,
            symbol=symbol,
            payload={k: v for k, v in raw.items() if _is_jsonable(v)},
        )

    async def fetch(self) -> List[AltDataItem]:
        ex = self.exchange_obj
        if ex is None:
            try:
                import ccxt  # type: ignore
            except ImportError:
                LOG.info("ccxt not installed; funding-rate source disabled")
                return []
            ex = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})

        out: List[AltDataItem] = []
        for sym in self.symbols:
            item = await self._fetch_one(ex, sym)
            if item is not None:
                out.append(item)
        return out


def _is_jsonable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), dict, list))


__all__ = ["CcxtFundingRateSource"]
