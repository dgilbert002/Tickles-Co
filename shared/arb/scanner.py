"""
shared.arb.scanner — deterministic arbitrage scanner.

Given a list of :class:`ArbVenue` and one quote per venue per symbol,
the scanner picks the cheapest ask venue and the dearest bid venue,
computes net basis points after fees (``taker_fee_bps`` on both legs),
and emits an :class:`ArbOpportunity` if ``net_bps >= min_net_bps``.

The scanner is pure — it never calls the network. Tests use the
:class:`OfflineQuoteFetcher`; live scans use :class:`CcxtQuoteFetcher`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from shared.arb.fetchers import QuoteFetcher
from shared.arb.protocol import ArbOpportunity, ArbQuote, ArbVenue


@dataclass
class ScannerConfig:
    min_net_bps: float = 5.0           # profit threshold after fees
    max_size_usd: float = 10_000.0     # cap per opportunity
    max_opportunities: int = 50
    use_maker_on_sell: bool = False    # assume taker on both legs by default


class ArbScanner:
    """Deterministic scanner — same quotes ⇒ same opportunities."""

    def __init__(
        self,
        venues: Iterable[ArbVenue],
        fetcher: QuoteFetcher,
        *,
        config: Optional[ScannerConfig] = None,
    ) -> None:
        self._venues: Dict[str, ArbVenue] = {
            v.name: v for v in venues if v.enabled
        }
        self._fetcher = fetcher
        self._config = config or ScannerConfig()

    @property
    def venues(self) -> List[ArbVenue]:
        return list(self._venues.values())

    async def scan(
        self,
        symbol: str,
        *,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[ArbOpportunity]:
        quotes = await self._fetcher.fetch_top_of_book(
            symbol, list(self._venues.keys()),
        )
        return self._evaluate(symbol, quotes,
                              correlation_id=correlation_id,
                              company_id=company_id)

    async def scan_many(
        self,
        symbols: Iterable[str],
        *,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[ArbOpportunity]:
        all_opps: List[ArbOpportunity] = []
        for sym in symbols:
            opps = await self.scan(
                sym, correlation_id=correlation_id, company_id=company_id,
            )
            all_opps.extend(opps)
        all_opps.sort(key=lambda o: (-o.net_bps, o.symbol))
        return all_opps[: self._config.max_opportunities]

    def _evaluate(
        self,
        symbol: str,
        quotes: Dict[str, ArbQuote],
        *,
        correlation_id: Optional[str],
        company_id: Optional[str],
    ) -> List[ArbOpportunity]:
        if len(quotes) < 2:
            return []

        # For each (buy_venue, sell_venue) pair where buy != sell.
        opportunities: List[ArbOpportunity] = []
        pairs = [
            (b, s)
            for b in quotes.keys()
            for s in quotes.keys()
            if b != s
        ]
        now = datetime.now(timezone.utc)

        for buy_v, sell_v in pairs:
            buy_q = quotes[buy_v]
            sell_q = quotes[sell_v]
            if buy_q.ask <= 0 or sell_q.bid <= 0:
                continue
            if sell_q.bid <= buy_q.ask:
                continue  # no gap
            buy_venue = self._venues.get(buy_v)
            sell_venue = self._venues.get(sell_v)
            if not buy_venue or not sell_venue:
                continue

            buy_fee = float(buy_venue.taker_fee_bps)
            sell_fee = (
                float(sell_venue.maker_fee_bps)
                if self._config.use_maker_on_sell
                else float(sell_venue.taker_fee_bps)
            )
            fees_bps = buy_fee + sell_fee
            gross_bps = (sell_q.bid - buy_q.ask) / buy_q.ask * 10_000.0
            net_bps = gross_bps - fees_bps
            if net_bps < self._config.min_net_bps:
                continue

            book_size = min(buy_q.ask_size or 0.0, sell_q.bid_size or 0.0)
            if book_size <= 0:
                max_size_base = self._config.max_size_usd / buy_q.ask
            else:
                cap_base = self._config.max_size_usd / buy_q.ask
                max_size_base = min(cap_base, book_size)

            est_profit_usd = max_size_base * buy_q.ask * net_bps / 10_000.0

            opportunities.append(ArbOpportunity(
                id=None,
                symbol=symbol,
                buy_venue=buy_v,
                sell_venue=sell_v,
                buy_ask=buy_q.ask,
                sell_bid=sell_q.bid,
                size_base=round(max_size_base, 10),
                gross_bps=round(gross_bps, 4),
                net_bps=round(net_bps, 4),
                est_profit_usd=round(est_profit_usd, 4),
                fees_bps=round(fees_bps, 4),
                company_id=company_id,
                correlation_id=correlation_id,
                metadata={
                    "buy_ask_size": buy_q.ask_size,
                    "sell_bid_size": sell_q.bid_size,
                    "taker_only": not self._config.use_maker_on_sell,
                },
                observed_at=now,
            ))

        opportunities.sort(key=lambda o: (-o.net_bps, o.symbol))
        return opportunities[: self._config.max_opportunities]


__all__ = ["ArbScanner", "ScannerConfig"]
