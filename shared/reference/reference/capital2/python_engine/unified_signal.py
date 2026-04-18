#!/usr/bin/env python3
"""
Unified Signal Evaluation Module

SINGLE ENTRY POINT for all indicator signal calculations across the system.

This module is called by:
- backtest_runner.py (for each day in historical loop)
- get_current_signal.py (for brain preview / live / validation)
- strategy_executor.py (for DNA strand testing)

By centralizing signal evaluation here, we ensure:
1. Identical calculation logic everywhere
2. Single place to update indicator behavior
3. Consistent crash protection handling
4. Consistent parameter mapping
5. No risk of drift between backtest and live trading

Usage:
    from unified_signal import evaluate_indicator_at_index, get_indicator_display_value
    
    result = evaluate_indicator_at_index(
        df=candle_data,
        indicator_name='rsi_oversold',
        indicator_params={'rsi_oversold': 30, 'rsi_period': 14},
        check_index=-1,  # Last candle
        crash_protection_enabled=True,
        crash_protection_date=datetime.date.today()
    )
    
    if result['signal']:
        print(f"BUY signal! Indicator value: {result['indicator_value']}")
"""

import sys
import inspect
import numpy as np
import pandas as pd
from datetime import date
from typing import Dict, Any, Optional, Tuple

# Import core dependencies
from indicators import IndicatorLibrary, create_indicator_library


