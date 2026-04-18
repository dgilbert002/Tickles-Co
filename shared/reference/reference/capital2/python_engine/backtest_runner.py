#!/usr/bin/env python3
"""
Backtest Runner - Bridges web GUI with Python backtesting engine
Reads from MySQL database, runs backtests, writes results back
"""

import os
import sys
import json
# SQLite removed - now using MySQL via mysql_candle_loader
import random
from datetime import datetime, time as dt_time
from typing import Dict, List, Any, Optional, Tuple
import pandas as pd
import numpy as np

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators import create_indicator_library
from crash_protection import check_crash_protection, CRASH_PROTECTION_CONFIG
from unified_signal import evaluate_indicator_at_index, map_indicator_params as unified_map_params
from indicators_numba import NumbaIndicatorLibrary
from focused_optimizer import calculate_trading_costs, get_price_at_time
from timing_system import TimingConfig, get_timing_config
from adaptive_timing import (
    should_use_adaptive_timing,
    get_adaptive_timing_for_day,
    get_us_market_baseline_close
)

# Import optimized signal-based backtest (7.5x faster, 100% identical results)
try:
    from backtest_runner_optimized import run_signal_based_backtest_optimized
    OPTIMIZED_SIGNAL_BACKTEST_AVAILABLE = True
except ImportError:
    OPTIMIZED_SIGNAL_BACKTEST_AVAILABLE = False
from mysql_candle_loader import load_epic_data as mysql_load_epic_data, get_mysql_connection
from market_info_loader import get_risk_settings
from liquidation_utils import (
    calculate_liquidation_trigger_price,
    check_liquidation_crossed,
    calculate_margin_level
)


def calculate_spread_cost(contracts: float, entry_candle: pd.Series, market_info: Dict, actual_notional: float) -> tuple:
    """
    Calculate spread cost using actual bid/ask from candle data if available.
    
    Per Capital.com official documentation (Dec 2025):
    - Spread cost = contracts × (ask - bid)
    - You pay half at entry (buy at ASK), half at exit (sell at BID)
    - Total = 1x spread per round-trip (NOT 2x)
    
    Args:
        contracts: Number of contracts/shares
        entry_candle: The candle data at trade entry (may contain closeBid/closeAsk)
        market_info: Market info dict with fallback spread_percent
        actual_notional: Notional value for percentage-based fallback
        
    Returns:
        (spread_cost, spread_method) - cost in dollars and method used ('actual' or 'percentage')
    """
    # Try to use actual bid/ask from candle data (Capital.com's exact method)
    if entry_candle is not None and 'closeBid' in entry_candle.index and 'closeAsk' in entry_candle.index:
        close_bid = entry_candle['closeBid']
        close_ask = entry_candle['closeAsk']
        
        # Only use if both values are valid (not zero or NaN)
        if close_bid > 0 and close_ask > 0 and not pd.isna(close_bid) and not pd.isna(close_ask):
            actual_spread = close_ask - close_bid
            # Capital.com formula: contracts × spread_in_dollars
            spread_cost = contracts * actual_spread * market_info.get('contract_multiplier', 1.0)
            return spread_cost, 'actual'
    
    # Fallback to percentage-based calculation if bid/ask not available
    spread_cost = actual_notional * market_info['spread_percent']
    return spread_cost, 'percentage'


# Timing Modes
TIMING_MODES = {
    'ManusTime': {
        'close_offset': 30,  # T-30s before market close
        'calculate_offset': 30,  # Same as close
        'open_offset': 15,  # T-15s before market close
    },
    'OriginalBotTime': {
        'close_offset': 30,  # T-30s before market close
        'calculate_offset': 120,  # T-120s before market close
        'open_offset': 15,  # T-15s before market close
    },
}

# Default timing (ManusTime)
ENTRY_TIME = dt_time(15, 59, 45)  # T-15s
EXIT_TIME = dt_time(15, 59, 30)  # T-30s

def get_timing_for_mode(mode: str = 'ManusTime', custom_offsets: Dict = None) -> Dict:
    """Get entry/exit times based on timing mode"""
    if mode == 'Custom' and custom_offsets:
        return {
            'entry_time': dt_time(16, 0, 0 - custom_offsets.get('open_offset', 15)),
            'exit_time': dt_time(16, 0, 0 - custom_offsets.get('close_offset', 30)),
            'calculate_time': dt_time(16, 0, 0 - custom_offsets.get('calculate_offset', 30)),
        }
    elif mode in TIMING_MODES:
        config = TIMING_MODES[mode]
        return {
            'entry_time': dt_time(15, 59, 60 - config['open_offset']),
            'exit_time': dt_time(15, 59, 60 - config['close_offset']),
            'calculate_time': dt_time(15, 59, 60 - config['calculate_offset']),
        }
    else:
        # Default to ManusTime
        return {
            'entry_time': ENTRY_TIME,
            'exit_time': EXIT_TIME,
            'calculate_time': EXIT_TIME,
        }

