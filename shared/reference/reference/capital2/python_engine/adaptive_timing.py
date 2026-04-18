"""
Adaptive Timing System for Backtesting

Handles DST, holidays, and early market closes by using actual candle data
instead of fixed times. This ensures accurate backtesting across different
market conditions.
"""

from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Optional, Tuple
import pandas as pd


def get_adaptive_market_close(
    df: pd.DataFrame,
    day: pd.Timestamp,
    mode: str = 'EpicClosingTime'
) -> Optional[dt_time]:
    """
    Get the actual market close time for a specific day based on candle data.
    
    This handles:
    - DST transitions (candles have correct timestamps)
    - Early market closes (holidays, half-days)
    - Extended hours trading (SOXL, etc.)
    
    Args:
        df: Full dataframe with all candle data
        day: The specific trading day to analyze
        mode: 'EpicClosingTime' or 'USMarketClosingTime'
    
    Returns:
        The actual market close time for that day, or None if no data
    """
    # Convert day to date object for comparison (handles both pd.Timestamp and datetime)
    if hasattr(day, 'date'):
        day_date = day.date() if callable(day.date) else day.date
    else:
        day_date = day
    
    # Get all candles for this specific day
    day_data = df[df['date'] == day_date]
    
    if len(day_data) == 0:
        return None
    
    # Find the last candle of the day
    last_candle = day_data.iloc[-1]
    last_candle_time = last_candle['snapshotTime'].time()
    
    return last_candle_time


def get_adaptive_timing_for_day(
    df: pd.DataFrame,
    day: pd.Timestamp,
    mode: str = 'MarketClose',
    close_offset_seconds: int = 30,
    open_offset_seconds: int = 15,
    calc_offset_seconds: int = 0
) -> Dict[str, dt_time]:
    """
    Calculate entry, exit, and calculation times for a specific day using adaptive timing.
    
    The market close time is detected from the actual candle data for each day,
    handling DST, holidays, early closes, and different epic types automatically.
    
    Args:
        df: Full dataframe with all candle data
        day: The specific trading day
        mode: Timing mode:
            - 'MarketClose': Use last candle of day (calc_offset=0)
            - 'T5BeforeClose': Use candle 5 min before close (calc_offset=300)
            - 'T60FakeCandle': Use fake 5-min from 1-min data (calc_offset=60)
            - Legacy modes also supported
        close_offset_seconds: Seconds before close to exit (default 30)
        open_offset_seconds: Seconds before close to enter (default 15)
        calc_offset_seconds: Seconds before close to calculate (default 0)
    
    Returns:
        Dictionary with 'entry_time', 'exit_time', 'calc_time', and 'market_close'
    """
    # Get the actual market close for this day from the data
    market_close = get_adaptive_market_close(df, day, mode)
    
    if market_close is None:
        # Fallback to standard US market hours if no data
        market_close = dt_time(16, 0, 0)
    
    # Set calc_offset based on mode
    if mode == 'MarketClose':
        calc_offset_seconds = 0       # Use closing candle
    elif mode == 'T5BeforeClose' or mode == 'EpicClosingTimeBrainCalc' or mode == 'SecondLastCandle':
        calc_offset_seconds = 300     # T-5 minutes
    elif mode == 'T60FakeCandle' or mode == 'Fake5min_4thCandle':
        calc_offset_seconds = 60      # T-60 seconds (uses 4x 1-min fake candle)
    elif mode == 'Fake5min_3rdCandle_API':
        calc_offset_seconds = 120     # T-120 seconds (uses 3x 1-min + API call)
    elif mode == 'OriginalBotTime':
        calc_offset_seconds = 120     # T-2 minutes
    # else: use the provided calc_offset_seconds
    
    # Calculate entry, exit, and calc times as offsets from actual close
    close_dt = datetime.combine(datetime.today(), market_close)
    exit_dt = close_dt - timedelta(seconds=close_offset_seconds)
    entry_dt = close_dt - timedelta(seconds=open_offset_seconds)
    calc_dt = close_dt - timedelta(seconds=calc_offset_seconds)
    
    return {
        'entry_time': entry_dt.time(),
        'exit_time': exit_dt.time(),
        'calc_time': calc_dt.time(),
        'market_close': market_close,
    }


def get_us_market_baseline_close(df: pd.DataFrame) -> dt_time:
    """
    Find the earliest typical market close time across all trading days.
    This represents the standard US market close (usually 16:00 ET).
    
    Filters out obvious early closes (< 14:00) to find the "normal" close time.
    
    Args:
        df: Full dataframe with all candle data
    
    Returns:
        The baseline US market close time
    """
    # Get all unique trading days
    trading_days = df['date'].unique()
    
    close_times = []
    for day in trading_days:
        day_data = df[df['date'] == day]
        if len(day_data) > 0:
            last_candle_time = day_data.iloc[-1]['snapshotTime'].time()
            
            # Filter out early closes (holidays, half-days)
            # Only consider closes after 14:00 as "normal" market hours
            if last_candle_time.hour >= 14:
                close_times.append(last_candle_time)
    
    if len(close_times) == 0:
        # Fallback to standard 4pm ET
        return dt_time(16, 0, 0)
    
    # Sort and get the earliest "normal" close time
    close_times.sort()
    
    # Return the most common early close time (typically 16:00 ET)
    # Use the 10th percentile to avoid outliers
    percentile_idx = max(0, int(len(close_times) * 0.1))
    return close_times[percentile_idx]


def should_use_adaptive_timing(mode: str) -> bool:
    """
    Check if the timing mode requires adaptive timing logic.
    
    Args:
        mode: Timing mode string
    
    Returns:
        True if adaptive timing should be used
    """
    # All auto-detect modes use adaptive timing
    auto_detect_modes = [
        'MarketClose',           # Auto-detect close
        'T5BeforeClose',         # Auto-detect close, then T-5min
        'T60FakeCandle',         # Auto-detect close, then T-60s with fake candle
        # New fake candle modes
        'Fake5min_4thCandle',    # 4 x 1-min candles = fake 5-min (T-60s)
        'Fake5min_3rdCandle_API', # 3 x 1-min + API = fake 5-min (T-120s)
        'SecondLastCandle',      # Second-to-last candle of day
        # Legacy modes that also use adaptive timing
        'EpicClosingTime',
        'EpicClosingTimeBrainCalc',
        # Random modes need to know market close for exit
        'random_morning',
        'random_afternoon',
        'Random',
        # ManusTime and OriginalBotTime also benefit from auto-detect
        'ManusTime',
        'OriginalBotTime',
    ]
    return mode in auto_detect_modes


# Test the adaptive timing system
if __name__ == '__main__':
    print("Adaptive Timing System")
    print("=" * 50)
    print()
    print("This module provides adaptive timing that:")
    print("  ✓ Handles DST transitions automatically")
    print("  ✓ Detects early market closes (holidays)")
    print("  ✓ Supports extended hours trading")
    print("  ✓ Uses actual candle timestamps as source of truth")
    print()
    print("Modes:")
    print("  - EpicClosingTime: Uses each epic's actual close time")
    print("  - USMarketClosingTime: Uses earliest close across all days")
