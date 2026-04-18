"""
MySQL Candle Data Loader

Replaces SQLite database_av.db with MySQL for candle data access.
All Python scripts should use this module instead of direct sqlite3 connections.

Usage:
    from mysql_candle_loader import load_epic_data, get_mysql_connection
    
    candles = load_epic_data("SOXL", "2024-01-01", "2024-12-31", "5m")
"""

import mysql.connector
import pandas as pd
import os
import sys
from typing import List, Tuple, Optional
from datetime import datetime

# Use centralized database config
from db_config import get_database_url, parse_database_url


def get_mysql_connection():
    """Create MySQL connection using centralized config"""
    db_url = get_database_url()
    config = parse_database_url(db_url)
    return mysql.connector.connect(**config)

def get_table_name(epic: str, data_source: str = 'capital') -> str:
    """
    DEPRECATED: Now using unified 'candles' table.
    Kept for backward compatibility with legacy code.
    """
    # All data now lives in unified 'candles' table
    return "candles"

def load_epic_data(
    epic: str,
    start_date: str,
    end_date: str,
    timeframe: str = "5m",
    data_source: str = "capital",
    include_bid_ask: bool = True
) -> pd.DataFrame:
    """
    Load candle data from unified MySQL 'candles' table for a given epic and date range.
    
    Args:
        epic: Epic symbol (e.g., "SOXL", "TECL", "BTCUSD")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        timeframe: Timeframe ('5m' or '1m')
        data_source: 'capital' for Capital.com data (UTC) - this is the default and recommended source
        include_bid_ask: If True, include closeBid/closeAsk columns for accurate spread calculation
    
    Returns:
        DataFrame with columns: snapshotTime, openPrice, highPrice, lowPrice, closePrice, lastTradedVolume, date
        If include_bid_ask=True (default), also includes: closeBid, closeAsk
    """
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    # Unified candles table uses source column: 'capital' or 'av'
    source_value = 'capital' if data_source == 'capital' else 'av'
    
    # Query the unified candles table
    # Capital.com data has bid/ask columns - include them for accurate spread calculation
    # Per Capital.com docs (Dec 2025): spread_cost = contracts × (ask - bid)
    # 
    # NOTE: Use DATE() for date comparison to include all candles on end_date
    # Without this, 'timestamp <= 2026-01-05' is interpreted as '<= 2026-01-05 00:00:00'
    # which excludes all candles during that day!
    query = """
    SELECT 
        timestamp,
        COALESCE((open_bid + open_ask) / 2, close_mid) as open,
        COALESCE((high_bid + high_ask) / 2, close_mid) as high,
        COALESCE((low_bid + low_ask) / 2, close_mid) as low,
        COALESCE((close_bid + close_ask) / 2, close_mid) as close,
        close_bid,
        close_ask,
        COALESCE(volume, 0) as volume
    FROM candles
    WHERE epic = %s 
      AND source = %s 
      AND timeframe = %s
      AND DATE(timestamp) >= %s 
      AND DATE(timestamp) <= %s
    ORDER BY timestamp
    """
    
    # Debug: Print actual query params
    print(f"[CandleLoader] Query: epic={epic}, source={source_value}, tf={timeframe}, start={start_date}, end={end_date}", file=sys.stderr, flush=True)
    
    cursor.execute(query, (epic, source_value, timeframe, start_date, end_date))
    rows = cursor.fetchall()
    print(f"[CandleLoader] Rows fetched: {len(rows)}", file=sys.stderr, flush=True)
    
    cursor.close()
    conn.close()
    
    if not rows:
        print(f"[CandleLoader] No data found for {epic} {timeframe} ({data_source}) from {start_date} to {end_date}", file=sys.stderr)
        return pd.DataFrame(columns=['snapshotTime', 'openPrice', 'highPrice', 'lowPrice', 'closePrice', 'closeBid', 'closeAsk', 'lastTradedVolume', 'date'])
    
    # Convert to DataFrame - now includes close_bid and close_ask
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'close_bid', 'close_ask', 'volume'])
    
    # Convert decimal strings to float (including bid/ask)
    for col in ['open', 'high', 'low', 'close', 'close_bid', 'close_ask', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(float)
    
    # Rename columns to match backtest runner expectations (camelCase)
    df = df.rename(columns={
        'open': 'openPrice',
        'high': 'highPrice',
        'low': 'lowPrice',
        'close': 'closePrice',
        'close_bid': 'closeBid',
        'close_ask': 'closeAsk',
        'volume': 'lastTradedVolume'
    })
    
    # Convert timestamp to datetime
    # ALL timestamps are now stored and used in UTC - no timezone conversion needed
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # NOTE: We previously converted Capital.com UTC to Eastern Time for AV compatibility.
    # This is NO LONGER NEEDED - all data is now stored and used in UTC.
    # The old AV tables have been deleted and we only use Capital.com data.
    
    df['date'] = df['timestamp'].dt.date
    
    # Rename timestamp to snapshotTime (expected by backtest_runner)
    df = df.rename(columns={'timestamp': 'snapshotTime'})
    
    # Optionally remove bid/ask columns if not needed (backward compatibility)
    if not include_bid_ask:
        df = df.drop(columns=['closeBid', 'closeAsk'], errors='ignore')
    
    print(f"[CandleLoader] Loaded {len(df)} candles for {epic} {timeframe} ({data_source}), bid/ask={include_bid_ask}", file=sys.stderr)
    
    return df

def get_date_range(epic: str, timeframe: str = '5m', source: str = 'capital') -> Tuple[Optional[str], Optional[str], int]:
    """
    Get the current date range and candle count for an epic from the unified candles table.
    
    Args:
        epic: Epic symbol (e.g., "SOXL")
        timeframe: Timeframe ('5m' or '1m')
        source: Data source ('capital' or 'av')
    
    Returns:
        (first_date, last_date, candle_count)
    """
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    query = """
    SELECT MIN(timestamp) as first_date, MAX(timestamp) as last_date, COUNT(*) as count
    FROM candles
    WHERE epic = %s AND timeframe = %s AND source = %s
    """
    
    cursor.execute(query, (epic, timeframe, source))
    result = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not result or result[0] is None:
        return None, None, 0
    
    return result[0], result[1], result[2]

def check_epic_data(epic: str, timeframe: str = '5m', source: str = 'capital') -> dict:
    """
    Check if an epic has data in the unified candles table.
    
    Args:
        epic: Epic symbol
        timeframe: Timeframe ('5m' or '1m')
        source: Data source ('capital' or 'av')
    
    Returns:
        {'has_data': bool, 'candle_count': int, 'is_complete': bool}
    """
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    # Query unified candles table
    query = """
    SELECT COUNT(*) FROM candles
    WHERE epic = %s AND timeframe = %s AND source = %s
    """
    cursor.execute(query, (epic, timeframe, source))
    count = cursor.fetchone()[0]
    
    cursor.close()
    conn.close()
    
    # Consider "complete" if we have at least 50,000 candles (rough heuristic)
    is_complete = count >= 50000
    
    return {
        'has_data': count > 0,
        'candle_count': count,
        'is_complete': is_complete
    }

def insert_candles(epic: str, candles: List[Tuple], timeframe: str = '5m', source: str = 'capital') -> Tuple[int, int]:
    """
    Insert candles into unified MySQL 'candles' table (with duplicate handling).
    
    Note: This function is mostly deprecated - the TypeScript CandleDataService 
    handles insertions. Kept for backward compatibility.
    
    Args:
        epic: Epic symbol
        candles: List of (timestamp, open, high, low, close, volume) tuples
        timeframe: Timeframe ('5m' or '1m')
        source: Data source ('capital' or 'av')
    
    Returns:
        (new_count, duplicate_count)
    """
    if not candles:
        return 0, 0
    
    conn = get_mysql_connection()
    cursor = conn.cursor()
    
    # Get existing count
    cursor.execute(
        "SELECT COUNT(*) FROM candles WHERE epic = %s AND timeframe = %s AND source = %s",
        (epic, timeframe, source)
    )
    result = cursor.fetchone()
    before_count = result[0] if result else 0
    
    # Insert with ON DUPLICATE KEY UPDATE
    # The unified table uses (epic, source, timeframe, timestamp) as composite key
    insert_sql = """
    INSERT INTO candles (epic, source, timeframe, timestamp, close_mid, volume)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
        close_mid = VALUES(close_mid),
        volume = VALUES(volume)
    """
    
    # Reformat candles for unified table structure
    formatted_candles = [
        (epic, source, timeframe, c[0], c[4], c[5])  # timestamp, close, volume
        for c in candles
    ]
    
    cursor.executemany(insert_sql, formatted_candles)
    conn.commit()
    
    # Get new count
    cursor.execute(
        "SELECT COUNT(*) FROM candles WHERE epic = %s AND timeframe = %s AND source = %s",
        (epic, timeframe, source)
    )
    after_count = cursor.fetchone()[0]
    
    cursor.close()
    conn.close()
    
    new_count = after_count - before_count
    duplicate_count = len(candles) - new_count
    
    return new_count, duplicate_count
