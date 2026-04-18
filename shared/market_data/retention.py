"""
Module: retention
Purpose: Manages candle partition lifecycle and data retention
Location: /opt/tickles/shared/market-data/retention.py
"""

import logging
from datetime import datetime, timedelta
import re
from typing import List, Optional, TypedDict

import aiomysql

from ..utils.db import DatabasePool
from ..utils import config

logger = logging.getLogger(__name__)

class PartitionStatus(TypedDict):
    PARTITION_NAME: str
    TABLE_ROWS: int
    DATA_LENGTH: int
    PARTITION_ORDINAL_POSITION: int

class RetentionManager:
    """Manages candle partition lifecycle and data retention.
    
    Retention policy:
    - 1m candles: keep 30 days
    - 5m candles: keep 90 days
    - 15m candles: keep 180 days
    - 1h candles: keep 730 days (2 years)
    - 4h+ candles: keep forever (None)
    """
    
    RETENTION_DAYS = {
        '1m': 30,
        '5m': 90,
        '15m': 180,
        '1h': 730,
        '4h': None,   # forever
        '1d': None,   # forever
        '1w': None,   # forever
    }
    
    def __init__(self, db_pool: DatabasePool):
        self._db = db_pool
        self._retention_overrides: dict = {}

    @staticmethod
    def _validate_partition_name(name: str) -> None:
        """Guard against SQL injection in dynamic partition DDL.
        Only allows names matching p_YYYY_MM or p_future."""
        if not re.match(r"^p_(\d{4}_\d{2}|future)$", name):
            raise ValueError(f"Invalid partition name: {name}")

    async def load_retention_config(self) -> None:
        """Load retention overrides from system_config table.
        Keys like 1m_days, 5m_days map to timeframe retention.
        Falls back to RETENTION_DAYS class defaults if not in DB."""
        try:
            rows = await self._db.fetch_all(
                "SELECT config_key, config_value FROM tickles_shared.system_config "
                "WHERE namespace = %s", ("candle_retention",)
            )
            for row in rows:
                key = row["config_key"]
                tf = key.replace("_days", "")
                try:
                    val = int(row["config_value"])
                    self._retention_overrides[tf] = val
                except (ValueError, TypeError):
                    pass
            if self._retention_overrides:
                logger.info("Loaded retention overrides: %s", self._retention_overrides)
        except Exception as e:
            logger.warning("Could not load retention config, using defaults: %s", e)

    def get_retention_days(self, timeframe: str):
        """Get retention days for a timeframe, checking overrides first."""
        if timeframe in self._retention_overrides:
            return self._retention_overrides[timeframe]
        return self.RETENTION_DAYS.get(timeframe)
    
    async def ensure_partitions(self) -> None:
        """Create partitions for the current month and the next two months."""
        now = datetime.utcnow()
        partitions_to_ensure = []
        current_year, current_month = now.year, now.month

        for i in range(3):
            target_year, target_month = current_year, current_month + i
            if target_month > 12:
                target_year += (target_month - 1) // 12
                target_month = (target_month - 1) % 12 + 1

            partition_name = f"p_{target_year}_{str(target_month).zfill(2)}"
            
            next_month_year, next_month = target_year, target_month + 1
            if next_month > 12:
                next_month_year += 1
                next_month = 1
            upper_bound_date = datetime(next_month_year, next_month, 1)

            partitions_to_ensure.append((partition_name, upper_bound_date.strftime('%Y-%m-%d')))

        query_existing = """
        SELECT PARTITION_NAME from information_schema.PARTITIONS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'candles'
        """
        existing_partitions = {p['PARTITION_NAME'] for p in await self._db.fetch_all(query_existing, (config.DB_NAME,))}

        for name, upper_bound in partitions_to_ensure:
            if name not in existing_partitions:
                self._validate_partition_name(name)
                alter_query = f"""
                ALTER TABLE tickles_shared.candles REORGANIZE PARTITION p_future INTO (
                    PARTITION {name} VALUES LESS THAN (TO_DAYS('{upper_bound}')),
                    PARTITION p_future VALUES LESS THAN MAXVALUE
                )
                """
                try:
                    await self._db.execute(alter_query)
                    logger.info("Successfully created partition: %s", name)
                except aiomysql.Error as e:
                    logger.error("Failed to create partition %s for upper bound %s", name, upper_bound, exc_info=True)
    
    async def drop_expired_partitions(self) -> None:
        """Drop partitions that are older than the retention policy allows."""
        now = datetime.utcnow()

        # Find the oldest date we need to keep data for, across all timeframes.
        all_days = [self.get_retention_days(tf) for tf in self.RETENTION_DAYS]
        shortest_retention_days = min(d for d in all_days if d is not None)
        cutoff_date = now - timedelta(days=shortest_retention_days)

        query = """
        SELECT PARTITION_NAME FROM information_schema.PARTITIONS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'candles'
          AND PARTITION_NAME NOT IN ('p_future')
        """
        all_partitions = await self._db.fetch_all(query, (config.DB_NAME,))
        
        partitions_to_drop = []
        for part in all_partitions:
            part_name = part['PARTITION_NAME']
            try:
                # example: p_2024_01
                if not part_name.startswith('p_'): continue
                year = int(part_name[2:6])
                month = int(part_name[7:9])
                partition_date = datetime(year, month, 1)

                if partition_date < cutoff_date.replace(day=1):
                    self._validate_partition_name(part_name)
                    partitions_to_drop.append(part_name)

            except (ValueError, IndexError):
                logger.warning("Could not parse date from partition name: %s", part_name)
        
        if partitions_to_drop:
            partitions_str = ",".join(partitions_to_drop)
            drop_query = f"ALTER TABLE tickles_shared.candles DROP PARTITION {partitions_str}"
            try:
                await self._db.execute(drop_query)
                logger.info("Dropped expired partitions: %s", partitions_to_drop)
            except aiomysql.Error as e:
                logger.error("Failed to drop partitions: %s", e, exc_info=True)
    
    async def get_partition_status(self) -> List[dict]:
        """Get current partition status for monitoring."""
        query = """
        SELECT
            PARTITION_NAME,
            TABLE_ROWS,
            DATA_LENGTH,
            PARTITION_ORDINAL_POSITION
        FROM information_schema.PARTITIONS
        WHERE TABLE_SCHEMA = %s
        AND TABLE_NAME = 'candles'
        ORDER BY PARTITION_ORDINAL_POSITION
        """
        return await self._db.fetch_all(query, (config.DB_NAME,))