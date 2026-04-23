"""
shared.strategies.producers.copy_producer — turns pending Phase 33
:class:`CopyTrade` rows into :class:`StrategyIntent`s.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from shared.copy_trader_trader.store import CopyStore
from shared.strategies.protocol import KIND_COPY, StrategyIntent


class CopyProducer:
    name = "copy-trader"
    kind = KIND_COPY

    def __init__(
        self,
        store: CopyStore,
        *,
        name: Optional[str] = None,
        max_trades: int = 50,
    ) -> None:
        self.name = name or "copy-trader"
        self._store = store
        self._max = int(max_trades)

    async def produce(
        self,
        *,
        limit: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[StrategyIntent]:
        trades = await self._store.list_trades(
            status="pending", limit=min(limit, self._max),
        )
        now = datetime.now(timezone.utc)
        intents: List[StrategyIntent] = []
        for t in trades:
            intents.append(StrategyIntent(
                id=None, strategy_name=self.name, strategy_kind=self.kind,
                symbol=t.symbol, side=t.side,
                size_base=float(t.mapped_qty_base),
                notional_usd=float(t.mapped_notional_usd),
                reference_price=(
                    float(t.source_price) if t.source_price else None
                ),
                correlation_id=(
                    correlation_id or t.correlation_id
                ),
                source_ref=f"copy_trades.id={t.id}",
                priority_score=float(t.mapped_notional_usd),
                company_id=company_id or t.company_id,
                metadata={
                    "source_id": t.source_id,
                    "source_fill_id": t.source_fill_id,
                    "source_qty_base": t.source_qty_base,
                    "source_notional_usd": t.source_notional_usd,
                },
                proposed_at=now,
            ))
        return intents


__all__ = ["CopyProducer"]
