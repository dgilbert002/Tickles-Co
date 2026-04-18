#!/usr/bin/env python3
"""
File Logger - Comprehensive logging to text files for Python

Creates timestamped log files for:
- Strategy test runs (parallel and sequential)
- DNA backtest executions
- Detailed timing information

All logs include timestamps, strategy info, and detailed execution data
"""

import os
import sys
from datetime import datetime
from typing import Dict, Any, Optional, List

# Ensure logs directory exists
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
STRATEGY_LOGS_DIR = os.path.join(LOGS_DIR, 'strategy')

for dir_path in [LOGS_DIR, STRATEGY_LOGS_DIR]:
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


def format_timestamp() -> str:
    """Format timestamp for log entries"""
    return datetime.now().isoformat()


def format_filename_timestamp() -> str:
    """Format timestamp for filenames (no colons)"""
    return datetime.now().strftime('%Y-%m-%dT%H-%M-%S-%f')


class StrategyTestLogger:
    """Strategy Test Logger for Python execution"""
    
    def __init__(
        self,
        strategy_id: int,
        strategy_name: str,
        dna_count: int,
        mode: str = 'parallel'  # 'parallel' or 'sequential'
    ):
        timestamp = format_filename_timestamp()
        filename = f"strategy_py_{mode}_{strategy_id}_{timestamp}.log"
        self.log_file = os.path.join(STRATEGY_LOGS_DIR, filename)
        self.start_time = datetime.now()
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        self.dna_count = dna_count
        self.mode = mode
        self.dna_timings: Dict[str, float] = {}
        
        self._write_header()
    
    def _write(self, message: str):
        """Write a line to the log file"""
        timestamp = format_timestamp()
        line = f"[{timestamp}] {message}\n"
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(line)
        # Also print to stderr for terminal visibility
        print(line.strip(), file=sys.stderr)
    
    def _write_header(self):
        """Write the log header"""
        self._write('╔══════════════════════════════════════════════════════════════════════╗')
        self._write(f'║           STRATEGY TEST LOG (PYTHON - {self.mode.upper()})                    ║')
        self._write('╚══════════════════════════════════════════════════════════════════════╝')
        self._write('')
        self._write(f'Started: {format_timestamp()}')
        self._write(f'Execution Mode: {self.mode.upper()}')
        self._write(f'Log File: {self.log_file}')
        self._write('')
        self._write('STRATEGY:')
        self._write(f'  ID: {self.strategy_id}')
        self._write(f'  Name: {self.strategy_name}')
        self._write(f'  DNA Strands: {self.dna_count}')
    
    def log(self, message: str, data: Any = None):
        """Log a message with optional data"""
        if data is not None:
            self._write(f'{message}')
            import json
            self._write(f'  DATA: {json.dumps(data, indent=2, default=str)}')
        else:
            self._write(message)
    
    def section(self, title: str):
        """Log a section header"""
        self._write('')
        self._write('═' * 70)
        self._write(f'  {title}')
        self._write('═' * 70)
    
    def subsection(self, title: str):
        """Log a subsection header"""
        self._write('')
        self._write(f'─── {title} {"─" * max(0, 60 - len(title))}')
    
    def error(self, message: str, error: Optional[Exception] = None):
        """Log an error"""
        self._write(f'❌ ERROR: {message}')
        if error:
            self._write(f'  Exception: {str(error)}')
            import traceback
            self._write(f'  Traceback: {traceback.format_exc()[:500]}')
    
    def success(self, message: str):
        """Log a success message"""
        self._write(f'✅ {message}')
    
    def warning(self, message: str):
        """Log a warning"""
        self._write(f'⚠️ WARNING: {message}')
    
    def timing(self, label: str, start_time: Optional[datetime] = None):
        """Log timing information"""
        if start_time:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
        else:
            elapsed = (datetime.now() - self.start_time).total_seconds() * 1000
        self._write(f'⏱️ {label}: {elapsed:.0f}ms')
        return elapsed
    
    def config_loaded(self, config: Dict[str, Any]):
        """Log configuration"""
        self.section('CONFIGURATION')
        self._write(f'Date Range: {config.get("start_date")} to {config.get("end_date")}')
        self._write(f'Initial Balance: ${config.get("initial_balance", 0)}')
        self._write(f'Monthly Topup: ${config.get("monthly_topup", 0)}')
        self._write(f'Investment %: {config.get("investment_pct", 99)}%')
        self._write(f'Calculation Mode: {config.get("calculation_mode", "standard")}')
    
    def dna_strand_details(self, dna: Dict[str, Any], index: int):
        """Log detailed DNA strand configuration including HMH"""
        self.subsection(f'DNA STRAND #{index}: {dna.get("id", "unknown")}')
        self._write(f'  Epic: {dna.get("epic", "N/A")}')
        self._write(f'  Indicator: {dna.get("indicatorName", "N/A")}')
        self._write(f'  Indicator Params: {dna.get("indicatorParams", {})}')
        self._write(f'  Timeframe: {dna.get("timeframe", "5m")}')
        self._write(f'  Leverage: {dna.get("leverage", 1)}x')
        self._write(f'  Stop Loss: {dna.get("stopLoss", 2.0)}%')
        self._write(f'  Direction: {dna.get("direction", "long")}')
        
        # HMH (Hold Means Hold) specific logging
        hmh_enabled = dna.get('hmhEnabled', False)
        hmh_offset = dna.get('hmhStopLossOffset', 0)
        self._write(f'  HMH Enabled: {hmh_enabled}')
        if hmh_enabled:
            self._write(f'  HMH Stop Loss Offset: {hmh_offset}%')
        
        # Timing and crash protection
        timing_config = dna.get('timingConfig', {})
        self._write(f'  Timing Mode: {timing_config.get("mode", "default")}')
        self._write(f'  Crash Protection: {dna.get("crashProtectionEnabled", False)}')
        self._write(f'  Data Source: {dna.get("dataSource", "capital")}')
    
    def dna_backtest_start(self, dna_id: str, epic: str, indicator: str, timing_mode: str, data_source: str):
        """Log DNA backtest start"""
        self.subsection(f'DNA BACKTEST: {dna_id}')
        self._write(f'Epic: {epic} | Indicator: {indicator}')
        self._write(f'Timing: {timing_mode} | Data Source: {data_source}')
        self._write(f'Started: {format_timestamp()}')
        self.dna_timings[dna_id] = datetime.now().timestamp()
    
    def candles_loaded(self, dna_id: str, epic: str, row_count: int, date_range: tuple, timing_mode: str):
        """Log candle data loading details"""
        self._write(f'  📊 Candles Loaded for {epic}:')
        self._write(f'     Rows: {row_count}')
        self._write(f'     Date Range: {date_range[0]} to {date_range[1]}')
        self._write(f'     Timing Mode: {timing_mode}')
    
    def candles_error(self, dna_id: str, epic: str, error: Exception):
        """Log candle loading error"""
        self._write(f'  ❌ CANDLE LOAD ERROR for {epic}:')
        self._write(f'     DNA ID: {dna_id}')
        self._write(f'     Error: {str(error)}')
        import traceback
        self._write(f'     Traceback: {traceback.format_exc()[:1000]}')
    
    def backtest_params(self, dna_id: str, params: Dict[str, Any]):
        """Log backtest parameters before execution"""
        self._write(f'  🔧 Backtest Params for {dna_id}:')
        for key, value in params.items():
            self._write(f'     {key}: {value}')
    
    def dna_backtest_complete(
        self, 
        dna_id: str, 
        trades: int, 
        final_balance: float, 
        sharpe: float,
        total_return: float,
        hmh_data: Optional[Dict[str, Any]] = None
    ):
        """Log DNA backtest completion with HMH details"""
        start_ts = self.dna_timings.get(dna_id)
        duration_ms = (datetime.now().timestamp() - start_ts) * 1000 if start_ts else 0
        
        self._write(f'✅ Completed in {duration_ms:.0f}ms')
        self._write(f'  Trades: {trades}')
        self._write(f'  Final Balance: ${final_balance:.2f}')
        self._write(f'  Total Return: {total_return:.2f}%')
        self._write(f'  Sharpe Ratio: {sharpe:.4f}')
        
        # Log HMH specific data if available
        if hmh_data:
            self._write(f'  HMH Stats:')
            self._write(f'    HMH Enabled: {hmh_data.get("hmh_enabled", False)}')
            self._write(f'    HMH Triggers: {hmh_data.get("hmh_triggers", 0)}')
            self._write(f'    HMH Exits: {hmh_data.get("hmh_exits", 0)}')
        
        return duration_ms
    
    def dna_backtest_error(self, dna_id: str, epic: str, indicator: str, error: Exception):
        """Log DNA backtest error with full context"""
        start_ts = self.dna_timings.get(dna_id)
        duration_ms = (datetime.now().timestamp() - start_ts) * 1000 if start_ts else 0
        
        self._write(f'❌ DNA BACKTEST FAILED: {dna_id}')
        self._write(f'  Epic: {epic}')
        self._write(f'  Indicator: {indicator}')
        self._write(f'  Failed after: {duration_ms:.0f}ms')
        self._write(f'  Error: {str(error)}')
        import traceback
        self._write(f'  Full Traceback:')
        for line in traceback.format_exc().split('\n'):
            self._write(f'    {line}')
    
    def all_dna_complete(self, dna_count: int):
        """Log all DNA backtests complete"""
        total_ms = self.timing('All DNA backtests')
        self.section('ALL DNA BACKTESTS COMPLETE')
        self._write(f'Total DNA strands: {dna_count}')
        self._write(f'Total time: {total_ms:.0f}ms')
        self._write(f'Average per DNA: {total_ms / max(dna_count, 1):.0f}ms')
        self._write(f'Execution mode: {self.mode.upper()}')
    
    def window_config_loaded(self, windows: List[Dict[str, Any]], total_allocation_pct: float):
        """Log window configuration details"""
        self.section(f'WINDOW CONFIGURATION ({len(windows)} windows)')
        self._write(f'Total Account Allocation: {total_allocation_pct}%')
        self._write('')
        
        for i, window in enumerate(windows):
            self._write(f'Window #{i+1}: {window.get("id", "unknown")}')
            self._write(f'  Close Time: {window.get("closeTime", "N/A")}')
            self._write(f'  Allocation: {window.get("allocationPct", 0)}%')
            self._write(f'  Carry Over: {window.get("carryOver", False)}')
            self._write(f'  Conflict Resolution: {window.get("conflictResolutionMetric", "sharpeRatio")}')
            dna_ids = window.get('dnaStrandIds', [])
            self._write(f'  DNA Strands: {len(dna_ids)}')
            for dna_id in dna_ids:
                self._write(f'    - {dna_id}')
            self._write('')
    
    def window_execution_start(self, day: str, window_id: str, window_num: int, total_windows: int):
        """Log window execution start"""
        self._write(f'📅 Day {day} | Window {window_num}/{total_windows}: {window_id}')
    
    def window_allocation(self, window_id: str, base_pct: float, carried_pct: float, available_pct: float, balance: float):
        """Log window allocation calculation"""
        available_funds = balance * (available_pct / 100)
        self._write(f'  💰 Allocation: base={base_pct}% + carry={carried_pct}% = {available_pct}%')
        self._write(f'     Available Funds: ${available_funds:.2f} (from balance ${balance:.2f})')
    
    def window_conflict_resolution(self, window_id: str, candidates: int, winner_dna: str, metric: str, metric_value: float):
        """Log conflict resolution within a window"""
        self._write(f'  ⚔️ Conflict Resolution: {candidates} candidates')
        self._write(f'     Winner: {winner_dna}')
        self._write(f'     Metric: {metric} = {metric_value:.4f}')
    
    def window_trade_executed(self, window_id: str, dna_id: str, epic: str, direction: str, 
                              contracts: float, entry_price: float, pnl: float, status: str):
        """Log trade execution within a window"""
        self._write(f'  📈 Trade Executed:')
        self._write(f'     DNA: {dna_id}')
        self._write(f'     Epic: {epic} | Direction: {direction}')
        self._write(f'     Contracts: {contracts:.2f} @ ${entry_price:.2f}')
        self._write(f'     P&L: ${pnl:.2f} | Status: {status}')
    
    def window_hold_decision(self, window_id: str, reason: str, carry_forward_pct: float):
        """Log HOLD decision within a window"""
        self._write(f'  ⏸️ HOLD Decision: {reason}')
        self._write(f'     Carrying forward: {carry_forward_pct}%')
    
    def window_no_trades(self, window_id: str, day: str):
        """Log when no trades available for a window"""
        self._write(f'  ⭕ No trades available for window {window_id} on {day}')
    
    def daily_summary(self, day: str, trades_today: int, balance: float, daily_pnl: float):
        """Log daily trading summary"""
        self._write(f'  📊 Day {day} Summary: {trades_today} trades, Balance: ${balance:.2f}, P&L: ${daily_pnl:.2f}')
    
    def monthly_topup(self, month: str, amount: float, new_balance: float):
        """Log monthly topup"""
        self._write(f'  💵 Monthly Topup ({month}): +${amount:.2f} → Balance: ${new_balance:.2f}')
    
    def capital_event(self, day: str, event_type: str, amount: float, new_balance: float):
        """Log capital deposit/withdrawal event"""
        symbol = '+' if event_type == 'deposit' else '-'
        self._write(f'  🏦 Capital Event ({day}): {event_type} {symbol}${amount:.2f} → Balance: ${new_balance:.2f}')
    
    def final_results(self, results: Dict[str, Any]):
        """Log final results
        
        FIX 2026-01-09: Changed from camelCase to snake_case keys to match
        what strategy_executor.py returns. Old keys caused 0 values in logs.
        
        ROLLBACK: Change snake_case back to camelCase if needed:
        # OLD (broken): results.get("finalBalance", 0)
        # NEW (fixed):  results.get("final_balance", 0)
        """
        self.section('FINAL RESULTS')
        self._write(f'Final Balance: ${results.get("final_balance", 0):.2f}')
        self._write(f'Total Return: {results.get("total_return", 0):.2f}%')
        self._write(f'Total Trades: {results.get("total_trades", 0)}')
        self._write(f'Winning Trades: {results.get("winning_trades", 0)}')
        self._write(f'Losing Trades: {results.get("losing_trades", 0)}')
        self._write(f'Win Rate: {results.get("win_rate", 0):.2f}%')
        self._write(f'Sharpe Ratio: {results.get("sharpe_ratio", 0):.4f}')
        self._write(f'Max Drawdown: {results.get("max_drawdown", 0):.2f}%')
        
        # Log fees breakdown if available
        if 'total_fees' in results:
            self._write(f'')
            self._write(f'Fee Breakdown:')
            self._write(f'  Total Fees: ${results.get("total_fees", 0):.2f}')
            self._write(f'  Spread Costs: ${results.get("total_spread_costs", 0):.2f}')
            self._write(f'  Overnight Costs: ${results.get("total_overnight_costs", 0):.2f}')
    
    def close(self):
        """Close the log file"""
        self.section('LOG COMPLETE')
        total_ms = (datetime.now() - self.start_time).total_seconds() * 1000
        self._write(f'Total execution time: {total_ms:.0f}ms')
        self._write(f'Log file: {self.log_file}')


# Global logger instance for the current strategy test
_current_logger: Optional[StrategyTestLogger] = None


def create_strategy_logger(
    strategy_id: int,
    strategy_name: str,
    dna_count: int,
    mode: str = 'parallel'
) -> StrategyTestLogger:
    """Create a new strategy test logger"""
    global _current_logger
    _current_logger = StrategyTestLogger(strategy_id, strategy_name, dna_count, mode)
    return _current_logger


def get_current_logger() -> Optional[StrategyTestLogger]:
    """Get the current logger instance"""
    return _current_logger


