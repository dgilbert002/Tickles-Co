"""
shared.copy_trader.service — orchestrates the copy-trader.

``CopyService.tick()`` pulls new fills from each enabled source, runs
them through the :class:`CopyMapper`, and persists the resulting
:class:`CopyTrade` records (both kept and skipped) so the audit trail
is complete. A fill that has already been mirrored is de-duplicated
by the ``(source_id, source_fill_id)`` unique constraint in
:table:`public.copy_trades`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.copy_trader_trader.mapper import CopyMapper, MappingResult
from shared.copy_trader_trader.protocol import CopySource, CopyTrade, SourceFill
from shared.copy_trader_trader.sources import BaseCopySource
from shared.copy_trader_trader.store import CopyStore

LOG = logging.getLogger("tickles.copy.service")


@dataclass
class CopyServiceConfig:
    auto_persist: bool = True


@dataclass
class CopyTickResult:
    source_id: int
    source_name: str
    fills_fetched: int
    trades_kept: int
    trades_skipped: int
    trades: List[CopyTrade]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "fills_fetched": self.fills_fetched,
            "trades_kept": self.trades_kept,
            "trades_skipped": self.trades_skipped,
            "trades": [t.to_dict() for t in self.trades],
        }


class CopyService:
    def __init__(
        self,
        store: CopyStore,
        fetcher: BaseCopySource,
        *,
        mapper: Optional[CopyMapper] = None,
        config: Optional[CopyServiceConfig] = None,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._mapper = mapper or CopyMapper()
        self._config = config or CopyServiceConfig()

    async def tick_one(
        self,
        source: CopySource,
        *,
        correlation_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> CopyTickResult:
        if persist is None:
            persist = self._config.auto_persist
        since: Optional[datetime] = source.last_checked_at
        fills: List[SourceFill] = await self._fetcher.fetch_fills(
            source, since=since,
        )
        trades: List[CopyTrade] = []
        kept = 0
        skipped = 0
        for fill in fills:
            result: MappingResult = self._mapper.map(
                source, fill, correlation_id=correlation_id,
            )
            assert result.trade is not None
            trade = result.trade
            if persist:
                try:
                    new_id = await self._store.record_trade(trade)
                    trade.id = new_id or trade.id
                except Exception as e:  # pragma: no cover
                    LOG.warning("persist copy trade failed: %s", e)
            if result.kept and result.skip_reason is None:
                kept += 1
            else:
                skipped += 1
            trades.append(trade)
        if persist and source.id:
            try:
                await self._store.touch_source(int(source.id))
            except Exception as e:  # pragma: no cover
                LOG.debug("touch_source failed: %s", e)
        return CopyTickResult(
            source_id=int(source.id or 0),
            source_name=source.name,
            fills_fetched=len(fills),
            trades_kept=kept,
            trades_skipped=skipped,
            trades=trades,
        )

    async def tick_all(
        self,
        *,
        correlation_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> List[CopyTickResult]:
        sources = await self._store.list_sources(enabled_only=True)
        out: List[CopyTickResult] = []
        for s in sources:
            out.append(await self.tick_one(
                s, correlation_id=correlation_id, persist=persist,
            ))
        return out


__all__ = ["CopyService", "CopyServiceConfig", "CopyTickResult"]
