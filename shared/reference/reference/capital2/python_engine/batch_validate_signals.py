#!/usr/bin/env python3
"""
Batch Validate Signals - Calculate signals for MULTIPLE DNA strands in ONE call

This is an optimized version for trade validation that:
1. Loads candle data ONCE per epic (not per DNA)
2. Evaluates ALL DNA strands in a single Python process
3. Returns all results in one JSON response

Input: JSON config with array of DNA configs
Output: RESULT:{array of signal results}

This is 3-10x faster than calling get_current_signal.py N times.

UPDATED: Now uses unified candle_data_loader for consistent fake 5min candle handling.
This ensures validation uses SAME 4th 1m candle logic as backtest runner.
"""

import sys
import json
from mysql_candle_loader import load_epic_data as mysql_load_epic_data
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

# Import unified signal evaluation (SINGLE SOURCE OF TRUTH)
from unified_signal import (
    evaluate_indicator_at_index,
    check_data_sufficiency,
)

# UNIFIED CANDLE LOADING - ensures consistency with batch_runner.py and strategy_executor.py
from candle_data_loader import load_candles_with_fake_5min


# Cache for loaded candle data (epic + timing_mode -> DataFrame)
_candle_cache: Dict[str, pd.DataFrame] = {}


def load_candles_cached(
    epic: str,
    start_date: str,
    end_date: str,
    data_source: str = 'capital',
    timeframe: str = '5m',
    timing_mode: str = 'Fake5min_4thCandle',  # NEW: Support timing mode
) -> pd.DataFrame:
    """
    Load candles with caching - if same epic/dates/timing already loaded, reuse it.
    
    UPDATED: Now uses unified candle loader for consistent fake 5min candle handling.
    For Fake5min_4thCandle mode, this loads 1m candles and builds fake_5min_close
    from the 4th 1-minute candle - SAME logic as backtest runner.
    """
    cache_key = f"{epic}_{start_date}_{end_date}_{data_source}_{timeframe}_{timing_mode}"
    
    if cache_key in _candle_cache:
        print(f"[BatchValidate] Using cached data for {epic} ({timing_mode})", file=sys.stderr)
        return _candle_cache[cache_key].copy()
    
    # Use unified candle loader (handles fake 5min candle building)
    print(f"[BatchValidate] Loading {epic} data (timing_mode={timing_mode})", file=sys.stderr)
    
    df = load_candles_with_fake_5min(
        epic=epic,
        start_date=start_date,
        end_date=end_date,
        timing_mode=timing_mode,
        data_source=data_source,
        use_cache=False,  # We manage cache here
        verbose=True,
    )
    
    if df.empty:
        raise ValueError(f"No data found for {epic}")
    
    # Normalize column names if needed
    if 'timestamp' in df.columns and 'snapshotTime' not in df.columns:
        df['snapshotTime'] = pd.to_datetime(df['timestamp'])
    
    if 'timestamp' not in df.columns and 'snapshotTime' in df.columns:
        df['timestamp'] = df['snapshotTime']
    
    df = df.sort_values('snapshotTime').reset_index(drop=True)
    
    # Log fake candle info
    if 'fake_5min_close' in df.columns:
        fake_count = df['fake_5min_close'].notna().sum()
        print(f"[BatchValidate] Loaded {len(df)} candles with {fake_count} fake_5min_close values", file=sys.stderr)
    else:
        print(f"[BatchValidate] Loaded {len(df)} candles (no fake_5min_close - timing mode doesn't require it)", file=sys.stderr)
    
    # Cache it
    _candle_cache[cache_key] = df
    
    return df.copy()


