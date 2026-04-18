#!/usr/bin/env python3
"""
Get Current Signal - Check if an indicator signals BUY on the latest candle
WITHOUT running a full backtest

This is used by:
1. Brain preview - check if indicator signals BUY on current data
2. Live trading - real-time signal calculation with WebSocket data
3. Backtesting validation - compare live vs historical signals

Supports two modes:
A) Database mode: Load historical data from MySQL (default)
B) Real-time mode: Use injected candle data (for WebSocket/live trading)

When fake_5min_close is provided, it appends a synthetic candle to historical
data to simulate what the brain would see at T-60s before market close.

REFACTORED: Now uses unified_signal.py for core signal evaluation.
This ensures identical calculation logic with backtest_runner.py.
"""

import sys
import json
# SQLite removed - now using MySQL via mysql_candle_loader
from mysql_candle_loader import load_epic_data as mysql_load_epic_data
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

# Import unified signal evaluation (SINGLE SOURCE OF TRUTH)
from unified_signal import (
    evaluate_indicator_at_index,
    check_data_sufficiency,
    map_indicator_params,
)


def load_epic_data_from_sqlite(db_path: str, epic: str, start_date: str, end_date: str, data_source: str = 'capital') -> pd.DataFrame:
    """
    Load epic data from MySQL database (db_path parameter ignored, kept for compatibility)
    
    ALL DATA IS FROM CAPITAL.COM IN UTC - NO TIMEZONE CONVERSION NEEDED.
    
    Args:
        db_path: DEPRECATED - ignored, kept for compatibility
        epic: Epic symbol (e.g., 'SOXL')
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        data_source: 'capital' for Capital.com data (UTC) - only supported source
        
    Returns:
        DataFrame with OHLCV data (timestamps in UTC)
    """
    # Load from MySQL using mysql_candle_loader with specified data source
    df = mysql_load_epic_data(epic, start_date, end_date, "5m", data_source)
    
    if df.empty:
        raise ValueError(f"No data found for {epic}")
    
    # Convert timestamp to datetime and normalize column names
    if 'timestamp' in df.columns:
        df['snapshotTime'] = pd.to_datetime(df['timestamp'])
    elif 'snapshotTime' not in df.columns:
        raise ValueError('No timestamp column found')
    
    # Normalize price column names
    if 'open' in df.columns:
        df['openPrice'] = df['open']
        df['highPrice'] = df['high']
        df['lowPrice'] = df['low']
        df['closePrice'] = df['close']
        df['lastTradedVolume'] = df['volume']
    
    # Add timestamp column if not present
    if 'timestamp' not in df.columns:
        df['timestamp'] = df['snapshotTime']
    
    # Filter by date range
    if start_date:
        start = pd.to_datetime(start_date)
        df = df[df['snapshotTime'] >= start]
    
    if end_date:
        end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        df = df[df['snapshotTime'] < end]
    
    if df.empty:
        raise ValueError(f"No data found for {epic} between {start_date} and {end_date}")
    
    return df.sort_values('snapshotTime').reset_index(drop=True)


def append_fake_candle(
    df: pd.DataFrame,
    fake_5min_close: float,
    fake_5min_timestamp: Optional[str] = None
) -> pd.DataFrame:
    """
    Append a synthetic "fake" 5-minute candle to simulate T-60s brain calculation.
    
    In live trading at T-60s, we have 4x 1-minute candles and use the API price
    to create a "fake" 5-minute candle. This function replicates that behavior
    for brain preview and validation.
    
    Args:
        df: DataFrame with existing candle data
        fake_5min_close: Close price from T-60 API call
        fake_5min_timestamp: Optional timestamp for the fake candle (ISO format)
        
    Returns:
        DataFrame with fake candle appended
    """
    last_candle = df.iloc[-1]
    
    # Calculate timestamp for fake candle
    if fake_5min_timestamp:
        fake_timestamp = pd.to_datetime(fake_5min_timestamp)
    else:
        fake_timestamp = last_candle['snapshotTime'] + pd.Timedelta(minutes=5)
    
    # Create fake candle - use fake_5min_close for OHLC
    # Open = previous close (continuous)
    # High = max(open, close)
    # Low = min(open, close)
    # Close = API price
    fake_candle = pd.DataFrame([{
        'snapshotTime': fake_timestamp,
        'timestamp': fake_timestamp,
        'openPrice': float(last_candle['closePrice']),
        'highPrice': max(float(last_candle['closePrice']), fake_5min_close),
        'lowPrice': min(float(last_candle['closePrice']), fake_5min_close),
        'closePrice': fake_5min_close,
        'lastTradedVolume': 0,
    }])
    
    df = pd.concat([df, fake_candle], ignore_index=True)
    print(f"[Signal] Appended fake 5-min candle: close={fake_5min_close}, timestamp={fake_timestamp}", file=sys.stderr)
    
    return df


