"""
shared.arb.protocol — dataclasses and constants for the arbitrage scanner.

The scanner is intentionally simple: it takes a list of ``ArbVenue`` +
a ``QuoteFetcher`` and produces ``ArbOpportunity`` records when the
highest bid across venues exceeds the lowest ask by more than
``min_net_bps`` after fees.

Prices are always expressed in the quote asset (USDT / USDC / USD /
whatever the pair uses). ``size_base`` is the tradable amount in the
base asset (e.g. BTC) capped by the shallower side of the book.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


SIDE_BUY = "buy"
SIDE_SELL = "sell"
Side = str


@dataclass
class ArbVenue:
    """A venue we're allowed to scan."""

    id: Optional[int]
    name: str
    kind: str = "spot"
    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 2.0
    enabled: bool = True
    company_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ArbQuote:
    """Top-of-book quote from one venue for one symbol at one instant."""

    venue: str
    symbol: str
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    observed_at: Optional[datetime] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid and self.ask else 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["observed_at"] = (
            self.observed_at.isoformat() if self.observed_at else None
        )
        d["mid"] = self.mid
        return d


@dataclass
class ArbOpportunity:
    """A tradable gap between two venues."""

    id: Optional[int]
    symbol: str
    buy_venue: str
    sell_venue: str
    buy_ask: float
    sell_bid: float
    size_base: float
    gross_bps: float
    net_bps: float
    est_profit_usd: float
    fees_bps: float
    company_id: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    observed_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["observed_at"] = (
            self.observed_at.isoformat() if self.observed_at else None
        )
        return d


__all__ = [
    "ArbOpportunity",
    "ArbQuote",
    "ArbVenue",
    "SIDE_BUY",
    "SIDE_SELL",
    "Side",
]