def evaluate_single_dna(
    dna_config: Dict[str, Any],
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """
    Evaluate a single DNA strand and return the result.
    Uses cached candle data for efficiency.
    
    UPDATED: Now extracts timing_mode from DNA config to ensure consistent
    fake 5min candle handling with backtest runner.
    """
    try:
        epic = dna_config.get('epic', 'SOXL')
        indicator_name = dna_config.get('indicatorName') or dna_config.get('indicator_name', 'unknown')
        indicator_params = dna_config.get('indicatorParams') or dna_config.get('indicator_params', {})
        timeframe = dna_config.get('timeframe', '5m')
        data_source = dna_config.get('dataSource') or dna_config.get('data_source', 'capital')
        crash_protection = dna_config.get('crashProtectionEnabled', False)
        
        # UPDATED: Extract timing mode from DNA config for consistent fake candle handling
        timing_config = dna_config.get('timingConfig') or dna_config.get('timing_config', {})
        timing_mode = timing_config.get('mode', 'Fake5min_4thCandle')  # Default to 4th candle mode
        
        # Load candles (cached) - now with timing mode support
        df = load_candles_cached(epic, start_date, end_date, data_source, timeframe, timing_mode)
        
        # Evaluate indicator at last candle
        eval_result = evaluate_indicator_at_index(
            df=df,
            indicator_name=indicator_name,
            indicator_params=indicator_params,
            check_index=-1,  # Last candle
            crash_protection_enabled=crash_protection,
        )
        
        if eval_result.get('error'):
            return {
                'success': False,
                'error': eval_result['error'],
                'indicatorName': indicator_name,
                'epic': epic,
                'signal': 'HOLD',
                'indicatorValue': None,
            }
        
        return {
            'success': True,
            'indicatorName': indicator_name,
            'epic': epic,
            'timeframe': timeframe,
            'signal': 'BUY' if eval_result['signal'] else 'HOLD',
            'indicatorValue': eval_result['indicator_value'],
            'timestamp': eval_result['timestamp'],
            'closePrice': eval_result['close_price'],
            'crashBlocked': eval_result['crash_blocked'],
            'crashReason': eval_result.get('crash_reason'),
            'candlesLoaded': len(df),
        }
        
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'indicatorName': dna_config.get('indicatorName') or dna_config.get('indicator_name', 'unknown'),
            'epic': dna_config.get('epic', 'unknown'),
            'signal': 'HOLD',
            'indicatorValue': None,
        }


def batch_evaluate(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate ALL DNA strands in a single call.
    
    Input config:
    {
        "dna_configs": [
            {"indicatorName": "rsi_oversold", "indicatorParams": {...}, "epic": "SOXL", ...},
            {"indicatorName": "macd_crossover", "indicatorParams": {...}, "epic": "SOXL", ...},
            ...
        ],
        "start_date": "2024-10-01",
        "end_date": "2024-12-01",
    }
    
    Returns:
    {
        "success": true,
        "results": [
            {"success": true, "indicatorName": "rsi_oversold", "signal": "BUY", ...},
            {"success": true, "indicatorName": "macd_crossover", "signal": "HOLD", ...},
        ],
        "candlesCached": ["SOXL_2024-10-01_2024-12-01_capital_5m"],
        "totalDna": 4,
        "dnaWithBuy": 2,
    }
    """
    dna_configs = config.get('dna_configs', [])
    start_date = config.get('start_date')
    end_date = config.get('end_date')
    
    if not dna_configs:
        return {
            'success': False,
            'error': 'No DNA configs provided',
            'results': [],
        }
    
    # Calculate dates if not provided
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')
    
    if not start_date:
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        start_dt = end_dt - timedelta(days=60)
        start_date = start_dt.strftime('%Y-%m-%d')
    
    print(f"[BatchValidate] Evaluating {len(dna_configs)} DNA strands from {start_date} to {end_date}", file=sys.stderr)
    
    # Clear cache for fresh run
    _candle_cache.clear()
    
    # Evaluate each DNA
    results = []
    for i, dna in enumerate(dna_configs):
        result = evaluate_single_dna(dna, start_date, end_date)
        result['index'] = i  # Preserve order
        results.append(result)
        
        signal_str = result.get('signal', 'ERROR')
        indicator = result.get('indicatorName', 'unknown')
        print(f"[BatchValidate]   DNA {i}: {indicator} -> {signal_str}", file=sys.stderr)
    
    # Summary
    buy_count = sum(1 for r in results if r.get('signal') == 'BUY')
    
    return {
        'success': True,
        'results': results,
        'candlesCached': list(_candle_cache.keys()),
        'totalDna': len(dna_configs),
        'dnaWithBuy': buy_count,
    }


def main():
    """
    Main entry point.
    
    Expects JSON config as command line argument with array of DNA configs.
    """
    if len(sys.argv) < 2:
        print("ERROR: Missing config JSON argument", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = json.loads(sys.argv[1])
        result = batch_evaluate(config)
        
        # Output result as JSON (same format as single signal script)
        print(f"RESULT:{json.dumps(result)}")
        sys.exit(0)
        
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        
        # Return error in expected format
        error_result = {
            'success': False,
            'error': str(e),
            'results': [],
        }
        print(f"RESULT:{json.dumps(error_result)}")
        sys.exit(1)


if __name__ == '__main__':
    main()






