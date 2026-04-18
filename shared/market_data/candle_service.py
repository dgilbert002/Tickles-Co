"""
Module: candle_service
Purpose: Main candle collection orchestrator for fetching and storing candle data
Location: /opt/tickles/shared/market-data/candle_service.py
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from ccxt.base.errors import ExchangeError

from ..connectors.base import Candle, BaseExchangeAdapter
from ..utils import config
from ..utils.db import DatabasePool

logger = logging.getLogger(__name__)

class CandleService:
    """Main candle collection orchestrator.
    
    Responsibilities:
    - Resolve instrument_id from exchange + symbol
    - Compute candle_data_hash (SHA-256 of OHLCV)
    - Write to tickles_shared.candles with ON DUPLICATE KEY UPDATE
    - Detect drift when existing candle hash differs from new data
    - Schedule gap detection and backfill
    """
    
    def __init__(self, db_pool: DatabasePool, adapters: Dict[str, BaseExchangeAdapter]):
        self._db = db_pool
        self._adapters = adapters
        self._instrument_cache: Dict[str, int] = {}

    async def resolve_instrument_id(self, exchange: str, symbol: str) -> int:
        """Look up instrument_id from the instruments table.
        
        Raises ValueError if instrument not found.
        Caches results in memory for performance.
        """
        cache_key = f"{exchange}:{symbol}"
        if cache_key in self._instrument_cache:
            return self._instrument_cache[cache_key]
            
        query = "SELECT id FROM instruments WHERE exchange = %s AND symbol = %s"
        result = await self._db.fetch_one(query, (exchange, symbol))
        
        if not result:
            raise ValueError(f"Instrument not found for {exchange}/{symbol}")
            
        instrument_id = result['id']
        self._instrument_cache[cache_key] = instrument_id
        return instrument_id

    async def collect_candles(self, exchange: str, symbol: str, timeframe: str,
                            since: Optional[datetime] = None, limit: int = 1000) -> (int, Optional[datetime]):
        """Fetch candles from exchange and write to DB.
        
        Returns:
            A tuple of (count of new rows inserted, timestamp of the last candle).
        """
        try:
            instrument_id = await self.resolve_instrument_id(exchange, symbol)
            adapter = self._adapters[exchange]

            # Fetch candles from exchange
            candles = await adapter.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
            if not candles:
                return 0, None

            # Prepare batch insert
            values = []
            for candle in candles:
                timestamp = candle.timestamp
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                
                values.append((
                    instrument_id,
                    timeframe,
                    timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.candle_data_hash,
                    candle.data_source,
                ))

            # Postgres-native upsert. Unique key = (instrument_id, source, timeframe, "timestamp").
            # `timestamp` and `open`/`close` are reserved words in Postgres → must be double-quoted.
            query = """
            INSERT INTO candles (
                instrument_id, timeframe, "timestamp", "open", high, low, "close", volume, data_hash, source
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instrument_id, source, timeframe, "timestamp") DO UPDATE SET
                "open" = EXCLUDED."open",
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                "close" = EXCLUDED."close",
                volume = EXCLUDED.volume,
                data_hash = EXCLUDED.data_hash,
                source = EXCLUDED.source
            """
            inserted_count = await self._db.execute_many(query, values)
            last_timestamp = candles[-1].timestamp if candles else None
            return inserted_count, last_timestamp

        except (ValueError, ConnectionError, ExchangeError) as e:
            logger.error(
                "Failed to collect candles for %s/%s/%s",
                exchange, symbol, timeframe, exc_info=True
            )
            raise

    async def collect_batch(self, exchange: str, symbols: List[str], timeframe: str,
                          since: Optional[datetime] = None) -> Dict[str, int]:
        """Collect candles for multiple symbols with rate limiting."""
        results = {}
        delay = config.CANDLE_FETCH_DELAY_MS / 1000

        for symbol in symbols:
            try:
                count, _ = await self.collect_candles(exchange, symbol, timeframe, since)
                results[symbol] = count
            except (ValueError, ConnectionError, ExchangeError):
                # Errors are already logged in collect_candles
                results[symbol] = 0
            await asyncio.sleep(delay)

        return results

    async def backfill(self, exchange: str, symbol: str, timeframe: str,
                     start_date: datetime, end_date: Optional[datetime] = None) -> int:
        """Backfill historical data for a symbol/timeframe."""
        total_candles = 0
        current_ts = start_date
        end_ts = end_date or datetime.now(timezone.utc)
        batch_size = config.CANDLE_FETCH_BATCH_SIZE
        delay_secs = config.CANDLE_FETCH_DELAY_MS / 1000
        timeframe_secs = self._timeframe_to_seconds(timeframe)

        while current_ts < end_ts:
            try:
                count, last_ts = await self.collect_candles(
                    exchange, symbol, timeframe, since=current_ts, limit=batch_size
                )
                total_candles += count
                logger.info("Backfilled %d candles for %s from %s", count, symbol, current_ts)

                if last_ts:
                    # Advance to the next candle after the last one we received
                    current_ts = last_ts + timedelta(seconds=timeframe_secs)
                elif count == 0:
                    # If no candles were returned, we are likely at the end of history.
                    # Advance by the requested window to continue searching forward.
                    logger.info("No candles returned for %s from %s, advancing window.", symbol, current_ts)
                    current_ts += timedelta(seconds=batch_size * timeframe_secs)

                await asyncio.sleep(delay_secs)

            except (ValueError, ConnectionError, ExchangeError) as e:
                logger.error("Error during backfill for %s, will retry after delay", symbol, exc_info=True)
                await asyncio.sleep(delay_secs * 5)  # Longer delay on error

        return total_candles

    def _timeframe_to_seconds(self, timeframe: str) -> int:
       """Convert timeframe string (e.g., '1m', '4h', '1d') to seconds."""
       try:
           value = int(timeframe[:-1])
           unit = timeframe[-1].lower()
           if unit == 'm':
               return value * 60
           elif unit == 'h':
               return value * 3600
           elif unit == 'd':
               return value * 86400
           else:
               raise ValueError(f"Invalid timeframe unit: {unit}")
       except (ValueError, IndexError) as e:
           logger.error("Invalid timeframe format: %s", timeframe, exc_info=True)
           raise ValueError(f"Invalid timeframe format: {timeframe}") from e