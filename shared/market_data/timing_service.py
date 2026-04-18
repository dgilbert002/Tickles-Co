"""Adaptive timing service for market hours, DST, and early closes.

Ported from Capital 2.0's adaptive_timing.py. Key changes:
- Reads from tickles_shared.candles instead of Capital.com format
- Supports crypto 24/7 markets (no market close)
- Supports CFD market hours (different per instrument type)
- Uses instrument metadata from tickles_shared.instruments
"""

import logging
from datetime import datetime, time, date, timedelta, timezone
from typing import Optional, Dict, TypedDict
from enum import Enum

from ..utils.db import DatabasePool

logger = logging.getLogger(__name__)

class MarketType(Enum):
    """Market type determines trading hours."""
    CRYPTO = "crypto"       # 24/7, no market close
    CFD_STOCKS = "cfd_stocks"     # Mon-Fri, 9:30-16:00 ET (US market hours)
    CFD_INDICES = "cfd_indices"    # Varies by index
    CFD_FOREX = "cfd_forex"       # 24/5, closes weekends
    CFD_COMMODITIES = "cfd_commodities"  # Varies by commodity


class TradingWindow(TypedDict):
    entry_time: time
    exit_time: time
    calc_time: time
    market_close: time


class TimingService:
    """Adaptive timing for market hours, DST, and early closes.
    
    For crypto: always open, no market close detection needed.
    For CFDs: uses candle data to detect actual close times (handles DST).
    """
    
    # Standard market hours by type (UTC)
    STANDARD_HOURS = {
        MarketType.CRYPTO: None,  # Always open
        MarketType.CFD_STOCKS: {
            "open": time(14, 30),   # 9:30 AM ET = 14:30 UTC
            "close": time(21, 0),   # 4:00 PM ET = 21:00 UTC
        },
        MarketType.CFD_INDICES: {
            "open": time(0, 0),     # Varies widely
            "close": time(21, 0),
        },
        MarketType.CFD_FOREX: {
            "open": time(22, 0),    # Sunday 22:00 UTC
            "close": time(22, 0),   # Friday 22:00 UTC
        },
        MarketType.CFD_COMMODITIES: {
            "open": time(0, 0),
            "close": time(22, 0),
        },
    }
    
    def __init__(self, db_pool: DatabasePool):
        self._db = db_pool
        self._market_type_cache: Dict[int, MarketType] = {}
    
    async def get_market_type(self, instrument_id: int) -> MarketType:
        """Determine market type from instrument metadata.
        
        Looks up instrument_type from the instruments table.
        Caches results for performance.
        """
        if instrument_id in self._market_type_cache:
            return self._market_type_cache[instrument_id]
        
        row = await self._db.fetch_one(
            "SELECT asset_class FROM tickles_shared.instruments WHERE id = %s",
            (instrument_id,)
        )
        if row is None:
            logger.warning("Instrument %d not found, defaulting to CRYPTO", instrument_id)
            self._market_type_cache[instrument_id] = MarketType.CRYPTO
            return MarketType.CRYPTO
        
        asset_class = row.get("asset_class")
        market_type_map = {
            "crypto": MarketType.CRYPTO,
            "stock": MarketType.CFD_STOCKS,
            "cfd": MarketType.CFD_STOCKS,
            "forex": MarketType.CFD_FOREX,
            "commodity": MarketType.CFD_COMMODITIES,
            "index": MarketType.CFD_INDICES,
        }
        mt = market_type_map.get(asset_class, MarketType.CRYPTO)
        
        self._market_type_cache[instrument_id] = mt
        return mt
    
    async def is_market_open(self, instrument_id: int, market_type: Optional[MarketType] = None) -> bool:
        """Check if a market is currently open.
        
        For crypto: always True.
        For CFDs: check current time against market hours.
        """
        if market_type is None:
            market_type = await self.get_market_type(instrument_id)
        
        if market_type == MarketType.CRYPTO:
            return True
        
        now = datetime.now(timezone.utc)
        weekday = now.weekday() # Monday is 0, Sunday is 6

        hours = self.STANDARD_HOURS.get(market_type)
        if hours is None:
            return True # 24/7 market
        
        # Handle Forex 24/5 market
        if market_type == MarketType.CFD_FOREX:
            # Closes Friday 22:00 UTC, Opens Sunday 22:00 UTC
            if weekday == 5: # Saturday
                return False
            if weekday == 4 and now.time() >= hours["close"]: # Friday after close
                return False
            if weekday == 6 and now.time() < hours["open"]: # Sunday before open
                return False
            return True
            
        current_time = now.time()
        open_time = hours["open"]
        close_time = hours["close"]

        # Standard Mon-Fri markets
        if weekday >= 5: # Saturday or Sunday
            return False
        
        if open_time <= close_time:
            return open_time <= current_time < close_time
        else: # Overnight market that crosses midnight
            return current_time >= open_time or current_time < close_time
    
    async def get_market_close(self, instrument_id: int, 
                                 day: date = None) -> Optional[time]:
        """Get the actual market close time for a specific day.
        
        For crypto: returns None (no close).
        For CFDs: uses candle data to detect actual close (handles DST).
        Falls back to standard hours if no candle data available.
        """
        market_type = await self.get_market_type(instrument_id)
        
        if market_type == MarketType.CRYPTO:
            return None
        
        if day is None:
            day = date.today()
        
        # Try to detect actual close from candle data
        row = await self._db.fetch_one(
            """SELECT MAX(timestamp) as last_candle 
               FROM tickles_shared.candles
               WHERE instrument_id = %s
               AND DATE(timestamp) = %s
               AND timeframe = '1m'""",
            (instrument_id, day)
        )
        
        if row and row["last_candle"]:
            last_ts = row["last_candle"]
            if isinstance(last_ts, datetime):
                return last_ts.time()
        
        # Fallback to standard hours
        hours = self.STANDARD_HOURS.get(market_type)
        if hours:
            return hours.get("close")
        return None
    
    async def get_trading_window(self, instrument_id: int,
                                   day: date = None) -> Optional[TradingWindow]:
        """Get entry/exit/calc times for a trading window.
        
        Returns:
            A TradingWindow dictionary or None for 24/7 markets.
        """
        market_type = await self.get_market_type(instrument_id)
        
        if market_type == MarketType.CRYPTO:
            return None  # No trading window for 24/7 markets
        
        market_close = await self.get_market_close(instrument_id, day)
        if market_close is None:
            return None
        
        # Calculate offsets from market close
        # Ensure we use the correct date and make it timezone-aware
        query_date = day if day else date.today()
        close_dt_naive = datetime.combine(query_date, market_close)
        close_dt_aware = close_dt_naive.replace(tzinfo=timezone.utc)
        
        # Note: 'entry' is when a position might be opened, 'exit' is the final moment to close.
        # The names were swapped; entry should be before exit.
        calc_dt = close_dt_aware - timedelta(minutes=5)
        entry_dt = close_dt_aware - timedelta(seconds=30)
        exit_dt = close_dt_aware - timedelta(seconds=15)
        
        return {
            "calc_time": calc_dt.time(),
            "entry_time": entry_dt.time(),
            "exit_time": exit_dt.time(),
            "market_close": market_close,
        }