def get_indicator_signal(
    df: pd.DataFrame,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    fake_5min_close: Optional[float] = None,
    fake_5min_timestamp: Optional[str] = None,
    crash_protection_enabled: bool = False,
) -> Dict[str, Any]:
    """
    Get the current signal for an indicator on the latest candle.
    
    NOW USES UNIFIED SIGNAL EVALUATION for consistency with backtest.
    
    Args:
        df: DataFrame with OHLCV data
        indicator_name: Name of indicator (e.g., 'rsi_oversold')
        indicator_params: Indicator parameters (e.g., {'period': 14, 'threshold': 30})
        fake_5min_close: Optional - if provided, append a synthetic candle with this close price
                         This simulates what the brain sees at T-60s (using 1-min data to build fake 5-min)
        fake_5min_timestamp: Optional - timestamp for the fake candle (ISO format)
        crash_protection_enabled: Whether to check crash protection (must match backtest)
        
    Returns:
        {
            'signal': 1 (BUY) or 0 (HOLD),
            'indicator_value': Current indicator value,
            'timestamp': Timestamp of latest candle,
            'close_price': Close price of latest candle,
            'used_fake_candle': True if fake_5min_close was used,
            'data_warning': Optional warning if data might be insufficient,
            'crash_blocked': True if signal was blocked by crash protection
        }
    """
    used_fake_candle = False
    
    # Append fake candle if provided (simulates T-60s brain calculation)
    if fake_5min_close is not None:
        df = append_fake_candle(df, fake_5min_close, fake_5min_timestamp)
        used_fake_candle = True
    
    # Check data sufficiency
    candles_available, candles_needed, data_warning = check_data_sufficiency(df, indicator_params)
    if data_warning:
        print(f"[Signal] {data_warning}", file=sys.stderr)
    
    # === USE UNIFIED SIGNAL EVALUATION ===
    # This ensures identical calculation with backtest_runner.py
    eval_result = evaluate_indicator_at_index(
        df=df,
        indicator_name=indicator_name,
        indicator_params=indicator_params,
        check_index=-1,  # Last candle
        crash_protection_enabled=crash_protection_enabled,
    )
    
    # Handle errors from unified evaluation
    if eval_result.get('error'):
        raise ValueError(eval_result['error'])
    
    # Build result in expected format
    result = {
        'signal': 1 if eval_result['signal'] else 0,
        'indicator_value': eval_result['indicator_value'],
        'timestamp': eval_result['timestamp'],
        'close_price': eval_result['close_price'],
        'candles_loaded': candles_available,
        'candles_needed': candles_needed,
        'used_fake_candle': used_fake_candle,
        'crash_blocked': eval_result['crash_blocked'],
    }
    
    if eval_result.get('crash_reason'):
        result['crash_reason'] = eval_result['crash_reason']
    
    if data_warning:
        result['data_warning'] = data_warning
    
    return result


def main():
    """
    Main entry point
    
    Expects JSON config as command line argument:
    {
        "db_path": "/path/to/database.db",  # Deprecated, ignored
        "epic": "SOXL",
        "start_date": "2024-10-28",
        "end_date": "2025-10-28",
        "indicator_name": "rsi_oversold",
        "indicator_params": {"period": 14, "threshold": 30},
        
        # Optional: Real-time mode parameters
        "fake_5min_close": 25.50,        # If provided, append fake candle with this close
        "fake_5min_timestamp": "2025-01-15T19:59:00",  # Timestamp for fake candle
        "data_source": "capital",        # 'capital' - Capital.com data (UTC) - only supported source
        "crash_protection_enabled": false  # MUST match original backtest for consistent signals
    }
    """
    
    if len(sys.argv) < 2:
        print("ERROR: Missing config JSON argument", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = json.loads(sys.argv[1])
        
        # Load data from MySQL using specified data source (default to Capital.com for realism)
        data_source = config.get('data_source', 'capital')
        timing_config = config.get('timing_config', {'mode': 'Fake5min_3rdCandle_API'})
        crash_protection = config.get('crash_protection_enabled', False)
        
        # Get date range - default to 60 days before end_date if start_date not provided
        end_date = config.get('end_date', datetime.now().strftime('%Y-%m-%d'))
        
        if 'start_date' in config and config['start_date']:
            start_date = config['start_date']
        else:
            # Default: 60 days before end_date (enough history for most indicators)
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            start_dt = end_dt - timedelta(days=60)
            start_date = start_dt.strftime('%Y-%m-%d')
        
        print(f"[Signal] Loading data: epic={config['epic']}, source={data_source}, "
              f"timing={timing_config.get('mode')}, crashProtection={crash_protection}, "
              f"dates={start_date} to {end_date}", file=sys.stderr)
        
        df = load_epic_data_from_sqlite(
            db_path=config.get('db_path', ''),  # Ignored, kept for compatibility
            epic=config['epic'],
            start_date=start_date,
            end_date=end_date,
            data_source=data_source,
        )
        
        # Get signal (with optional fake candle and crash protection)
        # NOW USES UNIFIED SIGNAL EVALUATION
        result = get_indicator_signal(
            df=df,
            indicator_name=config['indicator_name'],
            indicator_params=config.get('indicator_params', {}),
            fake_5min_close=config.get('fake_5min_close'),
            fake_5min_timestamp=config.get('fake_5min_timestamp'),
            crash_protection_enabled=crash_protection,
        )
        
        # Output result as JSON
        print(f"RESULT:{json.dumps(result)}")
        sys.exit(0)
        
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
