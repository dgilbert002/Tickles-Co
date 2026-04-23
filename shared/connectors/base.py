"""Base exchange adapter interface and data models for Tickles V2."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import List, Optional, Dict, Any

logger = logging.getLogger("tickles.connectors")

class MarketStatus(Enum):
    """Market status enumeration."""
    OPEN = auto()
    CLOSED = auto()
    UNKNOWN = auto()

@dataclass
class Candle:
    """OHLCV candle data model with additional metadata."""
    instrument_id: str  # e.g. the exchange-native symbol
    timeframe: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades_count: Optional[int]
    data_source: str
    candle_data_hash: str
    exchange: Optional[str] = None

    def to_gateway_model(self) -> Any:
        """Convert to the Pydantic model used by the streaming gateway."""
        from shared.gateway.schema import Candle as GatewayCandle
        return GatewayCandle(
            exchange=self.exchange or "unknown",
            symbol=self.instrument_id,
            timeframe=self.timeframe,
            timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

@dataclass(frozen=True)
class Instrument:
    """Standardized model for a tradable instrument."""
    id: str  # Exchange-specific instrument ID, e.g., 'BTCUSDT'
    symbol: str  # Standardized symbol, e.g., 'BTC/USDT'
    base_asset: str
    quote_asset: str
    type: str  # e.g., 'spot', 'future', 'perp'
    precision_price: Decimal
    precision_amount: Decimal
    min_amount: Decimal
    raw_data: Dict[str, Any]  # Store the original exchange data for flexibility


class BaseExchangeAdapter(ABC):
    """Abstract base class for all exchange adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Get the exchange name/identifier (e.g., 'bybit', 'blofin')."""
        ...

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        since: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[Candle]:
        """Fetch historical OHLCV data from the exchange.

        Args:
            symbol: Exchange symbol (e.g., 'BTC/USDT')
            timeframe: Candle interval ('1m', '5m', '15m', '1h', '4h', '1d')
            since: Start datetime (UTC). If None, fetches most recent.
            limit: Maximum number of candles to fetch.

        Returns:
            List of Candle objects.
        """
        ...

    @abstractmethod
    async def get_market_status(self, symbol: str) -> MarketStatus:
        """Get current market status for a trading pair.

        Args:
            symbol: Exchange symbol (e.g., 'BTC/USDT')

        Returns:
            MarketStatus enum value.
        """
        ...

    @abstractmethod
    async def get_instruments(self) -> List['Instrument']:
        """Get list of available instruments from the exchange.

        Returns:
            List of Instrument dataclasses.
        """
        ...

    @abstractmethod
    async def fetch_balance(self) -> Dict[str, Any]:
        """Fetch account balance from the exchange.

        Returns:
            Dict containing balance information (standardized format).
        """
        ...

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch current ticker (bid/ask/last) for a symbol.

        Returns:
            Dict with 'bid', 'ask', 'last', 'spread', 'timestamp'.
        """
        ...

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """Fetch current funding or overnight holding rate.

        Returns:
            Dict with 'symbol', 'rate', 'next_funding_time'.
        """
        ...

    @abstractmethod
    async def fetch_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Fetch market sentiment (buyers vs sellers) if available.

        Returns:
            Dict with 'symbol', 'long_pct', 'short_pct', 'num_buyers', 'num_sellers'.
        """
        ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently active/pending orders."""
        ...

    @abstractmethod
    async def fetch_closed_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch recently closed/filled orders."""
        ...

    @abstractmethod
    async def fetch_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently open positions."""
        ...

    @abstractmethod
    async def fetch_trades(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch trade/transaction history."""
        ...

    @abstractmethod
    async def get_market_hours(self, symbol: str) -> Dict[str, Any]:
        """Get detailed market open/close schedule for a symbol.

        Returns:
            Dict with 'timezone', 'schedule' (list of open/close per day), 'is_open'.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close any open connections/resources."""
        ...