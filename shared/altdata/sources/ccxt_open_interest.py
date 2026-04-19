"""CCXT-based open interest source."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from shared.altdata.protocol import (
    AltDataItem,
    METRIC_OI_CONTRACTS,
    METRIC_OI_USD,
    SOURCE_OPEN_INTEREST,
)

LOG = logging.getLogger("tickles.altdata.oi")


@dataclass
class CcxtOpenInterestSource:
    exchange_id: str = "binance"
    symbols: Sequence[str] = field(default_factory=lambda: ("BTC/USDT", "ETH/USDT"))
    name: str = "ccxt_open_interest"
    category: str = SOURCE_OPEN_INTEREST
    exchange_obj: Optional[Any] = None  # injected in tests

    async def _fetch_one(self, ex: Any, symbol: str) -> List[AltDataItem]:
        try:
            raw = await asyncio.to_thread(ex.fetch_open_interest, symbol)
        except Exception as exc:  # pragma: no cover - network-path
            LOG.warning("ccxt.fetch_open_interest %s failed: %s", symbol, exc)
            return []

        if not isinstance(raw, dict):
            return []

        ts_ms = raw.get("timestamp")
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            as_of = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        else:
            as_of = datetime.now(timezone.utc)

        normalised = symbol.replace("/", "")
        scope = f"ccxt:{self.exchange_id}/{normalised}/oi"
        base_payload = {k: v for k, v in raw.items() if _is_jsonable(v)}
        items: List[AltDataItem] = []

        oi_contracts = raw.get("openInterestAmount") or raw.get("openInterest")
        if isinstance(oi_contracts, (int, float)):
            items.append(AltDataItem(
                source=SOURCE_OPEN_INTEREST,
                provider=f"ccxt:{self.exchange_id}",
                scope_key=scope,
                metric=METRIC_OI_CONTRACTS,
                as_of=as_of,
                value_numeric=float(oi_contracts),
                unit="contracts",
                exchange=self.exchange_id,
                symbol=symbol,
                payload=base_payload,
            ))

        oi_usd = raw.get("openInterestValue") or raw.get("openInterestUsd")
        if isinstance(oi_usd, (int, float)):
            items.append(AltDataItem(
                source=SOURCE_OPEN_INTEREST,
                provider=f"ccxt:{self.exchange_id}",
                scope_key=scope,
                metric=METRIC_OI_USD,
                as_of=as_of,
                value_numeric=float(oi_usd),
                unit="usd",
                exchange=self.exchange_id,
                symbol=symbol,
                payload=base_payload,
            ))
        return items

    async def fetch(self) -> List[AltDataItem]:
        ex = self.exchange_obj
        if ex is None:
            try:
                import ccxt  # type: ignore
            except ImportError:
                LOG.info("ccxt not installed; open-interest source disabled")
                return []
            ex = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})

        out: List[AltDataItem] = []
        for sym in self.symbols:
            out.extend(await self._fetch_one(ex, sym))
        return out


def _is_jsonable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), dict, list))


__all__ = ["CcxtOpenInterestSource"]
