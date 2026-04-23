"""
shared.gateway.schema — pydantic models + channel naming for Phase 17.

The 21-year-old version
-----------------------
Every message that flows through the gateway has a strict shape so
downstream consumers (Phase 18 indicators, Phase 21 auditor, Phase 26
execution) cannot get surprised.  We keep the schemas small and
serialisable: ``model_dump_json()`` -> redis -> ``model_validate_json()``.

Channel naming convention (REDIS PUB/SUB):

    md.<exchange>.<symbol_safe>.<channel>

Where:
    - ``exchange`` is lowercased ccxt id (``binance``, ``bybit``, ...).
    - ``symbol_safe`` is the ccxt symbol with ``/`` replaced by ``-``
      so the channel is well-behaved (``BTC-USDT``).
    - ``channel`` is one of ``tick`` (top-of-book ticker), ``trade``
      (executed trade prints), ``l1`` (best-bid/best-ask), ``mark``
      (perp mark price), ``funding`` (perp funding rate).

A small ``SubscriptionKey`` value object captures the (exchange, symbol,
channel) triple so we can hash it in dicts/sets.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

CHANNEL_PREFIX = "md"


class TickChannel(str, Enum):
    """Logical channels emitted by a single exchange-symbol pair."""

    TICK = "tick"
    TRADE = "trade"
    L1 = "l1"
    MARK = "mark"
    FUNDING = "funding"
    CANDLE = "candle"


def safe_symbol(symbol: str) -> str:
    """Replace ``/`` with ``-`` so symbols are channel-name-safe.

    >>> safe_symbol("BTC/USDT")
    'BTC-USDT'
    >>> safe_symbol("BTC-USDT")
    'BTC-USDT'
    """
    return symbol.replace("/", "-")


def unsafe_symbol(symbol_safe: str) -> str:
    """Reverse :func:`safe_symbol`. Returns ``BTC/USDT`` for ``BTC-USDT``.

    Single ``-`` is replaced; for symbols already containing dashes the
    caller is responsible for preserving the original ``ccxt`` form.
    """
    if "-" in symbol_safe:
        return symbol_safe.replace("-", "/", 1)
    return symbol_safe


def channel_for(exchange: str, symbol: str, channel: TickChannel) -> str:
    """Compose the redis channel name. See module docstring for format."""
    return f"{CHANNEL_PREFIX}.{exchange.lower()}.{safe_symbol(symbol)}.{channel.value}"


class SubscriptionKey(BaseModel):
    """Hashable triple identifying a single live subscription."""

    model_config = ConfigDict(frozen=True)

    exchange: str
    symbol: str
    channel: TickChannel

    @property
    def redis_channel(self) -> str:
        return channel_for(self.exchange, self.symbol, self.channel)

    def as_tuple(self) -> Tuple[str, str, TickChannel]:
        return (self.exchange, self.symbol, self.channel)


class SubscriptionRequest(BaseModel):
    """Operator/agent ask: 'please ensure these channels are live'."""

    model_config = ConfigDict(extra="forbid")

    exchange: str
    symbol: str
    channels: List[TickChannel] = Field(default_factory=list, min_length=1)
    requested_by: str = Field(
        default="cli",
        description="Free-form attribution — agent name, user, or 'cli'.",
    )


class Tick(BaseModel):
    """Top-of-book ticker snapshot (ccxt watchTicker)."""

    model_config = ConfigDict(extra="ignore")

    exchange: str
    symbol: str
    timestamp: datetime
    last: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low: Optional[Decimal] = None
    base_volume: Optional[Decimal] = None
    quote_volume: Optional[Decimal] = None


class Trade(BaseModel):
    """One executed trade print (ccxt watchTrades)."""

    model_config = ConfigDict(extra="ignore")

    exchange: str
    symbol: str
    timestamp: datetime
    price: Decimal
    amount: Decimal
    side: Optional[str] = Field(default=None, description="'buy' or 'sell' if provided")
    trade_id: Optional[str] = None


class L1Book(BaseModel):
    """Best-bid/best-ask snapshot (ccxt watchOrderBook, depth=1)."""

    model_config = ConfigDict(extra="ignore")

    exchange: str
    symbol: str
    timestamp: datetime
    bid_price: Optional[Decimal] = None
    bid_size: Optional[Decimal] = None
    ask_price: Optional[Decimal] = None
    ask_size: Optional[Decimal] = None

    @property
    def spread(self) -> Optional[Decimal]:
        if self.bid_price is None or self.ask_price is None:
            return None
        return self.ask_price - self.bid_price


class MarkPrice(BaseModel):
    """Perp mark price snapshot (ccxt watchMarkPrice on supported venues)."""

    model_config = ConfigDict(extra="ignore")

    exchange: str
    symbol: str
    timestamp: datetime
    mark_price: Decimal
    index_price: Optional[Decimal] = None


class Candle(BaseModel):
    """One OHLCV candle (ccxt watchOHLCV or native)."""

    model_config = ConfigDict(extra="ignore")

    exchange: str
    symbol: str
    timestamp: datetime
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Optional[Decimal] = None


class GatewayStats(BaseModel):
    """Aggregate, point-in-time view of gateway health.

    Used by ``gateway_cli stats`` and by the daemon's housekeeping loop
    to publish ``md:gateway:stats``.
    """

    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    uptime_seconds: float
    subscriptions_total: int
    sources_total: int
    messages_in_total: int
    messages_published_total: int
    publish_errors_total: int
    reconnects_total: int
    last_message_at: Optional[datetime] = None