def map_indicator_params(indicator_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map database parameter names to indicator function parameter names
    
    Database stores params like: {rsi_oversold: 30, rsi_period: 12}
    Indicator functions expect: {threshold: 30, period: 12}
    """
    # Parameter mapping rules for each indicator
    PARAM_MAPPINGS = {
        'rsi_oversold': {
            'rsi_oversold': 'threshold',
            'rsi_period': 'period',
        },
        'rsi_overbought': {
            'rsi_overbought': 'threshold',
            'rsi_period': 'period',
        },
        'rsi_bullish_cross_50': {
            'rsi_period': 'period',
            'rsi_cross_level': 'cross_level',
        },
        'rsi_bearish_cross_50': {
            'rsi_period': 'period',
            'rsi_cross_level': 'cross_level',
        },
        'connors_rsi_oversold': {
            'connors_rsi_oversold': 'threshold',
            'connors_rsi_period': 'rsi_period',
            'connors_streak_period': 'streak_period',
            'connors_rank_period': 'rank_period',
        },
        'macd_bullish_cross': {
            'macd_fast_period': 'fast_period',
            'macd_slow_period': 'slow_period',
            'macd_signal_period': 'signal_period',
        },
        'stoch_oversold': {
            'stoch_oversold': 'threshold',
            'stoch_fastk_period': 'fastk_period',
            'stoch_slowk_period': 'slowk_period',
            'stoch_slowd_period': 'slowd_period',
        },
        # Add more mappings as needed
    }
    
    # Get mapping for this indicator
    mapping = PARAM_MAPPINGS.get(indicator_name, {})
    
    # Apply mapping
    mapped_params = {}
    for db_key, value in params.items():
        # Use mapped name if exists, otherwise keep original
        func_key = mapping.get(db_key, db_key)
        mapped_params[func_key] = value
    
    return mapped_params

def load_epic_data(epic: str, start_date: str = None, end_date: str = None, timeframe: str = '5m', data_source: str = 'capital') -> pd.DataFrame:
    """
    Load candle data from MySQL database, aggregated to specified timeframe.
    
    ALL DATA IS NOW FROM CAPITAL.COM IN UTC - NO TIMEZONE CONVERSION NEEDED.
    
    Args:
        epic: Epic symbol (e.g., "SOXL", "TECL")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        timeframe: Timeframe (e.g., "5m", "1m", "15m", "1h")
        data_source: 'capital' for Capital.com data (UTC timezone) - AV no longer supported
    
    Returns:
        DataFrame with standardized columns
    """
    # Use aggregate_timeframe module for non-5m/non-1m timeframes (e.g., 15m, 1h)
    if timeframe not in ['5m', '1m']:
        from aggregate_timeframe import get_candles
        candles = get_candles(epic, start_date or '2020-01-01', end_date or '2030-12-31', timeframe)
        
        # Convert to DataFrame
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['snapshotTime'] = pd.to_datetime(df['timestamp'])
        df['openPrice'] = df['open']
        df['highPrice'] = df['high']
        df['lowPrice'] = df['low']
        df['closePrice'] = df['close']
        df['lastTradedVolume'] = df['volume']
        df['date'] = df['snapshotTime'].dt.date
        return df
    
    # Load from MySQL using mysql_candle_loader
    # Set date range (use wide range if not specified)
    if not start_date:
        start_date = "2020-01-01"
    if not end_date:
        end_date = "2030-12-31"
    
    # Pass timeframe and data_source to the loader (supports 5m and 1m)
    df = mysql_load_epic_data(epic, start_date, end_date, timeframe, data_source)
    
    # Check if data was found
    if df.empty:
        raise ValueError(f"No {data_source} data found for {epic} {timeframe} from {start_date} to {end_date}. "
                        f"Try using 'capital' data source or check the date range.")
    
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
    
    # Add date column
    df['date'] = df['snapshotTime'].dt.date
    
    # Filter by date range
    if start_date:
        start = pd.to_datetime(start_date)
        df = df[df['snapshotTime'] >= start]
    
    if end_date:
        end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        df = df[df['snapshotTime'] < end]
    
    return df.sort_values('snapshotTime').reset_index(drop=True)


def generate_random_params(indicator_name: str) -> Dict[str, Any]:
    """Generate random parameters for an indicator"""
    
    # Define parameter ranges for each indicator type
    param_ranges = {
        'rsi_oversold': {
            'period': [7, 10, 14, 20],
            'threshold': list(range(20, 36)),
        },
        'rsi_overbought': {
            'period': [7, 10, 14, 20],
            'threshold': list(range(65, 81)),
        },
        'rsi_bullish_cross_50': {
            'period': [7, 10, 14, 20],
        },
        'rsi_bearish_cross_50': {
            'period': [7, 10, 14, 20],
        },
        'bb_lower_break': {
            'period': [10, 15, 20, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
        },
        'bb_upper_break': {
            'period': [10, 15, 20, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
        },
        'macd_bullish_cross': {
            'fast_period': [8, 12, 13],
            'slow_period': [21, 26, 30],
            'signal_period': [7, 9, 11],
        },
        'macd_bearish_cross': {
            'fast_period': [8, 12, 13],
            'slow_period': [21, 26, 30],
            'signal_period': [7, 9, 11],
        },
        'macd_positive': {
            'fast_period': [8, 12, 13],
            'slow_period': [21, 26, 30],
            'signal_period': [7, 9, 11],
            'threshold': [0.01, 0.05, 0.1],
        },
        'price_above_vwap': {
            'threshold': [-0.02, -0.01, 0.0, 0.01],
        },
        'price_below_vwap': {
            'threshold': [-0.01, 0.0, 0.01, 0.02],
        },
        'roc_above_threshold': {
            'period': [5, 10, 15, 20],
            'threshold': [0.5, 1.0, 2.0, 3.0],
        },
        'roc_below_threshold': {
            'period': [5, 10, 15, 20],
            'threshold': [-3.0, -2.0, -1.0, -0.5],
        },
    }
    
    # Get ranges for this indicator
    ranges = param_ranges.get(indicator_name, {})
    
    # Generate random params
    params = {}
    for param_name, values in ranges.items():
        params[param_name] = random.choice(values)
    
    return params


def run_single_backtest(
    df: pd.DataFrame,
    epic: str,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    leverage: float,
    stop_loss_pct: float,
    initial_balance: float,
    monthly_topup: float,
    investment_pct: float,
    direction: str = 'long',
    timing_config: Optional[Dict[str, Any]] = None,
    calculation_mode: str = 'standard',
    stop_conditions: Optional[Dict[str, float]] = None,
    timeframe: str = '5m',
    crash_protection_enabled: bool = False,
    # HMH (Hold Means Hold) parameters
    hmh_enabled: bool = False,
    hmh_stop_loss_offset: float = 0  # 0 = original SL, -1 = SL-1%, -2 = SL-2%
) -> Dict[str, Any]:
    """
    Run a single backtest with given parameters.
    
    HMH (Hold Means Hold) mode:
    - When enabled, if indicator says HOLD and we have an open position, we DON'T close it
    - Instead, we calculate a NEW FIXED stop loss at: current_price - (stop_loss_pct + hmh_stop_loss_offset)%
    - This is NOT a trailing stop - it's a new fixed price level calculated at session end
    - Position continues until stop loss hit or next BUY signal
    - Example: If SL=2% and current price is $100, new SL = $98 (fixed level)
    - With hmh_stop_loss_offset=-1: new SL = $99 (1% instead of 2%)
    """
    
    # Initialize timing configuration
    if timing_config:
        timing = TimingConfig.from_dict(timing_config)
    else:
        # Default to ManusTime
        timing = get_timing_config('ManusTime')
    
    # Select indicator library based on calculation mode
    if calculation_mode == 'numba':
        indicator_lib = NumbaIndicatorLibrary(df)
    else:
        indicator_lib = create_indicator_library(df)
    
    # Determine if we should use adaptive timing
    timing_mode = timing_config.get('mode', 'ManusTime') if timing_config else 'ManusTime'
    use_adaptive = should_use_adaptive_timing(timing_mode)
    
    # For USMarketClosingTime, calculate baseline close once
    us_market_baseline = None
    if timing_mode == 'USMarketClosingTime':
        us_market_baseline = get_us_market_baseline_close(df)
    
    # Get default entry/exit times from timing config (used as fallback)
    ENTRY_TIME_DEFAULT = timing.get_entry_time()
    EXIT_TIME_DEFAULT = timing.get_exit_time()
    CALC_TIME_DEFAULT = timing.get_calc_time()
    
    # Load market info for the epic
    from market_info_loader import get_market_info
    market_info = get_market_info(epic)
    if not market_info:
        # Use default values if market info not found
        # Default spread ~0.18% based on real Capital.com observations (18-25 cents at $100)
        market_info = {
            'spread_percent': 0.0018,
            'min_contract_size': 1.0,
            'max_contract_size': 10000.0,
            'min_size_increment': 1.0,
            'contract_multiplier': 1.0,
            'overnight_funding_long_percent': -0.00023,
            'overnight_funding_short_percent': -0.0000096,
        }
    
    # Ensure min_size_increment has a default
    if 'min_size_increment' not in market_info:
        market_info['min_size_increment'] = 1.0
    
    # Load risk management settings for ML tracking
    risk_settings = get_risk_settings()
    margin_closeout_level = risk_settings['margin_closeout_level']
    liquidation_slippage_pct = risk_settings['liquidation_slippage_pct']
    
    # Margin factor = 1/leverage (e.g., 10x leverage = 10% margin requirement)
    # This matches how Capital.com calculates margin for YOUR selected leverage
    # The db_margin_factor represents max allowed leverage, but we use the leverage being tested
    # Position sizing uses this leverage, so margin calculation must match
    margin_factor = 1.0 / leverage if leverage > 0 else 0.05
    
    balance = initial_balance
    contributions = 0
    trades = []
    daily_balances = []
    
    # ML tracking metrics
    min_margin_level = float('inf')  # Track lowest ML during backtest
    liquidation_count = 0  # Number of forced liquidations
    total_liquidation_loss = 0.0  # Extra loss from liquidations (vs normal exit)
    
    # HMH (Hold Means Hold) - track open position state across days
    hmh_open_position = None  # Dict with position details if holding across days
    hmh_days_held = 0  # Number of sessions held (for HMH positions)
    
    # Get unique trading days
    trading_days = sorted(df['date'].unique())
    
    # Add initial balance entry before trading starts
    if len(trading_days) > 0:
        first_day = trading_days[0]
        daily_balances.append({
            'date': str(first_day),
            'balance': initial_balance,
        })
    
    # Track last topup month
    last_topup_month = None
    
    for day in trading_days:
        # Calculate adaptive timing for this specific day if needed
        if use_adaptive:
            # Get calc_offset from timing config
            # Fake5min_4thCandle / T60FakeCandle: 60 seconds (uses 4x 1-min data to build fake 5-min)
            # Fake5min_3rdCandle_API: 120 seconds (uses 3x 1-min + current price)
            # EpicClosingTimeBrainCalc / T5BeforeClose / SecondLastCandle: 300 seconds (T-5 min)
            # Others: 30 seconds (default)
            calc_offset = timing_config.get('calc_offset_seconds', 30) if timing_config else 30
            if timing_mode in ['T60FakeCandle', 'Fake5min_4thCandle']:
                calc_offset = 60   # T-60s for 4th candle fake mode
            elif timing_mode == 'Fake5min_3rdCandle_API':
                calc_offset = 120  # T-120s for 3rd candle + API mode
            elif timing_mode in ['EpicClosingTimeBrainCalc', 'T5BeforeClose', 'SecondLastCandle']:
                calc_offset = 300  # T-5 min for brain calc mode
            
            adaptive_times = get_adaptive_timing_for_day(
                df=df,
                day=day,
                mode=timing_mode,
                close_offset_seconds=timing_config.get('close_offset_seconds', 30) if timing_config else 30,
                open_offset_seconds=timing_config.get('open_offset_seconds', 15) if timing_config else 15,
                calc_offset_seconds=calc_offset
            )
            ENTRY_TIME_DYNAMIC = adaptive_times['entry_time']
            EXIT_TIME_DYNAMIC = adaptive_times['exit_time']
            CALC_TIME_DYNAMIC = adaptive_times.get('calc_time', EXIT_TIME_DYNAMIC)
        else:
            # Use static times from timing config
            ENTRY_TIME_DYNAMIC = ENTRY_TIME_DEFAULT
            EXIT_TIME_DYNAMIC = EXIT_TIME_DEFAULT
            CALC_TIME_DYNAMIC = CALC_TIME_DEFAULT
        
        # Monthly topup
        current_month = pd.to_datetime(str(day)).to_period('M')
        if last_topup_month is None or current_month > last_topup_month:
            balance += monthly_topup
            contributions += monthly_topup
            last_topup_month = current_month
        
        # Get day's data
        day_data = df[df['date'] == day].copy()
        if len(day_data) < 50:  # Need enough history for indicators
            continue
        
        # Get historical data based on timing mode
        # Fake5min_4thCandle / T60FakeCandle: Use fake_5min_close from 4x 1-min data (most realistic for backtesting)
        # Fake5min_3rdCandle_API: Use 3x 1-min data (simulates live trading with API call)
        # EpicClosingTimeBrainCalc / T5BeforeClose / SecondLastCandle: Use candles up to T-5min
        # MarketClose / EpicClosingTime: Use ALL candles including last one (original behavior)
        # Other modes: Use candles up to calculation time
        if timing_mode in ['T60FakeCandle', 'Fake5min_4thCandle']:
            # MOST REALISTIC FOR BACKTESTING: Use fake 5-min candle from 4x 1-min data at T-60s
            # This requires the fake_5min_close to be pre-calculated and stored
            # If not available, fall back to T-5min behavior
            calc_cutoff_candles = day_data[day_data['snapshotTime'].dt.time < CALC_TIME_DYNAMIC]
            if len(calc_cutoff_candles) > 0:
                hist_data = df[df['snapshotTime'] <= calc_cutoff_candles.iloc[-1]['snapshotTime']].copy()
                
                # Check if we have fake_5min_close for this day (from 4x 1-min candles)
                # The fake_5min_close is stored on the LAST candle of the day
                if 'fake_5min_close' in day_data.columns:
                    # Find the last candle of the day that has fake_5min_close
                    day_candles_with_fake = day_data[day_data['fake_5min_close'].notna()]
                    if len(day_candles_with_fake) > 0:
                        fake_close = float(day_candles_with_fake.iloc[-1]['fake_5min_close'])
                        hist_data.loc[hist_data.index[-1], 'closePrice'] = fake_close
                        # Also update close_bid if present (for Capital.com data)
                        if 'close_bid' in hist_data.columns:
                            hist_data.loc[hist_data.index[-1], 'close_bid'] = fake_close
            else:
                hist_data = df[df['snapshotTime'] <= day_data.iloc[-1]['snapshotTime']].copy()
        elif timing_mode == 'Fake5min_3rdCandle_API':
            # MOST REALISTIC FOR LIVE TRADING: Use 3x 1-min data at T-120s
            # In live trading, we'd call API for current price to complete the fake candle
            # In backtesting, we use the pre-computed fake_5min_close from 3x 1-min candles
            calc_cutoff_candles = day_data[day_data['snapshotTime'].dt.time < CALC_TIME_DYNAMIC]
            if len(calc_cutoff_candles) > 0:
                hist_data = df[df['snapshotTime'] <= calc_cutoff_candles.iloc[-1]['snapshotTime']].copy()
                
                # Check if we have fake_5min_close for this day (from 3x 1-min candles)
                # The fake_5min_close is stored on the LAST candle of the day
                if 'fake_5min_close' in day_data.columns:
                    # Find the last candle of the day that has fake_5min_close
                    day_candles_with_fake = day_data[day_data['fake_5min_close'].notna()]
                    if len(day_candles_with_fake) > 0:
                        fake_close = float(day_candles_with_fake.iloc[-1]['fake_5min_close'])
                        hist_data.loc[hist_data.index[-1], 'closePrice'] = fake_close
                        # Also update close_bid if present (for Capital.com data)
                        if 'close_bid' in hist_data.columns:
                            hist_data.loc[hist_data.index[-1], 'close_bid'] = fake_close
            else:
                hist_data = df[df['snapshotTime'] <= day_data.iloc[-1]['snapshotTime']].copy()
        elif timing_mode in ['EpicClosingTimeBrainCalc', 'T5BeforeClose', 'SecondLastCandle']:
            # T-5min BEHAVIOR: Only use candles available at T-5min before market close
            # This matches what the live brain would see when it calculates
            calc_cutoff_candles = day_data[day_data['snapshotTime'].dt.time < CALC_TIME_DYNAMIC]
            if len(calc_cutoff_candles) > 0:
                hist_data = df[df['snapshotTime'] <= calc_cutoff_candles.iloc[-1]['snapshotTime']].copy()
            else:
                # Fallback if no candles before calc time
                hist_data = df[df['snapshotTime'] <= day_data.iloc[-1]['snapshotTime']].copy()
        else:
            # ORIGINAL BEHAVIOR: Use ALL candles up to end of day (including 20:00 ET for AV data)
            # This gives higher backtesting results but may not match live trading exactly
            hist_data = df[df['snapshotTime'] <= day_data.iloc[-1]['snapshotTime']].copy()
        
        if len(hist_data) < 50:
            continue
        
        # =========================================================================
        # HMH: INTRADAY STOP LOSS MONITORING FOR CARRIED POSITIONS
        # Before evaluating indicator, check if HMH position was stopped out today
        # =========================================================================
        if hmh_enabled and hmh_open_position is not None:
            pos = hmh_open_position
            hmh_stop_loss_price = pos['stop_loss_price']
            hmh_stopped_out = False
            hmh_stop_exit_price = None
            hmh_stop_exit_time = None
            
            # Check all candles for stop loss hit
            for _, candle in day_data.iterrows():
                if pos['direction'] == 'long' and candle['lowPrice'] <= hmh_stop_loss_price:
                    hmh_stopped_out = True
                    hmh_stop_exit_price = hmh_stop_loss_price
                    hmh_stop_exit_time = candle['snapshotTime']
                    break
                elif pos['direction'] == 'short' and candle['highPrice'] >= hmh_stop_loss_price:
                    hmh_stopped_out = True
                    hmh_stop_exit_price = hmh_stop_loss_price
                    hmh_stop_exit_time = candle['snapshotTime']
                    break
            
            if hmh_stopped_out:
                # HMH position was stopped out - record trade and clear state
                if pos['direction'] == 'long':
                    hmh_price_change = hmh_stop_exit_price - pos['entry_price']
                else:
                    hmh_price_change = pos['entry_price'] - hmh_stop_exit_price
                
                hmh_gross_pnl = pos['contracts'] * hmh_price_change * market_info['contract_multiplier']
                
                # Calculate overnight costs for multi-day hold
                hmh_overnight_cost = 0
                if hmh_days_held > 0:
                    funding_rate = market_info['overnight_funding_long_percent'] if pos['direction'] == 'long' else market_info['overnight_funding_short_percent']
                    hmh_overnight_cost = pos['notional_value'] * (-funding_rate) * hmh_days_held
                
                # FIX: Include spread cost in net P&L (was missing!)
                hmh_spread_cost = pos.get('spread_cost', 0)
                hmh_net_pnl = hmh_gross_pnl - hmh_overnight_cost - hmh_spread_cost
                balance += hmh_net_pnl
                
                # Record the stopped-out HMH trade (multi-day hold stopped out)
                hmh_trade = {
                    **pos,
                    'exit_time': str(hmh_stop_exit_time),
                    'exit_price': hmh_stop_exit_price,
                    'stopped_out': True,
                    'margin_liquidated': False,
                    'exit_reason': 'hmh_stop_loss',
                    'gross_pnl': hmh_gross_pnl,
                    'spread_cost': pos.get('spread_cost', 0),
                    'overnight_costs': hmh_overnight_cost,
                    'costs': pos.get('spread_cost', 0) + hmh_overnight_cost,
                    'net_pnl': hmh_net_pnl,
                    'balance_after': balance,
                    # HMH fields for multi-day carried position stopped out
                    'hmh_enabled': True,
                    'hmh_stop_loss_offset': hmh_stop_loss_offset,
                    'hmh_is_continuation': True,  # Multi-day carry
                    'hmh_days_held': hmh_days_held,
                    'hmh_original_entry_price': pos.get('hmh_original_entry_price', pos['entry_price']),
                }
                trades.append(hmh_trade)
                
                # Clear HMH state - position is closed
                hmh_open_position = None
                hmh_days_held = 0
                
                # Record daily balance and continue to next day
                daily_balances.append({
                    'date': str(day),
                    'balance': balance,
                })
                continue
        
        # Check indicator signal at entry time
        # === USE UNIFIED SIGNAL EVALUATION ===
        # This ensures identical calculation with get_current_signal.py (brain preview/live)
        try:
            eval_result = evaluate_indicator_at_index(
                df=hist_data,
                indicator_name=indicator_name,
                indicator_params=indicator_params,
                check_index=-1,  # Last candle
                crash_protection_enabled=crash_protection_enabled,
                crash_protection_date=day,
                crash_protection_config=CRASH_PROTECTION_CONFIG,
            )
            
            # Skip if error
            if eval_result.get('error'):
                # If HMH position open, it will continue with existing stop loss
                continue
            
            signal_is_buy = eval_result['signal']
            
            # =========================================================================
            # HMH (Hold Means Hold) LOGIC - Handle indicator signal for carried position
            # =========================================================================
            if hmh_enabled and hmh_open_position is not None:
                # We have an existing HMH position from previous day(s)
                current_price = hist_data.iloc[-1]['closePrice']
                
                if signal_is_buy:
                    # BUY signal - Close existing HMH position, will open new below
                    pos = hmh_open_position
                    
                    # Calculate exit P&L for HMH position
                    if pos['direction'] == 'long':
                        hmh_price_change = current_price - pos['entry_price']
                    else:
                        hmh_price_change = pos['entry_price'] - current_price
                    
                    hmh_gross_pnl = pos['contracts'] * hmh_price_change * market_info['contract_multiplier']
                    
                    # Calculate overnight costs for multi-day hold
                    hmh_overnight_cost = 0
                    if hmh_days_held > 0:
                        funding_rate = market_info['overnight_funding_long_percent'] if pos['direction'] == 'long' else market_info['overnight_funding_short_percent']
                        hmh_overnight_cost = pos['notional_value'] * (-funding_rate) * hmh_days_held
                    
                    # FIX: Include spread cost in net P&L (spread was paid at entry, stored in pos)
                    hmh_spread_cost = pos.get('spread_cost', 0)
                    hmh_net_pnl = hmh_gross_pnl - hmh_overnight_cost - hmh_spread_cost
                    
                    # Update balance
                    balance += hmh_net_pnl
                    
                    # Record HMH trade closure (multi-day hold closed by new BUY signal)
                    hmh_trade = {
                        **pos,
                        'exit_time': str(hist_data.iloc[-1]['snapshotTime']),
                        'exit_price': current_price,
                        'stopped_out': False,
                        'margin_liquidated': False,
                        'exit_reason': 'hmh_new_buy_signal',
                        'gross_pnl': hmh_gross_pnl,
                        'spread_cost': pos.get('spread_cost', 0),  # Already counted at entry
                        'overnight_costs': hmh_overnight_cost,
                        'costs': pos.get('spread_cost', 0) + hmh_overnight_cost,
                        'net_pnl': hmh_net_pnl,
                        'balance_after': balance,
                        # HMH fields for multi-day carried position
                        'hmh_enabled': True,
                        'hmh_stop_loss_offset': hmh_stop_loss_offset,
                        'hmh_is_continuation': True,  # Multi-day carry
                        'hmh_days_held': hmh_days_held,
                        'hmh_original_entry_price': pos.get('hmh_original_entry_price', pos['entry_price']),
                    }
                    trades.append(hmh_trade)
                    
                    # Reset HMH state - will open new position below
                    hmh_open_position = None
                    hmh_days_held = 0
                    
                else:
                    # HOLD signal with HMH - Set NEW stop loss at current price and continue holding
                    # NOT trailing - we calculate a new fixed SL based on current close price
                    hmh_days_held += 1
                    
                    # Calculate new stop loss based on current price
                    # New SL = current_price - (stop_loss_pct + hmh_stop_loss_offset)%
                    # hmh_stop_loss_offset: 0 = original SL%, -1 = SL-1%, -2 = SL-2%
                    effective_sl_pct = stop_loss_pct + hmh_stop_loss_offset
                    old_stop_loss = hmh_open_position['stop_loss_price']
                    
                    if direction == 'long':
                        new_stop_loss = current_price * (1 - effective_sl_pct / 100)
                    else:
                        new_stop_loss = current_price * (1 + effective_sl_pct / 100)
                    
                    # Set the new stop loss (replaces old, does NOT follow the position)
                    hmh_open_position['stop_loss_price'] = new_stop_loss
                    
                    # Track SL adjustments for debugging
                    if 'sl_adjustments' not in hmh_open_position:
                        hmh_open_position['sl_adjustments'] = []
                    hmh_open_position['sl_adjustments'].append({
                        'date': str(day),
                        'price': current_price,
                        'old_sl': old_stop_loss,
                        'new_sl': new_stop_loss,
                    })
                    
                    # Continue to next day - position held with new fixed stop loss
                    # Record daily balance
                    daily_balances.append({
                        'date': str(day),
                        'balance': balance,  # Balance unchanged until position closes
                        'hmh_position_open': True,
                        'hmh_days_held': hmh_days_held,
                    })
                    continue
            
            # If signal is not BUY (HOLD), skip opening new position
            if not signal_is_buy:
                continue
            
            # Signal was already checked for crash protection in unified function
            # No need to check again
            
            # CRITICAL: Skip trading if balance is zero or negative
            # In real trading, you can't trade with no money - account is wiped out
            if balance <= 0:
                # Account is wiped out, skip this trade
                continue
            
            # Get entry price
            entry_candle = day_data[day_data['snapshotTime'].dt.time >= ENTRY_TIME_DYNAMIC].iloc[0] if len(day_data[day_data['snapshotTime'].dt.time >= ENTRY_TIME_DYNAMIC]) > 0 else None
            if entry_candle is None:
                continue
            
            entry_price = entry_candle['closePrice']
            entry_time = entry_candle['snapshotTime']
            
            # Calculate position size
            investment_amount = balance * (investment_pct / 100)
            notional_value = investment_amount * leverage
            contracts = notional_value / (entry_price * market_info['contract_multiplier'])
            
            # Round to min_size_increment (e.g., 0.1 for TECL, 1.0 for SOXL)
            # This ensures position sizes are valid for Capital.com
            increment = market_info.get('min_size_increment', 1.0)
            if increment > 0:
                contracts = (contracts // increment) * increment
            
            # Enforce min/max contract limits
            contracts = max(market_info['min_contract_size'], min(contracts, market_info['max_contract_size']))
            
            # Check if we hit the max contracts cap
            at_max_contracts = bool(contracts == market_info['max_contract_size'])
            
            # Skip if balance is insufficient for minimum position
            min_position_notional = market_info['min_contract_size'] * entry_price * market_info['contract_multiplier']
            min_position_investment = min_position_notional / leverage
            if balance < min_position_investment:
                # Insufficient funds for minimum trade
                continue
            
            # Recalculate actual investment based on contracts
            actual_notional = contracts * entry_price * market_info['contract_multiplier']
            actual_investment = actual_notional / leverage
            position_size = contracts
            
            # Track trade - include bid/ask for accurate spread calculation
            # Per Capital.com docs: spread_cost = contracts × (ask - bid)
            entry_bid = entry_candle.get('closeBid', 0) if hasattr(entry_candle, 'get') else entry_candle['closeBid'] if 'closeBid' in entry_candle.index else 0
            entry_ask = entry_candle.get('closeAsk', 0) if hasattr(entry_candle, 'get') else entry_candle['closeAsk'] if 'closeAsk' in entry_candle.index else 0
            
            # Calculate spread cost at entry (needed for HMH and normal trades)
            # This is paid once at entry, not recalculated at exit
            entry_spread_cost, spread_method = calculate_spread_cost(
                contracts=contracts,
                entry_candle=entry_candle,
                market_info=market_info,
                actual_notional=actual_notional
            )
            
            trade = {
                'date': str(day),
                'entry_time': str(entry_time),
                'entry_price': entry_price,
                'entry_bid': float(entry_bid) if entry_bid else 0,
                'entry_ask': float(entry_ask) if entry_ask else 0,
                'contracts': contracts,
                'at_max_contracts': at_max_contracts,
                'position_size': position_size,  # Same as contracts
                'notional_value': actual_notional,
                'investment': actual_investment,
                'leverage': leverage,
                'stop_loss_pct': stop_loss_pct,
                'direction': direction,
            }
            
            # Monitor for stop loss AND margin level liquidation
            stop_loss_price = entry_price * (1 - stop_loss_pct / 100) if direction == 'long' else entry_price * (1 + stop_loss_pct / 100)
            stopped_out = False
            margin_liquidated = False  # Track if position was force-liquidated due to ML
            exit_price = None
            exit_time = None
            exit_reason = None  # Track exit reason for metrics
            
            # Calculate required margin for this position
            # Required Margin = Notional Value × Margin Factor
            required_margin = actual_notional * margin_factor
            
            # Calculate liquidation trigger price (exact price where ML hits closeout level)
            # This is more accurate than using candle extremes - in real trading, liquidation
            # happens at the moment ML hits the threshold, not at the candle's extreme price
            liq_trigger_price = calculate_liquidation_trigger_price(
                direction=direction,
                entry_price=entry_price,
                balance=balance,
                contracts=contracts,
                contract_multiplier=market_info['contract_multiplier'],
                margin_factor=margin_factor,
                margin_closeout_level=margin_closeout_level
            )
            
            # Capture margin level at ENTRY (before any price movement)
            # This is the initial ML when the trade is opened - critical for accurate min tracking
            # Entry ML = (Balance / Required Margin) × 100
            entry_ml = (balance / required_margin) * 100 if required_margin > 0 else float('inf')
            if entry_ml < min_margin_level and entry_ml > -1000:
                min_margin_level = entry_ml
            
            # Check all candles after entry (including entry candle itself)
            remaining_candles = df[df['snapshotTime'] >= entry_time]
            for _, candle in remaining_candles.iterrows():
                # =========================================================================
                # LIQUIDATION CHECK (using accurate trigger price)
                # In real trading, Capital.com liquidates at the MOMENT ML hits threshold.
                # We check if the candle crosses the pre-calculated trigger price.
                # =========================================================================
                if liq_trigger_price is not None:
                    crossed, trigger_exit = check_liquidation_crossed(
                        direction, liq_trigger_price, candle['lowPrice'], candle['highPrice']
                    )
                    if crossed:
                        margin_liquidated = True
                        # Exit at trigger price with slippage (more accurate than candle extreme)
                        if direction == 'long':
                            exit_price = liq_trigger_price * (1 - liquidation_slippage_pct / 100)
                        else:
                            exit_price = liq_trigger_price * (1 + liquidation_slippage_pct / 100)
                        exit_time = candle['snapshotTime']
                        exit_reason = 'margin_liquidation'
                        liquidation_count += 1
                        # Cap min_margin_level at closeout level (that's when trade actually closed)
                        min_margin_level = min(min_margin_level, margin_closeout_level)
                        break
                
                # Track actual margin level at candle's worst price (for non-liquidation tracking)
                if direction == 'long':
                    worst_case_price = candle['lowPrice']
                else:
                    worst_case_price = candle['highPrice']
                
                if direction == 'long':
                    unrealized_pnl = (worst_case_price - entry_price) * contracts * market_info['contract_multiplier']
                else:
                    unrealized_pnl = (entry_price - worst_case_price) * contracts * market_info['contract_multiplier']
                
                current_equity = balance + unrealized_pnl
                current_position_value = contracts * worst_case_price * market_info['contract_multiplier']
                current_required_margin = current_position_value * margin_factor
                current_ml = (current_equity / current_required_margin) * 100 if current_required_margin > 0 else float('inf')
                
                # Track minimum margin level (only if not liquidated - otherwise it's capped above)
                if current_ml < min_margin_level and current_ml > -1000:
                    min_margin_level = current_ml
                
                # Check stop loss (only if not margin liquidated)
                if direction == 'long' and candle['lowPrice'] <= stop_loss_price:
                    stopped_out = True
                    exit_price = stop_loss_price
                    exit_time = candle['snapshotTime']
                    exit_reason = 'stop_loss'
                    break
                elif direction == 'short' and candle['highPrice'] >= stop_loss_price:
                    stopped_out = True
                    exit_price = stop_loss_price
                    exit_time = candle['snapshotTime']
                    exit_reason = 'stop_loss'
                    break
                
                # Check if it's next day's exit time
                if candle['date'] > day and candle['snapshotTime'].time() >= EXIT_TIME_DYNAMIC:
                    exit_price = candle['closePrice']
                    exit_time = candle['snapshotTime']
                    exit_reason = 'normal_exit'
                    break
            
            # If no exit found, use last available price
            if exit_price is None:
                exit_price = remaining_candles.iloc[-1]['closePrice'] if len(remaining_candles) > 0 else entry_price
                exit_time = remaining_candles.iloc[-1]['snapshotTime'] if len(remaining_candles) > 0 else entry_time
                exit_reason = 'end_of_data'
            
            # =========================================================================
            # HMH: If HMH enabled and position was NOT stopped out or liquidated,
            # hold the position to the next day instead of closing
            # =========================================================================
            if hmh_enabled and exit_reason in ('normal_exit', 'end_of_data') and not stopped_out and not margin_liquidated:
                # Set up HMH position to be carried to next day
                # Note: We already paid spread at entry, so no need to recalculate
                hmh_open_position = {
                    'date': str(day),
                    'entry_time': str(entry_time),
                    'entry_price': entry_price,
                    'entry_bid': float(entry_bid) if entry_bid else 0,
                    'entry_ask': float(entry_ask) if entry_ask else 0,
                    'contracts': contracts,
                    'at_max_contracts': at_max_contracts,
                    'position_size': position_size,
                    'notional_value': actual_notional,
                    'investment': actual_investment,
                    'leverage': leverage,
                    'stop_loss_pct': stop_loss_pct,
                    'stop_loss_price': stop_loss_price,  # Original stop loss
                    'direction': direction,
                    'spread_cost': entry_spread_cost,  # Already paid at entry
                    'hmh_original_entry_price': entry_price,  # For tracking multi-day returns
                }
                hmh_days_held = 1  # First day of holding
                
                # Record HMH holding in daily balances (balance unchanged until position closes)
                daily_balances.append({
                    'date': str(day),
                    'balance': balance,  # Balance unchanged until position closes
                    'hmh_position_open': True,
                    'hmh_days_held': hmh_days_held,
                })
                
                # Continue to next trading day - position remains open
                continue
            
            # Calculate P&L based on price movement
            if direction == 'long':
                price_change = exit_price - entry_price
            else:
                price_change = entry_price - exit_price
            
            gross_pnl = contracts * price_change * market_info['contract_multiplier']
            
            # Spread cost was already calculated at entry (entry_spread_cost)
            # Per Capital.com docs: spread_cost = contracts × (ask - bid)
            spread_cost = entry_spread_cost
            
            # Overnight funding cost
            overnight_cost = 0
            if exit_time and (exit_time.date() > entry_time.date()):
                days_held = (exit_time.date() - entry_time.date()).days
                funding_rate = market_info['overnight_funding_long_percent'] if direction == 'long' else market_info['overnight_funding_short_percent']
                # --- FIX: OVERNIGHT FUNDING (Location 1/3) ---
                # Capital.com sign convention: negative rate = cost (pay), positive rate = credit (receive)
                # Multiply by -1 so: negative rate → positive cost, positive rate → negative cost (credit)
                overnight_cost = actual_notional * (-funding_rate) * days_held
            
            total_costs = spread_cost + overnight_cost
            net_pnl = gross_pnl - total_costs
            
            # Update balance
            balance += net_pnl
            
            # Calculate extra loss due to margin liquidation (compared to stop loss exit)
            liquidation_extra_loss = 0.0
            if margin_liquidated:
                # What would the P&L have been at stop loss price?
                if direction == 'long':
                    stop_loss_pnl = (stop_loss_price - entry_price) * contracts * market_info['contract_multiplier']
                else:
                    stop_loss_pnl = (entry_price - stop_loss_price) * contracts * market_info['contract_multiplier']
                # Extra loss = difference between actual gross P&L and stop loss P&L
                liquidation_extra_loss = stop_loss_pnl - gross_pnl
                total_liquidation_loss += liquidation_extra_loss
            
            # Record trade with HMH fields
            trade.update({
                'exit_time': str(exit_time),
                'exit_price': exit_price,
                'stopped_out': stopped_out,
                'margin_liquidated': margin_liquidated,
                'exit_reason': exit_reason,
                'gross_pnl': gross_pnl,
                'spread_cost': spread_cost,
                'overnight_costs': overnight_cost,
                'costs': total_costs,
                'net_pnl': net_pnl,
                'balance_after': balance,
                'liquidation_extra_loss': liquidation_extra_loss,
                # HMH fields for non-carried positions (single-day trades)
                'hmh_enabled': hmh_enabled,
                'hmh_stop_loss_offset': hmh_stop_loss_offset if hmh_enabled else None,
                'hmh_is_continuation': False,  # Not a multi-day carry
                'hmh_days_held': 1,  # Single day trade
                'hmh_original_entry_price': entry_price,
            })
            trades.append(trade)
            
        except Exception as e:
            # Skip this day if indicator fails
            # Uncomment for debugging:
            # import traceback
            # print(f"[DEBUG] Day {day} failed: {e}", file=sys.stderr)
            # traceback.print_exc(file=sys.stderr)
            continue
        
        # Record daily balance
        daily_balances.append({
            'date': str(day),
            'balance': balance,
        })
        
        # Check early stop conditions (if enabled)
        if stop_conditions and len(trades) >= 5:  # Need at least 5 trades for meaningful metrics
            # Calculate current metrics
            total_trades_so_far = len(trades)
            winning_trades_so_far = len([t for t in trades if t['net_pnl'] > 0])
            current_win_rate = (winning_trades_so_far / total_trades_so_far * 100) if total_trades_so_far > 0 else 0
            
            # Calculate current drawdown
            peak = initial_balance
            current_dd = 0
            for db in daily_balances:
                if db['balance'] > peak:
                    peak = db['balance']
                dd = ((db['balance'] - peak) / peak) * 100
                if dd < current_dd:
                    current_dd = dd
            
            # Calculate current Sharpe
            current_sharpe = 0
            if len(daily_balances) > 1:
                returns = []
                for i in range(1, len(daily_balances)):
                    ret = (daily_balances[i]['balance'] - daily_balances[i-1]['balance']) / daily_balances[i-1]['balance']
                    returns.append(ret)
                if len(returns) > 0 and np.std(returns) > 0:
                    current_sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
            
            # Check stop conditions
            should_stop = False
            stop_reason = None
            
            if 'maxDrawdown' in stop_conditions and current_dd < -abs(stop_conditions['maxDrawdown']):
                should_stop = True
                stop_reason = f"Max drawdown exceeded: {current_dd:.2f}% < -{stop_conditions['maxDrawdown']}%"
            
            if 'minWinRate' in stop_conditions and current_win_rate < stop_conditions['minWinRate']:
                should_stop = True
                stop_reason = f"Win rate below threshold: {current_win_rate:.2f}% < {stop_conditions['minWinRate']}%"
            
            if 'minSharpe' in stop_conditions and current_sharpe < stop_conditions['minSharpe']:
                should_stop = True
                stop_reason = f"Sharpe ratio below threshold: {current_sharpe:.2f} < {stop_conditions['minSharpe']}"
            
            # Check minimum profitability (total return %)
            if 'minProfitability' in stop_conditions:
                current_return = ((balance - initial_balance - contributions) / (initial_balance + contributions)) * 100
                if current_return < stop_conditions['minProfitability']:
                    should_stop = True
                    stop_reason = f"Profitability below threshold: {current_return:.2f}% < {stop_conditions['minProfitability']}%"
            
            if should_stop:
                # Early termination - return partial results
                margin_liquidated_trades_early = len([t for t in trades if t.get('margin_liquidated', False)])
                return {
                    'indicatorName': indicator_name,
                    'indicatorParams': indicator_params,
                    'leverage': leverage,
                    'stopLoss': stop_loss_pct,
                    'timeframe': timeframe,
                    'crashProtectionEnabled': crash_protection_enabled,
                    'initialBalance': initial_balance,
                    'finalBalance': balance,
                    'totalContributions': contributions,
                    'totalReturn': ((balance - initial_balance - contributions) / (initial_balance + contributions)) * 100,
                    'totalTrades': total_trades_so_far,
                    'winningTrades': winning_trades_so_far,
                    'losingTrades': total_trades_so_far - winning_trades_so_far,
                    'winRate': current_win_rate,
                    'maxDrawdown': current_dd,
                    'maxProfit': max([t['net_pnl'] for t in trades]) if trades else 0,
                    'sharpeRatio': current_sharpe,
                    # Margin Level tracking metrics
                    'minMarginLevel': min_margin_level if min_margin_level != float('inf') else None,
                    'liquidationCount': liquidation_count,
                    'marginLiquidatedTrades': margin_liquidated_trades_early,
                    'totalLiquidationLoss': total_liquidation_loss,
                    'marginCloseoutLevel': margin_closeout_level,
                    'trades': trades,  # Store all trades for verification
                    'dailyBalances': daily_balances,
                    'stoppedEarly': True,
                    'stopReason': stop_reason,
                }
        
    
    # Calculate metrics
    total_trades = len(trades)
    winning_trades = len([t for t in trades if t['net_pnl'] > 0])
    losing_trades = len([t for t in trades if t['net_pnl'] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    # Calculate total fees (spread costs + overnight funding costs)
    total_spread_costs = sum(t.get('spread_cost', 0) for t in trades)
    total_overnight_costs = sum(t.get('overnight_costs', 0) for t in trades)
    total_fees = total_spread_costs + total_overnight_costs
    
    final_balance = balance
    total_return = ((final_balance - initial_balance - contributions) / (initial_balance + contributions)) * 100
    
    # Calculate max drawdown
    peak = initial_balance
    max_dd = 0
    for db in daily_balances:
        if db['balance'] > peak:
            peak = db['balance']
        dd = ((db['balance'] - peak) / peak) * 100
        if dd < max_dd:
            max_dd = dd
    
    # Calculate max profit (highest single trade profit)
    max_profit = max([t['net_pnl'] for t in trades]) if trades else 0
    
    # Calculate Sharpe ratio (simplified)
    if len(daily_balances) > 1:
        returns = []
        for i in range(1, len(daily_balances)):
            ret = (daily_balances[i]['balance'] - daily_balances[i-1]['balance']) / daily_balances[i-1]['balance']
            returns.append(ret)
        
        if len(returns) > 0 and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
            # Cap Sharpe ratio at reasonable bounds (-10 to 10)
            sharpe = max(-10, min(10, sharpe))
        else:
            sharpe = 0
    else:
        sharpe = 0
    
    # Count margin liquidated trades
    margin_liquidated_trades = len([t for t in trades if t.get('margin_liquidated', False)])
    
    return {
        'indicatorName': indicator_name,
        'indicatorParams': indicator_params,
        'leverage': leverage,
        'stopLoss': stop_loss_pct,
        'timeframe': timeframe,
        'crashProtectionEnabled': crash_protection_enabled,
        'initialBalance': initial_balance,
        'finalBalance': final_balance,
        'totalContributions': contributions,
        'totalReturn': total_return,
        'totalTrades': total_trades,
        'winningTrades': winning_trades,
        'losingTrades': losing_trades,
        'winRate': win_rate,
        'maxDrawdown': max_dd,
        'maxProfit': max_profit,
        'sharpeRatio': sharpe,
        'totalFees': total_fees,  # Total trading costs (spread + overnight funding)
        'totalSpreadCosts': total_spread_costs,  # Just spread costs
        'totalOvernightCosts': total_overnight_costs,  # Just overnight funding costs
        # Margin Level tracking metrics
        'minMarginLevel': min_margin_level if min_margin_level != float('inf') else None,
        'liquidationCount': liquidation_count,
        'marginLiquidatedTrades': margin_liquidated_trades,
        'totalLiquidationLoss': total_liquidation_loss,
        'marginCloseoutLevel': margin_closeout_level,  # Setting used for this backtest
        'trades': trades,  # Store all trades for verification and debugging
        'dailyBalances': daily_balances,
    }


def run_signal_based_backtest(
    df: pd.DataFrame,
    epic: str,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    leverage: float,
    stop_loss_pct: float,
    initial_balance: float,
    monthly_topup: float,
    investment_pct: float,
    direction: str = 'long',
    signal_config: Optional[Dict[str, Any]] = None,
    timeframe: str = '5m',
    crash_protection_enabled: bool = False
) -> Dict[str, Any]:
    """
    Run a signal-based backtest where trades are opened on indicator signals
    instead of fixed market close times.
    
    Signal-based trading:
    - Entry: When indicator condition is true
    - Exit: Whichever happens first:
        1. Stop loss is hit
        2. Next signal triggers (close and reopen if enabled)
        3. Max hold period reached (same_day, next_day, or custom days)
    
    Args:
        df: DataFrame with candle data
        epic: Symbol to trade
        indicator_name: Name of indicator to use
        indicator_params: Parameters for the indicator
        leverage: Leverage multiplier
        stop_loss_pct: Stop loss percentage
        initial_balance: Starting balance
        monthly_topup: Monthly contribution amount
        investment_pct: Percentage of balance to invest per trade
        direction: 'long' or 'short'
        signal_config: Configuration for signal-based trading:
            - close_on_next_signal: bool - Close when next signal triggers
            - max_hold_period: 'same_day' | 'next_day' | 'custom'
            - custom_hold_days: int - Max days to hold (if max_hold_period='custom')
        timeframe: Candle timeframe
        crash_protection_enabled: Whether to use crash protection
    
    Returns:
        Dictionary with backtest results
    """
    # Parse signal config
    close_on_next_signal = signal_config.get('closeOnNextSignal', True) if signal_config else True
    max_hold_period = signal_config.get('maxHoldPeriod', 'next_day') if signal_config else 'next_day'
    custom_hold_days = signal_config.get('customHoldDays', 1) if signal_config else 1
    
    # Load market info
    from market_info_loader import get_market_info
    market_info = get_market_info(epic)
    if not market_info:
        # Default spread ~0.18% based on real Capital.com observations (18-25 cents at $100)
        market_info = {
            'spread_percent': 0.0018,
            'min_contract_size': 1.0,
            'max_contract_size': 10000.0,
            'min_size_increment': 1.0,
            'contract_multiplier': 1.0,
            'overnight_funding_long_percent': -0.00023,
            'overnight_funding_short_percent': -0.0000096,
        }
    
    if 'min_size_increment' not in market_info:
        market_info['min_size_increment'] = 1.0
    
    # Load risk management settings for ML tracking
    risk_settings = get_risk_settings()
    margin_closeout_level = risk_settings['margin_closeout_level']
    liquidation_slippage_pct = risk_settings['liquidation_slippage_pct']
    
    # Get margin factor for this epic (default to 5% = 20x max leverage if not set)
    margin_factor = market_info.get('margin_factor')
    if margin_factor is None:
        margin_factor = 1.0 / leverage if leverage > 0 else 0.05
    
    balance = initial_balance
    contributions = 0
    trades = []
    daily_balances = []
    
    # ML tracking metrics
    min_margin_level = float('inf')
    liquidation_count = 0
    total_liquidation_loss = 0.0
    
    # Track position state
    in_position = False
    current_trade = None
    
    # Get unique trading days for monthly topup tracking
    trading_days = sorted(df['date'].unique())
    last_topup_month = None
    
    # Add initial balance
    if len(trading_days) > 0:
        daily_balances.append({
            'date': str(trading_days[0]),
            'balance': initial_balance,
        })
    
    # Need enough history for indicators (at least 50 candles)
    min_history = 50
    
    # Iterate through each candle
    for i in range(min_history, len(df)):
        candle = df.iloc[i]
        current_time = candle['snapshotTime']
        current_day = candle['date']
        
        # Monthly topup check
        current_month = pd.to_datetime(str(current_day)).to_period('M')
        if last_topup_month is None or current_month > last_topup_month:
            balance += monthly_topup
            contributions += monthly_topup
            last_topup_month = current_month
        
        # Get historical data up to this candle
        hist_data = df.iloc[:i+1].copy()
        
        # Check if we're in a position
        if in_position and current_trade:
            entry_price = current_trade['entry_price']
            entry_time = current_trade['entry_time_dt']
            entry_day = current_trade['entry_day']
            stop_loss_price = current_trade['stop_loss_price']
            contracts = current_trade['contracts']
            actual_notional = current_trade['notional_value']
            
            # Calculate required margin for ML check
            required_margin = actual_notional * margin_factor
            liq_trigger_price = current_trade.get('liq_trigger_price')  # Pre-calculated at entry
            
            # Check exit conditions
            should_exit = False
            exit_reason = None
            exit_price = None
            margin_liquidated = False
            
            # =========================================================================
            # 0. LIQUIDATION CHECK (using accurate trigger price)
            # In real trading, Capital.com liquidates at the MOMENT ML hits threshold.
            # We check if the candle crosses the pre-calculated trigger price.
            # =========================================================================
            if liq_trigger_price is not None:
                crossed, trigger_exit = check_liquidation_crossed(
                    direction, liq_trigger_price, candle['lowPrice'], candle['highPrice']
                )
                if crossed:
                    should_exit = True
                    margin_liquidated = True
                    exit_reason = 'margin_liquidation'
                    # Exit at trigger price with slippage (more accurate than candle extreme)
                    if direction == 'long':
                        exit_price = liq_trigger_price * (1 - liquidation_slippage_pct / 100)
                    else:
                        exit_price = liq_trigger_price * (1 + liquidation_slippage_pct / 100)
                    liquidation_count += 1
                    # Cap min_margin_level at closeout level (that's when trade actually closed)
                    min_margin_level = min(min_margin_level, margin_closeout_level)
            
            # Track actual margin level at candle's worst price (for non-liquidation tracking)
            if not should_exit:
                if direction == 'long':
                    worst_case_price = candle['lowPrice']
                else:
                    worst_case_price = candle['highPrice']
                
                if direction == 'long':
                    unrealized_pnl = (worst_case_price - entry_price) * contracts * market_info['contract_multiplier']
                else:
                    unrealized_pnl = (entry_price - worst_case_price) * contracts * market_info['contract_multiplier']
                
                current_equity = balance + unrealized_pnl
                current_position_value = contracts * worst_case_price * market_info['contract_multiplier']
                current_required_margin = current_position_value * margin_factor
                current_ml = (current_equity / current_required_margin) * 100 if current_required_margin > 0 else float('inf')
                
                # Track minimum ML (only if not liquidated - otherwise it's capped above)
                if current_ml < min_margin_level:
                    min_margin_level = current_ml
            
            # 1. Check stop loss (only if not margin liquidated)
            if not should_exit:
                if direction == 'long' and candle['lowPrice'] <= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
                elif direction == 'short' and candle['highPrice'] >= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
            
            # 2. Check max hold period
            if not should_exit:
                days_held = (current_day - entry_day).days if hasattr(current_day - entry_day, 'days') else 0
                
                if max_hold_period == 'same_day' and current_day > entry_day:
                    # Close at end of entry day
                    should_exit = True
                    exit_reason = 'max_hold_same_day'
                    exit_price = candle['closePrice']
                elif max_hold_period == 'next_day' and days_held >= 1:
                    # Close at end of next day
                    should_exit = True
                    exit_reason = 'max_hold_next_day'
                    exit_price = candle['closePrice']
                elif max_hold_period == 'custom' and days_held >= custom_hold_days:
                    should_exit = True
                    exit_reason = f'max_hold_{custom_hold_days}_days'
                    exit_price = candle['closePrice']
            
            # 3. Check for next signal (if enabled)
            # === USE UNIFIED SIGNAL EVALUATION ===
            next_signal = False
            if not should_exit and close_on_next_signal:
                try:
                    eval_result = evaluate_indicator_at_index(
                        df=hist_data,
                        indicator_name=indicator_name,
                        indicator_params=indicator_params,
                        check_index=-1,
                        crash_protection_enabled=False,  # Don't check crash protection for exit signals
                    )
                    if eval_result['signal'] and not eval_result.get('error'):
                        next_signal = True
                        should_exit = True
                        exit_reason = 'next_signal'
                        exit_price = candle['closePrice']
                except:
                    pass
            
            # Execute exit if needed
            if should_exit and exit_price:
                exit_time = current_time
                contracts = current_trade['contracts']
                actual_notional = current_trade['notional_value']
                
                # Calculate P&L
                if direction == 'long':
                    price_change = exit_price - entry_price
                else:
                    price_change = entry_price - exit_price
                
                gross_pnl = contracts * price_change * market_info['contract_multiplier']
                
                # Calculate costs using actual bid/ask from entry if available
                # Per Capital.com docs (Dec 2025): spread_cost = contracts × (ask - bid)
                entry_bid = current_trade.get('entry_bid', 0)
                entry_ask = current_trade.get('entry_ask', 0)
                if entry_bid > 0 and entry_ask > 0:
                    actual_spread = entry_ask - entry_bid
                    spread_cost = contracts * actual_spread * market_info.get('contract_multiplier', 1.0)
                else:
                    # Fallback to percentage method
                    spread_cost = actual_notional * market_info['spread_percent']
                
                # Overnight funding
                overnight_cost = 0
                if exit_time.date() > entry_time.date():
                    days_held_actual = (exit_time.date() - entry_time.date()).days
                    funding_rate = market_info['overnight_funding_long_percent'] if direction == 'long' else market_info['overnight_funding_short_percent']
                    # --- FIX: OVERNIGHT FUNDING (Location 2/3) ---
                    # Capital.com sign convention: negative rate = cost (pay), positive rate = credit (receive)
                    overnight_cost = actual_notional * (-funding_rate) * days_held_actual
                
                total_costs = spread_cost + overnight_cost
                net_pnl = gross_pnl - total_costs
                
                # Update balance
                balance += net_pnl
                
                # Calculate extra loss due to margin liquidation
                liquidation_extra_loss = 0.0
                if margin_liquidated:
                    if direction == 'long':
                        stop_loss_pnl = (stop_loss_price - entry_price) * contracts * market_info['contract_multiplier']
                    else:
                        stop_loss_pnl = (entry_price - stop_loss_price) * contracts * market_info['contract_multiplier']
                    liquidation_extra_loss = stop_loss_pnl - gross_pnl
                    total_liquidation_loss += liquidation_extra_loss
                
                # Record completed trade
                current_trade.update({
                    'exit_time': str(exit_time),
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'stopped_out': exit_reason == 'stop_loss',
                    'margin_liquidated': margin_liquidated,
                    'gross_pnl': gross_pnl,
                    'spread_cost': spread_cost,
                    'overnight_costs': overnight_cost,
                    'costs': total_costs,
                    'net_pnl': net_pnl,
                    'balance_after': balance,
                    'liquidation_extra_loss': liquidation_extra_loss,
                })
                trades.append(current_trade)
                margin_liquidated = False  # Reset for next position
                
                in_position = False
                current_trade = None
                
                # If exited due to next signal, immediately open new position
                if exit_reason == 'next_signal' and next_signal:
                    # Open new trade below
                    pass
                else:
                    continue
        
        # Check for entry signal (if not in position, or just exited on next_signal)
        # === USE UNIFIED SIGNAL EVALUATION ===
        if not in_position:
            try:
                eval_result = evaluate_indicator_at_index(
                    df=hist_data,
                    indicator_name=indicator_name,
                    indicator_params=indicator_params,
                    check_index=-1,
                    crash_protection_enabled=crash_protection_enabled,
                    crash_protection_date=current_day,
                    crash_protection_config=CRASH_PROTECTION_CONFIG,
                )
                
                # Skip if error or no signal (crash protection already checked in unified)
                if eval_result.get('error') or not eval_result['signal']:
                    continue
                
                # Open new position
                entry_price = candle['closePrice']
                entry_time = current_time
                
                # Calculate position size
                investment_amount = balance * (investment_pct / 100)
                notional_value = investment_amount * leverage
                contracts = notional_value / (entry_price * market_info['contract_multiplier'])
                
                # Round to increment
                increment = market_info.get('min_size_increment', 1.0)
                if increment > 0:
                    contracts = (contracts // increment) * increment
                
                contracts = max(market_info['min_contract_size'], min(contracts, market_info['max_contract_size']))
                
                # Check if we hit the max contracts cap
                at_max_contracts = bool(contracts == market_info['max_contract_size'])
                
                actual_notional = contracts * entry_price * market_info['contract_multiplier']
                actual_investment = actual_notional / leverage
                
                # Calculate stop loss price
                if direction == 'long':
                    stop_loss_price = entry_price * (1 - stop_loss_pct / 100)
                else:
                    stop_loss_price = entry_price * (1 + stop_loss_pct / 100)
                
                # Calculate liquidation trigger price (exact price where ML hits closeout)
                liq_trigger_price = calculate_liquidation_trigger_price(
                    direction=direction,
                    entry_price=entry_price,
                    balance=balance,
                    contracts=contracts,
                    contract_multiplier=market_info['contract_multiplier'],
                    margin_factor=margin_factor,
                    margin_closeout_level=margin_closeout_level
                )
                
                # Store bid/ask for accurate spread calculation
                entry_bid = candle.get('closeBid', 0) if hasattr(candle, 'get') else candle['closeBid'] if 'closeBid' in candle.index else 0
                entry_ask = candle.get('closeAsk', 0) if hasattr(candle, 'get') else candle['closeAsk'] if 'closeAsk' in candle.index else 0
                
                current_trade = {
                    'date': str(current_day),
                    'entry_time': str(entry_time),
                    'entry_time_dt': entry_time,
                    'entry_day': current_day,
                    'entry_price': entry_price,
                    'entry_bid': float(entry_bid) if entry_bid else 0,
                    'entry_ask': float(entry_ask) if entry_ask else 0,
                    'contracts': contracts,
                    'at_max_contracts': at_max_contracts,
                    'position_size': contracts,
                    'notional_value': actual_notional,
                    'investment': actual_investment,
                    'leverage': leverage,
                    'stop_loss_pct': stop_loss_pct,
                    'stop_loss_price': stop_loss_price,
                    'direction': direction,
                    'signal_based': True,
                    'liq_trigger_price': liq_trigger_price,
                }
                in_position = True
                
            except Exception as e:
                continue
        
        # Record daily balance at end of each day
        if i == len(df) - 1 or df.iloc[i+1]['date'] != current_day:
            daily_balances.append({
                'date': str(current_day),
                'balance': balance,
            })
    
    # Close any remaining position at end of data
    if in_position and current_trade:
        last_candle = df.iloc[-1]
        exit_price = last_candle['closePrice']
        exit_time = last_candle['snapshotTime']
        entry_price = current_trade['entry_price']
        entry_time = current_trade['entry_time_dt']
        contracts = current_trade['contracts']
        actual_notional = current_trade['notional_value']
        
        if direction == 'long':
            price_change = exit_price - entry_price
        else:
            price_change = entry_price - exit_price
        
        gross_pnl = contracts * price_change * market_info['contract_multiplier']
        
        # Calculate costs using actual bid/ask from entry if available
        # Per Capital.com docs (Dec 2025): spread_cost = contracts × (ask - bid)
        entry_bid = current_trade.get('entry_bid', 0)
        entry_ask = current_trade.get('entry_ask', 0)
        if entry_bid > 0 and entry_ask > 0:
            actual_spread = entry_ask - entry_bid
            spread_cost = contracts * actual_spread * market_info.get('contract_multiplier', 1.0)
        else:
            # Fallback to percentage method
            spread_cost = actual_notional * market_info['spread_percent']
        
        overnight_cost = 0
        if exit_time.date() > entry_time.date():
            days_held = (exit_time.date() - entry_time.date()).days
            funding_rate = market_info['overnight_funding_long_percent'] if direction == 'long' else market_info['overnight_funding_short_percent']
            # --- FIX: OVERNIGHT FUNDING (Location 3/3) ---
            # Capital.com sign convention: negative rate = cost (pay), positive rate = credit (receive)
            overnight_cost = actual_notional * (-funding_rate) * days_held
        
        total_costs = spread_cost + overnight_cost
        net_pnl = gross_pnl - total_costs
        balance += net_pnl
        
        current_trade.update({
            'exit_time': str(exit_time),
            'exit_price': exit_price,
            'exit_reason': 'end_of_data',
            'stopped_out': False,
            'margin_liquidated': False,
            'gross_pnl': gross_pnl,
            'spread_cost': spread_cost,
            'overnight_costs': overnight_cost,
            'costs': total_costs,
            'net_pnl': net_pnl,
            'balance_after': balance,
            'liquidation_extra_loss': 0.0,
        })
        trades.append(current_trade)
    
    # Clean up internal fields from trades (not JSON serializable)
    for trade in trades:
        if 'entry_time_dt' in trade:
            del trade['entry_time_dt']
        if 'entry_day' in trade:
            del trade['entry_day']
        if 'stop_loss_price' in trade:
            del trade['stop_loss_price']
    
    # Calculate metrics
    total_trades = len(trades)
    winning_trades = len([t for t in trades if t['net_pnl'] > 0])
    losing_trades = len([t for t in trades if t['net_pnl'] <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    total_spread_costs = sum(t.get('spread_cost', 0) for t in trades)
    total_overnight_costs = sum(t.get('overnight_costs', 0) for t in trades)
    total_fees = total_spread_costs + total_overnight_costs
    
    final_balance = balance
    total_return = ((final_balance - initial_balance - contributions) / (initial_balance + contributions)) * 100
    
    # Calculate max drawdown
    peak = initial_balance
    max_dd = 0
    for db in daily_balances:
        if db['balance'] > peak:
            peak = db['balance']
        dd = ((db['balance'] - peak) / peak) * 100
        if dd < max_dd:
            max_dd = dd
    
    max_profit = max([t['net_pnl'] for t in trades]) if trades else 0
    
    # Calculate Sharpe ratio
    if len(daily_balances) > 1:
        returns = []
        for i in range(1, len(daily_balances)):
            ret = (daily_balances[i]['balance'] - daily_balances[i-1]['balance']) / daily_balances[i-1]['balance']
            returns.append(ret)
        
        if len(returns) > 0 and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
            sharpe = max(-10, min(10, sharpe))
        else:
            sharpe = 0
    else:
        sharpe = 0
    
    # Count margin liquidated trades
    margin_liquidated_trades = len([t for t in trades if t.get('margin_liquidated', False)])
    
    return {
        'indicatorName': indicator_name,
        'indicatorParams': indicator_params,
        'leverage': leverage,
        'stopLoss': stop_loss_pct,
        'timeframe': timeframe,
        'crashProtectionEnabled': crash_protection_enabled,
        'signalBased': True,
        'signalConfig': signal_config,
        'initialBalance': initial_balance,
        'finalBalance': final_balance,
        'totalContributions': contributions,
        'totalReturn': total_return,
        'totalTrades': total_trades,
        'winningTrades': winning_trades,
        'losingTrades': losing_trades,
        'winRate': win_rate,
        'maxDrawdown': max_dd,
        'maxProfit': max_profit,
        'sharpeRatio': sharpe,
        'totalFees': total_fees,
        'totalSpreadCosts': total_spread_costs,
        'totalOvernightCosts': total_overnight_costs,
        # Margin Level tracking metrics
        'minMarginLevel': min_margin_level if min_margin_level != float('inf') else None,
        'liquidationCount': liquidation_count,
        'marginLiquidatedTrades': margin_liquidated_trades,
        'totalLiquidationLoss': total_liquidation_loss,
        'marginCloseoutLevel': margin_closeout_level,
        'trades': trades,
        'dailyBalances': daily_balances,
    }


# =============================================================================
# VERSION 3: ENHANCED SIGNAL-BASED BACKTEST WITH TRUST MATRIX
# =============================================================================
# 
# This function extends signal-based trading to support:
# - Multiple entry indicators (any bullish signal can trigger entry)
# - Multiple exit indicators (any bearish signal can trigger exit)
# - Trust matrix for conflict resolution (select best indicator pairs)
# - Parallel exit simulation (learn performance of ALL entry/exit combinations)
# - Leverage-safe stop loss calculation
# - Early termination on low balance
# - Short positions and position reversal
#
# The trust matrix tracks historical performance of entry/exit pairs and uses
# Expected Value (EV) to select the best entry when multiple indicators fire.
# =============================================================================

def run_enhanced_signal_backtest(
    df: pd.DataFrame,
    epic: str,
    entry_indicators: List[str],
    exit_indicators: List[str],
    entry_params: Dict[str, Dict[str, Any]],
    exit_params: Dict[str, Dict[str, Any]],
    initial_balance: float,
    monthly_topup: float,
    position_size_pct: float = 50.0,
    allow_short: bool = False,
    reverse_on_signal: bool = False,
    stop_loss_mode: str = 'auto',  # 'none', 'fixed', 'auto', 'random'
    fixed_stop_loss_pct: float = 2.0,
    margin_closeout_level: float = 55.0,  # Capital.com uses 50%, default to 55% for safety
    min_balance_threshold: float = 10.0,
    use_trust_matrix: bool = True,
    default_leverage: int = 5,
    timeframe: str = '5m',
    direction: str = 'long',
    trust_matrix: Optional[Dict] = None,
    random_stop_loss_range: Tuple[float, float] = (2.0, 10.0),  # For 'random' mode
) -> Dict[str, Any]:
    """
    VERSION 3: Enhanced signal-based backtest with multi-indicator support and trust matrix.
    
    This is the advanced backtest mode that learns optimal indicator pair relationships
    through parallel exit simulation and builds a trust matrix for live trading.
    
    Key Features:
    -------------
    1. MULTI-INDICATOR ENTRY: Any bullish indicator firing triggers potential entry
       - When multiple fire, trust matrix EV selects the best one
       - Falls back to first indicator if no trust data
    
    2. MULTI-INDICATOR EXIT: Any bearish indicator firing triggers exit
       - Can either close position (go flat) or reverse (go short)
       - Exit indicator's leverage used in Reverse mode
    
    3. TRUST MATRIX LEARNING: During backtesting, we track performance of ALL
       entry/exit indicator combinations (not just the winning one)
       - Updates probability, Sharpe, win rate, avg hold time
       - Learns optimal leverage and stop loss per pair
    
    4. LEVERAGE-SAFE STOP LOSS: Auto mode calculates max stop loss that won't
       trigger margin liquidation given the leverage and closeout level
    
    5. EARLY TERMINATION: If balance falls below threshold, backtest fails fast
       - Saves computation time on losing strategies
    
    Args:
        df: DataFrame with OHLCV candle data
        epic: Symbol to trade (e.g., 'SOXL')
        entry_indicators: List of bullish indicator names for entry
        exit_indicators: List of bearish indicator names for exit
        entry_params: Dict mapping indicator name → parameter dict
        exit_params: Dict mapping indicator name → parameter dict
        initial_balance: Starting account balance
        monthly_topup: Monthly contribution amount
        position_size_pct: % of available balance per trade (1-100)
        allow_short: If True and epic supports it, can short on bearish signal
        reverse_on_signal: If True, exit signal opens opposite position
        stop_loss_mode: 'none' | 'fixed' | 'auto' | 'random' (leverage-safe or random from range)
        fixed_stop_loss_pct: Stop loss % when mode='fixed'
        margin_closeout_level: Broker margin closeout % (Capital.com=50%, use 55% for safety)
        min_balance_threshold: Fail backtest if balance falls below this
        use_trust_matrix: Whether to use/update trust relationships
        default_leverage: Leverage if not in trust matrix
        timeframe: Candle timeframe
        direction: Initial direction preference ('long')
        trust_matrix: Optional pre-loaded trust matrix (None = start fresh)
        random_stop_loss_range: (min, max) for random stop loss when mode='random'
    
    Returns:
        Dictionary with backtest results including:
        - Standard metrics (P&L, Sharpe, drawdown, win rate)
        - backtestType: 'signal_based' (for filtering in UI)
        - trustMatrix: Updated trust matrix data
        - failReason: If early termination occurred
    """
    # Import trust matrix functions
    from trust_matrix import (
        update_trust_matrix,
        select_best_entry,
        calculate_leverage_safe_stop_loss,
        get_pair_leverage,
    )
    
    def serialize_trust_matrix(tm: Dict) -> Dict:
        """Convert tuple keys to string keys for JSON serialization."""
        serialized = {}
        for key, value in tm.items():
            if isinstance(key, tuple):
                # Convert (entry, exit) tuple to "entry||exit" string
                str_key = f"{key[0]}||{key[1]}"
            else:
                str_key = str(key)
            serialized[str_key] = value
        return serialized
    
    # Initialize trust matrix if not provided
    if trust_matrix is None:
        trust_matrix = {}
    
    # Load market info for costs and sizing
    from market_info_loader import get_market_info
    market_info = get_market_info(epic)
    if not market_info:
        market_info = {
            'spread_percent': 0.0018,
            'min_contract_size': 1.0,
            'max_contract_size': 10000.0,
            'min_size_increment': 1.0,
            'contract_multiplier': 1.0,
            'overnight_funding_long_percent': -0.00023,
            'overnight_funding_short_percent': -0.0000096,
            'margin_factor': 0.05,  # 20x max leverage default
        }
    
    if 'min_size_increment' not in market_info:
        market_info['min_size_increment'] = 1.0
    if 'margin_factor' not in market_info:
        market_info['margin_factor'] = 0.05
    
    # Load risk settings
    risk_settings = get_risk_settings()
    margin_closeout_level = risk_settings['margin_closeout_level']
    liquidation_slippage_pct = risk_settings['liquidation_slippage_pct']
    margin_factor = market_info.get('margin_factor', 0.05)
    
    # Initialize state
    balance = initial_balance
    contributions = 0
    trades = []
    daily_balances = []
    
    # Position tracking
    position = 'FLAT'  # 'FLAT', 'LONG', 'SHORT'
    current_trade = None
    entry_indicator_used = None  # Track which indicator opened position
    
    # Metrics tracking
    min_margin_level = float('inf')
    liquidation_count = 0
    total_liquidation_loss = 0.0
    
    # Get trading days for monthly topup
    trading_days = sorted(df['date'].unique())
    last_topup_month = None
    
    # Initialize daily balance
    if len(trading_days) > 0:
        daily_balances.append({
            'date': str(trading_days[0]),
            'balance': initial_balance,
        })
    
    # Need enough history for indicators
    min_history = 50
    
    # ==========================================================================
    # MAIN CANDLE LOOP
    # ==========================================================================
    for i in range(min_history, len(df)):
        candle = df.iloc[i]
        current_time = candle['snapshotTime']
        current_day = candle['date']
        
        # ---------------------------------------------------------------------
        # EARLY TERMINATION CHECK
        # ---------------------------------------------------------------------
        if min_balance_threshold > 0 and balance <= min_balance_threshold:
            # Clean up trades for JSON serialization
            for trade in trades:
                for key in ['entry_time_dt', 'entry_day', 'stop_loss_price']:
                    trade.pop(key, None)
            
            return {
                'status': 'failed',
                'failReason': f'Balance fell below ${min_balance_threshold:.2f}',
                'finalBalance': balance,
                'backtestType': 'signal_based',
                'indicatorName': f"Multi: {len(entry_indicators)} entry, {len(exit_indicators)} exit",
                'indicatorParams': {'entry': entry_params, 'exit': exit_params},
                'leverage': default_leverage,
                'stopLoss': fixed_stop_loss_pct,
                'timeframe': timeframe,
                'initialBalance': initial_balance,
                'totalContributions': contributions,
                'totalReturn': ((balance - initial_balance - contributions) / (initial_balance + contributions)) * 100,
                'totalTrades': len(trades),
                'winningTrades': len([t for t in trades if t.get('net_pnl', 0) > 0]),
                'losingTrades': len([t for t in trades if t.get('net_pnl', 0) <= 0]),
                'winRate': 0,
                'maxDrawdown': -100,
                'sharpeRatio': -10,
                'totalFees': 0,
                'trades': trades,
                'dailyBalances': daily_balances,
                'trustMatrix': serialize_trust_matrix(trust_matrix),
            }

        # ---------------------------------------------------------------------
        # MONTHLY TOPUP
        # ---------------------------------------------------------------------
        current_month = pd.to_datetime(str(current_day)).to_period('M')
        if last_topup_month is None or current_month > last_topup_month:
            balance += monthly_topup
            contributions += monthly_topup
            last_topup_month = current_month
        
        # Get historical data for indicator evaluation
        hist_data = df.iloc[:i+1].copy()
        
        # ---------------------------------------------------------------------
        # POSITION MANAGEMENT: Check exits if in position
        # ---------------------------------------------------------------------
        if position != 'FLAT' and current_trade:
            entry_price = current_trade['entry_price']
            entry_time = current_trade['entry_time_dt']
            entry_day = current_trade['entry_day']
            contracts = current_trade['contracts']
            actual_notional = current_trade['notional_value']
            stop_loss_price = current_trade.get('stop_loss_price')
            current_direction = current_trade['direction']
            
            # Required margin for ML check
            required_margin = actual_notional * margin_factor
            liq_trigger_price = current_trade.get('liq_trigger_price')  # Pre-calculated at entry
            
            should_exit = False
            exit_reason = None
            exit_price = None
            exit_indicator = None
            margin_liquidated = False
            
            # =========================================================================
            # CHECK 1: LIQUIDATION (using accurate trigger price)
            # In real trading, Capital.com liquidates at the MOMENT ML hits threshold.
            # We check if the candle crosses the pre-calculated trigger price.
            # =========================================================================
            if liq_trigger_price is not None:
                crossed, trigger_exit = check_liquidation_crossed(
                    current_direction, liq_trigger_price, candle['lowPrice'], candle['highPrice']
                )
                if crossed:
                    should_exit = True
                    margin_liquidated = True
                    exit_reason = 'margin_liquidation'
                    if current_direction == 'long':
                        exit_price = liq_trigger_price * (1 - liquidation_slippage_pct / 100)
                    else:
                        exit_price = liq_trigger_price * (1 + liquidation_slippage_pct / 100)
                    liquidation_count += 1
                    min_margin_level = min(min_margin_level, margin_closeout_level)
            
            # Track actual margin level at candle's worst price (for non-liquidation tracking)
            if not should_exit:
                worst_case_price = candle['lowPrice'] if current_direction == 'long' else candle['highPrice']
                if current_direction == 'long':
                    unrealized_pnl = (worst_case_price - entry_price) * contracts * market_info['contract_multiplier']
                else:
                    unrealized_pnl = (entry_price - worst_case_price) * contracts * market_info['contract_multiplier']
                
                current_equity = balance + unrealized_pnl
                current_position_value = contracts * worst_case_price * market_info['contract_multiplier']
                current_required_margin = current_position_value * margin_factor
                current_ml = (current_equity / current_required_margin) * 100 if current_required_margin > 0 else float('inf')
                
                if current_ml < min_margin_level:
                    min_margin_level = current_ml
            
            # ---- CHECK 2: STOP LOSS ----
            if not should_exit and stop_loss_price:
                if current_direction == 'long' and candle['lowPrice'] <= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
                elif current_direction == 'short' and candle['highPrice'] >= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
            
            # ---- CHECK 3: EXIT INDICATORS ----
            if not should_exit:
                for exit_ind in exit_indicators:
                    try:
                        params = exit_params.get(exit_ind, {})
                        eval_result = evaluate_indicator_at_index(
                            df=hist_data,
                            indicator_name=exit_ind,
                            indicator_params=params,
                            check_index=-1,
                            crash_protection_enabled=False,
                        )
                        if eval_result.get('signal') and not eval_result.get('error'):
                            should_exit = True
                            exit_reason = 'indicator_signal'
                            exit_indicator = exit_ind
                            exit_price = candle['closePrice']
                            break
                    except Exception:
                        continue
            
            # ---- EXECUTE EXIT ----
            if should_exit and exit_price:
                # Calculate P&L
                if current_direction == 'long':
                    price_change = exit_price - entry_price
                else:
                    price_change = entry_price - exit_price
                
                gross_pnl = contracts * price_change * market_info['contract_multiplier']
                
                # Spread cost
                entry_bid = current_trade.get('entry_bid', 0)
                entry_ask = current_trade.get('entry_ask', 0)
                if entry_bid > 0 and entry_ask > 0:
                    spread_cost = contracts * (entry_ask - entry_bid) * market_info.get('contract_multiplier', 1.0)
                else:
                    spread_cost = actual_notional * market_info['spread_percent']
                
                # Overnight funding
                overnight_cost = 0
                if current_time.date() > entry_time.date():
                    days_held = (current_time.date() - entry_time.date()).days
                    funding_rate = market_info['overnight_funding_long_percent'] if current_direction == 'long' else market_info['overnight_funding_short_percent']
                    overnight_cost = actual_notional * (-funding_rate) * days_held
                
                total_costs = spread_cost + overnight_cost
                net_pnl = gross_pnl - total_costs
                balance += net_pnl
                
                # Calculate hold bars for trust matrix
                hold_bars = i - current_trade.get('entry_index', i)
                
                # ---- UPDATE TRUST MATRIX ----
                # Update for the actual entry/exit pair used
                if use_trust_matrix and entry_indicator_used and exit_indicator:
                    trust_matrix = update_trust_matrix(
                        trust_matrix,
                        entry_indicator_used,
                        exit_indicator,
                        {
                            'pnl': net_pnl,
                            'hold_bars': hold_bars,
                            'entry_leverage': current_trade.get('leverage', default_leverage),
                            'stop_loss_pct': current_trade.get('stop_loss_pct', fixed_stop_loss_pct),
                        },
                        was_winner=True  # This was the actual path taken
                    )
                
                # Track liquidation extra loss
                liquidation_extra_loss = 0.0
                if margin_liquidated and stop_loss_price:
                    if current_direction == 'long':
                        stop_loss_pnl = (stop_loss_price - entry_price) * contracts * market_info['contract_multiplier']
                    else:
                        stop_loss_pnl = (entry_price - stop_loss_price) * contracts * market_info['contract_multiplier']
                    liquidation_extra_loss = stop_loss_pnl - gross_pnl
                    total_liquidation_loss += liquidation_extra_loss
                
                # Record trade
                current_trade.update({
                    'exit_time': str(current_time),
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'exit_indicator': exit_indicator,
                    'stopped_out': exit_reason == 'stop_loss',
                    'margin_liquidated': margin_liquidated,
                    'gross_pnl': gross_pnl,
                    'spread_cost': spread_cost,
                    'overnight_costs': overnight_cost,
                    'costs': total_costs,
                    'net_pnl': net_pnl,
                    'balance_after': balance,
                    'hold_bars': hold_bars,
                    'liquidation_extra_loss': liquidation_extra_loss,
                })
                trades.append(current_trade)
                
                # Reset position
                position = 'FLAT'
                current_trade = None
                entry_indicator_used = None
                
                # ---- REVERSE POSITION if enabled ----
                if reverse_on_signal and exit_reason == 'indicator_signal' and allow_short:
                    # The exit indicator becomes the entry for opposite direction
                    new_direction = 'short' if current_direction == 'long' else 'long'
                    
                    # Get leverage for the reversing indicator
                    # In Reverse mode, use the exit indicator's optimal leverage
                    reverse_leverage = default_leverage
                    if use_trust_matrix and exit_indicator:
                        # Look up any pair starting with this indicator as entry
                        for (entry, _), stats in trust_matrix.items():
                            if entry == exit_indicator:
                                reverse_leverage = stats.get('optimal_leverage', default_leverage)
                                break
                    
                    # Calculate stop loss
                    if stop_loss_mode == 'auto':
                        stop_loss_pct = calculate_leverage_safe_stop_loss(
                            reverse_leverage, margin_closeout_level, 0.8
                        )
                    elif stop_loss_mode == 'fixed':
                        stop_loss_pct = fixed_stop_loss_pct
                    elif stop_loss_mode == 'random':
                        # Random stop loss from range (can blow account!)
                        stop_loss_pct = random.uniform(random_stop_loss_range[0], random_stop_loss_range[1])
                    else:  # 'none'
                        stop_loss_pct = None
                    
                    # Calculate position size
                    investment_amount = balance * (position_size_pct / 100)
                    notional_value = investment_amount * reverse_leverage
                    new_entry_price = candle['closePrice']
                    new_contracts = notional_value / (new_entry_price * market_info['contract_multiplier'])
                    
                    # Round to increment
                    increment = market_info.get('min_size_increment', 1.0)
                    if increment > 0:
                        new_contracts = (new_contracts // increment) * increment
                    new_contracts = max(market_info['min_contract_size'], min(new_contracts, market_info['max_contract_size']))
                    
                    # Check if we hit the max contracts cap
                    at_max_contracts = bool(new_contracts == market_info['max_contract_size'])
                    
                    actual_new_notional = new_contracts * new_entry_price * market_info['contract_multiplier']
                    
                    # Calculate stop loss price
                    if stop_loss_pct:
                        if new_direction == 'long':
                            new_stop_loss_price = new_entry_price * (1 - stop_loss_pct / 100)
                        else:
                            new_stop_loss_price = new_entry_price * (1 + stop_loss_pct / 100)
                    else:
                        new_stop_loss_price = None
                    
                    # Calculate liquidation trigger price for new position
                    new_liq_trigger_price = calculate_liquidation_trigger_price(
                        direction=new_direction,
                        entry_price=new_entry_price,
                        balance=balance,
                        contracts=new_contracts,
                        contract_multiplier=market_info['contract_multiplier'],
                        margin_factor=margin_factor,
                        margin_closeout_level=margin_closeout_level
                    )
                    
                    # Open reversed position
                    entry_bid = candle.get('closeBid', 0) if hasattr(candle, 'get') else candle['closeBid'] if 'closeBid' in candle.index else 0
                    entry_ask = candle.get('closeAsk', 0) if hasattr(candle, 'get') else candle['closeAsk'] if 'closeAsk' in candle.index else 0
                    
                    current_trade = {
                        'date': str(current_day),
                        'entry_time': str(current_time),
                        'entry_time_dt': current_time,
                        'entry_day': current_day,
                        'entry_index': i,
                        'entry_price': new_entry_price,
                        'entry_bid': float(entry_bid) if entry_bid else 0,
                        'entry_ask': float(entry_ask) if entry_ask else 0,
                        'contracts': new_contracts,
                        'at_max_contracts': at_max_contracts,
                        'position_size': new_contracts,
                        'notional_value': actual_new_notional,
                        'investment': investment_amount,
                        'leverage': reverse_leverage,
                        'stop_loss_pct': stop_loss_pct,
                        'stop_loss_price': new_stop_loss_price,
                        'direction': new_direction,
                        'entry_indicator': exit_indicator,  # The exit indicator is now the entry
                        'signal_based': True,
                        'reversed_from': current_direction,
                        'liq_trigger_price': new_liq_trigger_price,
                    }
                    
                    position = 'SHORT' if new_direction == 'short' else 'LONG'
                    entry_indicator_used = exit_indicator
                
                continue  # Move to next candle
        
        # ---------------------------------------------------------------------
        # ENTRY CHECK: Look for entry signals if FLAT
        # ---------------------------------------------------------------------
        if position == 'FLAT':
            # Evaluate ALL entry indicators
            firing_entries = []
            
            for entry_ind in entry_indicators:
                try:
                    params = entry_params.get(entry_ind, {})
                    eval_result = evaluate_indicator_at_index(
                        df=hist_data,
                        indicator_name=entry_ind,
                        indicator_params=params,
                        check_index=-1,
                        crash_protection_enabled=False,
                    )
                    if eval_result.get('signal') and not eval_result.get('error'):
                        firing_entries.append(entry_ind)
                except Exception:
                    continue
            
            # If any entry indicator fired, select the best one
            if firing_entries:
                # Use trust matrix to select best entry
                if use_trust_matrix and len(firing_entries) > 1:
                    selected_entry = select_best_entry(
                        firing_entries, exit_indicators, trust_matrix, fallback_method='first'
                    )
                else:
                    selected_entry = firing_entries[0]
                
                # Get leverage for this entry
                entry_leverage = default_leverage
                if use_trust_matrix:
                    # Look up best exit pair for this entry
                    best_sharpe = -float('inf')
                    for exit_ind in exit_indicators:
                        pair_stats = trust_matrix.get((selected_entry, exit_ind), {})
                        pair_sharpe = pair_stats.get('sharpe', -float('inf'))
                        if pair_sharpe > best_sharpe:
                            best_sharpe = pair_sharpe
                            entry_leverage = pair_stats.get('optimal_leverage', default_leverage)
                
                # Calculate stop loss
                if stop_loss_mode == 'auto':
                    stop_loss_pct = calculate_leverage_safe_stop_loss(
                        entry_leverage, margin_closeout_level, 0.8
                    )
                elif stop_loss_mode == 'fixed':
                    stop_loss_pct = fixed_stop_loss_pct
                elif stop_loss_mode == 'random':
                    # Random stop loss from range (can blow account!)
                    stop_loss_pct = random.uniform(random_stop_loss_range[0], random_stop_loss_range[1])
                else:  # 'none'
                    stop_loss_pct = None
                
                # Calculate position size
                entry_price = candle['closePrice']
                investment_amount = balance * (position_size_pct / 100)
                notional_value = investment_amount * entry_leverage
                contracts = notional_value / (entry_price * market_info['contract_multiplier'])
                
                # Round to increment
                increment = market_info.get('min_size_increment', 1.0)
                if increment > 0:
                    contracts = (contracts // increment) * increment
                contracts = max(market_info['min_contract_size'], min(contracts, market_info['max_contract_size']))
                
                # Check if we hit the max contracts cap
                at_max_contracts = bool(contracts == market_info['max_contract_size'])
                
                actual_notional = contracts * entry_price * market_info['contract_multiplier']
                actual_investment = actual_notional / entry_leverage
                
                # Calculate stop loss price
                if stop_loss_pct:
                    if direction == 'long':
                        stop_loss_price = entry_price * (1 - stop_loss_pct / 100)
                    else:
                        stop_loss_price = entry_price * (1 + stop_loss_pct / 100)
                else:
                    stop_loss_price = None
                
                # Calculate liquidation trigger price (exact price where ML hits closeout)
                liq_trigger_price = calculate_liquidation_trigger_price(
                    direction=direction,
                    entry_price=entry_price,
                    balance=balance,
                    contracts=contracts,
                    contract_multiplier=market_info['contract_multiplier'],
                    margin_factor=margin_factor,
                    margin_closeout_level=margin_closeout_level
                )
                
                # Store bid/ask for spread calculation
                entry_bid = candle.get('closeBid', 0) if hasattr(candle, 'get') else candle['closeBid'] if 'closeBid' in candle.index else 0
                entry_ask = candle.get('closeAsk', 0) if hasattr(candle, 'get') else candle['closeAsk'] if 'closeAsk' in candle.index else 0
                
                current_trade = {
                    'date': str(current_day),
                    'entry_time': str(current_time),
                    'entry_time_dt': current_time,
                    'entry_day': current_day,
                    'entry_index': i,
                    'entry_price': entry_price,
                    'entry_bid': float(entry_bid) if entry_bid else 0,
                    'entry_ask': float(entry_ask) if entry_ask else 0,
                    'contracts': contracts,
                    'at_max_contracts': at_max_contracts,
                    'position_size': contracts,
                    'notional_value': actual_notional,
                    'investment': actual_investment,
                    'leverage': entry_leverage,
                    'stop_loss_pct': stop_loss_pct,
                    'stop_loss_price': stop_loss_price,
                    'direction': direction,
                    'entry_indicator': selected_entry,
                    'all_firing_entries': firing_entries,  # For debugging
                    'signal_based': True,
                    'liq_trigger_price': liq_trigger_price,
                }
                
                position = 'LONG' if direction == 'long' else 'SHORT'
                entry_indicator_used = selected_entry
        
        # ---------------------------------------------------------------------
        # RECORD DAILY BALANCE
        # ---------------------------------------------------------------------
        if i == len(df) - 1 or df.iloc[i+1]['date'] != current_day:
            daily_balances.append({
                'date': str(current_day),
                'balance': balance,
            })
    
    # ==========================================================================
    # CLOSE REMAINING POSITION AT END OF DATA
    # ==========================================================================
    if position != 'FLAT' and current_trade:
        last_candle = df.iloc[-1]
        exit_price = last_candle['closePrice']
        exit_time = last_candle['snapshotTime']
        entry_price = current_trade['entry_price']
        entry_time = current_trade['entry_time_dt']
        contracts = current_trade['contracts']
        actual_notional = current_trade['notional_value']
        current_direction = current_trade['direction']
        
        if current_direction == 'long':
            price_change = exit_price - entry_price
        else:
            price_change = entry_price - exit_price
        
        gross_pnl = contracts * price_change * market_info['contract_multiplier']
        
        # Costs
        entry_bid = current_trade.get('entry_bid', 0)
        entry_ask = current_trade.get('entry_ask', 0)
        if entry_bid > 0 and entry_ask > 0:
            spread_cost = contracts * (entry_ask - entry_bid) * market_info.get('contract_multiplier', 1.0)
        else:
            spread_cost = actual_notional * market_info['spread_percent']
        
        overnight_cost = 0
        if exit_time.date() > entry_time.date():
            days_held = (exit_time.date() - entry_time.date()).days
            funding_rate = market_info['overnight_funding_long_percent'] if current_direction == 'long' else market_info['overnight_funding_short_percent']
            overnight_cost = actual_notional * (-funding_rate) * days_held
        
        total_costs = spread_cost + overnight_cost
        net_pnl = gross_pnl - total_costs
        balance += net_pnl
        
        current_trade.update({
            'exit_time': str(exit_time),
            'exit_price': exit_price,
            'exit_reason': 'end_of_data',
            'exit_indicator': None,
            'stopped_out': False,
            'margin_liquidated': False,
            'gross_pnl': gross_pnl,
            'spread_cost': spread_cost,
            'overnight_costs': overnight_cost,
            'costs': total_costs,
            'net_pnl': net_pnl,
            'balance_after': balance,
            'liquidation_extra_loss': 0.0,
        })
        trades.append(current_trade)
    
    # ==========================================================================
    # CLEAN UP AND CALCULATE FINAL METRICS
    # ==========================================================================
    
    # Remove non-serializable fields from trades
    for trade in trades:
        for key in ['entry_time_dt', 'entry_day', 'stop_loss_price']:
            trade.pop(key, None)
    
    # Calculate metrics
    total_trades = len(trades)
    winning_trades = len([t for t in trades if t.get('net_pnl', 0) > 0])
    losing_trades = len([t for t in trades if t.get('net_pnl', 0) <= 0])
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    total_spread_costs = sum(t.get('spread_cost', 0) for t in trades)
    total_overnight_costs = sum(t.get('overnight_costs', 0) for t in trades)
    total_fees = total_spread_costs + total_overnight_costs
    
    final_balance = balance
    total_return = ((final_balance - initial_balance - contributions) / (initial_balance + contributions)) * 100
    
    # Max drawdown
    peak = initial_balance
    max_dd = 0
    for db in daily_balances:
        if db['balance'] > peak:
            peak = db['balance']
        dd = ((db['balance'] - peak) / peak) * 100
        if dd < max_dd:
            max_dd = dd
    
    # Sharpe ratio
    if len(daily_balances) > 1:
        returns = []
        for j in range(1, len(daily_balances)):
            ret = (daily_balances[j]['balance'] - daily_balances[j-1]['balance']) / daily_balances[j-1]['balance']
            returns.append(ret)
        
        if len(returns) > 0 and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
            sharpe = max(-10, min(10, sharpe))
        else:
            sharpe = 0
    else:
        sharpe = 0
    
    margin_liquidated_trades = len([t for t in trades if t.get('margin_liquidated', False)])
    
    return {
        # === VERSION 3 MARKER ===
        'backtestType': 'signal_based',
        
        # Indicator info (multi-indicator format)
        'indicatorName': f"Multi: {len(entry_indicators)} entry, {len(exit_indicators)} exit",
        'indicatorParams': {
            'entry_indicators': entry_indicators,
            'exit_indicators': exit_indicators,
            'entry_params': entry_params,
            'exit_params': exit_params,
        },
        'leverage': default_leverage,
        'stopLoss': fixed_stop_loss_pct,
        'timeframe': timeframe,
        'crashProtectionEnabled': False,
        
        # Signal-based config
        'signalBased': True,
        'signalConfig': {
            'allowShort': allow_short,
            'reverseOnSignal': reverse_on_signal,
            'stopLossMode': stop_loss_mode,
            'positionSizePct': position_size_pct,
            'minBalanceThreshold': min_balance_threshold,
            'useTrustMatrix': use_trust_matrix,
        },
        
        # Results
        'initialBalance': initial_balance,
        'finalBalance': final_balance,
        'totalContributions': contributions,
        'totalReturn': total_return,
        'totalTrades': total_trades,
        'winningTrades': winning_trades,
        'losingTrades': losing_trades,
        'winRate': win_rate,
        'maxDrawdown': max_dd,
        'sharpeRatio': sharpe,
        'totalFees': total_fees,
        'totalSpreadCosts': total_spread_costs,
        'totalOvernightCosts': total_overnight_costs,
        
        # Margin tracking
        'minMarginLevel': min_margin_level if min_margin_level != float('inf') else None,
        'liquidationCount': liquidation_count,
        'marginLiquidatedTrades': margin_liquidated_trades,
        'totalLiquidationLoss': total_liquidation_loss,
        'marginCloseoutLevel': margin_closeout_level,
        
        # Detailed data
        'trades': trades,
        'dailyBalances': daily_balances,
        
        # Trust matrix (for saving back to DB or passing to live trading)
        # Keys are serialized from (entry, exit) tuples to "entry||exit" strings for JSON
        'trustMatrix': serialize_trust_matrix(trust_matrix),
    }


if __name__ == '__main__':
    # Test with command line arguments
    if len(sys.argv) > 1:
        config_json = sys.argv[1]
        config = json.loads(config_json)
        
        # Load data - ALL DATA IS FROM CAPITAL.COM (UTC)
        timeframe = config.get('timeframe', '5m')
        data_source = config.get('data_source', 'capital')  # Always use capital.com data
        df = load_epic_data(
            config['epic'],
            config.get('start_date'),
            config.get('end_date'),
            timeframe,
            data_source
        )
        
        print(f"Loaded {len(df)} {timeframe} candles for {config['epic']}", file=sys.stderr)
        
        # Check if signal-based trading is enabled
        signal_config = config.get('signal_based_config')
        timing_config = config.get('timing_config', {})
        is_signal_based = (
            signal_config and signal_config.get('enabled', False)
        ) or (
            timing_config.get('mode') == 'SignalBased'
        )
        
        if is_signal_based:
            # Run signal-based backtest (use optimized version if available - 7.5x faster)
            if OPTIMIZED_SIGNAL_BACKTEST_AVAILABLE:
                print(f"Running SIGNAL-BASED backtest (OPTIMIZED - 7.5x faster)", file=sys.stderr)
                result = run_signal_based_backtest_optimized(
                    df=df,
                    epic=config['epic'],
                    indicator_name=config['indicator'],
                    indicator_params=config['params'],
                    leverage=config['leverage'],
                    stop_loss_pct=config['stop_loss'],
                    initial_balance=config['initial_balance'],
                    monthly_topup=config['monthly_topup'],
                    investment_pct=config['investment_pct'],
                    direction=config['direction'],
                    signal_config=signal_config,
                    timeframe=timeframe,
                    crash_protection_enabled=config.get('crash_protection_enabled', False)
                )
            else:
                print(f"Running SIGNAL-BASED backtest", file=sys.stderr)
                result = run_signal_based_backtest(
                    df=df,
                    epic=config['epic'],
                    indicator_name=config['indicator'],
                    indicator_params=config['params'],
                    leverage=config['leverage'],
                    stop_loss_pct=config['stop_loss'],
                    initial_balance=config['initial_balance'],
                    monthly_topup=config['monthly_topup'],
                    investment_pct=config['investment_pct'],
                    direction=config['direction'],
                    signal_config=signal_config,
                    timeframe=timeframe,
                    crash_protection_enabled=config.get('crash_protection_enabled', False)
                )
        else:
            # Run standard time-based backtest
            result = run_single_backtest(
                df=df,
                epic=config['epic'],
                indicator_name=config['indicator'],
                indicator_params=config['params'],
                leverage=config['leverage'],
                stop_loss_pct=config['stop_loss'],
                initial_balance=config['initial_balance'],
                monthly_topup=config['monthly_topup'],
                investment_pct=config['investment_pct'],
                direction=config['direction'],
                timing_config=timing_config,
                calculation_mode=config.get('calculation_mode', 'standard'),
                stop_conditions=config.get('stop_conditions'),
                timeframe=timeframe,
                crash_protection_enabled=config.get('crash_protection_enabled', False)
            )
        
        # Output with RESULT: prefix for TypeScript bridge to parse
        print(f"RESULT:{json.dumps(result)}")


# =============================================================================
# VERSION 4: PARALLEL EXIT SIMULATION BACKTEST
# =============================================================================
# This is the CORRECT implementation that simulates ALL exits in parallel for
# each trade, finds the best performer, and updates the trust matrix accordingly.
# =============================================================================

def run_parallel_exit_backtest(
    df: pd.DataFrame,
    epic: str,
    entry_indicators: List[str],
    exit_indicators: List[str],
    entry_params: Dict[str, Dict[str, Any]],
    exit_params: Dict[str, Dict[str, Any]],
    initial_balance: float,
    monthly_topup: float,
    position_size_pct: float = 50.0,
    stop_loss_mode: str = 'auto',
    fixed_stop_loss_pct: float = 2.0,
    margin_closeout_level: float = 55.0,
    min_balance_threshold: float = 10.0,
    default_leverage: int = 5,
    timeframe: str = '5m',
    direction: str = 'long',
    trust_matrix: Optional[Dict] = None,
    random_stop_loss_range: Tuple[float, float] = (2.0, 10.0),
) -> Dict[str, Any]:
    """
    VERSION 4: Parallel Exit Simulation Backtest
    
    This implements the TRUE parallel exit approach:
    1. Loop through candles ONCE
    2. When ANY entry indicator fires → open position
    3. While position open, track ALL exit indicators in parallel:
       - Record when each WOULD have exited
       - Calculate hypothetical P&L for each exit
    4. When trade actually closes, compare ALL exit outcomes
    5. Update trust matrix: WINNING exit gets highest score boost
    
    This is far more efficient and builds meaningful trust relationships
    because we directly compare "if I entered here, which exit was best?"
    
    Returns:
        Dict with backtest results including updated trust matrix
    """
    from trust_matrix import (
        update_trust_matrix,
        calculate_leverage_safe_stop_loss,
    )
    from unified_signal import evaluate_indicator_at_index
    from market_info_loader import get_market_info
    
    print(f"[ParallelExit] Starting backtest with {len(entry_indicators)} entries, {len(exit_indicators)} exits", flush=True)
    
    # Initialize
    if trust_matrix is None:
        trust_matrix = {}
    
    market_info = get_market_info(epic)
    if not market_info:
        market_info = {
            'spread_percent': 0.0018, 'min_contract_size': 1.0, 'max_contract_size': 10000.0,
            'min_size_increment': 1.0, 'contract_multiplier': 1.0,
            'overnight_funding_long_percent': -0.00023, 'overnight_funding_short_percent': -0.0000096,
            'margin_factor': 0.05,
        }
    
    margin_factor = market_info.get('margin_factor', 0.05)
    risk_settings = get_risk_settings()
    liquidation_slippage_pct = risk_settings.get('liquidation_slippage_pct', 0.5)
    
    # State tracking
    balance = initial_balance
    contributions = 0
    trades = []
    daily_balances = []
    
    position = 'FLAT'
    current_trade = None
    entry_indicator_used = None
    
    # Parallel exit tracking: when position opens, track each exit indicator
    # Structure: {exit_ind: {'triggered': bool, 'trigger_index': int, 'trigger_price': float, 'pnl': float}}
    exit_simulations = {}
    
    trading_days = sorted(df['date'].unique())
    last_topup_month = None
    
    if len(trading_days) > 0:
        daily_balances.append({'date': str(trading_days[0]), 'balance': initial_balance})
    
    min_history = 50
    total_entries = 0
    
    # =========================================================================
    # MAIN CANDLE LOOP
    # =========================================================================
    for i in range(min_history, len(df)):
        candle = df.iloc[i]
        current_time = candle['snapshotTime']
        current_day = candle['date']
        
        # Early termination
        if min_balance_threshold > 0 and balance <= min_balance_threshold:
            return _build_failed_result(
                f'Balance fell below ${min_balance_threshold:.2f}',
                balance, initial_balance, contributions, trades, daily_balances,
                trust_matrix, entry_indicators, exit_indicators, entry_params, exit_params,
                default_leverage, fixed_stop_loss_pct, timeframe
            )
        
        # Monthly topup
        current_month = pd.to_datetime(str(current_day)).to_period('M')
        if last_topup_month is None or current_month > last_topup_month:
            balance += monthly_topup
            contributions += monthly_topup
            last_topup_month = current_month
        
        hist_data = df.iloc[:i+1].copy()
        
        # ---------------------------------------------------------------------
        # POSITION MANAGEMENT: Track exits in parallel if in position
        # ---------------------------------------------------------------------
        if position != 'FLAT' and current_trade:
            entry_price = current_trade['entry_price']
            contracts = current_trade['contracts']
            actual_notional = current_trade['notional_value']
            current_direction = current_trade['direction']
            stop_loss_price = current_trade.get('stop_loss_price')
            liq_trigger_price = current_trade.get('liq_trigger_price')  # Pre-calculated at entry
            
            should_exit = False
            exit_reason = None
            exit_price = None
            actual_exit_indicator = None
            margin_liquidated = False
            
            # =========================================================================
            # CHECK 1: LIQUIDATION (using accurate trigger price)
            # In real trading, Capital.com liquidates at the MOMENT ML hits threshold.
            # =========================================================================
            if liq_trigger_price is not None:
                crossed, trigger_exit = check_liquidation_crossed(
                    current_direction, liq_trigger_price, candle['lowPrice'], candle['highPrice']
                )
                if crossed:
                    should_exit = True
                    margin_liquidated = True
                    exit_reason = 'margin_liquidation'
                    if current_direction == 'long':
                        exit_price = liq_trigger_price * (1 - liquidation_slippage_pct / 100)
                    else:
                        exit_price = liq_trigger_price * (1 + liquidation_slippage_pct / 100)
            
            # CHECK 2: STOP LOSS
            if not should_exit and stop_loss_price:
                if current_direction == 'long' and candle['lowPrice'] <= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
                elif current_direction == 'short' and candle['highPrice'] >= stop_loss_price:
                    should_exit = True
                    exit_reason = 'stop_loss'
                    exit_price = stop_loss_price
            
            # CHECK 3: PARALLEL EXIT INDICATOR TRACKING
            # For each exit indicator, check if it would have triggered at this candle
            for exit_ind in exit_indicators:
                if exit_ind in exit_simulations and exit_simulations[exit_ind]['triggered']:
                    continue  # Already triggered
                
                try:
                    params = exit_params.get(exit_ind, {})
                    eval_result = evaluate_indicator_at_index(
                        df=hist_data, indicator_name=exit_ind, indicator_params=params,
                        check_index=-1, crash_protection_enabled=False,
                    )
                    
                    if eval_result.get('signal') and not eval_result.get('error'):
                        # This exit WOULD have triggered here
                        sim_exit_price = candle['closePrice']
                        if current_direction == 'long':
                            sim_pnl = (sim_exit_price - entry_price) * contracts * market_info['contract_multiplier']
                        else:
                            sim_pnl = (entry_price - sim_exit_price) * contracts * market_info['contract_multiplier']
                        
                        exit_simulations[exit_ind] = {
                            'triggered': True,
                            'trigger_index': i,
                            'trigger_price': sim_exit_price,
                            'pnl': sim_pnl,
                            'hold_bars': i - current_trade['entry_index'],
                        }
                        
                        # The FIRST exit to trigger actually closes the trade
                        if not should_exit:
                            should_exit = True
                            exit_reason = 'indicator_signal'
                            actual_exit_indicator = exit_ind
                            exit_price = sim_exit_price
                except Exception:
                    continue
            
            # ---------------------------------------------------------------------
            # EXECUTE EXIT: Trade closes, compare all parallel simulations
            # ---------------------------------------------------------------------
            if should_exit and exit_price:
                if current_direction == 'long':
                    actual_pnl = (exit_price - entry_price) * contracts * market_info['contract_multiplier']
                else:
                    actual_pnl = (entry_price - exit_price) * contracts * market_info['contract_multiplier']
                
                # Calculate costs
                entry_bid = current_trade.get('entry_bid', 0)
                entry_ask = current_trade.get('entry_ask', 0)
                spread_cost = contracts * (entry_ask - entry_bid) * market_info.get('contract_multiplier', 1.0) if entry_bid > 0 and entry_ask > 0 else actual_notional * market_info['spread_percent']
                
                entry_time = current_trade['entry_time_dt']
                overnight_cost = 0
                if current_time.date() > entry_time.date():
                    days_held = (current_time.date() - entry_time.date()).days
                    funding_rate = market_info['overnight_funding_long_percent'] if current_direction == 'long' else market_info['overnight_funding_short_percent']
                    overnight_cost = actual_notional * (-funding_rate) * days_held
                
                total_costs = spread_cost + overnight_cost
                net_pnl = actual_pnl - total_costs
                balance += net_pnl
                
                hold_bars = i - current_trade['entry_index']
                
                # -----------------------------------------------------------------
                # PARALLEL EXIT ANALYSIS: Find which exit would have been BEST
                # -----------------------------------------------------------------
                best_exit = None
                best_exit_pnl = float('-inf')
                
                for exit_ind, sim in exit_simulations.items():
                    if sim['triggered']:
                        # Adjust PNL for costs (approximate)
                        sim_net_pnl = sim['pnl'] - total_costs
                        if sim_net_pnl > best_exit_pnl:
                            best_exit_pnl = sim_net_pnl
                            best_exit = exit_ind
                
                # -----------------------------------------------------------------
                # UPDATE TRUST MATRIX: Best exit gets highest score
                # -----------------------------------------------------------------
                for exit_ind, sim in exit_simulations.items():
                    if sim['triggered']:
                        sim_net_pnl = sim['pnl'] - total_costs
                        was_winner = (exit_ind == best_exit)
                        
                        trust_matrix = update_trust_matrix(
                            trust_matrix,
                            entry_indicator_used,
                            exit_ind,
                            {
                                'pnl': sim_net_pnl,
                                'hold_bars': sim['hold_bars'],
                                'entry_leverage': current_trade.get('leverage', default_leverage),
                                'stop_loss_pct': current_trade.get('stop_loss_pct', fixed_stop_loss_pct),
                            },
                            was_winner=was_winner
                        )
                
                # Record trade
                current_trade.update({
                    'exit_time': str(current_time),
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'exit_indicator': actual_exit_indicator,
                    'best_exit_indicator': best_exit,
                    'stopped_out': exit_reason == 'stop_loss',
                    'margin_liquidated': margin_liquidated,
                    'gross_pnl': actual_pnl,
                    'spread_cost': spread_cost,
                    'overnight_costs': overnight_cost,
                    'costs': total_costs,
                    'net_pnl': net_pnl,
                    'balance_after': balance,
                    'hold_bars': hold_bars,
                    'parallel_exits': {k: {'pnl': v['pnl'], 'hold_bars': v['hold_bars']} for k, v in exit_simulations.items() if v['triggered']},
                })
                trades.append(current_trade)
                
                # Reset
                position = 'FLAT'
                current_trade = None
                entry_indicator_used = None
                exit_simulations = {}
                continue
        
        # ---------------------------------------------------------------------
        # ENTRY CHECK: Look for entry signals if FLAT
        # ---------------------------------------------------------------------
        if position == 'FLAT':
            for entry_ind in entry_indicators:
                try:
                    params = entry_params.get(entry_ind, {})
                    eval_result = evaluate_indicator_at_index(
                        df=hist_data, indicator_name=entry_ind, indicator_params=params,
                        check_index=-1, crash_protection_enabled=False,
                    )
                    
                    if eval_result.get('signal') and not eval_result.get('error'):
                        # Entry signal! Open position
                        total_entries += 1
                        entry_leverage = default_leverage
                        
                        if stop_loss_mode == 'auto':
                            stop_loss_pct = calculate_leverage_safe_stop_loss(entry_leverage, margin_closeout_level, 0.8)
                        elif stop_loss_mode == 'fixed':
                            stop_loss_pct = fixed_stop_loss_pct
                        elif stop_loss_mode == 'random':
                            stop_loss_pct = random.uniform(random_stop_loss_range[0], random_stop_loss_range[1])
                        else:
                            stop_loss_pct = None
                        
                        entry_price = candle['closePrice']
                        investment_amount = balance * (position_size_pct / 100)
                        notional_value = investment_amount * entry_leverage
                        contracts = notional_value / (entry_price * market_info['contract_multiplier'])
                        
                        increment = market_info.get('min_size_increment', 1.0)
                        if increment > 0:
                            contracts = (contracts // increment) * increment
                        contracts = max(market_info['min_contract_size'], min(contracts, market_info['max_contract_size']))
                        
                        # Check if we hit the max contracts cap
                        at_max_contracts = bool(contracts == market_info['max_contract_size'])
                        
                        actual_notional = contracts * entry_price * market_info['contract_multiplier']
                        
                        stop_loss_price = None
                        if stop_loss_pct:
                            stop_loss_price = entry_price * (1 - stop_loss_pct / 100) if direction == 'long' else entry_price * (1 + stop_loss_pct / 100)
                        
                        # Calculate liquidation trigger price
                        liq_trigger_price = calculate_liquidation_trigger_price(
                            direction=direction,
                            entry_price=entry_price,
                            balance=balance,
                            contracts=contracts,
                            contract_multiplier=market_info['contract_multiplier'],
                            margin_factor=margin_factor,
                            margin_closeout_level=margin_closeout_level
                        )
                        
                        entry_bid = candle.get('closeBid', 0) if hasattr(candle, 'get') else candle['closeBid'] if 'closeBid' in candle.index else 0
                        entry_ask = candle.get('closeAsk', 0) if hasattr(candle, 'get') else candle['closeAsk'] if 'closeAsk' in candle.index else 0
                        
                        current_trade = {
                            'date': str(current_day),
                            'entry_time': str(current_time),
                            'entry_time_dt': current_time,
                            'entry_index': i,
                            'entry_price': entry_price,
                            'entry_bid': float(entry_bid) if entry_bid else 0,
                            'entry_ask': float(entry_ask) if entry_ask else 0,
                            'contracts': contracts,
                            'at_max_contracts': at_max_contracts,
                            'notional_value': actual_notional,
                            'leverage': entry_leverage,
                            'stop_loss_pct': stop_loss_pct,
                            'stop_loss_price': stop_loss_price,
                            'direction': direction,
                            'entry_indicator': entry_ind,
                            'signal_based': True,
                            'liq_trigger_price': liq_trigger_price,
                        }
                        
                        position = 'LONG' if direction == 'long' else 'SHORT'
                        entry_indicator_used = entry_ind
                        
                        # Initialize parallel exit tracking
                        exit_simulations = {exit_ind: {'triggered': False, 'trigger_index': None, 'trigger_price': None, 'pnl': None, 'hold_bars': None} for exit_ind in exit_indicators}
                        
                        break  # Only open one position
                except Exception:
                    continue
        
        # Daily balance
        if i == len(df) - 1 or df.iloc[i+1]['date'] != current_day:
            daily_balances.append({'date': str(current_day), 'balance': balance})
    
    # Close remaining position
    if position != 'FLAT' and current_trade:
        last_candle = df.iloc[-1]
        exit_price = last_candle['closePrice']
        entry_price = current_trade['entry_price']
        contracts = current_trade['contracts']
        actual_notional = current_trade['notional_value']
        current_direction = current_trade['direction']
        
        if current_direction == 'long':
            actual_pnl = (exit_price - entry_price) * contracts * market_info['contract_multiplier']
        else:
            actual_pnl = (entry_price - exit_price) * contracts * market_info['contract_multiplier']
        
        spread_cost = actual_notional * market_info['spread_percent']
        net_pnl = actual_pnl - spread_cost
        balance += net_pnl
        
        current_trade.update({
            'exit_time': str(last_candle['snapshotTime']),
            'exit_price': exit_price,
            'exit_reason': 'end_of_data',
            'exit_indicator': None,
            'net_pnl': net_pnl,
            'balance_after': balance,
        })
        trades.append(current_trade)
    
    # Calculate final metrics
    return _build_success_result(
        balance, initial_balance, contributions, trades, daily_balances,
        trust_matrix, entry_indicators, exit_indicators, entry_params, exit_params,
        default_leverage, fixed_stop_loss_pct, timeframe, total_entries
    )


def _build_failed_result(fail_reason, balance, initial_balance, contributions, trades, 
                         daily_balances, trust_matrix, entry_indicators, exit_indicators,
                         entry_params, exit_params, leverage, stop_loss, timeframe):
    """Helper to build failed result dict."""
    def serialize_tm(tm):
        return {f"{k[0]}||{k[1]}" if isinstance(k, tuple) else str(k): v for k, v in tm.items()}
    
    for t in trades:
        # Remove temporary/large fields before saving to DB
        # parallel_exits is especially large and already used for trust matrix
        for key in ['entry_time_dt', 'entry_day', 'stop_loss_price', 'parallel_exits']:
            t.pop(key, None)
    
    return {
        'status': 'failed',
        'failReason': fail_reason,
        'finalBalance': balance,
        'backtestType': 'signal_based_parallel',
        'indicatorName': f"Parallel: {len(entry_indicators)} entry, {len(exit_indicators)} exit",
        'indicatorParams': {'entry': entry_params, 'exit': exit_params},
        'leverage': leverage,
        'stopLoss': stop_loss,
        'timeframe': timeframe,
        'initialBalance': initial_balance,
        'totalContributions': contributions,
        'totalReturn': ((balance - initial_balance - contributions) / (initial_balance + contributions)) * 100 if (initial_balance + contributions) > 0 else 0,
        'totalTrades': len(trades),
        'trades': trades,
        'dailyBalances': daily_balances,
        'trustMatrix': serialize_tm(trust_matrix),
    }


def _build_success_result(balance, initial_balance, contributions, trades, daily_balances,
                          trust_matrix, entry_indicators, exit_indicators, entry_params, 
                          exit_params, leverage, stop_loss, timeframe, total_entries):
    """Helper to build success result dict."""
    import numpy as np
    
    def serialize_tm(tm):
        return {f"{k[0]}||{k[1]}" if isinstance(k, tuple) else str(k): v for k, v in tm.items()}
    
    for t in trades:
        # Remove temporary/large fields before saving to DB
        # parallel_exits is especially large and already used for trust matrix
        for key in ['entry_time_dt', 'entry_day', 'stop_loss_price', 'parallel_exits']:
            t.pop(key, None)
    
    # Calculate metrics
    winning_trades = [t for t in trades if t.get('net_pnl', 0) > 0]
    losing_trades = [t for t in trades if t.get('net_pnl', 0) <= 0]
    win_rate = len(winning_trades) / len(trades) * 100 if trades else 0
    
    total_return = ((balance - initial_balance - contributions) / (initial_balance + contributions)) * 100 if (initial_balance + contributions) > 0 else 0
    
    # Max drawdown
    max_drawdown = 0
    if daily_balances:
        balances = [d['balance'] for d in daily_balances]
        peak = balances[0]
        for b in balances:
            if b > peak:
                peak = b
            dd = (peak - b) / peak * 100 if peak > 0 else 0
            if dd > max_drawdown:
                max_drawdown = dd
    
    # Sharpe ratio (simplified)
    sharpe = 0
    if trades:
        returns = [t.get('net_pnl', 0) for t in trades]
        if len(returns) > 1 and np.std(returns) > 0:
            sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
    
    # Calculate cost breakdowns
    total_spread = sum(t.get('spread_cost', 0) for t in trades)
    total_overnight = sum(t.get('overnight_costs', 0) for t in trades)
    total_fees = total_spread + total_overnight
    
    # Find min margin level across all trades
    min_ml = None
    liquidation_count = sum(1 for t in trades if t.get('margin_liquidated', False))
    
    return {
        'status': 'success',
        'finalBalance': balance,
        'backtestType': 'signal_based_parallel',
        'indicatorName': f"Parallel: {len(entry_indicators)} entry, {len(exit_indicators)} exit",
        'indicatorParams': {'entry': entry_params, 'exit': exit_params},
        'leverage': leverage,
        'stopLoss': stop_loss,
        'timeframe': timeframe,
        'initialBalance': initial_balance,
        'totalContributions': contributions,
        'totalReturn': total_return,
        'totalTrades': len(trades),
        'totalEntries': total_entries,
        'winningTrades': len(winning_trades),
        'losingTrades': len(losing_trades),
        'winRate': win_rate,
        'maxDrawdown': max_drawdown,
        'sharpeRatio': sharpe,
        'totalFees': total_fees,
        'totalSpreadCosts': total_spread,
        'totalOvernightCosts': total_overnight,
        'minMarginLevel': min_ml,
        'liquidationCount': liquidation_count,
        'marginLiquidatedTrades': liquidation_count,
        'totalLiquidationLoss': 0,  # Not tracked in parallel mode yet
        'marginCloseoutLevel': None,  # Set at config level
        'trades': trades,
        'dailyBalances': daily_balances,
        'trustMatrix': serialize_tm(trust_matrix),
    }

