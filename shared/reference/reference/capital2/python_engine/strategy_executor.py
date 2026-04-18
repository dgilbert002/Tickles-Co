#!/usr/bin/env python3
"""
Strategy Executor - Window-Based Execution with Multi-Epic DNA Strands

BACKUP LOCATION: strategy_executor.old.py (non-window version)

MAJOR CHANGES FROM OLD VERSION:
1. Multi-epic DNA strands: Each DNA strand has its own epic
2. Window-based execution: DNA strands grouped by window (based on dnaStrandIds)
3. Intra-day carryover: W1→W2 positions carry ONLY if same day
4. Per-window conflict resolution: Each window resolves conflicts independently
5. Daily compounding: Balance compounds daily with monthly topups

WINDOW SYSTEM OVERVIEW:
- Windows group DNA strands by market close time (W1=16:00, W2=20:00, etc.)
- Each window has dnaStrandIds[] mapping to specific DNA strands
- Mode 'carry_over': Each window gets base allocation
- If window decision = HOLD → funds carry to next window (same day only)
- Last window CANNOT carry over (market closes, new day starts)

DNA STRAND FORMAT (from TypeScript):
{
  "id": "dna_1763728301917_3mhmqlvho",
  "epic": "SOXL",
  "indicatorName": "rsi_oversold",
  "indicatorParams": {"rsi_oversold": 27, "rsi_period": 16},
  "leverage": 10,
  "stopLoss": 8,
  "timeframe": "5m",
  "timingConfig": {"mode": "ManusTime"}
}

WINDOW CONFIG FORMAT:
{
  "windows": [
    {
      "id": "window_1",
      "closeTime": "16:00:00",
      "allocationPct": 50,
      "carryOver": true,
      "conflictResolutionMetric": "sharpeRatio",
      "dnaStrandIds": ["dna_xxx", "dna_yyy"]
    }
  ],
  "totalAccountBalancePct": 99
}
"""

import sys
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from backtest_runner import run_single_backtest, load_epic_data
from collections import defaultdict
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from file_logger import create_strategy_logger, StrategyTestLogger

# UNIFIED CANDLE LOADING - ensures consistency with batch_runner.py
from candle_data_loader import load_candles_with_fake_5min


