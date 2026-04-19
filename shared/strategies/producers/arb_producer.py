"""
shared.strategies.producers.arb_producer — turns Phase 33
:class:`ArbOpportunity` rows into a pair of :class:`StrategyIntent`s
(one buy leg at ``buy_venue``, one sell leg at ``sell_venue``).

The producer is read-only; it doesn't modify the underlying
``arb_opportunities`` table. The composer is responsible for
persisting the resulting intents and (eventually, in Phase 34+)
handing them to the ExecutionRouter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from shared.arb.protocol import ArbOpportunity
from shared.arb.store import ArbStore
from shared.strategies.protocol import (
    KIND_ARB,
    SIDE_BUY,
    SIDE_SELL,
    StrategyIntent,
)


class ArbProducer:
    name = "arb-scanner"
    kind = KIND_ARB

    def __init__(
        self,
        store: ArbStore,
        *,
        name: Optional[str] = None,
        min_net_bps: float = 5.0,
        max_opportunities: int = 50,
    ) -> None:
        self.name = name or "arb-scanner"
        self._store = store
        self._min_net_bps = float(min_net_bps)
        self._max = int(max_opportunities)

    async def produce(
        self,
        *,
        limit: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[StrategyIntent]:
        opps = await self._store.list_opportunities(
            min_net_bps=self._min_net_bps,
            limit=min(limit, self._max),
        )
        intents: List[StrategyIntent] = []
        now = datetime.now(timezone.utc)
        for opp in opps:
            intents.extend(self._intents_for(
                opp, correlation_id=correlation_id,
                company_id=company_id, now=now,
            ))
        return intents

    def _intents_for(
        self, opp: ArbOpportunity, *, correlation_id: Optional[str],
        company_id: Optional[str], now: datetime,
    ) -> List[StrategyIntent]:
        notional = float(opp.size_base) * float(opp.buy_ask)
        source_ref = f"arb_opportunities.id={opp.id}"
        meta = {
            "gross_bps": opp.gross_bps,
            "net_bps": opp.net_bps,
            "fees_bps": opp.fees_bps,
            "est_profit_usd": opp.est_profit_usd,
        }
        return [
            StrategyIntent(
                id=None, strategy_name=self.name, strategy_kind=self.kind,
                symbol=opp.symbol, side=SIDE_BUY, size_base=opp.size_base,
                notional_usd=notional, venue=opp.buy_venue,
                reference_price=opp.buy_ask,
                correlation_id=correlation_id,
                source_ref=f"{source_ref}:buy",
                priority_score=float(opp.net_bps),
                company_id=company_id or opp.company_id,
                metadata={**meta, "leg": "buy", "sell_venue": opp.sell_venue},
                proposed_at=now,
            ),
            StrategyIntent(
                id=None, strategy_name=self.name, strategy_kind=self.kind,
                symbol=opp.symbol, side=SIDE_SELL, size_base=opp.size_base,
                notional_usd=float(opp.size_base) * float(opp.sell_bid),
                venue=opp.sell_venue,
                reference_price=opp.sell_bid,
                correlation_id=correlation_id,
                source_ref=f"{source_ref}:sell",
                priority_score=float(opp.net_bps),
                company_id=company_id or opp.company_id,
                metadata={**meta, "leg": "sell", "buy_venue": opp.buy_venue},
                proposed_at=now,
            ),
        ]


__all__ = ["ArbProducer"]
