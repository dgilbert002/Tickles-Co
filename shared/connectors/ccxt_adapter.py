"""CCXT-based exchange adapter for crypto exchanges.

Supports: Bybit (primary), BloFin, Bitget, and any CCXT-supported exchange.
Handles rate limiting, retries, and error recovery.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional, Dict, Any

import ccxt.async_support as ccxt
from ccxt.base.errors import ExchangeError, NetworkError

from .base import BaseExchangeAdapter, Candle, Instrument, MarketStatus
from ..utils.config import CANDLE_FETCH_BATCH_SIZE

logger = logging.getLogger("tickles.connectors.ccxt")


# Map our timeframe strings to CCXT timeframe strings (they're the same)
TIMEFRAME_MAP = {
    "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"
}


class CCXTAdapter(BaseExchangeAdapter):
    """CCXT-based adapter for crypto exchanges.

    Usage:
        adapter = CCXTAdapter("bybit", use_sandbox=True, config={"apiKey": "..."})
        candles = await adapter.fetch_ohlcv("BTC/USDT", "5m")
    """

    def __init__(
        self,
        exchange_id: str,
        use_sandbox: bool = False,
        config: Optional[Dict[str, Any]] = None,
        account_name: str = "main"
    ):
        self._exchange_id = exchange_id.lower()
        self._use_sandbox = use_sandbox
        self._config = config or {}
        self._account_name = account_name
        self._exchange: Optional[ccxt.Exchange] = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        """Get the exchange name/identifier (e.g., 'bybit', 'blofin')."""
        return self._exchange_id

    async def _get_exchange(self) -> ccxt.Exchange:
        """Lazy-initialize the CCXT exchange instance in a thread-safe way."""
        if self._exchange is None:
            async with self._lock:
                if self._exchange is None:
                    logger.info(
                        "Initializing CCXT exchange: %s (sandbox: %s)",
                        self._exchange_id,
                        self._use_sandbox,
                    )
                    exchange_class = getattr(ccxt, self._exchange_id, None)
                    if not exchange_class:
                        raise ValueError(f"Unknown exchange ID: {self._exchange_id}")

                    # Base config, enable rate limiting, use SWAP markets by default
                    exchange_config = {
                        "enableRateLimit": True,
                        "options": {"defaultType": "swap"},
                    }
                    exchange_config.update(self._config) # Add user config (apiKey, secret)

                    self._exchange = exchange_class(exchange_config)

                    # Set sandbox mode if supported and requested
                    if self._use_sandbox and self._exchange.has.get("test", False):
                        self._exchange.set_sandbox_mode(True)
                        logger.info("Sandbox mode enabled for %s", self._exchange_id)

        return self._exchange

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        since: Optional[datetime] = None,
        limit: int = CANDLE_FETCH_BATCH_SIZE,
    ) -> List[Candle]:
        """Fetch historical OHLCV data from the exchange with retries.

        Args:
            symbol: CCXT unified symbol (e.g., 'BTC/USDT')
            timeframe: Candle interval ('1m', '5m', '15m', '1h', '4h', '1d')
            since: Start datetime (UTC). If None, fetches most recent.
            limit: Maximum number of candles to fetch.
            use_sandbox: Whether to use the exchange's testnet/sandbox.

        Returns:
            List of Candle objects with data_source='api'.

        Raises:
            ConnectionError: if the exchange is unreachable after retries.
            ValueError: if symbol or timeframe is invalid.
            ExchangeError: for other exchange-side issues.
        """
        exchange = await self._get_exchange()

        ccxt_timeframe = TIMEFRAME_MAP.get(timeframe)
        if not ccxt_timeframe:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if since and not since.tzinfo:
            raise ValueError("'since' datetime must be timezone-aware.")

        since_ms = int(since.timestamp() * 1000) if since else None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Make the API call
                ohlcv_list = await exchange.fetch_ohlcv(
                    symbol, ccxt_timeframe, since=since_ms, limit=limit
                )

                # Process the results
                candles = [
                    self._parse_ohlcv_row(row, symbol, timeframe)
                    for row in ohlcv_list
                ]

                logger.debug(
                    "Fetched %d candles for %s/%s from %s",
                    len(candles),
                    symbol,
                    timeframe,
                    self._exchange_id,
                )
                return candles

            except NetworkError as e:
                logger.warning(
                    "Network error fetching OHLCV (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    e,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.5 ** attempt)  # Exponential backoff
                else:
                    raise ConnectionError(
                        f"Failed to reach {self._exchange_id} after {max_retries} attempts"
                    ) from e

            except ExchangeError as e:
                logger.error(
                    "Exchange error from %s while fetching OHLCV for %s: %s",
                    self._exchange_id,
                    symbol,
                    e,
                )
                # Re-raise to allow for specific handling upstream
                raise

        return []  # Should be unreachable if retries fail, but here for safety

    def _parse_ohlcv_row(self, row: list, symbol: str, timeframe: str) -> Candle:
        """Parse a single OHLCV row from CCXT into a Candle object."""
        ts = datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc)
        o, h, l, c, v = row[1], row[2], row[3], row[4], row[5]

        hash_input = f"{symbol}:{timeframe}:{ts.isoformat()}:{o}:{h}:{l}:{c}:{v}"
        data_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        volume = Decimal(str(v)) if v is not None else Decimal("0.0")
        close_price = Decimal(str(c))
        quote_volume = volume * close_price if v and c else Decimal("0.0")

        return Candle(
            instrument_id=symbol, # Use the symbol as the unique ID
            timeframe=timeframe,
            timestamp=ts,
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(l)),
            close=close_price,
            volume=volume,
            quote_volume=quote_volume,
            trades_count=None,  # fetch_ohlcv doesn't provide this
            data_source="api",
            candle_data_hash=data_hash,
            exchange=self._exchange_id,
        )

    async def get_market_status(self, symbol: str) -> MarketStatus:
        """Crypto markets are generally considered open 24/7."""
        # In a more complex system, we might ping the market status endpoint
        return MarketStatus.OPEN

    async def get_instruments(self) -> List[Instrument]:
        """Get a list of all available perpetual swap instruments from the exchange."""
        exchange = await self._get_exchange()
        try:
            await exchange.load_markets(True)  # Force reload
            instruments = []
            for market in exchange.markets.values():
                if market.get("type") == "swap" and market.get("active"):
                    
                    try:
                        instruments.append(
                            Instrument(
                                id=market["id"],
                                symbol=market["symbol"],
                                base_asset=market["base"],
                                quote_asset=market["quote"],
                                type=market["type"],
                                precision_price=Decimal(str(market["precision"]["price"])),
                                precision_amount=Decimal(str(market["precision"]["amount"])),
                                min_amount=Decimal(str(market["limits"]["amount"]["min"])),
                                raw_data=market,
                            )
                        )
                    except (KeyError, TypeError) as e:
                        logger.warning(
                            "Skipping instrument %s due to missing data: %s",
                            market.get("symbol", "UNKNOWN"), e
                        )

            logger.info(
                "Loaded %d active swap instruments from %s",
                len(instruments), self._exchange_id
            )
            return instruments
        except (NetworkError, ExchangeError) as e:
            logger.error(
                "Failed to load markets from %s: %s", self._exchange_id, e, exc_info=True
            )
            raise

    async def fetch_balance(self) -> Dict[str, Any]:
        """Fetch account balance from the exchange using CCXT."""
        exchange = await self._get_exchange()
        try:
            balance = await exchange.fetch_balance()
            return balance
        except (NetworkError, ExchangeError) as e:
            logger.error(
                "Failed to fetch balance from %s: %s", self._exchange_id, e, exc_info=True
            )
            raise

    async def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """Fetch current ticker (bid/ask/last) for a symbol."""
        exchange = await self._get_exchange()
        try:
            ticker = await exchange.fetch_ticker(symbol)
            return {
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "last": ticker.get("last"),
                "spread": (ticker["ask"] - ticker["bid"]) if ticker.get("ask") and ticker.get("bid") else None,
                "timestamp": ticker.get("timestamp"),
            }
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch ticker for %s from %s: %s", symbol, self._exchange_id, e)
            raise

    async def fetch_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """Fetch current funding rate for a symbol."""
        exchange = await self._get_exchange()
        try:
            if not exchange.has.get("fetchFundingRate"):
                return {"symbol": symbol, "rate": 0.0, "next_funding_time": None}
            rate = await exchange.fetch_funding_rate(symbol)
            return {
                "symbol": symbol,
                "rate": rate.get("fundingRate"),
                "next_funding_time": rate.get("nextFundingTimestamp"),
            }
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch funding rate for %s from %s: %s", symbol, self._exchange_id, e)
            return {"symbol": symbol, "rate": 0.0, "next_funding_time": None}

    async def fetch_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Fetch market sentiment (not universally supported in CCXT)."""
        return {"symbol": symbol, "long_pct": 50.0, "short_pct": 50.0, "num_buyers": 0, "num_sellers": 0}

    async def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently active/pending orders."""
        exchange = await self._get_exchange()
        try:
            orders = await exchange.fetch_open_orders(symbol)
            return orders
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch open orders from %s: %s", self._exchange_id, e)
            raise

    async def fetch_closed_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch recently closed/filled orders."""
        exchange = await self._get_exchange()
        try:
            orders = await exchange.fetch_closed_orders(symbol)
            return orders
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch closed orders from %s: %s", self._exchange_id, e)
            raise

    async def fetch_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch currently open positions."""
        exchange = await self._get_exchange()
        try:
            if not exchange.has.get("fetchPositions"):
                return []
            positions = await exchange.fetch_positions(symbols=[symbol] if symbol else None)
            return positions
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch positions from %s: %s", self._exchange_id, e)
            raise

    async def fetch_trades(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch trade/transaction history."""
        exchange = await self._get_exchange()
        try:
            trades = await exchange.fetch_my_trades(symbol)
            return trades
        except (NetworkError, ExchangeError) as e:
            logger.error("Failed to fetch trades from %s: %s", self._exchange_id, e)
            raise

    async def get_market_hours(self, symbol: str) -> Dict[str, Any]:
        """Crypto markets are 24/7."""
        return {
            "timezone": "UTC",
            "schedule": [{"day": i, "open": "00:00", "close": "23:59"} for i in range(7)],
            "is_open": True,
        }

    async def close(self) -> None:
        """Close the underlying CCXT exchange connection if it exists."""
        if self._exchange:
            logger.info("Closing CCXT exchange connection: %s", self._exchange_id)
            await self._exchange.close()
            self._exchange = None