def execute_strategy(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a strategy with DNA strands and window-based execution
    
    NEW CONFIG FORMAT (DNA-based):
        config: {
            start_date: "2024-01-01",
            end_date: "2025-10-20",
            initial_balance: 500,
            monthly_topup: 100,
            dna_strands: [
                {
                    id: "dna_xxx",
                    epic: "SOXL",
                    indicatorName: "rsi_oversold",
                    indicatorParams: {...},
                    leverage: 10,
                    stopLoss: 8,
                    timeframe: "5m",
                    timingConfig: {...}
                }
            ],
            window_config: {
                windows: [
                    {
                        id: "window_1",
                        closeTime: "16:00:00",
                        allocationPct: 50,
                        carryOver: true,
                        conflictResolutionMetric: "sharpeRatio",
                        dnaStrandIds: ["dna_xxx"]
                    }
                ],
                totalAccountBalancePct: 99
            },
            conflict_resolution: {
                mode: "first_signal_wins"  // Fallback for non-window execution
            }
        }
    
    OLD CONFIG FORMAT (for backward compatibility):
        config: {
            epic: "SOXL",
            start_date: "2024-01-01",
            end_date: "2025-10-20",
            initial_balance: 500,
            monthly_topup: 100,
            tests: [{...}],
            conflict_resolution: {...}
        }
    
    Returns:
        Combined strategy results with window-level details
    """
    start_date = config['start_date']
    end_date = config['end_date']
    initial_balance = config['initial_balance']
    monthly_topup = config['monthly_topup']
    calculation_mode = config.get('calculation_mode', 'standard')
    
    # NEW: Capital events for apples-to-apples comparison
    capital_events = config.get('capital_events', [])
    if capital_events:
        print(f"[Strategy Executor] Received {len(capital_events)} capital events from TypeScript", file=sys.stderr)
    
    # NEW: Check if DNA strands are provided (new format)
    dna_strands = config.get('dna_strands')
    window_config = config.get('window_config')
    
    if dna_strands:
        # NEW FORMAT: DNA strands with window config
        return execute_with_dna_strands(
            dna_strands=dna_strands,
            window_config=window_config,
            initial_balance=initial_balance,
            monthly_topup=monthly_topup,
            start_date=start_date,
            end_date=end_date,
            calculation_mode=calculation_mode,
            capital_events=capital_events,
        )
    else:
        # OLD FORMAT: Single epic with tests (backward compatibility)
        epic = config['epic']
        tests = config['tests']
        conflict_mode = config['conflict_resolution']['mode']
        data_source = config.get('dataSource', 'capital')  # Default to capital.com
        
        # UPDATED: Use unified candle loader for consistent fake 5min candle handling
        # Get timing mode from first test (assume all tests use same mode for this epic)
        first_test_timing = tests[0].get('timingConfig', {}).get('mode', 'Fake5min_3rdCandle_API') if tests else 'Fake5min_3rdCandle_API'
        
        df = load_candles_with_fake_5min(
            epic=epic,
            start_date=start_date,
            end_date=end_date,
            timing_mode=first_test_timing,
            data_source=data_source,
            use_cache=True,
            verbose=True,
        )
        
        # Run each test in PARALLEL
        def run_test_backtest(test):
            """Run a single test backtest - called in parallel"""
            timing_config = test.get('timingConfig', {'mode': 'Fake5min_3rdCandle_API'})
            timeframe = test.get('timeframe', '5m')
            crash_protection = test.get('crashProtectionEnabled', False)
            
            result = run_single_backtest(
                df=df.copy(),  # Copy DataFrame for thread safety
                epic=epic,
                indicator_name=test['indicatorName'],
                indicator_params=test.get('indicatorParams', {}),
                leverage=test['leverage'],
                stop_loss_pct=test.get('stopLoss', 2.0),
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                investment_pct=99.0,
                direction='long',
                timing_config=timing_config,
                calculation_mode=calculation_mode,
                timeframe=timeframe,  # Match original backtest
                crash_protection_enabled=crash_protection,  # Match original backtest
                # HMH (Hold Means Hold) parameters
                hmh_enabled=test.get('hmhEnabled', False),
                hmh_stop_loss_offset=test.get('hmhStopLossOffset', 0),
            )
            
            return {
                'test': test,
                'result': result,
                'allocation': test.get('allocationPercent', 100.0 / len(tests)),
            }
        
        # Run ALL test backtests in PARALLEL
        test_results = []
        max_workers = min(len(tests), 8)  # Cap at 8 parallel workers
        
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_test = {executor.submit(run_test_backtest, test): test for test in tests}
            
            for future in as_completed(future_to_test):
                try:
                    result = future.result()
                    test_results.append(result)
                except Exception as e:
                    print(f"[Strategy Executor] Test failed: {e}", file=sys.stderr)
        
        # Combine results
        combined_result = combine_test_results(
            test_results=test_results,
            conflict_mode=conflict_mode,
            initial_balance=initial_balance,
            monthly_topup=monthly_topup,
            epic=epic,
            start_date=start_date,
            end_date=end_date,
        )
        
        return {
            **combined_result,
            'individual_test_results': test_results
        }


def execute_with_dna_strands(
    dna_strands: List[Dict[str, Any]],
    window_config: Optional[Dict[str, Any]],
    initial_balance: float,
    monthly_topup: float,
    start_date: str,
    end_date: str,
    calculation_mode: str = 'standard',
    capital_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Execute strategy with DNA strands and window-based execution
    
    FLOW:
    1. Run backtest for each DNA strand (each epic)
    2. Map DNA strands to windows based on dnaStrandIds
    3. Group trades by window and day
    4. Execute window-based strategy with carryover
    
    Args:
        dna_strands: List of DNA strand configurations
        window_config: Window configuration with windows array and totalAccountBalancePct
        initial_balance: Starting balance
        monthly_topup: Monthly capital injection
        start_date, end_date: Date range
        calculation_mode: 'standard' or 'numba'
        capital_events: List of real capital events (deposits/withdrawals) from Capital.com
                       Format: [{'date': 'YYYY-MM-DD', 'type': 'deposit'|'withdrawal', 'amount': float}]
        
    Returns:
        Combined strategy results
    """
    from market_info_loader import get_market_info
    
    # Get investment percentage from strategy config (user's "Maximum Account Allocation")
    # This should match what user enters, e.g., 99% to match a 99% backtest
    investment_pct = 99.0  # Default
    if window_config:
        investment_pct = window_config.get('totalAccountBalancePct', 99.0)
    
    print(f"[Strategy Executor] Using investment_pct={investment_pct}% from strategy config", file=sys.stderr)
    
    # Step 1: Run backtest for each DNA strand (PARALLEL)
    print(f"[Strategy Executor] Running {len(dna_strands)} DNA strands (PARALLEL)...", file=sys.stderr)
    
    # Create file logger for this strategy test
    strategy_name = window_config.get('strategyName', f'Strategy_{len(dna_strands)}DNA') if window_config else 'Unknown'
    strategy_id = window_config.get('strategyId', 0) if window_config else 0
    file_logger = create_strategy_logger(strategy_id, strategy_name, len(dna_strands), 'parallel')
    
    file_logger.config_loaded({
        'start_date': start_date,
        'end_date': end_date,
        'initial_balance': initial_balance,
        'monthly_topup': monthly_topup,
        'investment_pct': investment_pct,
        'calculation_mode': calculation_mode,
    })
    
    # Log detailed DNA strand configurations (including HMH)
    file_logger.section('DNA STRAND CONFIGURATIONS')
    for i, dna in enumerate(dna_strands):
        file_logger.dna_strand_details(dna, i)
    
    # Log window configuration if provided
    if window_config and window_config.get('windows'):
        file_logger.window_config_loaded(
            window_config['windows'],
            window_config.get('totalAccountBalancePct', 99.0)
        )
    
    def run_dna_backtest(dna):
        """Run a single DNA backtest - called in parallel"""
        epic = dna['epic']
        dna_id = dna['id']
        indicator_name = dna['indicatorName']
        
        # Get DNA's configuration (MUST match original backtest for consistent signals)
        data_source = dna.get('dataSource', 'capital')  # Default to capital.com
        timing_config = dna.get('timingConfig', {'mode': 'Fake5min_3rdCandle_API'})
        timeframe = dna.get('timeframe', '5m')  # Match original backtest timeframe
        crash_protection = dna.get('crashProtectionEnabled', False)  # Match original backtest
        timing_mode = timing_config.get('mode', 'default')
        
        # HMH parameters
        hmh_enabled = dna.get('hmhEnabled', False)
        hmh_stop_loss_offset = dna.get('hmhStopLossOffset', 0)
        
        # Log DNA start
        file_logger.dna_backtest_start(dna_id, epic, indicator_name, timing_mode, data_source)
        
        print(f"[Strategy Executor] Running DNA {dna_id} for epic {epic} "
              f"(timing={timing_mode}, source={data_source}, "
              f"timeframe={timeframe}, crashProtection={crash_protection}, "
              f"HMH={hmh_enabled}, HMH_offset={hmh_stop_loss_offset})...", file=sys.stderr)
        
        # Load candles with detailed logging
        try:
            file_logger.log(f'  Loading candles for {epic}...')
            df = load_candles_with_fake_5min(
                epic=epic,
                start_date=start_date,
                end_date=end_date,
                timing_mode=timing_mode,
                data_source=data_source,
                use_cache=True,  # Cache for parallel execution efficiency
                verbose=False,   # Reduce noise in parallel execution
            )
            
            # Log candle loading details
            if df is not None and len(df) > 0:
                date_range = (str(df.index.min())[:10], str(df.index.max())[:10])
                file_logger.candles_loaded(dna_id, epic, len(df), date_range, timing_mode)
            else:
                file_logger.warning(f'No candles loaded for {epic} ({dna_id})')
                return {
                    'dna_id': dna_id,
                    'dna': dna,
                    'result': {'trades': [], 'totalTrades': 0, 'finalBalance': initial_balance, 'sharpeRatio': 0, 'totalReturn': 0},
                }
        except Exception as e:
            file_logger.candles_error(dna_id, epic, e)
            raise
        
        # Log backtest parameters before execution
        backtest_params = {
            'epic': epic,
            'indicator_name': indicator_name,
            'indicator_params': dna.get('indicatorParams', {}),
            'leverage': dna.get('leverage', 1),
            'stop_loss_pct': dna.get('stopLoss', 2.0),
            'initial_balance': initial_balance,
            'monthly_topup': monthly_topup,
            'investment_pct': investment_pct,
            'direction': dna.get('direction', 'long'),
            'timing_mode': timing_mode,
            'timeframe': timeframe,
            'crash_protection': crash_protection,
            'hmh_enabled': hmh_enabled,
            'hmh_stop_loss_offset': hmh_stop_loss_offset,
        }
        file_logger.backtest_params(dna_id, backtest_params)
        
        # Run backtest for this DNA strand - IDENTICAL to backtest runner
        try:
            result = run_single_backtest(
                df=df,
                epic=epic,
                indicator_name=indicator_name,
                indicator_params=dna.get('indicatorParams', {}),
                leverage=dna.get('leverage', 1),
                stop_loss_pct=dna.get('stopLoss', 2.0),
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                investment_pct=investment_pct,  # From strategy's totalAccountBalancePct
                direction=dna.get('direction', 'long'),
                timing_config=timing_config,
                calculation_mode=calculation_mode,
                timeframe=timeframe,  # Match original backtest
                crash_protection_enabled=crash_protection,  # Match original backtest
                # HMH (Hold Means Hold) parameters
                hmh_enabled=hmh_enabled,
                hmh_stop_loss_offset=hmh_stop_loss_offset,
            )
        except Exception as e:
            file_logger.dna_backtest_error(dna_id, epic, indicator_name, e)
            raise
        
        # Log DNA completion with HMH data if available
        hmh_data = None
        if hmh_enabled:
            hmh_data = {
                'hmh_enabled': hmh_enabled,
                'hmh_triggers': result.get('hmh_triggers', 0),
                'hmh_exits': result.get('hmh_exits', 0),
            }
        
        file_logger.dna_backtest_complete(
            dna_id,
            result.get('totalTrades', 0),
            result.get('finalBalance', 0),
            result.get('sharpeRatio', 0),
            result.get('totalReturn', 0),
            hmh_data=hmh_data
        )
        
        return {
            'dna_id': dna_id,
            'dna': dna,
            'result': result,
        }
    
    # Run ALL DNA backtests in PARALLEL using ThreadPoolExecutor
    # This drastically reduces total time when testing multiple DNA strands
    dna_results = []
    failed_dna = []
    max_workers = min(len(dna_strands), 8)  # Cap at 8 parallel workers
    
    file_logger.section(f'PARALLEL EXECUTION ({max_workers} workers)')
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all DNA backtests
        future_to_dna = {executor.submit(run_dna_backtest, dna): dna for dna in dna_strands}
        
        # Collect results as they complete
        for future in as_completed(future_to_dna):
            dna = future_to_dna[future]
            try:
                result = future.result()
                dna_results.append(result)
                file_logger.success(f"DNA {result['dna_id']} complete: {result['result'].get('totalTrades', 0)} trades")
                print(f"[Strategy Executor] ✓ DNA {result['dna_id']} complete", file=sys.stderr)
            except Exception as e:
                failed_dna.append({'dna_id': dna['id'], 'error': str(e)})
                file_logger.error(f"DNA {dna['id']} failed", e)
                print(f"[Strategy Executor] ✗ DNA {dna['id']} failed: {e}", file=sys.stderr)
    
    # Log summary of DNA execution
    if failed_dna:
        file_logger.warning(f'{len(failed_dna)} DNA strands failed execution')
        for fail in failed_dna:
            file_logger.log(f'  - {fail["dna_id"]}: {fail["error"]}')
    
    file_logger.all_dna_complete(len(dna_results))
    print(f"[Strategy Executor] All {len(dna_results)} DNA backtests complete (parallel)", file=sys.stderr)
    
    # Step 2: If window config provided, execute with windows
    if window_config and window_config.get('windows'):
        print(f"[Strategy Executor] Executing with {len(window_config['windows'])} windows...", file=sys.stderr)
        if capital_events:
            print(f"[Strategy Executor] Using {len(capital_events)} real capital events", file=sys.stderr)
        result = execute_windows_with_dna(
            dna_results=dna_results,
            window_config=window_config,
            initial_balance=initial_balance,
            monthly_topup=monthly_topup,
            start_date=start_date,
            end_date=end_date,
            investment_pct=investment_pct,
            capital_events=capital_events,
        )
        # Log final results
        file_logger.final_results(result)
        file_logger.close()
        return result
    else:
        # No window config: combine all DNA results (simple merge)
        print("[Strategy Executor] No window config, using simple merge...", file=sys.stderr)
        result = merge_dna_results_simple(
            dna_results=dna_results,
            initial_balance=initial_balance,
            monthly_topup=monthly_topup,
            start_date=start_date,
            end_date=end_date,
            capital_events=capital_events,
        )
        # Log final results
        file_logger.final_results(result)
        file_logger.close()
        return result


def execute_windows_with_dna(
    dna_results: List[Dict[str, Any]],
    window_config: Dict[str, Any],
    initial_balance: float,
    monthly_topup: float,
    start_date: str,
    end_date: str,
    investment_pct: float = 99.0,
    capital_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Execute window-based strategy with DNA strand results
    
    WINDOW EXECUTION LOGIC:
    1. Map DNA strands to windows based on dnaStrandIds
    2. Group trades by window and day
    3. For each day:
       a. Apply capital events (deposits/withdrawals) at start of day
       b. Reset carried-over funds to 0
       c. For each window in that day:
          - Calculate available funds = base allocation + carried over
          - Resolve conflicts within window (pick best DNA strand)
          - Execute trade or HOLD
          - If HOLD, carry funds to next window (same day only)
    4. Track balance across days with monthly topups
    
    Args:
        capital_events: List of real capital events from Capital.com
                       Format: [{'date': 'YYYY-MM-DD', 'type': 'deposit'|'withdrawal', 'amount': float}]
    """
    from market_info_loader import get_market_info, get_risk_settings
    from file_logger import get_current_logger
    
    # Get file logger for detailed logging
    file_logger = get_current_logger()
    
    file_logger.section('WINDOW EXECUTION')
    file_logger.log(f'Starting window-based execution with {len(dna_results)} DNA results')
    
    # Load risk management settings for account-level ML tracking
    risk_settings = get_risk_settings()
    margin_closeout_level = risk_settings['margin_closeout_level']
    liquidation_slippage_pct = risk_settings['liquidation_slippage_pct']
    
    file_logger.log(f'Risk Settings: margin_closeout={margin_closeout_level}%, liquidation_slippage={liquidation_slippage_pct}%')
    
    windows = window_config['windows']
    
    # Create mapping: dna_id → window
    dna_to_window = {}
    for window in windows:
        for dna_id in window.get('dnaStrandIds', []):
            dna_to_window[dna_id] = window
    
    file_logger.log(f'DNA to Window mapping: {len(dna_to_window)} DNA strands mapped to {len(windows)} windows')
    
    # Group trades by window and day
    # Structure: {date: {window_id: [trades]}}
    trades_by_day_window = defaultdict(lambda: defaultdict(list))
    total_trades_to_process = 0
    
    for dna_result in dna_results:
        dna_id = dna_result['dna_id']
        window = dna_to_window.get(dna_id)
        
        if not window:
            file_logger.warning(f"DNA {dna_id} not mapped to any window, skipping")
            print(f"[Strategy Executor] WARNING: DNA {dna_id} not mapped to any window, skipping", file=sys.stderr)
            continue
        
        window_id = window['id']
        epic = dna_result['dna']['epic']
        
        # Get market info for this epic (same as backtest_runner.py)
        market_info = get_market_info(epic)
        if not market_info:
            file_logger.warning(f"No market info for {epic}, using defaults")
            market_info = {
                'spread_percent': 0.000190,
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
        
        # Add trades to window
        dna_trade_count = 0
        for trade in dna_result['result']['trades']:
            entry_date = datetime.fromisoformat(trade['entry_time']).date()
            
            trades_by_day_window[entry_date][window_id].append({
                'dna_id': dna_id,
                'dna_name': dna_result['dna']['indicatorName'],
                'epic': epic,
                'trade': trade,
                'window': window,
                'market_info': market_info,
                'sharpe_ratio': dna_result['result']['sharpeRatio'],
                'total_return': dna_result['result']['totalReturn'],
            })
            dna_trade_count += 1
            total_trades_to_process += 1
        
        file_logger.log(f'  DNA {dna_id} ({epic}): {dna_trade_count} trades → Window {window_id}')
    
    file_logger.log(f'Total trades to process: {total_trades_to_process} across {len(trades_by_day_window)} trading days')
    
    # Pre-calculate monthly topups
    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)
    date_range = pd.date_range(start=start_dt, end=end_dt, freq='MS')
    all_months = [dt.strftime('%Y-%m') for dt in date_range]
    
    # Execute strategy across days and windows
    balance = initial_balance
    trades_executed = []
    last_topup_month = None
    
    # NEW: Track position values per window (for rebalancing)
    # Structure: {window_id: {'value': float, 'contracts': float, 'entry_price': float, 'epic': str, 'margin_factor': float}}
    window_positions = {}
    
    # Account-level ML tracking
    min_account_margin_level = float('inf')  # Track lowest account-level ML
    account_liquidation_count = 0  # Number of forced account liquidations
    total_account_liquidation_loss = 0.0  # Total extra loss from liquidations
    
    # NEW: Index capital events by date for O(1) lookup
    capital_events_by_date = {}
    if capital_events:
        for event in capital_events:
            event_date = event.get('date', '')
            if event_date:
                if event_date not in capital_events_by_date:
                    capital_events_by_date[event_date] = []
                capital_events_by_date[event_date].append(event)
        file_logger.log(f'Capital events indexed: {len(capital_events_by_date)} dates with events')
        print(f"[Strategy Executor] Capital events indexed: {len(capital_events_by_date)} dates with events", file=sys.stderr)
    
    # Track total capital injected/withdrawn for reporting
    total_deposits = 0.0
    total_withdrawals = 0.0
    
    # Sort days chronologically
    sorted_days = sorted(trades_by_day_window.keys())
    
    file_logger.subsection(f'DAILY EXECUTION ({len(sorted_days)} trading days)')
    print(f"[Strategy Executor] Executing {len(sorted_days)} trading days...", file=sys.stderr)
    
    # DEBUG: Log first few days to verify structure
    if sorted_days:
        file_logger.log(f'First trading day: {sorted_days[0]} (type: {type(sorted_days[0]).__name__})')
        first_day_windows = trades_by_day_window.get(sorted_days[0], {})
        file_logger.log(f'Windows for first day: {list(first_day_windows.keys())}')
        for wid, trades in first_day_windows.items():
            file_logger.log(f'  Window {wid}: {len(trades)} trades')

    for day in sorted_days:
        # Reset carryover at start of each day
        carried_over_pct = 0.0
        day_str = day.strftime('%Y-%m-%d')
        
        # NEW: Apply capital events at START of day (before any trading)
        if day_str in capital_events_by_date:
            for event in capital_events_by_date[day_str]:
                event_type = event.get('type', '')
                event_amount = event.get('amount', 0)
                
                if event_type == 'deposit' and event_amount > 0:
                    balance += event_amount
                    total_deposits += event_amount
                    print(f"[Strategy Executor] Day {day}: DEPOSIT +${event_amount:.2f}, balance now ${balance:.2f}", file=sys.stderr)
                elif event_type == 'withdrawal' and event_amount > 0:
                    # Ensure we don't withdraw more than available
                    actual_withdrawal = min(event_amount, balance * 0.9)  # Leave at least 10%
                    balance -= actual_withdrawal
                    total_withdrawals += actual_withdrawal
                    print(f"[Strategy Executor] Day {day}: WITHDRAWAL -${actual_withdrawal:.2f}, balance now ${balance:.2f}", file=sys.stderr)
        
        # CRITICAL: Add monthly topup at START of day (same as backtest_runner.py)
        # This must happen BEFORE calculating available_funds for any trades
        day_dt = datetime.combine(day, datetime.min.time())
        current_month = day_dt.strftime('%Y-%m')
        if last_topup_month is None or current_month > last_topup_month:
            balance += monthly_topup
            last_topup_month = current_month
            if monthly_topup > 0:
                print(f"[Strategy Executor] Day {day}: Added monthly topup ${monthly_topup}, balance now ${balance:.2f}", file=sys.stderr)
        
        # IMPORTANT: Iterate ALL windows (not just those with trades) to handle carry over
        # Sort windows by close time
        all_window_ids = sorted(
            [w['id'] for w in windows],
            key=lambda wid: next((w['closeTime'] for w in windows if w['id'] == wid), '00:00:00')
        )
        
        for window_idx, window_id in enumerate(all_window_ids):
            # Get trades for this window (may be empty)
            conflicting_trades = trades_by_day_window[day].get(window_id, [])
            
            # DEBUG: Log first 3 days in detail
            if len(trades_executed) == 0 and window_idx == 0 and day in sorted_days[:3]:
                file_logger.log(f'  Day {day}: window={window_id}, trades_found={len(conflicting_trades)}')
                if conflicting_trades:
                    file_logger.log(f'    First trade: {conflicting_trades[0].get("dna_name", "unknown")} - sharpe={conflicting_trades[0].get("sharpe_ratio", 0):.4f}')
            
            # Get window config
            window = next((w for w in windows if w['id'] == window_id), None)
            if not window:
                file_logger.warning(f'Window {window_id} not found in config, skipping')
                continue
            
            base_allocation_pct = window.get('allocationPct', 100.0)
            can_carry_over = window.get('carryOver', True)
            conflict_metric = window.get('conflictResolutionMetric', 'sharpeRatio')
            
            # Calculate current allocation across all windows (for rebalancing)
            total_position_value = sum(pos['value'] for pos in window_positions.values())
            total_position_pct = (total_position_value / balance * 100) if balance > 0 else 0
            
            # Calculate available funds for this window
            # Start with base allocation + carryover
            available_pct = min(base_allocation_pct + carried_over_pct, 99.0)
            
            # NEW: Check if rebalancing is needed
            # If another window has excess allocation, we can "free" some funds
            rebalance_freed_pct = 0.0
            rebalance_events = []
            
            if conflicting_trades and available_pct < base_allocation_pct:
                # This window has a BUY signal but not enough allocation
                # Check if we can rebalance from other windows
                for other_window_id, pos_info in window_positions.items():
                    if other_window_id == window_id:
                        continue
                    
                    other_window = next((w for w in windows if w['id'] == other_window_id), None)
                    if not other_window:
                        continue
                    
                    other_target_pct = other_window.get('allocationPct', 0)
                    other_current_pct = (pos_info['value'] / balance * 100) if balance > 0 else 0
                    other_excess_pct = other_current_pct - other_target_pct
                    
                    if other_excess_pct > 1.0:  # Only rebalance if excess > 1%
                        # Calculate how much to free
                        needed_pct = base_allocation_pct - available_pct
                        free_pct = min(other_excess_pct, needed_pct)
                        free_value = (free_pct / 100) * balance
                        
                        # "Close" part of the other window's position
                        # In backtest, we just reduce the position value
                        old_value = pos_info['value']
                        new_value = old_value - free_value
                        
                        if new_value > 0:
                            window_positions[other_window_id]['value'] = new_value
                            window_positions[other_window_id]['contracts'] *= (new_value / old_value)
                        else:
                            del window_positions[other_window_id]
                        
                        rebalance_freed_pct += free_pct
                        rebalance_events.append({
                            'from_window': other_window_id,
                            'freed_pct': free_pct,
                            'freed_value': free_value,
                        })
                        
                        print(f"[Strategy Executor] Day {day}: Rebalanced {free_pct:.1f}% from {other_window_id} to {window_id}", file=sys.stderr)
                        
                        if rebalance_freed_pct >= needed_pct:
                            break
            
            # Update available with rebalanced funds
            # Cap at totalAccountBalancePct (e.g., 99%) from strategy config
            available_pct = min(available_pct + rebalance_freed_pct, investment_pct)
            available_funds = balance * (available_pct / 100.0)
            
            # Check if we have any trades for this window
            if not conflicting_trades:
                # HOLD: No trades for this window, carry over funds to next window (same day only)
                if can_carry_over and window_idx < len(all_window_ids) - 1:
                    carried_over_pct += base_allocation_pct
                    print(f"[Strategy Executor] Day {day}, Window {window_id}: HOLD, carrying over {base_allocation_pct}%", file=sys.stderr)
                continue
            
            # Apply conflict resolution to pick ONE trade
            if conflict_metric == 'sharpeRatio':
                selected_trade_info = max(conflicting_trades, key=lambda x: x['sharpe_ratio'])
            elif conflict_metric == 'profitability':
                selected_trade_info = max(conflicting_trades, key=lambda x: x['total_return'])
            else:
                # Default: first signal wins
                selected_trade_info = conflicting_trades[0]
            
            trade = selected_trade_info['trade']
            market_info = selected_trade_info['market_info']
            
            # DEBUG: Log selected trade details for first few trades
            if len(trades_executed) < 3:
                file_logger.log(f'  Selected trade for day {day}: {selected_trade_info.get("dna_name")} on {selected_trade_info.get("epic")}')
                file_logger.log(f'    Entry: {trade.get("entry_time")}, Exit: {trade.get("exit_time")}')
            
            try:
                entry_date_dt = datetime.fromisoformat(trade['entry_time'])
                exit_date_dt = datetime.fromisoformat(trade['exit_time'])
            except Exception as e:
                file_logger.error(f'Failed to parse trade dates: entry={trade.get("entry_time")}, exit={trade.get("exit_time")}', e)
                continue
            
            # Monthly topup already added at start of day (see above)
            # Do NOT add again here - this was causing double topup or wrong timing
            
            # Recalculate P&L based on strategy's actual balance
            # NOTE: available_funds already accounts for window allocation AND is capped at 99%
            # So DO NOT multiply by 0.99 again - that was causing double-application!
            # The totalAccountBalancePct (e.g., 99%) is applied via the available_pct cap
            investment = available_funds
            
            # DEBUG: Log balance/investment for first few trades
            if len(trades_executed) < 3:
                file_logger.log(f'    Balance: ${balance:.2f}, Available %: {available_pct:.1f}%, Investment: ${investment:.2f}')
            
            # DEBUG: Check trade data validity
            if len(trades_executed) < 5:
                file_logger.log(f'    Extracting trade data...')
            
            leverage = trade.get('leverage')
            entry_price = trade.get('entry_price')
            exit_price = trade.get('exit_price')
            direction = trade.get('direction')
            
            # DEBUG: Log extracted values
            if len(trades_executed) < 5:
                file_logger.log(f'    leverage={leverage}, entry=${entry_price}, exit=${exit_price}, dir={direction}')
            
            # Validate required fields
            if leverage is None or entry_price is None or exit_price is None:
                file_logger.error(f'Missing trade data: leverage={leverage}, entry={entry_price}, exit={exit_price}')
                continue
            
            # Calculate position size in contracts
            notional_value = investment * leverage
            contracts = notional_value / (entry_price * market_info['contract_multiplier'])
            
            # Round to min_size_increment (e.g., 0.1 for TECL, 1.0 for SOXL)
            # This ensures position sizes match what Capital.com accepts
            # MUST match backtest_runner.py logic exactly!
            increment = market_info.get('min_size_increment', 1.0)
            if increment > 0:
                contracts = (contracts // increment) * increment
            
            # Enforce min/max contract limits
            contracts = max(
                market_info['min_contract_size'],
                min(contracts, market_info['max_contract_size'])
            )
            
            # Recalculate actual notional
            actual_notional = contracts * entry_price * market_info['contract_multiplier']
            
            # Calculate P&L
            if direction == 'long':
                price_change = exit_price - entry_price
            else:
                price_change = entry_price - exit_price
            
            gross_pnl = contracts * price_change * market_info['contract_multiplier']
            
            # Calculate costs using real market parameters from Capital.com
            # 
            # Per Capital.com official documentation (Dec 2025):
            # - You pay HALF the spread at entry (buy at ASK)
            # - You pay HALF the spread at exit (sell at BID)
            # - Total = 1x spread (NOT 2x!)
            # 
            # Example: 10 shares × $0.13 spread = $1.30 total cost
            # MUST match backtest_runner.py logic exactly!
            # 
            # FIX: Use actual bid/ask spread from candle data when available
            # This matches backtest_runner.py for consistent P&L calculation
            entry_bid = trade.get('entry_bid', 0)
            entry_ask = trade.get('entry_ask', 0)
            if entry_bid > 0 and entry_ask > 0:
                # Use actual spread from candle data (more accurate)
                actual_spread = entry_ask - entry_bid
                spread_cost = contracts * actual_spread * market_info['contract_multiplier']
            else:
                # Fallback to database percentage
                spread_cost = actual_notional * market_info['spread_percent']
            
            overnight_cost = 0
            hmh_is_continuation = trade.get('hmh_is_continuation', False)
            if exit_date_dt.date() > entry_date_dt.date():
                # FIX: For HMH continuation trades, use actual days held (from backtest)
                # For normal trades, use calendar days
                if hmh_is_continuation:
                    days_for_overnight = trade.get('hmh_days_held', 0)
                else:
                    days_for_overnight = (exit_date_dt.date() - entry_date_dt.date()).days
                
                if days_for_overnight > 0:
                    funding_rate = (
                        market_info['overnight_funding_long_percent']
                        if direction == 'long'
                        else market_info['overnight_funding_short_percent']
                    )
                    overnight_cost = actual_notional * abs(funding_rate) * days_for_overnight
            
            total_costs = spread_cost + overnight_cost
            net_pnl = gross_pnl - total_costs
            
            # DEBUG: Log P&L calculation
            if len(trades_executed) < 5:
                file_logger.log(f'    P&L: gross=${gross_pnl:.2f}, costs=${total_costs:.2f}, net=${net_pnl:.2f}')
            
            # Update balance
            balance += net_pnl
            
            # Get window close time - needed for comparison with actual trades
            window_close_time = window.get('closeTime', '00:00:00')
            # Ensure it has seconds (01:00 -> 01:00:00)
            if window_close_time.count(':') == 1:
                window_close_time = window_close_time + ':00'
            
            # Calculate WINDOW CLOSE DATE (not entry date)
            # For a 01:00 window, entry at 23:15 on Jan 5 means window closes Jan 6 at 01:00
            entry_dt = datetime.fromisoformat(trade['entry_time'])
            close_hour = int(window_close_time.split(':')[0])
            entry_hour = entry_dt.hour
            
            # If window close hour is less than entry hour, window closes on NEXT day
            # e.g., entry at 23:15, window at 01:00 → window closes next day
            window_close_date = day
            if close_hour < entry_hour:
                window_close_date = day + timedelta(days=1)
            
            # DEBUG: Log right before append
            if len(trades_executed) < 5:
                file_logger.log(f'    APPENDING trade: net_pnl=${net_pnl:.2f}, balance_after=${balance:.2f}')
            
            trades_executed.append({
                **trade,
                'date': str(window_close_date),  # Override with window close date
                'window_close_time': window_close_time,  # Add window close time for comparison
                'dna_id': selected_trade_info['dna_id'],
                'dna_name': selected_trade_info['dna_name'],
                'epic': selected_trade_info['epic'],
                'window_id': window_id,
                'window_name': window.get('windowName', f"Window {window_id}"),
                'base_allocation_pct': base_allocation_pct,
                'carried_over_pct': carried_over_pct,
                'available_pct': available_pct,
                'investment': investment,
                'contracts': contracts,
                'notional_value': actual_notional,
                'gross_pnl': gross_pnl,
                'spread_cost': spread_cost,
                'overnight_costs': overnight_cost,
                'costs': total_costs,
                'net_pnl': net_pnl,
                'balance_after': balance,
                'conflict_count': len(conflicting_trades),
                'rebalance_events': rebalance_events if rebalance_events else None,
            })
            
            # DEBUG: Confirm append succeeded
            if len(trades_executed) <= 5:
                file_logger.log(f'    ✅ Trade appended. Total trades now: {len(trades_executed)}')
            
            # NEW: Track this position for potential future rebalancing
            # Position value = investment amount (what we put in)
            # Get margin factor for ML tracking
            margin_factor = market_info.get('margin_factor')
            if margin_factor is None:
                margin_factor = 1.0 / leverage if leverage > 0 else 0.05
            
            window_positions[window_id] = {
                'value': investment,
                'contracts': contracts,
                'entry_price': entry_price,
                'epic': selected_trade_info['epic'],
                'margin_factor': margin_factor,
                'notional': actual_notional,
            }
            
            # Account-level ML check after opening position
            # Calculate total required margin across all open positions
            total_required_margin = sum(
                pos['notional'] * pos['margin_factor'] 
                for pos in window_positions.values()
            )
            
            # Account-level ML = (Equity / Total Required Margin) × 100
            # Note: At entry moment, equity = balance (unrealized P&L is 0 for just-opened positions)
            if total_required_margin > 0:
                account_ml = (balance / total_required_margin) * 100
                if account_ml < min_account_margin_level:
                    min_account_margin_level = account_ml
                
                # Log if approaching danger zone
                if account_ml <= margin_closeout_level * 1.2:  # Within 20% of threshold
                    print(f"[Strategy Executor] WARNING: Day {day}, Account ML={account_ml:.1f}% "
                          f"(threshold={margin_closeout_level}%)", file=sys.stderr)
            
            # Reset carryover after executing a trade
            carried_over_pct = 0.0
    
    # DEBUG: Final count summary
    file_logger.log(f'Window execution complete. Final trades_executed count: {len(trades_executed)}')
    file_logger.log(f'Final balance: ${balance:.2f}')
    
    # Calculate final metrics
    total_contributions = initial_balance + (len(all_months) * monthly_topup)
    final_balance = balance
    total_return = ((final_balance - total_contributions) / total_contributions) * 100
    
    winning_trades = sum(1 for t in trades_executed if t['net_pnl'] > 0)
    losing_trades = sum(1 for t in trades_executed if t['net_pnl'] <= 0)
    win_rate = (winning_trades / len(trades_executed) * 100) if trades_executed else 0
    
    # Calculate total fees (spread costs + overnight funding costs)
    total_spread_costs = sum(t.get('spread_cost', 0) for t in trades_executed)
    total_overnight_costs = sum(t.get('overnight_costs', 0) for t in trades_executed)
    total_fees = total_spread_costs + total_overnight_costs
    
    # Calculate max drawdown
    peak = initial_balance
    max_dd = 0
    for trade in trades_executed:
        if trade['balance_after'] > peak:
            peak = trade['balance_after']
        dd = ((trade['balance_after'] - peak) / peak) * 100
        if dd < max_dd:
            max_dd = dd
    
    # Calculate Sharpe ratio
    if trades_executed:
        returns = [t['net_pnl'] / total_contributions for t in trades_executed]
        avg_return = sum(returns) / len(returns)
        std_return = (sum((r - avg_return) ** 2 for r in returns) / len(returns)) ** 0.5
        sharpe_ratio = (avg_return / std_return * (252 ** 0.5)) if std_return > 0 else 0
    else:
        sharpe_ratio = 0
    
    # Aggregate ML metrics from individual DNA results
    dna_liquidation_count = sum(
        dr['result'].get('liquidationCount', 0) for dr in dna_results
    )
    dna_liquidation_loss = sum(
        dr['result'].get('totalLiquidationLoss', 0) for dr in dna_results
    )
    dna_min_ml_values = [
        dr['result'].get('minMarginLevel') 
        for dr in dna_results 
        if dr['result'].get('minMarginLevel') is not None
    ]
    dna_min_ml = min(dna_min_ml_values) if dna_min_ml_values else None
    
    print(f"[Strategy Executor] Complete: {len(trades_executed)} trades, Final Balance: ${final_balance:.2f}, Return: {total_return:.2f}%", file=sys.stderr)
    if dna_liquidation_count > 0:
        print(f"[Strategy Executor] WARNING: {dna_liquidation_count} margin liquidations occurred in DNA backtests", file=sys.stderr)
    
    return {
        'initial_balance': initial_balance,
        'final_balance': final_balance,
        'total_contributions': total_contributions,
        'total_return': total_return,
        'total_trades': len(trades_executed),
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'max_drawdown': max_dd,
        'sharpe_ratio': sharpe_ratio,
        'total_fees': total_fees,  # Total trading costs (spread + overnight funding)
        'total_spread_costs': total_spread_costs,  # Just spread costs
        'total_overnight_costs': total_overnight_costs,  # Just overnight funding costs
        # Account-level Margin Level tracking
        'minAccountMarginLevel': min_account_margin_level if min_account_margin_level != float('inf') else None,
        # Aggregated DNA-level ML metrics
        'dnaLiquidationCount': dna_liquidation_count,
        'dnaTotalLiquidationLoss': dna_liquidation_loss,
        'dnaMinMarginLevel': dna_min_ml,
        'marginCloseoutLevel': margin_closeout_level,  # Setting used
        'trades': trades_executed,
        'window_count': len(windows),
    }


def merge_dna_results_simple(
    dna_results: List[Dict[str, Any]],
    initial_balance: float,
    monthly_topup: float,
    start_date: str,
    end_date: str,
    capital_events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Simple merge of DNA results without window logic (fallback)
    Just picks the best DNA strand by Sharpe ratio
    
    Note: capital_events are not fully utilized in simple merge mode.
    For accurate capital event handling, use window-based execution.
    """
    if capital_events:
        print(f"[Strategy Executor] Warning: Simple merge mode doesn't fully support capital_events. Use window config for accurate comparison.", file=sys.stderr)
    if not dna_results:
        return {
            'initial_balance': initial_balance,
            'final_balance': initial_balance,
            'total_contributions': initial_balance,
            'total_return': 0,
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0,
            'max_drawdown': 0,
            'sharpe_ratio': 0,
            'total_fees': 0,
            'total_spread_costs': 0,
            'total_overnight_costs': 0,
            'trades': [],
        }
    
    # Pick best DNA by Sharpe ratio
    best_dna = max(dna_results, key=lambda x: x['result']['sharpeRatio'])
    result = best_dna['result']
    
    return {
        'initial_balance': result['initialBalance'],
        'final_balance': result['finalBalance'],
        'total_contributions': result['totalContributions'],
        'total_return': result['totalReturn'],
        'total_trades': result['totalTrades'],
        'winning_trades': result['winningTrades'],
        'losing_trades': result['losingTrades'],
        'win_rate': result['winRate'],
        'max_drawdown': result['maxDrawdown'],
        'sharpe_ratio': result['sharpeRatio'],
        'total_fees': result.get('totalFees', 0),
        'total_spread_costs': result.get('totalSpreadCosts', 0),
        'total_overnight_costs': result.get('totalOvernightCosts', 0),
        # ML metrics from best DNA
        'minMarginLevel': result.get('minMarginLevel'),
        'liquidationCount': result.get('liquidationCount', 0),
        'marginLiquidatedTrades': result.get('marginLiquidatedTrades', 0),
        'totalLiquidationLoss': result.get('totalLiquidationLoss', 0),
        'marginCloseoutLevel': result.get('marginCloseoutLevel'),
        'trades': result['trades'],
    }


def normalize_result_keys(result: Dict) -> Dict:
    """Convert camelCase keys to snake_case"""
    key_mapping = {
        'indicatorName': 'indicator_name',
        'indicatorParams': 'indicator_params',
        'stopLoss': 'stop_loss',
        'timeframe': 'timeframe',
        'crashProtectionEnabled': 'crash_protection_enabled',
        'initialBalance': 'initial_balance',
        'finalBalance': 'final_balance',
        'totalContributions': 'total_contributions',
        'totalReturn': 'total_return',
        'totalTrades': 'total_trades',
        'winningTrades': 'winning_trades',
        'losingTrades': 'losing_trades',
        'winRate': 'win_rate',
        'maxDrawdown': 'max_drawdown',
        'maxProfit': 'max_profit',
        'sharpeRatio': 'sharpe_ratio',
        'trades': 'trades',
        'dailyBalances': 'daily_balances',
    }
    
    normalized = {}
    for key, value in result.items():
        normalized_key = key_mapping.get(key, key)
        normalized[normalized_key] = value
    
    return normalized


def combine_test_results(
    test_results: List[Dict[str, Any]],
    conflict_mode: str,
    initial_balance: float,
    monthly_topup: float,
    epic: str,
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """OLD: Non-window execution (backward compatibility)"""
    
    if conflict_mode == 'single_best_sharpe':
        best_test = max(test_results, key=lambda x: x['result']['sharpeRatio'])
        return normalize_result_keys(best_test['result'])
    
    elif conflict_mode == 'single_best_return':
        best_test = max(test_results, key=lambda x: x['result']['totalReturn'])
        return normalize_result_keys(best_test['result'])
    
    else:
        # For other modes, just pick first test
        return normalize_result_keys(test_results[0]['result'])


def clean_for_json(obj):
    """
    Recursively clean an object for JSON serialization.
    Converts NaN, inf, -inf to None (which becomes null in JSON).
    """
    import math
    import numpy as np_local
    import pandas as pd_local
    
    if isinstance(obj, dict):
        return {k: clean_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_for_json(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (np_local.floating, np_local.integer)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    elif pd_local.isna(obj):
        return None
    else:
        return obj


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: strategy_executor.py <config_json>", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = json.loads(sys.argv[1])
        result = execute_strategy(config)
        # Clean result for JSON serialization (convert NaN/inf to null)
        clean_result = clean_for_json(result)
        print(f"RESULT:{json.dumps(clean_result)}")
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