def map_indicator_params(indicator_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map database parameter names to indicator function parameter names.
    
    Database stores params like: {rsi_oversold: 30, rsi_period: 12}
    Indicator functions expect: {threshold: 30, period: 12}
    
    This is the SINGLE source of truth for parameter mapping.
    Both backtest and live signal calculation use this function.
    
    Args:
        indicator_name: Name of the indicator (e.g., 'rsi_oversold')
        params: Raw parameters from database/config
        
    Returns:
        Mapped parameters ready for indicator function
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
        'connors_rsi_overbought': {
            'connors_rsi_overbought': 'threshold',
            'connors_rsi_period': 'rsi_period',
            'connors_streak_period': 'streak_period',
            'connors_rank_period': 'rank_period',
        },
        'macd_bullish_cross': {
            'macd_fast_period': 'fast_period',
            'macd_slow_period': 'slow_period',
            'macd_signal_period': 'signal_period',
        },
        'macd_bearish_cross': {
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
        'stoch_overbought': {
            'stoch_overbought': 'threshold',
            'stoch_fastk_period': 'fastk_period',
            'stoch_slowk_period': 'slowk_period',
            'stoch_slowd_period': 'slowd_period',
        },
        'bb_squeeze': {
            'bb_period': 'period',
            'bb_std_dev': 'std_dev',
            'bb_squeeze_threshold': 'squeeze_threshold',
        },
        'bb_breakout_up': {
            'bb_period': 'period',
            'bb_std_dev': 'std_dev',
        },
        'bb_breakout_down': {
            'bb_period': 'period',
            'bb_std_dev': 'std_dev',
        },
        'ema_crossover_bullish': {
            'ema_fast_period': 'fast_period',
            'ema_slow_period': 'slow_period',
        },
        'ema_crossover_bearish': {
            'ema_fast_period': 'fast_period',
            'ema_slow_period': 'slow_period',
        },
        'sma_crossover_bullish': {
            'sma_fast_period': 'fast_period',
            'sma_slow_period': 'slow_period',
        },
        'sma_crossover_bearish': {
            'sma_fast_period': 'fast_period',
            'sma_slow_period': 'slow_period',
        },
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


def get_indicator_display_value(
    lib: IndicatorLibrary,
    indicator_name: str,
    check_index: int,
    params: Dict[str, Any]
) -> Optional[float]:
    """
    Get the indicator's numeric value for display purposes.
    
    This extracts the actual indicator value (e.g., RSI=28.5) rather than
    just the signal (BUY/HOLD). Useful for UI display and logging.
    
    Args:
        lib: IndicatorLibrary instance
        indicator_name: Name of the indicator
        check_index: Index in the dataframe to check
        params: Mapped indicator parameters
        
    Returns:
        Indicator value as float, or None if not extractable
    """
    try:
        indicator_lower = indicator_name.lower()
        
        if 'rsi' in indicator_lower and 'connors' not in indicator_lower:
            period = params.get('period', 14)
            rsi_values = lib.rsi(period=period)
            if check_index < len(rsi_values) and not np.isnan(rsi_values[check_index]):
                return float(rsi_values[check_index])
        
        elif 'connors_rsi' in indicator_lower:
            rsi_period = params.get('rsi_period', 3)
            streak_period = params.get('streak_period', 2)
            rank_period = params.get('rank_period', 100)
            crsi_values = lib.connors_rsi(rsi_period=rsi_period, streak_period=streak_period, rank_period=rank_period)
            if check_index < len(crsi_values) and not np.isnan(crsi_values[check_index]):
                return float(crsi_values[check_index])
        
        elif 'macd' in indicator_lower:
            fast = params.get('fast_period', 12)
            slow = params.get('slow_period', 26)
            signal_period = params.get('signal_period', 9)
            macd_line, signal_line, histogram = lib.macd(fast_period=fast, slow_period=slow, signal_period=signal_period)
            if check_index < len(histogram) and not np.isnan(histogram[check_index]):
                return float(histogram[check_index])
        
        elif 'stoch' in indicator_lower:
            fastk = params.get('fastk_period', 14)
            slowk = params.get('slowk_period', 3)
            slowd = params.get('slowd_period', 3)
            slowk_vals, slowd_vals = lib.stochastic(fastk_period=fastk, slowk_period=slowk, slowd_period=slowd)
            if check_index < len(slowk_vals) and not np.isnan(slowk_vals[check_index]):
                return float(slowk_vals[check_index])
        
        elif 'bb' in indicator_lower or 'bollinger' in indicator_lower:
            period = params.get('period', 20)
            std_dev = params.get('std_dev', 2.0)
            upper, middle, lower = lib.bollinger_bands(period=period, std_dev=std_dev)
            if check_index < len(lower) and not np.isnan(lower[check_index]) and not np.isnan(upper[check_index]):
                close = lib.close[check_index]
                # Return %B (position within bands)
                if upper[check_index] != lower[check_index]:
                    return float((close - lower[check_index]) / (upper[check_index] - lower[check_index]))
        
        elif 'ema' in indicator_lower or 'sma' in indicator_lower:
            fast = params.get('fast_period', 12)
            slow = params.get('slow_period', 26)
            if 'ema' in indicator_lower:
                fast_vals = lib.ema(period=fast)
                slow_vals = lib.ema(period=slow)
            else:
                fast_vals = lib.sma(period=fast)
                slow_vals = lib.sma(period=slow)
            # Return the difference (fast - slow)
            if check_index < len(fast_vals) and not np.isnan(fast_vals[check_index]) and not np.isnan(slow_vals[check_index]):
                return float(fast_vals[check_index] - slow_vals[check_index])
        
    except Exception as e:
        print(f"[UnifiedSignal] Error extracting indicator value: {e}", file=sys.stderr)
    
    return None


def evaluate_indicator_at_index(
    df: pd.DataFrame,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    check_index: int = -1,
    crash_protection_enabled: bool = False,
    crash_protection_date: Optional[date] = None,
    crash_protection_config: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    SINGLE ENTRY POINT for all indicator signal calculations.
    
    This function is called by:
    - backtest_runner.py (for each day in historical loop)
    - get_current_signal.py (for brain preview / live / validation)
    - strategy_executor.py (for DNA strand testing)
    
    Args:
        df: DataFrame with OHLCV data. Must have columns:
            - closePrice, openPrice, highPrice, lowPrice, lastTradedVolume
            - snapshotTime (for crash protection date extraction)
        indicator_name: Name of indicator (e.g., 'rsi_oversold', 'macd_bullish_cross')
        indicator_params: Raw parameters from database/config (will be mapped internally)
        check_index: Index in dataframe to evaluate. -1 = last candle (default)
        crash_protection_enabled: Whether to check crash protection rules
        crash_protection_date: Date for crash protection check (extracted from df if None)
        crash_protection_config: Optional custom crash protection config
        
    Returns:
        {
            'signal': bool - True if indicator signals (e.g., BUY for oversold indicators)
            'indicator_value': float or None - Numeric value of indicator
            'check_index': int - Actual index that was checked
            'timestamp': str - Timestamp of checked candle (ISO format)
            'close_price': float - Close price of checked candle
            'crash_blocked': bool - True if signal was blocked by crash protection
            'crash_reason': str or None - Reason for blocking
            'error': str or None - Error message if evaluation failed
        }
    """
    result = {
        'signal': False,
        'indicator_value': None,
        'check_index': check_index,
        'timestamp': None,
        'close_price': None,
        'crash_blocked': False,
        'crash_reason': None,
        'error': None,
    }
    
    try:
        # Validate df is a proper DataFrame (can happen with malformed shared memory reconstruction)
        if not isinstance(df, pd.DataFrame):
            result['error'] = f"Invalid df type: expected DataFrame, got {type(df).__name__}"
            print(f"[UnifiedSignal] ERROR: df is not a DataFrame, type={type(df).__name__}", file=sys.stderr)
            return result
        
        if len(df) == 0:
            result['error'] = "DataFrame is empty"
            return result
        
        # Normalize check_index
        if check_index < 0:
            check_index = len(df) + check_index  # Convert negative to positive
        
        if check_index >= len(df) or check_index < 0:
            result['error'] = f"Invalid check_index {check_index} for DataFrame of length {len(df)}"
            return result
        
        result['check_index'] = check_index
        
        # Create indicator library
        lib = create_indicator_library(df)
        conditions = lib.get_conditions()
        
        # Validate indicator exists
        if indicator_name not in conditions:
            result['error'] = f"Unknown indicator: {indicator_name}"
            return result
        
        # Map parameters (database format → function format)
        mapped_params = map_indicator_params(indicator_name, indicator_params)
        
        # Filter params to only those the indicator function accepts
        # This prevents "unexpected keyword argument" errors for indicators
        # that don't accept all possible params (e.g., threshold)
        condition_func = conditions[indicator_name]
        try:
            sig = inspect.signature(condition_func)
            valid_params = set(sig.parameters.keys()) - {'df', 'idx'}  # Remove positional args
            filtered_params = {k: v for k, v in mapped_params.items() if k in valid_params}
        except (ValueError, TypeError):
            # If we can't inspect the function, use all params (original behavior)
            filtered_params = mapped_params
        
        # Convert period/window/length parameters to integers (pandas rolling requires int)
        # This prevents "window must be an integer 0 or greater" errors
        INT_PARAMS = {'period', 'window', 'length', 'lookback', 'bars', 'range_bars', 'confirm_bars',
                      'bb_period', 'bb_length', 'kc_length', 'kc_period', 'ma_period', 'fast_period', 'slow_period',
                      'signal_period', 'fastk_period', 'slowk_period', 'slowd_period', 'rsi_period',
                      'streak_period', 'rank_period', 'st_atr_len', 'swing_lookback', 'atr_period',
                      'n_period', 'smooth', 'signal_len', 'ema_fast', 'ema_slow', 'rsi_len',
                      'macd_fast', 'macd_slow', 'macd_signal', 'volume_period', 'price_period',
                      'momentum_length', 'atr_len', 'atr_sma_len', 'ulcer_period', 'chandelier_period',
                      'confirmation_bars', 'timeperiod', 'fastperiod', 'slowperiod', 'signalperiod'}
        for param_name in INT_PARAMS:
            if param_name in filtered_params and filtered_params[param_name] is not None:
                try:
                    filtered_params[param_name] = int(filtered_params[param_name])
                except (ValueError, TypeError):
                    pass  # Leave as-is if conversion fails
        
        # Evaluate signal
        signal = condition_func(df, check_index, **filtered_params)
        result['signal'] = bool(signal)
        
        # Extract indicator display value
        result['indicator_value'] = get_indicator_display_value(lib, indicator_name, check_index, mapped_params)
        
        # Extract timestamp and close price
        if 'snapshotTime' in df.columns:
            ts = df['snapshotTime'].iloc[check_index]
            result['timestamp'] = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
        elif 'timestamp' in df.columns:
            ts = df['timestamp'].iloc[check_index]
            result['timestamp'] = ts.isoformat() if hasattr(ts, 'isoformat') else str(ts)
        
        result['close_price'] = float(df['closePrice'].iloc[check_index])
        
        # Check crash protection (only if signal is True)
        if crash_protection_enabled and result['signal']:
            try:
                from crash_protection import check_crash_protection, CRASH_PROTECTION_CONFIG
                
                # Determine date for crash protection
                if crash_protection_date is None:
                    if 'snapshotTime' in df.columns:
                        crash_protection_date = pd.to_datetime(df['snapshotTime'].iloc[check_index]).date()
                    elif 'date' in df.columns:
                        crash_protection_date = pd.to_datetime(df['date'].iloc[check_index]).date()
                    else:
                        crash_protection_date = date.today()
                
                config = crash_protection_config or CRASH_PROTECTION_CONFIG
                should_block, block_reason = check_crash_protection(df[:check_index + 1], crash_protection_date, config)
                
                if should_block:
                    result['signal'] = False
                    result['crash_blocked'] = True
                    result['crash_reason'] = block_reason
                    # Note: Logging removed - was too noisy in console. 
                    # Block reason is still captured in result['crash_reason'] for reporting.
                    
            except ImportError:
                print("[UnifiedSignal] Crash protection module not available", file=sys.stderr)
            except Exception as e:
                print(f"[UnifiedSignal] Crash protection check failed: {e}", file=sys.stderr)
                # Fail-open: don't block signal if crash protection fails
        
        return result
        
    except Exception as e:
        import traceback
        result['error'] = str(e)
        print(f"[UnifiedSignal] Error evaluating indicator '{indicator_name}': {e}", file=sys.stderr)
        print(f"[UnifiedSignal] Params passed: {indicator_params}", file=sys.stderr)
        print(f"[UnifiedSignal] Filtered params: {filtered_params if 'filtered_params' in dir() else 'N/A'}", file=sys.stderr)
        print(f"[UnifiedSignal] Traceback:\n{traceback.format_exc()}", file=sys.stderr)
        return result


def check_data_sufficiency(
    df: pd.DataFrame,
    indicator_params: Dict[str, Any]
) -> Tuple[int, int, Optional[str]]:
    """
    Check if DataFrame has enough candles for accurate indicator calculation.
    
    Args:
        df: DataFrame with candle data
        indicator_params: Indicator parameters (to find period requirements)
        
    Returns:
        Tuple of (candles_available, candles_needed, warning_message or None)
    """
    # Period parameters that indicate data requirements
    period_params = [
        'period', 'slow_period', 'long_period', 'lookback', 'lookback_period',
        'rank_period', 'atr_period', 'bb_period', 'kc_period', 'dc_period', 'window'
    ]
    
    max_period = 14  # Default minimum
    
    # Handle None or empty indicator_params
    if indicator_params:
        for param_name in period_params:
            if param_name in indicator_params:
                val = indicator_params[param_name]
                if isinstance(val, (int, float)) and val > max_period:
                    max_period = int(val)
    
    # Need at least 2x the period for warmup
    candles_needed = max_period * 2
    candles_available = len(df)
    
    warning = None
    if candles_available < candles_needed:
        warning = f"Only {candles_available} candles available, indicator needs {candles_needed} for accurate calculation (period={max_period})"
    
    return candles_available, candles_needed, warning


# Convenience function for backward compatibility
def get_signal_for_last_candle(
    df: pd.DataFrame,
    indicator_name: str,
    indicator_params: Dict[str, Any],
    crash_protection_enabled: bool = False,
) -> Dict[str, Any]:
    """
    Convenience wrapper to get signal for the last candle.
    
    This is the most common use case for brain preview / live trading.
    
    Args:
        df: DataFrame with OHLCV data
        indicator_name: Name of indicator
        indicator_params: Raw parameters from database
        crash_protection_enabled: Whether to check crash protection
        
    Returns:
        Same as evaluate_indicator_at_index()
    """
    return evaluate_indicator_at_index(
        df=df,
        indicator_name=indicator_name,
        indicator_params=indicator_params,
        check_index=-1,
        crash_protection_enabled=crash_protection_enabled,
    )

