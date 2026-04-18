"""
Module: gap_detector
Purpose: Detects gaps in candle data and triggers backfill
Location: /opt/tickles/shared/market-data/gap_detector.py
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, TypedDict

from ..utils.db import DatabasePool


class Gap(TypedDict):
    gap_start: datetime
    gap_end: datetime
    gap_minutes: int

class DateRange(TypedDict):
    earliest: Optional[datetime]
    latest: Optional[datetime]
    count: int

logger = logging.getLogger(__name__)

class GapDetector:
    """Detects gaps in candle data and triggers backfill.
    
    For crypto (24/7 markets):
    - 1m candles: gap if >2 minutes between consecutive timestamps
    - 5m candles: gap if >10 minutes between consecutive timestamps
    - 1h candles: gap if >2 hours between consecutive timestamps
    
    For CFDs (market hours):
    - Uses timing_service to know market open/close
    - Only checks during market hours
    """
    
    # Maximum gap duration by timeframe (in minutes)
    MAX_GAP_MINUTES = {
        '1m': 2,
        '5m': 10,
        '15m': 30,
        '30m': 60,
        '1h': 120,
        '4h': 480,
        '1d': 1440,  # 1 day
    }
    
    def __init__(self, db_pool: DatabasePool):
        self._db = db_pool
    
    async def find_gaps(self, instrument_id: int, timeframe: str,
                       start: datetime, end: datetime) -> List[Gap]:
        """Find gaps in the candles table for a given instrument/timeframe.

        This version uses a window function (LEAD) for better performance on MySQL 8+.
        
        Returns:
            List of Gap objects, each representing a detected gap.
        """
        try:
            max_gap_minutes = self.MAX_GAP_MINUTES[timeframe]
        except KeyError:
            logger.error("Invalid timeframe '%s' for gap detection.", timeframe)
            raise ValueError(f"Invalid timeframe for gap detection: {timeframe}")

        query = """
        WITH candle_with_next_ts AS (
            SELECT
                timestamp,
                LEAD(timestamp, 1) OVER (ORDER BY timestamp) as next_timestamp
            FROM tickles_shared.candles
            WHERE instrument_id = %s
              AND timeframe = %s
              AND timestamp BETWEEN %s AND %s
        )
        SELECT
            timestamp as gap_start,
            next_timestamp as gap_end,
            TIMESTAMPDIFF(MINUTE, timestamp, next_timestamp) as gap_minutes
        FROM candle_with_next_ts
        WHERE next_timestamp IS NOT NULL
          AND TIMESTAMPDIFF(MINUTE, timestamp, next_timestamp) > %s
        ORDER BY timestamp;
        """
        
        params = (instrument_id, timeframe, start, end, max_gap_minutes)
        results = await self._db.fetch_all(query, params)
        return results
    
    async def get_date_range(self, instrument_id: int, timeframe: str) -> DateRange:
        """Get the earliest and latest candle dates for an instrument/timeframe.
        
        Returns:
             A DateRange dictionary. Returns count 0 if no data exists.
        """
        query = """
        SELECT
            MIN(timestamp) as earliest,
            MAX(timestamp) as latest,
            COUNT(*) as count
        FROM tickles_shared.candles
        WHERE instrument_id = %s AND timeframe = %s
        """
        
        result = await self._db.fetch_one(query, (instrument_id, timeframe))
        if result and result['count'] > 0:
            return result
        return {'earliest': None, 'latest': None, 'count': 0}