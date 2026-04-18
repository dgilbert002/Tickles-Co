#!/usr/bin/env python3
"""
Batch Backtest Runner - Integrates batch_optimizer with backtest_runner
Outputs progress and results in a format the Node.js backend can parse
"""

import sys
import json
import mysql.connector
from pathlib import Path
from typing import Dict, List, Any, Optional, Set
import random
import os
import pickle
import time
import signal
from multiprocessing import shared_memory
import numpy as np

# Import the backtest functions
from backtest_runner import load_epic_data, run_single_backtest, run_enhanced_signal_backtest, run_parallel_exit_backtest, run_signal_based_backtest

# Import optimized signal-based backtest (7.5x faster, 100% identical results)
try:
    from backtest_runner_optimized import run_signal_based_backtest_optimized
    OPTIMIZED_SIGNAL_BACKTEST_AVAILABLE = True
    print("[OK] Optimized signal backtest engine loaded (7.5x faster)", file=sys.stderr)
except ImportError as e:
    OPTIMIZED_SIGNAL_BACKTEST_AVAILABLE = False
    print(f"[WARN] Optimized signal backtest not available: {e}", file=sys.stderr)

# Import chain backtest optimized (V3 vectorized - 10-30x faster for entry/exit pairs)
try:
    from chain_backtest_optimized import run_chain_backtest
    CHAIN_BACKTEST_OPTIMIZED_AVAILABLE = True
    print("[OK] V3 Chain backtest engine loaded (10-30x faster)", file=sys.stderr)
except ImportError as e:
    CHAIN_BACKTEST_OPTIMIZED_AVAILABLE = False
    print(f"[WARN] V3 Chain backtest not available: {e}", file=sys.stderr)

# Import fast backtest engine (4-20x faster, 100% identical results)
try:
    from fast_backtest_engine import (
        FastBacktestEngine, 
        run_single_backtest_fast,
        fast_backtest_worker,
        FastBacktestData,
        calculate_signals_for_indicator,
        run_fast_backtest
    )
    FAST_ENGINE_AVAILABLE = True
    print("✅ Fast backtest engine loaded", file=sys.stderr)
except ImportError as e:
    FAST_ENGINE_AVAILABLE = False
    print(f"⚠️ Fast backtest engine not available: {e}", file=sys.stderr)

# Import optimization components
from batch_optimizer import (
    BatchOptimizer,
    OptimizationConfig,
    ParameterSpace
)

# Import connection pooling for faster DB operations
from db_pool import check_hashes_batch, check_run_status_pooled, get_connection, load_all_tested_hashes

# Global references for signal handler cleanup
_pool = None
_shm_numeric = None
_shm_meta = None

def signal_handler(signum, frame):
    """Handle SIGTERM/SIGINT to cleanly shutdown worker pool and shared memory"""
    global _pool, _shm_numeric, _shm_meta
    print("\n⚠️  Received stop signal, terminating workers...", file=sys.stderr)
    
    # Terminate worker pool
    if _pool is not None:
        try:
            _pool.terminate()
            _pool.join()
        except Exception as e:
            print(f"  ⚠️ Error terminating pool: {e}", file=sys.stderr)
        _pool = None
    
    # Clean up shared memory
    if _shm_numeric is not None:
        try:
            _shm_numeric.close()
            _shm_numeric.unlink()
            print("  🧹 Cleaned up numeric shared memory", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠️ Error cleaning up numeric shared memory: {e}", file=sys.stderr)
        _shm_numeric = None
    
    if _shm_meta is not None:
        try:
            _shm_meta.close()
            _shm_meta.unlink()
            print("  🧹 Cleaned up metadata shared memory", file=sys.stderr)
        except Exception as e:
            print(f"  ⚠️ Error cleaning up metadata shared memory: {e}", file=sys.stderr)
        _shm_meta = None
    
    print("  ✅ Cleanup complete, exiting...", file=sys.stderr)
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# ============================================================================
# DUPLICATE HASH PRE-CHECK FUNCTIONS
# ============================================================================

def generate_param_hash(
    epic: str,
    start_date: str,
    end_date: str,
    direction: str,
    initial_balance: float,
    monthly_topup: float,
    investment_pct: float,
    timing_config: dict,
    data_source: str,
    indicator_name: str,
    indicator_params: dict,
    leverage: float,
    stop_loss: float,
    timeframe: str,
    crash_protection_enabled: bool,
    hmh_enabled: bool = False,
    hmh_stop_loss_offset: float = None
) -> str:
    """
    Generate the exact same hash that Node.js generates for backtest deduplication.
    This must match the hash calculation in server/backtest_bridge_v2.ts exactly.
    
    HMH (Hold Means Hold) parameters:
    - hmh_enabled: Whether to trail stop loss on HOLD instead of closing
    - hmh_stop_loss_offset: SL adjustment (0=original, -1=orig-1%, -2=orig-2%)
    """
    import hashlib
    
    # Build the hash input object (same structure as Node)
    # Keys MUST be in alphabetical order for consistent hashing
    hash_input_obj = {
        'crashProtectionEnabled': crash_protection_enabled,
        'dataSource': data_source,
        'direction': direction,
        'endDate': end_date,
        'epic': epic,
        'hmhEnabled': hmh_enabled,
        'hmhStopLossOffset': hmh_stop_loss_offset,
        'indicatorName': indicator_name,
        'indicatorParams': indicator_params,
        'initialBalance': initial_balance,
        'investmentPct': investment_pct,
        'leverage': leverage,
        'monthlyTopup': monthly_topup,
        'startDate': start_date,
        'stopLoss': stop_loss,
        'timeframe': timeframe,
        'timingConfig': timing_config,
    }
    
    # Sort keys recursively (same as Node's sortObjectKeys function)
    def sort_object_keys(obj):
        if obj is None or not isinstance(obj, (dict, list)):
            return obj
        if isinstance(obj, list):
            return [sort_object_keys(item) for item in obj]
        return {k: sort_object_keys(v) for k, v in sorted(obj.items())}
    
    sorted_obj = sort_object_keys(hash_input_obj)
    hash_input = json.dumps(sorted_obj, separators=(',', ':'))  # Compact JSON like Node
    
    return hashlib.sha256(hash_input.encode()).hexdigest()


def check_existing_hashes(hashes: List[str], db_config: dict = None) -> set:
    """
    Check which hashes already exist in the database.
    Returns a set of hashes that already exist.
    
    Now uses connection pooling via db_pool.py for better performance.
    The db_config parameter is kept for backwards compatibility but is ignored.
    """
    # Use the pooled batch check function (much faster)
    return check_hashes_batch(hashes)


def try_claim_test_by_hash(
    run_id: int,
    param_hash: str,
    epic: str,
    indicator_name: str,
    indicator_params: dict,
    leverage: float,
    stop_loss: float,
    timeframe: str,
    crash_protection: bool,
    worker_id: str,
    db_config: dict
) -> bool:
    """
    Atomically try to claim a test using the queue table.
    Uses INSERT ... ON DUPLICATE KEY to prevent race conditions.
    Returns True if claim succeeded (this worker should run the test).
    Returns False if claim failed (another worker already claimed it or test exists).
    """
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        # First check if this hash already has a completed result
        cursor.execute(
            'SELECT id FROM backtestResults WHERE paramHash = %s LIMIT 1',
            (param_hash,)
        )
        if cursor.fetchone():
            conn.close()
            return False  # Already completed, skip
        
        # Try to claim by inserting into queue
        # ON DUPLICATE KEY UPDATE checks if we claimed it
        cursor.execute('''
            INSERT INTO backtestQueue 
            (runId, paramHash, epic, indicatorName, indicatorParams, leverage, stopLoss, timeframe, crashProtection, status, workerId, claimedAt)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'processing', %s, NOW())
            ON DUPLICATE KEY UPDATE 
                id = IF(status = 'pending', LAST_INSERT_ID(id), id),
                status = IF(status = 'pending', 'processing', status),
                workerId = IF(status = 'pending', VALUES(workerId), workerId),
                claimedAt = IF(status = 'pending', NOW(), claimedAt)
        ''', (
            run_id, param_hash, epic, indicator_name,
            json.dumps(indicator_params), leverage, stop_loss,
            timeframe, crash_protection, worker_id
        ))
        
        affected_rows = cursor.rowcount
        conn.commit()
        conn.close()
        
        # rowcount: 1 = new insert, 2 = update (we claimed it), 0 = no change (already claimed by someone else)
        claimed = affected_rows > 0
        if claimed:
            print(f"    🔒 Claimed test {param_hash[:8]}... (worker {worker_id})", file=sys.stderr)
        return claimed
        
    except Exception as e:
        print(f"Warning: Could not claim test {param_hash[:8]}...: {e}", file=sys.stderr)
        return False


def batch_claim_tests(
    hashes: List[str],
    run_id: int,
    worker_id: str,
    db_config: dict
) -> Set[str]:
    """
    BATCH VERSION: Claim multiple tests at once using a single DB transaction.
    
    Much faster than individual claims:
    - 763 individual claims: ~4 seconds (763 connections, 1526 queries)
    - 1 batch claim: ~0.1 seconds (1 connection, 2 queries)
    
    Args:
        hashes: List of param_hash values to try to claim
        run_id: The current run ID
        worker_id: This worker's ID
        db_config: Database connection config
    
    Returns:
        Set of hashes that were successfully claimed (can be run by this worker)
    """
    if not hashes:
        return set()
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Step 1: Get all hashes that already have completed results (skip these)
        placeholders = ','.join(['%s'] * len(hashes))
        cursor.execute(
            f'SELECT paramHash FROM backtestResults WHERE paramHash IN ({placeholders})',
            hashes
        )
        completed_hashes = {row[0] for row in cursor.fetchall()}
        
        # Step 2: Get all hashes already in queue (processing/completed)
        cursor.execute(
            f"SELECT paramHash FROM backtestQueue WHERE paramHash IN ({placeholders}) AND status IN ('processing', 'completed')",
            hashes
        )
        queued_hashes = {row[0] for row in cursor.fetchall()}
        
        # Step 3: Determine which hashes can be claimed
        already_done = completed_hashes | queued_hashes
        claimable = [h for h in hashes if h not in already_done]
        
        if not claimable:
            cursor.close()
            conn.close()
            return set()
        
        # Step 4: Batch insert claims (using INSERT IGNORE to handle race conditions)
        # This is safe because paramHash has a unique index
        insert_values = [(run_id, h, worker_id) for h in claimable]
        cursor.executemany('''
            INSERT IGNORE INTO backtestQueue 
            (runId, paramHash, status, workerId, claimedAt)
            VALUES (%s, %s, 'processing', %s, NOW())
        ''', insert_values)
        
        conn.commit()
        claimed_count = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        print(f"  🔒 Batch claim: {claimed_count} claimed, {len(already_done)} already done", file=sys.stderr)
        return set(claimable[:claimed_count]) if claimed_count > 0 else set()
        
    except Exception as e:
        print(f"[BatchClaim] Error: {e}", file=sys.stderr)
        return set()


def mark_queue_test_complete(param_hash: str, result_id: int, db_config: dict) -> None:
    """Mark a queue item as completed with the result ID."""
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE backtestQueue 
            SET status = 'completed', completedAt = NOW(), resultId = %s
            WHERE paramHash = %s
        ''', (result_id, param_hash))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not mark queue complete for {param_hash[:8]}...: {e}", file=sys.stderr)


def mark_queue_test_failed(param_hash: str, error_message: str, db_config: dict) -> None:
    """Mark a queue item as failed with error message."""
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE backtestQueue 
            SET status = 'failed', completedAt = NOW(), errorMessage = %s
            WHERE paramHash = %s
        ''', (error_message, param_hash))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Warning: Could not mark queue failed for {param_hash[:8]}...: {e}", file=sys.stderr)


def get_queue_stats(run_id: int, db_config: dict) -> dict:
    """Get queue statistics for a run."""
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) as processing,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
                COUNT(*) as total
            FROM backtestQueue WHERE runId = %s
        ''', (run_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'pending': int(row[0] or 0),
                'processing': int(row[1] or 0),
                'completed': int(row[2] or 0),
                'failed': int(row[3] or 0),
                'total': int(row[4] or 0)
            }
        return {'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'total': 0}
    except Exception as e:
        print(f"Warning: Could not get queue stats: {e}", file=sys.stderr)
        return {'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'total': 0}


def clear_stale_queue_entries(db_config: dict) -> int:
    """
    Clear stale queue entries from previous runs.
    Removes:
    1. completed/failed entries (done processing)
    2. processing entries older than 2 minutes (stale from crashes)
    3. pending entries (leftover from aborted runs)
    4. Any entry where result already exists in backtestResults
    Returns the number of entries deleted.
    """
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        # First, count what we have
        cursor.execute('SELECT COUNT(*), status FROM backtestQueue GROUP BY status')
        rows = cursor.fetchall()
        if rows:
            counts = {status: count for count, status in rows}
            print(f"  📊 Queue status before clear: {counts}", file=sys.stderr)
        
        # Delete ALL stale queue entries - be aggressive to prevent blocking
        # The queue is regenerated for each run anyway
        cursor.execute('''
            DELETE q FROM backtestQueue q
            WHERE q.status IN ('completed', 'failed', 'pending')
            OR (q.status = 'processing' AND q.claimedAt < DATE_SUB(NOW(), INTERVAL 2 MINUTE))
            OR EXISTS (
                SELECT 1 FROM backtestResults r WHERE r.paramHash = q.paramHash
            )
        ''')
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        print(f"  🧹 Cleared {deleted} stale queue entries", file=sys.stderr)
        return deleted
    except Exception as e:
        print(f"Warning: Could not clear stale queue entries: {e}", file=sys.stderr)
        return 0


# Global worker function for multiprocessing (must be at module level to be picklable)
def _parallel_test_worker(args):
    """
    Worker function for parallel test execution using shared memory.
    Must be at module level to be picklable by multiprocessing.
    
    Supports both original and fast backtest engines (engine parameter in args).
    Includes configurable timeout that KILLS the worker if exceeded.
    """
    import threading
    import os as _os
    
    # Handle multiple arg formats for backwards compatibility:
    # - 20 args: newest with HMH parameters
    # - 18 args: with configurable timeout
    # - 17 args: with engine but hardcoded timeout
    # - 16 args: original format
    hmh_enabled = False
    hmh_stop_loss_offset = 0
    if len(args) == 20:
        (
            params, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, indicator_name, initial_balance, monthly_topup, investment_pct,
            direction, timing_config, stop_conditions, timeframe, crash_protection_enabled, engine,
            timeout_seconds, hmh_enabled, hmh_stop_loss_offset
        ) = args
    elif len(args) == 18:
        (
            params, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, indicator_name, initial_balance, monthly_topup, investment_pct,
            direction, timing_config, stop_conditions, timeframe, crash_protection_enabled, engine,
            timeout_seconds
        ) = args
    elif len(args) == 17:
        (
            params, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, indicator_name, initial_balance, monthly_topup, investment_pct,
            direction, timing_config, stop_conditions, timeframe, crash_protection_enabled, engine
        ) = args
        timeout_seconds = 60  # Default for backwards compatibility
    else:
        (
            params, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, indicator_name, initial_balance, monthly_topup, investment_pct,
            direction, timing_config, stop_conditions, timeframe, crash_protection_enabled
        ) = args
        engine = 'original'  # Default for backwards compatibility
        timeout_seconds = 60
    
    worker_id = _os.getpid()
    
    # ========================================================================
    # TIMEOUT WATCHDOG: KILL worker process if test takes longer than configured timeout
    # This is aggressive but necessary because stuck computations won't check flags
    # ========================================================================
    TIMEOUT_SECONDS = timeout_seconds
    
    def timeout_killer():
        """Called when timeout expires - KILLS THIS PROCESS"""
        print(f"WORKER_TIMEOUT:{worker_id}:{test_num}:{indicator_name}:KILLING after {TIMEOUT_SECONDS}s", flush=True)
        # Force kill this process - the pool will spawn a new worker
        _os._exit(1)  # Immediate termination, no cleanup
    
    # Start timeout timer that will KILL us if we don't finish in time
    timeout_timer = threading.Timer(TIMEOUT_SECONDS, timeout_killer)
    timeout_timer.daemon = True
    timeout_timer.start()
    
    # Report worker start
    print(f"WORKER_START:{worker_id}:{test_num}:{indicator_name}:{engine}", flush=True)
    
    try:
        # Access shared memory and reconstruct DataFrame
        import pandas as pd
        import pickle
        
        # Load metadata
        shm_meta = shared_memory.SharedMemory(name=shm_meta_name)
        metadata_bytes = bytes(shm_meta.buf[:meta_size])
        metadata = pickle.loads(metadata_bytes)
        shm_meta.close()
        
        # Load numeric data
        shm_numeric = shared_memory.SharedMemory(name=shm_numeric_name)
        numeric_array = np.ndarray(numeric_shape, dtype=np.float64, buffer=shm_numeric.buf)
        
        # Reconstruct DataFrame with numeric columns
        df = pd.DataFrame(numeric_array.copy(), columns=metadata['numeric_cols'])
        
        # Add datetime columns
        for col in metadata['datetime_cols']:
            df[col] = metadata['datetime_data'][col]
        
        # Add object columns
        for col in metadata['object_cols']:
            df[col] = metadata['object_data'][col]
        
        # Close shared memory (don't unlink)
        shm_numeric.close()
        
        # Run the backtest using selected engine with HMH support
        result = run_backtest_with_params(
            df, epic, indicator_name, params,
            initial_balance, monthly_topup, investment_pct, direction,
            timing_config=timing_config,
            stop_conditions=stop_conditions,
            timeframe=timeframe,
            engine=engine,
            crash_protection_enabled=crash_protection_enabled,
            hmh_enabled=hmh_enabled,
            hmh_stop_loss_offset=hmh_stop_loss_offset if hmh_stop_loss_offset is not None else 0,
        )
        
        # Cancel timeout timer since we finished successfully
        timeout_timer.cancel()
        
        # Report worker completion
        print(f"WORKER_COMPLETE:{worker_id}:{test_num}:{indicator_name}", flush=True)
        
        # Return result with metadata
        return {
            'success': True,
            'test_num': test_num,
            'params': params,
            'result': result,
            'indicator_name': indicator_name,
            'timeframe': timeframe,
            'crash_protection': crash_protection_enabled,
        }
    except Exception as e:
        import traceback
        # Cancel timeout timer on failure too
        timeout_timer.cancel()
        
        # Report worker failure
        print(f"WORKER_FAILED:{worker_id}:{test_num}:{indicator_name}:{str(e)}", flush=True)
        return {
            'success': False,
            'test_num': test_num,
            'params': params,
            'error': str(e),
            'traceback': traceback.format_exc(),
        }


# Signal-based worker function for multiprocessing (must be at module level to be picklable)
def _signal_based_test_worker(args):
    """
    Worker function for signal-based backtest execution using shared memory.
    Mirrors _parallel_test_worker but calls run_enhanced_signal_backtest.
    Includes configurable timeout that KILLS the worker if exceeded.
    """
    import threading
    import os as _os
    
    # Handle both old (18 args) and new (19 args with timeout) formats
    if len(args) == 19:
        (
            test_spec, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, initial_balance, monthly_topup, investment_pct,
            direction, timeframe, position_size_pct, allow_short, reverse_on_signal,
            stop_loss_mode, margin_closeout_level, min_balance_threshold, timeout_seconds
        ) = args
    else:
        (
            test_spec, test_num, shm_numeric_name, shm_meta_name, meta_size,
            numeric_shape, epic, initial_balance, monthly_topup, investment_pct,
            direction, timeframe, position_size_pct, allow_short, reverse_on_signal,
            stop_loss_mode, margin_closeout_level, min_balance_threshold
        ) = args
        timeout_seconds = 300  # Default 5 minutes for signal-based tests
    
    entry_ind = test_spec['entry_indicator']
    exit_ind = test_spec['exit_indicator']
    entry_params = test_spec['entry_params']
    exit_params = test_spec['exit_params']
    test_leverage = test_spec['leverage']
    test_stop_loss = test_spec['stop_loss']
    test_hash = test_spec['_param_hash']
    
    worker_id = _os.getpid()
    pair_name = f"{entry_ind} -> {exit_ind}"
    
    # ========================================================================
    # TIMEOUT WATCHDOG: KILL worker process if test takes longer than configured timeout
    # Signal-based tests process many candles and can take longer than timing-based
    # ========================================================================
    TIMEOUT_SECONDS = timeout_seconds

    def timeout_killer():
        """Called when timeout expires - KILLS THIS PROCESS"""
        print(f"WORKER_TIMEOUT:{worker_id}:{test_num}:{pair_name}:KILLING after {TIMEOUT_SECONDS}s", flush=True)
        _os._exit(1)  # Immediate termination
    
    timeout_timer = threading.Timer(TIMEOUT_SECONDS, timeout_killer)
    timeout_timer.daemon = True
    timeout_timer.start()
    
    # Report worker start
    print(f"WORKER_START:{worker_id}:{test_num}:{pair_name}", flush=True)
    
    try:
        # Access shared memory and reconstruct DataFrame
        import pandas as pd
        import pickle
        
        # Try to use V3 optimized chain backtest (10-30x faster)
        try:
            from chain_backtest_optimized import run_chain_backtest
            use_v3_optimized = True
        except ImportError:
            from backtest_runner import run_enhanced_signal_backtest
            use_v3_optimized = False
        
        # Load metadata
        shm_meta = shared_memory.SharedMemory(name=shm_meta_name)
        metadata_bytes = bytes(shm_meta.buf[:meta_size])
        metadata = pickle.loads(metadata_bytes)
        shm_meta.close()
        
        # Load numeric data
        shm_numeric = shared_memory.SharedMemory(name=shm_numeric_name)
        numeric_array = np.ndarray(numeric_shape, dtype=np.float64, buffer=shm_numeric.buf)
        
        # Reconstruct DataFrame with numeric columns
        df = pd.DataFrame(numeric_array.copy(), columns=metadata['numeric_cols'])
        
        # Add datetime columns
        for col in metadata['datetime_cols']:
            df[col] = metadata['datetime_data'][col]
        
        # Add object columns
        for col in metadata['object_cols']:
            df[col] = metadata['object_data'][col]
        
        # Close shared memory (don't unlink)
        shm_numeric.close()
        
        # Debug: Print what we're passing
        engine_type = "V3_OPTIMIZED" if use_v3_optimized else "V2_LEGACY"
        print(f"[Worker {worker_id}] [{engine_type}] entry_ind={entry_ind}, entry_params={entry_params}", flush=True)
        print(f"[Worker {worker_id}] exit_ind={exit_ind}, exit_params={exit_params}", flush=True)
        print(f"[Worker {worker_id}] df.shape={df.shape}, columns={list(df.columns)[:5]}...", flush=True)
        
        # Run the backtest - use V3 optimized if available
        if use_v3_optimized:
            # V3: Pre-calculates ALL signals once, then loops with array lookups
            # Convert direction to direction_mode format
            direction_mode = 'long_only' if direction == 'long' else 'short_only' if direction == 'short' else 'both'
            
            v3_result = run_chain_backtest(
                df=df,
                epic=epic,
                entry_indicator=entry_ind,
                entry_params=entry_params,
                exit_indicator=exit_ind,
                exit_params=exit_params,
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                position_size_pct=position_size_pct,
                leverage=test_leverage,
                stop_loss_pct=test_stop_loss,
                direction_mode=direction_mode,
                margin_closeout_level=margin_closeout_level,
                min_balance_threshold=min_balance_threshold,
                verbose=False,
            )
            
            # Map V3 snake_case keys to camelCase for compatibility with rest of system
            result = {
                'finalBalance': v3_result.get('final_balance', 0),
                'totalReturn': v3_result.get('total_return_pct', 0),
                'totalTrades': v3_result.get('total_trades', 0),
                'winningTrades': v3_result.get('winning_trades', 0),
                'losingTrades': v3_result.get('losing_trades', 0),
                'winRate': v3_result.get('win_rate', 0),
                'maxDrawdown': v3_result.get('max_drawdown', 0),
                'sharpeRatio': v3_result.get('sharpe_ratio', 0),
                'trades': v3_result.get('trades', []),
                'dailyBalances': v3_result.get('daily_balances', []),
                'minMarginLevel': v3_result.get('min_margin_level'),
                'liquidationCount': v3_result.get('liquidation_count', 0),
                'totalLiquidationLoss': v3_result.get('total_liquidation_loss', 0),
                'status': v3_result.get('status', 'completed'),
                'backtestType': 'chain_v3_optimized',
            }
        else:
            # V2 fallback: Row-by-row indicator evaluation (slower)
            from backtest_runner import run_enhanced_signal_backtest
            result = run_enhanced_signal_backtest(
                df=df,
                epic=epic,
                entry_indicators=[entry_ind],
                exit_indicators=[exit_ind],
                entry_params={entry_ind: entry_params},
                exit_params={exit_ind: exit_params},
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                position_size_pct=position_size_pct,
                allow_short=allow_short,
                reverse_on_signal=reverse_on_signal,
                stop_loss_mode=stop_loss_mode,
                fixed_stop_loss_pct=test_stop_loss,
                margin_closeout_level=margin_closeout_level,
                min_balance_threshold=min_balance_threshold,
                use_trust_matrix=False,  # Learning mode
                default_leverage=test_leverage,
                timeframe=timeframe,
                direction=direction,
                trust_matrix={},
            )
        
        # Enrich result with test info
        result['entryIndicator'] = entry_ind
        result['entryParams'] = entry_params
        result['exitIndicator'] = exit_ind
        result['exitParams'] = exit_params
        result['indicatorName'] = pair_name
        result['leverage'] = test_leverage
        result['stopLoss'] = test_stop_loss
        result['_queueHash'] = test_hash
        
        # Cancel timeout timer since we finished successfully
        timeout_timer.cancel()
        
        # Report worker completion
        print(f"WORKER_COMPLETE:{worker_id}:{test_num}:{pair_name}", flush=True)
        
        return {
            'success': True,
            'test_num': test_num,
            'test_spec': test_spec,
            'result': result,
        }
    except Exception as e:
        import traceback
        timeout_timer.cancel()
        
        print(f"WORKER_FAILED:{worker_id}:{test_num}:{pair_name}:{str(e)}", flush=True)
        return {
            'success': False,
            'test_num': test_num,
            'test_spec': test_spec,
            'error': str(e),
            'traceback': traceback.format_exc(),
        }


def _signal_based_worker(spec):
    """
    Simple worker function for signal-based backtest execution using pickle.
    Called by multiprocessing pool for parallel test execution.
    """
    import pickle
    import os as _os
    import sys
    
    worker_id = _os.getpid()
    test_num = spec['test_num']
    total_tests = spec['total_tests']
    entry_ind = spec['entry_indicator']
    exit_ind = spec['exit_indicator']
    entry_params = spec['entry_params']
    exit_params = spec['exit_params']
    
    pair_name = f"{entry_ind} -> {exit_ind}"
    
    try:
        # Reconstruct DataFrame from pickle
        import pandas as pd
        from backtest_runner import run_enhanced_signal_backtest
        
        df = pickle.loads(spec['df_pickle'])
        
        # Run the signal-based backtest
        result = run_enhanced_signal_backtest(
            df=df,
            epic=spec['epic'],
            entry_indicators=[entry_ind],
            exit_indicators=[exit_ind],
            entry_params={entry_ind: entry_params},
            exit_params={exit_ind: exit_params},
            initial_balance=spec['initial_balance'],
            monthly_topup=spec['monthly_topup'],
            position_size_pct=spec['position_size_pct'],
            allow_short=False,
            reverse_on_signal=False,
            stop_loss_mode=spec['stop_loss_mode'],
            fixed_stop_loss_pct=spec['stop_loss_pct'],
            margin_closeout_level=spec['margin_closeout_level'],
            min_balance_threshold=spec['min_balance_threshold'],
            use_trust_matrix=True,
            default_leverage=spec['default_leverage'],
            timeframe=spec['timeframe'],
            direction=spec['direction'],
            trust_matrix={},  # Each worker gets empty trust matrix
        )
        
        # Update result with indicator info
        result['indicatorName'] = pair_name
        result['indicatorParams'] = {'entry': {entry_ind: entry_params}, 'exit': {exit_ind: exit_params}}
        result['entry_indicator'] = entry_ind
        result['exit_indicator'] = exit_ind
        result['test_num'] = test_num
        
        print(f"[Test {test_num}/{total_tests}] {pair_name} -> ${result.get('finalBalance', 0):.2f}", file=sys.stderr)
        
        return result
        
    except Exception as e:
        import traceback
        print(f"[Test {test_num}/{total_tests}] {pair_name} FAILED: {e}", file=sys.stderr)
        return {
            'status': 'error',
            'error': str(e),
            'entry_indicator': entry_ind,
            'exit_indicator': exit_ind,
            'test_num': test_num,
        }


def get_db_config():
    """Get database config using centralized db_config module"""
    from db_config import get_database_url, parse_database_url
    
    try:
        db_url = get_database_url()
        config = parse_database_url(db_url)
        
        # Add SSL if required (TiDB Cloud)
        if config.get('host') and 'tidbcloud.com' in config['host']:
            config['ssl_verify_cert'] = True
            config['ssl_verify_identity'] = True
        
        return config
    except ValueError:
        # Fallback to individual env vars for backwards compatibility
        return {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'root'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'capitaltwo'),
        }


def check_run_status(run_id: int) -> str:
    """
    Check if backtest run is paused by querying database.
    
    Now uses connection pooling for faster status checks during parallel execution.
    Status is checked frequently (every 10 tests) so pooling makes a big difference.
    """
    result = check_run_status_pooled(run_id)
    return result if result else 'running'


def save_checkpoint(run_id: int, checkpoint_data: Dict) -> None:
    """Save checkpoint data to database"""
    try:
        db_config = get_db_config()
        
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE backtestRuns SET checkpointData = %s, lastProcessedIndex = %s WHERE id = %s",
            (json.dumps(checkpoint_data), checkpoint_data.get('last_index', 0), run_id)
        )
        conn.commit()
        
        cursor.close()
        conn.close()
        
        print(f"✓ Checkpoint saved at index {checkpoint_data.get('last_index', 0)}", file=sys.stderr)
    except Exception as e:
        print(f"ERROR: Could not save checkpoint: {e}", file=sys.stderr)


def load_checkpoint(run_id: int) -> Optional[Dict]:
    """Load checkpoint data from database"""
    try:
        db_config = get_db_config()
        
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT checkpointData FROM backtestRuns WHERE id = %s",
            (run_id,)
        )
        result = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if result and result[0]:
            return json.loads(result[0])
        return None
    except Exception as e:
        print(f"WARNING: Could not load checkpoint: {e}", file=sys.stderr)
        return None


def fetch_leverage_options_from_db(epic: str) -> List[int]:
    """
    Fetch valid leverage options for an epic from the marketInfo table.
    
    These leverage options come from Capital.com API and are stored when:
    - A new epic is added via the Data Manager
    - An epic's data is refreshed
    
    Returns:
        List of valid leverage values (e.g., [1, 2, 3, 4, 5, 10, 20])
        Falls back to default SHARES leverage if not found
    """
    DEFAULT_LEVERAGE_OPTIONS = [1, 2, 3, 4, 5, 10, 20]  # Default for SHARES
    
    try:
        db_config = get_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        # Fetch leverage options from marketInfo table
        cursor.execute(
            "SELECT leverageOptions FROM marketInfo WHERE epic = %s",
            (epic,)
        )
        result = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        if result and result[0]:
            # Parse JSON array from database
            leverage_options = json.loads(result[0]) if isinstance(result[0], str) else result[0]
            if isinstance(leverage_options, list) and len(leverage_options) > 0:
                print(f"📊 Loaded leverage options for {epic} from database: {leverage_options}", file=sys.stderr)
                return leverage_options
        
        print(f"⚠️  No leverage options found for {epic} in database, using defaults: {DEFAULT_LEVERAGE_OPTIONS}", file=sys.stderr)
        return DEFAULT_LEVERAGE_OPTIONS
        
    except Exception as e:
        print(f"⚠️  Error fetching leverage options for {epic}: {e}", file=sys.stderr)


def fetch_indicator_params_from_db(indicator_name: str) -> Dict[str, Any]:
    """
    Fetch indicator parameters from the indicatorParams database table.
    
    Returns a dict with:
    - 'universal': params that apply to all indicators (leverage, stop_loss)
    - 'specific': params specific to this indicator (period, threshold, etc.)
    
    Each param has: min, max, step, default, type, discrete_values (for list type)
    """
    result = {
        'universal': {},
        'specific': {},
    }
    
    try:
        db_config = get_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)
        
        # Fetch universal params (_universal)
        cursor.execute("""
            SELECT paramName, `minValue`, `maxValue`, `stepValue`, `defaultValue`, isEnabled, paramType, discreteValues
            FROM indicatorParams
            WHERE indicatorName = '_universal' AND isEnabled = 1
            ORDER BY sortOrder
        """)
        for row in cursor.fetchall():
            param_data = {
                'min': row['minValue'],
                'max': row['maxValue'],
                'step': row['stepValue'],
                'default': row['defaultValue'],
                'type': row['paramType'],
            }
            # For list type, parse the discrete values
            if row['paramType'] == 'list' and row['discreteValues']:
                try:
                    param_data['discrete_values'] = json.loads(row['discreteValues'])
                except json.JSONDecodeError:
                    param_data['discrete_values'] = []
            result['universal'][row['paramName']] = param_data
        
        # Fetch indicator-specific params
        cursor.execute("""
            SELECT paramName, `minValue`, `maxValue`, `stepValue`, `defaultValue`, isEnabled, paramType, discreteValues
            FROM indicatorParams
            WHERE indicatorName = %s AND isEnabled = 1
            ORDER BY sortOrder
        """, (indicator_name,))
        for row in cursor.fetchall():
            param_data = {
                'min': row['minValue'],
                'max': row['maxValue'],
                'step': row['stepValue'],
                'default': row['defaultValue'],
                'type': row['paramType'],
            }
            # For list type, parse the discrete values
            if row['paramType'] == 'list' and row['discreteValues']:
                try:
                    param_data['discrete_values'] = json.loads(row['discreteValues'])
                except json.JSONDecodeError:
                    param_data['discrete_values'] = []
            result['specific'][row['paramName']] = param_data
        
        cursor.close()
        conn.close()
        
        param_count = len(result['universal']) + len(result['specific'])
        if param_count > 0:
            print(f"[BatchRunner] Loaded {param_count} params for {indicator_name} from database (U:{len(result['universal'])}, S:{len(result['specific'])})", file=sys.stderr)
        
        return result
        
    except Exception as e:
        print(f"[BatchRunner] Error fetching indicator params for {indicator_name}: {e}", file=sys.stderr)
        return result


def get_backtest_settings() -> dict:
    """
    Fetch backtest settings from the settings table.
    
    Returns dict with:
        - process_recycle_count: int (default 500)
        - enable_process_recycling: bool (default True)
        - enable_queue_mode: bool (default True)
        - queue_batch_size: int (default 50)
        - stale_claim_timeout_minutes: int (default 10)
    """
    defaults = {
        'process_recycle_count': 500,
        'enable_process_recycling': True,
        'enable_queue_mode': True,
        'queue_batch_size': 50,
        'stale_claim_timeout_minutes': 10,
    }
    
    try:
        db_config = get_db_config()
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT `key`, value, valueType FROM settings WHERE category = 'backtest'"
        )
        rows = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        settings = defaults.copy()
        for key, value, value_type in rows:
            if key in settings:
                if value_type == 'number':
                    settings[key] = int(value)
                elif value_type == 'boolean':
                    settings[key] = value.lower() in ('true', '1', 'yes')
                else:
                    settings[key] = value
        
        print(f"📊 Loaded backtest settings: recycle_count={settings['process_recycle_count']}, recycling_enabled={settings['enable_process_recycling']}", file=sys.stderr)
        return settings
        
    except Exception as e:
        print(f"⚠️  Error fetching backtest settings: {e}", file=sys.stderr)
        print(f"    Using defaults: {defaults}", file=sys.stderr)
        return defaults


def load_strategy_registry(epic: str = None):
    """
    Load available strategies and their parameter ranges.
    
    Args:
        epic: The epic/symbol being tested. If provided, leverage options
              will be fetched from the database (from Capital.com API data).
    
    NOTE: This function now returns FALLBACK defaults only.
    The actual parameters are loaded from the indicatorParams table via 
    fetch_indicator_params_from_db() for each specific indicator.
    """
    # Fetch leverage options from database if epic is provided
    if epic:
        leverage_options = fetch_leverage_options_from_db(epic)
    else:
        # Fallback to default SHARES leverage options
        leverage_options = [1, 2, 3, 4, 5, 10, 20]
        print(f"⚠️  No epic provided, using default leverage options: {leverage_options}", file=sys.stderr)
    
    # FALLBACK defaults - used only if database lookup fails
    return {
        'leverage': leverage_options,
        'stop_loss': {'min': 2.0, 'max': 10.0, 'step': 0.5},
        
        # RSI parameters (fallback)
        'rsi_period': {'min': 7, 'max': 21, 'step': 1},
        'rsi_oversold': {'min': 20, 'max': 35, 'step': 5},
        'rsi_overbought': {'min': 65, 'max': 80, 'step': 5},
        
        # Moving Average parameters (fallback)
        'ma_short': {'min': 5, 'max': 20, 'step': 5},
        'ma_long': {'min': 20, 'max': 100, 'step': 10},
        
        # MACD parameters (fallback)
        'macd_fast': {'min': 8, 'max': 16, 'step': 2},
        'macd_slow': {'min': 20, 'max': 30, 'step': 2},
        'macd_signal': {'min': 7, 'max': 11, 'step': 2},
        
        # Bollinger Bands (fallback)
        'bb_period': {'min': 15, 'max': 25, 'step': 5},
        'bb_std': {'min': 1.5, 'max': 2.5, 'step': 0.5},
        
        # Stochastic (fallback)
        'stoch_k': {'min': 10, 'max': 20, 'step': 5},
        'stoch_d': {'min': 3, 'max': 5, 'step': 1},
        'stoch_oversold': {'min': 15, 'max': 25, 'step': 5},
        'stoch_overbought': {'min': 75, 'max': 85, 'step': 5},
    }


def get_indicator_params(indicator_name: str, param_ranges: Dict, epic: str = None) -> Dict[str, Any]:
    """
    Get relevant parameters for a specific indicator.
    
    Priority order:
    1. Database (indicatorParams table) - PRIMARY SOURCE
    2. Fallback to param_ranges dict if DB lookup fails
    
    Returns dict ready for generate_param_combinations()
    """
    # Try loading from database first
    db_params = fetch_indicator_params_from_db(indicator_name)
    
    relevant_params = {}
    
    # Universal params (leverage, stop_loss)
    if db_params['universal']:
        # Use database values for universal params
        for param_name, param_config in db_params['universal'].items():
            if param_name == 'leverage':
                # Leverage uses discrete_values (list type) from database
                if param_config.get('type') == 'list' and 'discrete_values' in param_config:
                    # Use the discrete values from DB (e.g., [1, 2, 3, 4, 5, 10, 20])
                    leverage_list = [int(v) for v in param_config['discrete_values']]
                else:
                    # Fallback: generate list from min/max/step
                    leverage_list = []
                    val = param_config['min']
                    while val <= param_config['max']:
                        leverage_list.append(int(val) if param_config['type'] == 'int' else val)
                        val += param_config['step'] if param_config['step'] > 0 else 1
                
                # Filter by epic-specific leverage if available
                if epic:
                    epic_leverage = fetch_leverage_options_from_db(epic)
                    # Only keep leverages that are both in our DB list AND supported by the epic
                    filtered_list = [l for l in leverage_list if l in epic_leverage]
                    if filtered_list:
                        leverage_list = filtered_list
                    # If no overlap, use whichever is smaller (safety)
                    elif epic_leverage:
                        leverage_list = [l for l in epic_leverage if l <= max(leverage_list)]
                        
                relevant_params['leverage'] = leverage_list
                print(f"[BatchRunner] Leverage values for {epic or 'default'}: {leverage_list}", file=sys.stderr)
            else:
                relevant_params[param_name] = {
                    'min': param_config['min'],
                    'max': param_config['max'],
                    'step': param_config['step'],
                }
    else:
        # Fallback to param_ranges for universal params
        if 'leverage' in param_ranges:
            relevant_params['leverage'] = param_ranges['leverage']
        if 'stop_loss' in param_ranges:
            relevant_params['stop_loss'] = param_ranges['stop_loss']
    
    # Indicator-specific params
    if db_params['specific']:
        # Use database values for indicator-specific params
        for param_name, param_config in db_params['specific'].items():
            # Check if this is a list-type param with discrete values
            if param_config.get('type') == 'list' and 'discrete_values' in param_config and param_config['discrete_values']:
                # Use the discrete values directly (like leverage)
                relevant_params[param_name] = param_config['discrete_values']
                print(f"      [DEBUG] Using discrete values for {param_name}: {param_config['discrete_values']}", file=sys.stderr)
            else:
                # Use min/max/step range
                relevant_params[param_name] = {
                    'min': param_config['min'],
                    'max': param_config['max'],
                    'step': param_config['step'],
                }
        print(f"📊 Using DB params for {indicator_name}: {list(db_params['specific'].keys())}", file=sys.stderr)
    else:
        # Fallback to old hardcoded mapping if not in database
        indicator_param_map = {
            'rsi_oversold': ['rsi_period', 'rsi_oversold'],
            'rsi_overbought': ['rsi_period', 'rsi_overbought'],
            'rsi_divergence': ['rsi_period'],
            'ma_crossover': ['ma_short', 'ma_long'],
            'ema_crossover': ['ma_short', 'ma_long'],
            'macd_crossover': ['macd_fast', 'macd_slow', 'macd_signal'],
            'macd_divergence': ['macd_fast', 'macd_slow', 'macd_signal'],
            'bb_breakout': ['bb_period', 'bb_std'],
            'bb_reversal': ['bb_period', 'bb_std'],
            'stoch_oversold': ['stoch_k', 'stoch_d', 'stoch_oversold'],
            'stoch_overbought': ['stoch_k', 'stoch_d', 'stoch_overbought'],
        }
        
        param_keys = indicator_param_map.get(indicator_name, [])
        for key in param_keys:
            if key in param_ranges:
                relevant_params[key] = param_ranges[key]
        
        if not param_keys:
            print(f"⚠️  No DB params and no fallback mapping for {indicator_name} - using only leverage/stop_loss", file=sys.stderr)
    
    return relevant_params


def translate_params_for_indicator(indicator_name: str, params: Dict) -> Dict:
    """Translate batch parameter names to indicator library format"""
    # Mapping from batch param names to indicator param names
    param_translation = {
        'rsi_oversold': {
            'rsi_period': 'period',
            'rsi_oversold': 'threshold',
        },
        'rsi_overbought': {
            'rsi_period': 'period',
            'rsi_overbought': 'threshold',
        },
        'rsi_bullish_cross_50': {
            'rsi_period': 'period',
        },
        'rsi_bearish_cross_50': {
            'rsi_period': 'period',
        },
        'macd_crossover': {
            'macd_fast': 'fast_period',
            'macd_slow': 'slow_period',
            'macd_signal': 'signal_period',
        },
        'bb_oversold': {
            'bb_period': 'period',
            'bb_std': 'num_std',
        },
        'bb_overbought': {
            'bb_period': 'period',
            'bb_std': 'num_std',
        },
    }
    
    translation_map = param_translation.get(indicator_name, {})
    translated = {}
    
    # ALL integer parameters from indicatorParams table (paramType='int')
    # These MUST be converted to int for TA-Lib functions
    integer_params = {
        # Period/window params
        'period', 'ma_period', 'rsi_period', 'bb_period', 'adx_period', 'kc_period',
        'fast_period', 'slow_period', 'signal_period', 'wma_period', 'anchor_period',
        'fastk_period', 'fastd_period', 'slowk_period', 'slowd_period',
        'macd_fast', 'macd_slow', 'roc1_period', 'roc2_period',
        'percentile_period', 'rank_period', 'streak_period',
        # Threshold/lookback params that are integers
        'lookback', 'adx_threshold', 'cross_level', 'dd_threshold',
        'rsi_threshold', 'threshold', 'ulcer_threshold',
        # Generic names
        'window', 'timeperiod', 'length', 'n', 'k', 'd',
    }
    
    # Patterns that indicate integer params (ending with _period, _window, _length, _threshold)
    integer_suffixes = ('_period', '_window', '_length', '_lookback')
    
    for batch_name, value in params.items():
        # Skip internal params (leverage, stop_loss, _param_hash)
        if batch_name in ['leverage', 'stop_loss', '_param_hash']:
            continue
        
        # Translate or keep original name
        indicator_param_name = translation_map.get(batch_name, batch_name)
        
        # Convert integer params (TA-Lib requirement)
        # Check exact match OR suffix pattern
        is_integer_param = (indicator_param_name in integer_params or 
                           any(indicator_param_name.endswith(suffix) for suffix in integer_suffixes))
        
        if is_integer_param and value is not None:
            translated[indicator_param_name] = int(value)
        else:
            translated[indicator_param_name] = value
    
    return translated


def run_backtest_with_params(df, epic, indicator_name, params, initial_balance, monthly_topup, investment_pct, direction, timing_config=None, stop_conditions=None, timeframe='5m', engine='original', crash_protection_enabled=False, hmh_enabled=False, hmh_stop_loss_offset=0):
    """
    Run a single backtest with given parameters.
    
    Args:
        engine: 'original' or 'fast' - fast engine is 4-20x faster with identical results
        hmh_enabled: Whether to use HMH (Hold Means Hold) mode - set new SL on HOLD instead of closing
        hmh_stop_loss_offset: Stop loss adjustment for HMH (0=original SL%, -1=SL-1%, -2=SL-2%)
    """
    try:
        # Extract leverage and stop_loss
        leverage = params.get('leverage', 4)
        stop_loss = params.get('stop_loss', 3.5)
        
        # Translate parameter names for indicator library
        indicator_params = translate_params_for_indicator(indicator_name, params)
        
        # Use fast engine if requested and available
        if engine == 'fast' and FAST_ENGINE_AVAILABLE:
            result = run_single_backtest_fast(
                df=df,
                epic=epic,
                indicator_name=indicator_name,
                indicator_params=indicator_params,
                leverage=leverage,
                stop_loss_pct=stop_loss,
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                investment_pct=investment_pct,
                direction=direction,
                timing_config=timing_config,
                timeframe=timeframe,
                crash_protection_enabled=crash_protection_enabled,
                # Note: Fast engine doesn't support HMH yet - falls back to non-HMH
            )
        else:
            # Run backtest using the original function with HMH support
            result = run_single_backtest(
                df=df,
                epic=epic,
                indicator_name=indicator_name,
                indicator_params=indicator_params,
                leverage=leverage,
                stop_loss_pct=stop_loss,
                initial_balance=initial_balance,
                monthly_topup=monthly_topup,
                investment_pct=investment_pct,
                direction=direction,
                timing_config=timing_config,
                stop_conditions=stop_conditions,
                timeframe=timeframe,
                crash_protection_enabled=crash_protection_enabled,
                hmh_enabled=hmh_enabled,
                hmh_stop_loss_offset=hmh_stop_loss_offset if hmh_stop_loss_offset is not None else 0,
            )
        
        return result
    except Exception as e:
        print(f"ERROR: Backtest failed: {str(e)}", file=sys.stderr)
        return {
            'totalReturn': -100,
            'sharpeRatio': -999,
            'winRate': 0,
            'maxDrawdown': -100,
            'totalTrades': 0,
            'winningTrades': 0,
            'losingTrades': 0,
            'finalBalance': 0,
        }


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("ERROR: No configuration provided", file=sys.stderr)
        sys.exit(1)
    
    # Global declarations for signal handler cleanup
    global _pool, _shm_numeric, _shm_meta
    
    try:
        # Load backtest settings for process recycling
        backtest_settings = get_backtest_settings()
        process_recycle_count = backtest_settings['process_recycle_count']
        enable_process_recycling = backtest_settings['enable_process_recycling']
        
        # Parse configuration from command line
        config_json = sys.argv[1]
        config = json.loads(config_json)
        
        # Extract configuration
        db_path = config['db_path']
        epic = config['epic']
        start_date = config['start_date']
        end_date = config['end_date']
        indicators = config['indicators']
        num_tests = config['num_tests']
        initial_balance = config['initial_balance']
        monthly_topup = config['monthly_topup']
        investment_pct = config['investment_pct']
        direction = config.get('direction', 'both')
        optimization_strategy = config.get('optimization_strategy', 'random')
        stop_conditions = config.get('stop_conditions', {})
        run_id = config.get('run_id')  # For pause/resume
        data_source = config.get('data_source', 'capital')  # Always use capital.com data (UTC)
        
        # Configurable worker timeout (default 60s for time-based, can be increased via UI)
        worker_timeout_seconds = config.get('worker_timeout_seconds', 60)
        print(f"⏱️  Worker timeout: {worker_timeout_seconds}s", file=sys.stderr)
        
        # Engine selection: 'original' or 'fast' (fast engine is 4-20x faster, 100% identical results)
        engine = config.get('engine', 'fast' if FAST_ENGINE_AVAILABLE else 'original')
        if engine == 'fast' and not FAST_ENGINE_AVAILABLE:
            print("⚠️ Fast engine requested but not available, falling back to original", file=sys.stderr)
            engine = 'original'
        print(f"🔧 Backtest engine: {engine.upper()}", file=sys.stderr)

        # Handle timeframe configuration
        timeframe_config = config.get('timeframe_config', {'mode': 'default', 'timeframes': ['5m']})
        timeframe_mode = timeframe_config.get('mode', 'default')
        timeframes = timeframe_config.get('timeframes', ['5m'])
        
        # Memory management
        free_memory = config.get('free_memory', False)
        
        # Performance optimization
        test_order = config.get('test_order', 'sequential')
        requested_cores = config.get('parallel_cores', 1)
        
        # Duplicate handling
        rerun_duplicates = config.get('rerun_duplicates', False)
        
        # VERSION 3: Enhanced signal-based config (multi-indicator, trust matrix)
        enhanced_signal_config = config.get('enhanced_signal_config')
        is_enhanced_signal_mode = enhanced_signal_config and enhanced_signal_config.get('enabled', False)
        
        if is_enhanced_signal_mode:
            print(f"\n🚀 VERSION 3: Enhanced Signal-Based Mode Detected!", file=sys.stderr)
            print(f"   Entry indicators: {enhanced_signal_config.get('entryIndicators', [])}", file=sys.stderr)
            print(f"   Exit indicators: {enhanced_signal_config.get('exitIndicators', [])}", file=sys.stderr)
            print(f"   Allow short: {enhanced_signal_config.get('allowShort', False)}", file=sys.stderr)
            print(f"   Reverse on signal: {enhanced_signal_config.get('reverseOnSignal', False)}", file=sys.stderr)
            print(f"   Stop loss mode: {enhanced_signal_config.get('stopLossMode', 'auto')}", file=sys.stderr)
            print(f"   Use trust matrix: {enhanced_signal_config.get('useTrustMatrix', True)}", file=sys.stderr)
        
        # Dynamic memory-based core allocation
        if requested_cores > 1:
            import psutil
            available_memory_gb = psutil.virtual_memory().available / (1024**3)
            
            # Estimate memory per worker: ~200MB (safety margin over measured 140MB)
            # Previous estimate of 400MB was way too conservative
            memory_per_worker_gb = 0.2
            
            # Reserve 1GB for system, calculate max safe workers
            usable_memory_gb = max(0.5, available_memory_gb - 1.0)
            max_safe_workers = int(usable_memory_gb / memory_per_worker_gb)
            
            if max_safe_workers < requested_cores:
                print(f"\n⚠️  Memory constraint detected:", file=sys.stderr)
                print(f"   Available: {available_memory_gb:.2f}GB", file=sys.stderr)
                print(f"   Requested: {requested_cores} cores ({requested_cores * memory_per_worker_gb:.2f}GB needed)", file=sys.stderr)
                print(f"   Reducing to: {max_safe_workers} cores to prevent OOM kills", file=sys.stderr)
                parallel_cores = max(1, max_safe_workers)
            else:
                parallel_cores = requested_cores
        else:
            parallel_cores = requested_cores
        
        print(f"Timeframe mode: {timeframe_mode}", file=sys.stderr)
        print(f"Selected timeframes: {timeframes}", file=sys.stderr)
        
        # Check for checkpoint (resume from pause)
        checkpoint = load_checkpoint(run_id) if run_id else None
        start_index = 0
        if checkpoint:
            start_index = checkpoint.get('last_index', 0)
            print(f"\n↻ Resuming from checkpoint at indicator {start_index}/{len(indicators)}", file=sys.stderr)
            indicators = indicators[start_index:]  # Skip completed indicators
        
        # Clear stale queue entries from previous runs BEFORE starting
        db_config = get_db_config()
        clear_stale_queue_entries(db_config)
        
        print(f"Starting batch optimization:", file=sys.stderr)
        print(f"  Epic: {epic}", file=sys.stderr)
        print(f"  Date range: {start_date} to {end_date}", file=sys.stderr)
        print(f"  Indicators: {', '.join(indicators)}", file=sys.stderr)
        print(f"  Tests per indicator: {num_tests}", file=sys.stderr)
        print(f"  Strategy: {optimization_strategy}", file=sys.stderr)
        
        # Load parameter ranges (pass epic to fetch valid leverage options from database)
        all_param_ranges = load_strategy_registry(epic)
        
        # Extract crash protection mode
        crash_protection_mode = config.get('crash_protection_mode', 'without')
        
        # Extract HMH (Hold Means Hold) configuration
        # HMH trails stop loss on HOLD instead of closing position
        # hmh_mode can be: 'off' or list of offsets like [0, -1, -2]
        hmh_config = config.get('hmh_config', {})
        hmh_enabled = hmh_config.get('enabled', False)
        hmh_offsets = hmh_config.get('offsets', [0])  # Default to original SL if enabled
        
        # Determine HMH modes to test
        if hmh_enabled and hmh_offsets:
            # Test each HMH offset variation
            hmh_modes_to_test = [(True, offset) for offset in hmh_offsets]
        else:
            # HMH disabled - single test with hmh_enabled=False
            hmh_modes_to_test = [(False, None)]
        
        # Calculate multipliers
        timeframe_multiplier = len(timeframes) if timeframe_mode == 'multiple' else 1
        crash_protection_multiplier = 2 if crash_protection_mode == 'both' else 1
        hmh_multiplier = len(hmh_modes_to_test)
        
        # ============================================================
        # ACCURATE TOTAL CALCULATION - Count actual parameter combinations
        # ============================================================
        print(f"\n📊 Calculating actual test count for {len(indicators)} indicators...", file=sys.stderr)
        actual_total_tests = 0
        indicator_test_counts = {}  # Track per-indicator for debugging
        
        for ind_name in indicators:
            try:
                # Get param ranges for this indicator (same as during actual run)
                ind_param_ranges = get_indicator_params(ind_name, all_param_ranges, epic=epic)
                ind_param_space = ParameterSpace(ind_param_ranges)
                ind_combos = ind_param_space.get_total_combinations()
                
                # Cap at num_tests (user's requested sample count)
                tests_for_indicator = min(num_tests, ind_combos)
                
                # Multiply by timeframes and crash protection modes
                total_for_indicator = tests_for_indicator * timeframe_multiplier * crash_protection_multiplier
                
                indicator_test_counts[ind_name] = {
                    'combos': ind_combos,
                    'capped': tests_for_indicator,
                    'total': total_for_indicator
                }
                actual_total_tests += total_for_indicator
            except Exception as e:
                # Fallback to num_tests if param calculation fails
                fallback = num_tests * timeframe_multiplier * crash_protection_multiplier
                indicator_test_counts[ind_name] = {'error': str(e), 'fallback': fallback}
                actual_total_tests += fallback
                print(f"  ⚠️ {ind_name}: using fallback count ({e})", file=sys.stderr)
        
        total_tests = actual_total_tests
        completed_tests = 0
        
        # Send accurate total to TypeScript immediately (parsed by backtest_bridge_v2.ts)
        print(f"TOTAL_TESTS:{total_tests}", flush=True)
        
        print(f"✅ Actual total tests: {total_tests} ({len(indicators)} indicators × {timeframe_multiplier} timeframes × {crash_protection_multiplier} crash modes × {hmh_multiplier} HMH modes)", file=sys.stderr)
        print(f"   (Based on actual parameter combinations, capped at {num_tests} per indicator)", file=sys.stderr)
        
        # Determine which timeframes to iterate through
        timeframes_to_test = timeframes if timeframe_mode == 'multiple' else [timeframes[0] if timeframes else '5m']
        
        # Determine crash protection modes to test
        if crash_protection_mode == 'both':
            crash_modes_to_test = [False, True]  # Run without, then with
        elif crash_protection_mode == 'with':
            crash_modes_to_test = [True]
        else:  # 'without'
            crash_modes_to_test = [False]
        
        # Load epic data ONCE and cache for all indicators (memory optimization)
        print(f"\n📦 Loading epic data for all timeframes (source: {data_source})...", file=sys.stderr)
        epic_data_cache = {}
        for timeframe in timeframes_to_test:
            print(f"  Loading {epic} {timeframe} data from {data_source}...", file=sys.stderr)
            epic_data_cache[timeframe] = load_epic_data(epic, start_date, end_date, timeframe, data_source)
            print(f"  ✓ Loaded {len(epic_data_cache[timeframe])} {timeframe} candles", file=sys.stderr)
        print(f"✅ Epic data cached for {len(epic_data_cache)} timeframe(s)", file=sys.stderr)
        
        # Check if timing mode requires fake 5-min candles from 1-min data
        timing_config = config.get('timing_config', {})
        timing_mode = timing_config.get('mode', 'MarketClose') if timing_config else 'MarketClose'
        
        print(f"📋 Timing config received: {timing_config}", file=sys.stderr)
        print(f"📋 Timing mode: {timing_mode}", file=sys.stderr)
        
        from fake_candle_builder import get_timing_modes_requiring_fake_candles, add_fake_5min_closes_to_dataframe
        
        fake_candle_modes = get_timing_modes_requiring_fake_candles()
        print(f"📋 Fake candle modes: {fake_candle_modes}", file=sys.stderr)
        
        # Skip fake candle loading for Enhanced Signal-Based mode (indicator-based)
        # Indicator-based backtests use candle close, not timing-based decisions
        if is_enhanced_signal_mode:
            print(f"⏭️  Skipping fake candle loading - Enhanced Signal-Based mode uses candle close, not timing", file=sys.stderr)
        elif timing_mode in fake_candle_modes:
            print(f"\n🕐 Timing mode '{timing_mode}' requires fake 5-min candles from 1-min data", file=sys.stderr)
            print(f"  Loading {epic} 1m data to build fake candles...", file=sys.stderr)
            print(f"  [DEBUG] load_epic_data params: epic={epic}, start={start_date}, end={end_date}, tf=1m, src={data_source}", file=sys.stderr)
            
            # Load 1-minute data
            df_1m = load_epic_data(epic, start_date, end_date, '1m', data_source)
            
            if len(df_1m) > 0:
                print(f"  ✓ Loaded {len(df_1m)} 1m candles", file=sys.stderr)
                
                # Ensure timestamp column exists for 1m data
                if 'snapshotTime' in df_1m.columns and 'timestamp' not in df_1m.columns:
                    df_1m['timestamp'] = df_1m['snapshotTime']
                
                # Add fake_5min_close to each 5m timeframe
                for tf in timeframes_to_test:
                    if tf == '5m':
                        print(f"  Building fake_5min_close for {tf} candles...", file=sys.stderr)
                        epic_data_cache[tf] = add_fake_5min_closes_to_dataframe(
                            epic_data_cache[tf], 
                            df_1m, 
                            timing_mode
                        )
                
                # Discard 1m data to free memory
                del df_1m
                print(f"  ✓ 1m data discarded after building fake candles", file=sys.stderr)
            else:
                print(f"  ⚠️ No 1m data available - fake candle mode will use fallback (second-to-last 5m candle)", file=sys.stderr)
        
        print(f"", file=sys.stderr)  # Empty line for readability
        
        print(f"🛠️  Performance: {parallel_cores} core(s), {test_order} order, free_memory={free_memory}\n", file=sys.stderr)
        
        # ===================================================================
        # VERSION 3: ENHANCED SIGNAL-BASED BACKTEST MODE
        # Test ALL entry/exit indicator PAIRS with parameter variations.
        # Uses same hash/dedup/shotgun approach as timing-based mode.
        # Total tests = entry_count × exit_count × num_samples (minus duplicates)
        # ===================================================================
        if is_enhanced_signal_mode:
            print(f"\n🚀 VERSION 5: Signal-Based Multi-Test Mode!", file=sys.stderr)
            print(f"   Tests entry/exit indicator pairs with parameter variations", file=sys.stderr)

            # Load trust matrix from database
            from trust_matrix import load_trust_matrix, save_trust_matrix
            from backtest_runner import run_enhanced_signal_backtest

            # Get the first (only) timeframe for signal-based mode
            timeframe = timeframes_to_test[0] if timeframes_to_test else '5m'
            df = epic_data_cache.get(timeframe)

            if df is None or len(df) == 0:
                print(f"ERROR: No data loaded for {epic} {timeframe}", file=sys.stderr)
                sys.exit(1)

            # Load existing trust matrix (if any)
            trust_matrix = load_trust_matrix(epic, timeframe)
            print(f"  Loaded {len(trust_matrix)} existing trust pairs", file=sys.stderr)

            # Extract enhanced config params
            entry_indicators = enhanced_signal_config.get('entryIndicators', [])
            exit_indicators = enhanced_signal_config.get('exitIndicators', [])
            entry_samples = enhanced_signal_config.get('entrySamplesPerIndicator', 5)
            exit_samples = enhanced_signal_config.get('exitSamplesPerIndicator', 5)
            stop_loss_mode = enhanced_signal_config.get('stopLossMode', 'auto')
            stop_loss_pct = enhanced_signal_config.get('stopLossPct', 2.0)
            margin_closeout_level = enhanced_signal_config.get('marginCloseoutLevel', 55.0)
            position_size_pct = enhanced_signal_config.get('positionSizePct', 50.0)
            min_balance_threshold = enhanced_signal_config.get('minBalanceThreshold', 10.0)
            default_leverage = enhanced_signal_config.get('defaultLeverage', 5)
            
            print(f"  Entry indicators: {entry_indicators} (x{entry_samples} samples each)", file=sys.stderr)
            print(f"  Exit indicators: {exit_indicators} (x{exit_samples} samples each)", file=sys.stderr)
            print(f"  Position size: {position_size_pct}%", file=sys.stderr)
            print(f"  Stop loss mode: {stop_loss_mode} (closeout={margin_closeout_level}%)", file=sys.stderr)
            print(f"  Leverage: {default_leverage}x", file=sys.stderr)
            
            # =================================================================
            # GENERATE TEST COMBINATIONS WITH PARAMETER VARIATIONS
            # =================================================================
            print(f"\n📋 Generating test combinations...", file=sys.stderr)
            
            # Get parameter ranges from database (same as timing-based mode)
            all_param_ranges = load_strategy_registry(epic)
            
            # Generate entry indicator + params combinations
            entry_combos = []
            for entry_ind in entry_indicators:
                param_ranges = get_indicator_params(entry_ind, all_param_ranges, epic=epic)
                if param_ranges:
                    param_space = ParameterSpace(param_ranges)
                    sampled_params = param_space.sample_unique(entry_samples)
                    for params in sampled_params:
                        entry_combos.append((entry_ind, params))
                else:
                    # No param ranges, use default
                    entry_combos.append((entry_ind, {}))
            
            # Generate exit indicator + params combinations
            exit_combos = []
            for exit_ind in exit_indicators:
                param_ranges = get_indicator_params(exit_ind, all_param_ranges, epic=epic)
                if param_ranges:
                    param_space = ParameterSpace(param_ranges)
                    sampled_params = param_space.sample_unique(exit_samples)
                    for params in sampled_params:
                        exit_combos.append((exit_ind, params))
                else:
                    # No param ranges, use default
                    exit_combos.append((exit_ind, {}))
            
            print(f"  Generated {len(entry_combos)} entry combinations", file=sys.stderr)
            print(f"  Generated {len(exit_combos)} exit combinations", file=sys.stderr)
            
            # Generate all test specs (entry × exit)
            all_test_specs = []
            for entry_ind, entry_params in entry_combos:
                for exit_ind, exit_params in exit_combos:
                    all_test_specs.append({
                        'entry_indicator': entry_ind,
                        'entry_params': entry_params,
                        'exit_indicator': exit_ind,
                        'exit_params': exit_params,
                    })
            
            # Shuffle for random order
            random.shuffle(all_test_specs)
            
            total_tests = len(all_test_specs)
            print(f"\n🎯 Total test combinations: {total_tests}", file=sys.stderr)
            print(f"   ({len(entry_combos)} entries × {len(exit_combos)} exits)", file=sys.stderr)
            print(f"\nTOTAL_TESTS:{total_tests}", flush=True)
            
            # =================================================================
            # EXECUTE TESTS IN PARALLEL (using multiprocessing pool)
            # =================================================================
            print(f"\n🚀 Starting parallel execution with {parallel_cores} cores...", file=sys.stderr)
            
            # Import multiprocessing for parallel execution
            import multiprocessing as mp
            
            # Prepare shared data for workers
            # Convert DataFrame to pickle for multiprocessing
            import pickle
            df_pickle = pickle.dumps(df)
            
            # Create test specs with all needed data
            worker_specs = []
            for i, spec in enumerate(all_test_specs):
                worker_specs.append({
                    'test_num': i + 1,
                    'total_tests': total_tests,
                    'df_pickle': df_pickle,
                    'epic': epic,
                    'entry_indicator': spec['entry_indicator'],
                    'entry_params': spec['entry_params'],
                    'exit_indicator': spec['exit_indicator'],
                    'exit_params': spec['exit_params'],
                    'initial_balance': initial_balance,
                    'monthly_topup': monthly_topup,
                    'position_size_pct': position_size_pct,
                    'stop_loss_mode': stop_loss_mode,
                    'stop_loss_pct': stop_loss_pct,
                    'margin_closeout_level': margin_closeout_level,
                    'min_balance_threshold': min_balance_threshold,
                    'default_leverage': default_leverage,
                    'timeframe': timeframe,
                    'direction': direction,
                })
            
            # Run tests in parallel using multiprocessing pool
            completed = 0
            results_count = 0
            best_result = None
            
            try:
                with mp.Pool(processes=parallel_cores) as pool:
                    for result in pool.imap_unordered(_signal_based_worker, worker_specs):
                        completed += 1
                        
                        if result and result.get('status') != 'error':
                            # Output result
                            print(f"RESULT:{json.dumps(result)}", flush=True)
                            results_count += 1
                            
                            # Track best
                            if not best_result or result.get('finalBalance', 0) > best_result.get('finalBalance', 0):
                                best_result = result
                            
                            # Update trust matrix
                            entry_ind = result.get('entry_indicator')
                            exit_ind = result.get('exit_indicator')
                            if entry_ind and exit_ind and result.get('totalTrades', 0) > 0:
                                from trust_matrix import update_trust_matrix
                                trade_data = {
                                    'pnl': result.get('finalBalance', 0) - initial_balance,
                                    'hold_bars': result.get('totalTrades', 1),
                                    'entry_leverage': default_leverage,
                                    'stop_loss_pct': stop_loss_pct,
                                }
                                trust_matrix = update_trust_matrix(
                                    trust_matrix, entry_ind, exit_ind, trade_data, was_winner=True
                                )
                        else:
                            error_msg = result.get('error', 'Unknown error') if result else 'Worker returned None'
                            print(f"  Test failed: {error_msg}", file=sys.stderr)
                        
                        print(f"PROGRESS:{completed}/{total_tests}", flush=True)
                        
                        # Check for pause every 10 tests
                        if completed % 10 == 0:
                            print(f"  Progress: {completed}/{total_tests} ({100*completed//total_tests}%)", file=sys.stderr)
                            
                            # PAUSE CHECK: Poll database for pause status
                            if run_id:
                                status = check_run_status(run_id)
                                if status == 'paused':
                                    print(f"\n⏸️ PAUSE detected in signal-based mode at {completed}/{total_tests}", file=sys.stderr)
                                    pool.terminate()
                                    pool.join()
                                    # Save trust matrix before exit
                                    save_trust_matrix(epic, timeframe, trust_matrix)
                                    checkpoint_data = {'completed_tests': completed, 'total_tests': total_tests}
                                    save_checkpoint(run_id, checkpoint_data)
                                    print(f"PAUSE_CHECKPOINT:{completed}", flush=True)
                                    sys.exit(101)  # Exit code 101 = paused
                            
            except Exception as e:
                print(f"Pool error: {e}", file=sys.stderr)
                import traceback
                print(f"Traceback:\n{traceback.format_exc()}", file=sys.stderr)
            
            # Save trust matrix
            save_trust_matrix(epic, timeframe, trust_matrix)
            print(f"\n✅ Saved {len(trust_matrix)} trust pairs to database", file=sys.stderr)
            
            print(f"\n" + "="*60, file=sys.stderr)
            print(f"✅ Signal-Based Testing Complete!", file=sys.stderr)
            print(f"="*60, file=sys.stderr)
            print(f"  Tests Completed: {results_count}/{total_tests}", file=sys.stderr)
            if best_result:
                print(f"  Best Result: ${best_result.get('finalBalance', 0):.2f}", file=sys.stderr)
                print(f"  Best Strategy: {best_result.get('indicatorName', 'N/A')}", file=sys.stderr)
            
            print(f"\n✅ Batch optimization complete!", file=sys.stderr)
            sys.exit(0)
        
        # Flag to track if shotgun mode completed (skip sequential processing)
        shotgun_mode_completed = False
        
        # ===================================================================
        # SHOTGUN MODE: When test_order='random', collect ALL tests from ALL
        # indicators first, then shuffle them together, then execute.
        # This gives TRUE randomization across indicators.
        # ===================================================================
        if test_order == 'random':
            print(f"\n🎲 SHOTGUN MODE: Collecting tests from all indicators before randomized execution...", file=sys.stderr)
            
            # Phase 1: Collect all test specifications from all indicators
            all_test_specs = []
            db_config = get_db_config()
            
            # =================================================================
            # OPTIMIZATION: Load ALL tested hashes ONCE before the indicator loop
            # This replaces N database queries (one per indicator) with just 1
            # =================================================================
            if not rerun_duplicates:
                print(f"  🔍 Loading all tested hashes from database (one-time load)...", file=sys.stderr)
                all_existing_hashes = load_all_tested_hashes(epic)
                print(f"  ✓ Loaded {len(all_existing_hashes):,} existing hashes - no more DB queries needed!", file=sys.stderr)
            else:
                all_existing_hashes = set()  # Empty set - will run everything
            
            for indicator_name in indicators:
                print(f"  📋 Collecting tests for {indicator_name}...", file=sys.stderr)
                
                param_ranges = get_indicator_params(indicator_name, all_param_ranges, epic=epic)
                param_space = ParameterSpace(param_ranges)
                
                for crash_protection_enabled in crash_modes_to_test:
                    for hmh_enabled_val, hmh_offset in hmh_modes_to_test:
                        for current_timeframe in timeframes_to_test:
                            # Generate all possible combinations for this indicator
                            total_possible = param_space.get_total_combinations()
                            
                            if not rerun_duplicates:
                                # Generate hashes and filter against pre-loaded cache (NO DB query!)
                                all_combos = param_space._get_all_combinations()
                                
                                # Filter to available combinations using cached hashes
                                available_combos = []
                                for combo in all_combos:
                                    indicator_params = {k: v for k, v in combo.items() if k not in ['leverage', 'stop_loss']}
                                    leverage = combo.get('leverage', 4)
                                    stop_loss = combo.get('stop_loss', 3.5)
                                    
                                    full_hash = generate_param_hash(
                                        epic=epic,
                                        start_date=start_date,
                                        end_date=end_date,
                                        direction=direction,
                                        initial_balance=initial_balance,
                                        monthly_topup=monthly_topup,
                                        investment_pct=investment_pct,
                                        timing_config=config.get('timing_config', {}),
                                        data_source=data_source,
                                        indicator_name=indicator_name,
                                        indicator_params=indicator_params,
                                        leverage=leverage,
                                        stop_loss=stop_loss,
                                        timeframe=current_timeframe,
                                        crash_protection_enabled=crash_protection_enabled,
                                        hmh_enabled=hmh_enabled_val,
                                        hmh_stop_loss_offset=hmh_offset
                                    )
                                    
                                    # Check against pre-loaded cache (instant - no DB query!)
                                    if full_hash not in all_existing_hashes:
                                        combo['_param_hash'] = full_hash
                                        available_combos.append(combo)
                                
                                if len(available_combos) == 0:
                                    continue
                                
                                # Sample from available combinations (respecting numSamples)
                                if num_tests >= len(available_combos):
                                    sampled_params = available_combos
                                else:
                                    sampled_params = random.sample(available_combos, num_tests)
                            else:
                                # Rerun duplicates mode - just sample
                                sampled_params = param_space.sample_unique(min(num_tests, total_possible))
                                for p in sampled_params:
                                    indicator_params = {k: v for k, v in p.items() if k not in ['leverage', 'stop_loss']}
                                    p['_param_hash'] = generate_param_hash(
                                        epic=epic, start_date=start_date, end_date=end_date,
                                        direction=direction, initial_balance=initial_balance,
                                        monthly_topup=monthly_topup, investment_pct=investment_pct,
                                        timing_config=config.get('timing_config', {}),
                                        data_source=data_source, indicator_name=indicator_name,
                                        indicator_params=indicator_params,
                                        leverage=p.get('leverage', 4), stop_loss=p.get('stop_loss', 3.5),
                                        timeframe=current_timeframe,
                                        crash_protection_enabled=crash_protection_enabled,
                                        hmh_enabled=hmh_enabled_val,
                                        hmh_stop_loss_offset=hmh_offset
                                    )
                            
                            # Add to master list with context
                            for params in sampled_params:
                                all_test_specs.append({
                                    'indicator_name': indicator_name,
                                    'params': params,
                                    'crash_protection_enabled': crash_protection_enabled,
                                    'hmh_enabled': hmh_enabled_val,
                                    'hmh_stop_loss_offset': hmh_offset,
                                    'timeframe': current_timeframe,
                                })
                
                collected_for_ind = sum(1 for t in all_test_specs if t['indicator_name'] == indicator_name)
                print(f"    ✓ {indicator_name}: {collected_for_ind} tests collected", file=sys.stderr)
            
            # Phase 2: Shuffle all tests together (TRUE shotgun!)
            print(f"\n🔀 Shuffling {len(all_test_specs)} tests across all indicators...", file=sys.stderr)
            random.shuffle(all_test_specs)
            
            # Update total_tests to reflect actual collected tests
            total_tests = len(all_test_specs)
            print(f"TOTAL_TESTS:{total_tests}", flush=True)
            print(f"✅ Ready to execute {total_tests} tests in random order\n", file=sys.stderr)
            
            # Phase 3: Execute tests in shuffled order (PARALLEL!)
            if parallel_cores > 1 and len(all_test_specs) > 0:
                print(f"  ⚡ SHOTGUN PARALLEL MODE: Using {parallel_cores} workers", file=sys.stderr)
                
                # Find unique timeframes in all_test_specs
                unique_timeframes = list(set(t['timeframe'] for t in all_test_specs))
                print(f"  📊 Timeframes in use: {unique_timeframes}", file=sys.stderr)
                
                import multiprocessing as mp
                import pickle
                
                # Create shared memory for each unique timeframe
                timeframe_shm_map = {}
                for tf in unique_timeframes:
                    df = epic_data_cache[tf]
                    
                    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
                    datetime_cols = df.select_dtypes(include=['datetime64']).columns.tolist()
                    object_cols = df.select_dtypes(include=['object']).columns.tolist()
                    
                    numeric_data = df[numeric_cols].values.astype(np.float64, copy=False)
                    
                    metadata = {
                        'numeric_cols': numeric_cols,
                        'datetime_cols': datetime_cols,
                        'object_cols': object_cols,
                        'datetime_data': {col: df[col].values for col in datetime_cols},
                        'object_data': {col: df[col].values for col in object_cols},
                        'index': df.index.tolist()
                    }
                    metadata_bytes = pickle.dumps(metadata)
                    
                    shm_numeric = shared_memory.SharedMemory(create=True, size=numeric_data.nbytes)
                    shared_array = np.ndarray(numeric_data.shape, dtype=np.float64, buffer=shm_numeric.buf)
                    shared_array[:] = numeric_data[:]
                    
                    shm_meta = shared_memory.SharedMemory(create=True, size=len(metadata_bytes))
                    shm_meta.buf[:len(metadata_bytes)] = metadata_bytes
                    
                    timeframe_shm_map[tf] = {
                        'shm_numeric': shm_numeric,
                        'shm_meta': shm_meta,
                        'meta_size': len(metadata_bytes),
                        'shape': numeric_data.shape,
                    }
                    print(f"    💾 Shared memory for {tf}: {shm_numeric.name} ({numeric_data.nbytes / 1024 / 1024:.1f}MB)", file=sys.stderr)
                
                try:
                    # Build worker arguments generator
                    def shotgun_worker_args_generator():
                        for i, test_spec in enumerate(all_test_specs):
                            tf = test_spec['timeframe']
                            shm_info = timeframe_shm_map[tf]
                            yield (
                                test_spec['params'],
                                i + 1,  # test_num
                                shm_info['shm_numeric'].name,
                                shm_info['shm_meta'].name,
                                shm_info['meta_size'],
                                shm_info['shape'],
                                epic,
                                test_spec['indicator_name'],
                                initial_balance,
                                monthly_topup,
                                investment_pct,
                                direction,
                                config.get('timing_config'),
                                stop_conditions,
                                tf,
                                test_spec['crash_protection_enabled'],
                                engine,  # 'original' or 'fast'
                                worker_timeout_seconds,  # Configurable timeout
                                test_spec.get('hmh_enabled', False),  # HMH (Hold Means Hold) mode
                                test_spec.get('hmh_stop_loss_offset', 0),  # HMH stop loss offset
                            )
                    
                    # ====================================================================
                    # WINDOWS FIX: Don't use maxtasksperchild - causes silent worker death
                    # 
                    # On Windows, maxtasksperchild causes workers to die silently when they
                    # hit their task limit. The replacement workers often fail to spawn.
                    # 
                    # Instead, we let workers run indefinitely and rely on:
                    # 1. Python's GC to clean up between tests
                    # 2. The test itself to release memory after completion
                    # 3. If needed, manual GC every 100 tests (already implemented)
                    # ====================================================================
                    print(f"  🚀 Workers run indefinitely (no maxtasksperchild - Windows fix)", file=sys.stderr)
                    
                    with mp.Pool(processes=parallel_cores) as pool:
                        _pool = pool
                        print(f"  📦 Starting parallel execution with chunksize=1 for {total_tests} tests", file=sys.stderr)
                        result_iterator = pool.imap_unordered(_parallel_test_worker, shotgun_worker_args_generator(), chunksize=1)
                        
                        successful_count = 0
                        failed_count = 0
                        worker_crash_count = 0
                        received_test_nums = set()  # Track which test numbers we've received results for
                        
                        # Wrap result iteration in robust error handling
                        # This catches BrokenPipeError and other worker crash scenarios
                        try:
                            for worker_result in result_iterator:
                                try:
                                    if worker_result is None:
                                        # Worker returned None - shouldn't happen but handle gracefully
                                        print(f"  ⚠️ Worker returned None result - skipping", file=sys.stderr)
                                        worker_crash_count += 1
                                        continue
                                    
                                    test_num = worker_result.get('test_num', -1)
                                    received_test_nums.add(test_num)
                                    
                                    if worker_result['success']:
                                        result = worker_result['result']
                                        params = worker_result['params']
                                        
                                        # Find the test_spec to get indicator_name and other info
                                        test_spec = all_test_specs[test_num - 1]
                                        indicator_name = test_spec['indicator_name']
                                        current_timeframe = test_spec['timeframe']
                                        crash_protection_enabled = test_spec['crash_protection_enabled']
                                        
                                        completed_tests += 1
                                        successful_count += 1
                                        
                                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                                        
                                        indicator_params_out = {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']}
                                        
                                        # Get HMH settings from test_spec
                                        hmh_enabled = test_spec.get('hmh_enabled', False)
                                        hmh_stop_loss_offset = test_spec.get('hmh_stop_loss_offset', None)
                                        
                                        result_output = {
                                            'testNum': test_num,
                                            'indicatorName': indicator_name,
                                            'indicatorParams': indicator_params_out,
                                            'leverage': params.get('leverage', 4),
                                            'stopLoss': params.get('stop_loss', 3.5),
                                            'timeframe': current_timeframe,
                                            'crashProtectionEnabled': crash_protection_enabled,
                                            'hmhEnabled': hmh_enabled,
                                            'hmhStopLossOffset': hmh_stop_loss_offset,
                                            'initialBalance': initial_balance,
                                            'finalBalance': result.get('finalBalance', 0),
                                            'totalContributions': result.get('totalContributions', 0),
                                            'totalReturn': result.get('totalReturn', 0),
                                            'totalTrades': result.get('totalTrades', 0),
                                            'winningTrades': result.get('winningTrades', 0),
                                            'losingTrades': result.get('losingTrades', 0),
                                            'winRate': result.get('winRate', 0),
                                            'maxDrawdown': result.get('maxDrawdown', 0),
                                            'sharpeRatio': result.get('sharpeRatio', 0),
                                            'minMarginLevel': result.get('minMarginLevel'),
                                            'liquidationCount': result.get('liquidationCount', 0),
                                            'marginLiquidatedTrades': result.get('marginLiquidatedTrades', 0),
                                            'totalLiquidationLoss': result.get('totalLiquidationLoss', 0),
                                            'marginCloseoutLevel': result.get('marginCloseoutLevel'),
                                            'trades': result.get('trades', []),
                                            'dailyBalances': result.get('dailyBalances', []),
                                            '_queueHash': params.get('_param_hash'),
                                        }
                                        print(f"RESULT:{json.dumps(result_output)}", flush=True)
                                        
                                        # Pause check
                                        if run_id and completed_tests % 10 == 0:
                                            status = check_run_status(run_id)
                                            if status == 'paused':
                                                print(f"\n⏸️ PAUSE in shotgun parallel at {completed_tests}/{total_tests}", file=sys.stderr)
                                                pool.terminate()
                                                pool.join()
                                                _pool = None
                                                # Cleanup shared memory
                                                for tf, shm_info in timeframe_shm_map.items():
                                                    shm_info['shm_numeric'].close()
                                                    shm_info['shm_numeric'].unlink()
                                                    shm_info['shm_meta'].close()
                                                    shm_info['shm_meta'].unlink()
                                                checkpoint_data = {'completed_tests': completed_tests, 'total_tests': total_tests}
                                                save_checkpoint(run_id, checkpoint_data)
                                                print(f"PAUSE_CHECKPOINT:{completed_tests}", flush=True)
                                                sys.exit(101)
                                        
                                        # NOTE: Manual process recycling REMOVED
                                        # Workers auto-recycle via maxtasksperchild (much faster, no restart overhead)
                                    else:
                                        print(f"  ❌ Test {worker_result['test_num']} FAILED: {worker_result['error']}", file=sys.stderr)
                                        failed_hash = worker_result['params'].get('_param_hash')
                                        if failed_hash and db_config:
                                            mark_queue_test_failed(failed_hash, str(worker_result['error'])[:500], db_config)
                                        completed_tests += 1
                                        failed_count += 1
                                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                                
                                except (KeyError, TypeError, AttributeError) as e:
                                    # Malformed result - log and continue
                                    print(f"  ⚠️ Malformed worker result (skipping): {e}", file=sys.stderr)
                                    worker_crash_count += 1
                                    completed_tests += 1
                                    print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                        
                        except BrokenPipeError as e:
                            # =====================================================================
                            # WORKER CRASH: BrokenPipeError
                            # This happens when a worker process dies while sending results back.
                            # Common causes:
                            #   1. Out of memory (OOM) - Windows killed the worker
                            #   2. Segmentation fault in native code (NumPy, etc.)
                            #   3. Result too large for pipe buffer
                            #   4. Timeout or system resource limits
                            # The pool may be corrupted - we should stop and let the process
                            # recycle mechanism restart with fresh workers.
                            # =====================================================================
                            worker_crash_count += 1
                            print(f"\n", file=sys.stderr)
                            print(f"  ╔════════════════════════════════════════════════════════════════════╗", file=sys.stderr)
                            print(f"  ║  ⚠️  WORKER CRASH DETECTED: BrokenPipeError                        ║", file=sys.stderr)
                            print(f"  ╠════════════════════════════════════════════════════════════════════╣", file=sys.stderr)
                            print(f"  ║  A worker process died while sending results back.                 ║", file=sys.stderr)
                            print(f"  ║                                                                    ║", file=sys.stderr)
                            print(f"  ║  COMMON CAUSES:                                                    ║", file=sys.stderr)
                            print(f"  ║   • Out of memory (OOM) - Windows killed the worker               ║", file=sys.stderr)
                            print(f"  ║   • Very long-running test exhausted resources                    ║", file=sys.stderr)
                            print(f"  ║   • Result data too large for IPC pipe                            ║", file=sys.stderr)
                            print(f"  ║                                                                    ║", file=sys.stderr)
                            print(f"  ║  WHAT HAPPENS NOW:                                                 ║", file=sys.stderr)
                            print(f"  ║   • Some test results may be lost (will be retried on restart)   ║", file=sys.stderr)
                            print(f"  ║   • Triggering process recycle to get fresh workers              ║", file=sys.stderr)
                            print(f"  ╚════════════════════════════════════════════════════════════════════╝", file=sys.stderr)
                            print(f"  Error details: {e}", file=sys.stderr)
                            print(f"  Completed before crash: {completed_tests}/{total_tests}", file=sys.stderr)
                            
                            # Calculate lost tests
                            expected_test_nums = set(range(1, total_tests + 1))
                            lost_test_nums = expected_test_nums - received_test_nums
                            if lost_test_nums and len(lost_test_nums) <= 20:
                                print(f"  Lost test numbers: {sorted(lost_test_nums)}", file=sys.stderr)
                            elif lost_test_nums:
                                print(f"  Lost tests: ~{len(lost_test_nums)} tests may need to be re-run", file=sys.stderr)
                            
                            # Trigger process recycle to recover
                            print(f"\n🔄 Forcing RECYCLE due to worker crash at {completed_tests} tests", file=sys.stderr)
                            try:
                                pool.terminate()
                                pool.join()
                            except:
                                pass
                            _pool = None
                            # Cleanup shared memory
                            for tf, shm_info in timeframe_shm_map.items():
                                try:
                                    shm_info['shm_numeric'].close()
                                    shm_info['shm_numeric'].unlink()
                                    shm_info['shm_meta'].close()
                                    shm_info['shm_meta'].unlink()
                                except:
                                    pass
                            print(f"RECYCLE_CHECKPOINT:{completed_tests}", flush=True)
                            sys.exit(100)  # Exit with recycle code so parent restarts us
                        
                        except (ConnectionResetError, EOFError, OSError) as e:
                            # =====================================================================
                            # WORKER CRASH: Connection/OS Error
                            # Similar to BrokenPipeError - a worker died unexpectedly.
                            # =====================================================================
                            worker_crash_count += 1
                            print(f"\n  ⚠️ WORKER CRASH: {type(e).__name__}: {e}", file=sys.stderr)
                            print(f"    A worker process terminated unexpectedly.", file=sys.stderr)
                            print(f"    Completed before crash: {completed_tests}/{total_tests}", file=sys.stderr)
                            print(f"    Triggering recycle to recover...", file=sys.stderr)
                            
                            try:
                                pool.terminate()
                                pool.join()
                            except:
                                pass
                            _pool = None
                            for tf, shm_info in timeframe_shm_map.items():
                                try:
                                    shm_info['shm_numeric'].close()
                                    shm_info['shm_numeric'].unlink()
                                    shm_info['shm_meta'].close()
                                    shm_info['shm_meta'].unlink()
                                except:
                                    pass
                            print(f"RECYCLE_CHECKPOINT:{completed_tests}", flush=True)
                            sys.exit(100)
                        
                        # Normal completion - print summary
                        print(f"✅ Shotgun parallel complete:", file=sys.stderr)
                        print(f"   • Successful: {successful_count}", file=sys.stderr)
                        print(f"   • Failed: {failed_count}", file=sys.stderr)
                        if worker_crash_count > 0:
                            print(f"   • Worker crashes handled: {worker_crash_count}", file=sys.stderr)
                        _pool = None
                finally:
                    # Cleanup all shared memory
                    for tf, shm_info in timeframe_shm_map.items():
                        try:
                            shm_info['shm_numeric'].close()
                            shm_info['shm_numeric'].unlink()
                            shm_info['shm_meta'].close()
                            shm_info['shm_meta'].unlink()
                        except Exception as e:
                            print(f"  ⚠️ Error cleaning up shared memory for {tf}: {e}", file=sys.stderr)
                    print(f"  🧹 Shared memory cleaned up for all timeframes", file=sys.stderr)
            else:
                # Sequential fallback (when parallel_cores <= 1)
                print(f"  🔄 SHOTGUN SEQUENTIAL MODE (cores={parallel_cores})", file=sys.stderr)
                for i, test_spec in enumerate(all_test_specs):
                    indicator_name = test_spec['indicator_name']
                    params = test_spec['params']
                    crash_protection_enabled = test_spec['crash_protection_enabled']
                    hmh_enabled = test_spec.get('hmh_enabled', False)
                    hmh_stop_loss_offset = test_spec.get('hmh_stop_loss_offset', None)
                    current_timeframe = test_spec['timeframe']
                    
                    df = epic_data_cache[current_timeframe]
                    
                    try:
                        param_hash = params.get('_param_hash')
                        if run_id and param_hash:
                            worker_id = f"proc_{os.getpid()}"
                            claimed = try_claim_test_by_hash(
                                run_id=run_id, param_hash=param_hash, epic=epic,
                                indicator_name=indicator_name,
                                indicator_params={k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']},
                                leverage=params.get('leverage', 4), stop_loss=params.get('stop_loss', 3.5),
                                timeframe=current_timeframe, crash_protection=crash_protection_enabled,
                                worker_id=worker_id, db_config=db_config
                            )
                            if not claimed:
                                completed_tests += 1
                                print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                                continue
                        
                        result = run_backtest_with_params(
                            df, epic, indicator_name, params,
                            initial_balance, monthly_topup, investment_pct, direction,
                            timing_config=config.get('timing_config'),
                            stop_conditions=stop_conditions,
                            timeframe=current_timeframe,
                            engine=engine,
                            crash_protection_enabled=crash_protection_enabled,
                            hmh_enabled=hmh_enabled,
                            hmh_stop_loss_offset=hmh_stop_loss_offset if hmh_stop_loss_offset is not None else 0,
                        )
                        completed_tests += 1
                        
                        if run_id and completed_tests % 10 == 0:
                            status = check_run_status(run_id)
                            if status == 'paused':
                                save_checkpoint(run_id, {'completed_tests': completed_tests, 'total_tests': total_tests})
                                print(f"\n⏸ Paused at {completed_tests}/{total_tests}", file=sys.stderr)
                                sys.exit(101)
                        
                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                        
                        result_output = {
                            'testNum': i + 1, 'indicatorName': indicator_name,
                            'indicatorParams': {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']},
                            'leverage': params.get('leverage', 4), 'stopLoss': params.get('stop_loss', 3.5),
                            'timeframe': current_timeframe, 'crashProtectionEnabled': crash_protection_enabled,
                            'hmhEnabled': hmh_enabled, 'hmhStopLossOffset': hmh_stop_loss_offset,
                            'initialBalance': initial_balance, 'finalBalance': result.get('finalBalance', 0),
                            'totalContributions': result.get('totalContributions', 0),
                            'totalReturn': result.get('totalReturn', 0), 'totalTrades': result.get('totalTrades', 0),
                            'winningTrades': result.get('winningTrades', 0), 'losingTrades': result.get('losingTrades', 0),
                            'winRate': result.get('winRate', 0), 'maxDrawdown': result.get('maxDrawdown', 0),
                            'sharpeRatio': result.get('sharpeRatio', 0),
                            'minMarginLevel': result.get('minMarginLevel'), 'liquidationCount': result.get('liquidationCount', 0),
                            'marginLiquidatedTrades': result.get('marginLiquidatedTrades', 0),
                            'totalLiquidationLoss': result.get('totalLiquidationLoss', 0),
                            'marginCloseoutLevel': result.get('marginCloseoutLevel'),
                            'trades': result.get('trades', []), 'dailyBalances': result.get('dailyBalances', []),
                            '_queueHash': params.get('_param_hash'),
                        }
                        print(f"RESULT:{json.dumps(result_output)}", flush=True)
                        
                        if enable_process_recycling and completed_tests >= process_recycle_count:
                            print(f"\n🔄 RECYCLE at {completed_tests} tests", file=sys.stderr)
                            print(f"RECYCLE_CHECKPOINT:{completed_tests}", flush=True)
                            sys.exit(100)
                        
                    except Exception as test_error:
                        print(f"  ❌ Test {i + 1} ({indicator_name}) FAILED: {str(test_error)}", file=sys.stderr)
                        failed_hash = params.get('_param_hash')
                        if failed_hash and db_config:
                            mark_queue_test_failed(failed_hash, str(test_error)[:500], db_config)
                        completed_tests += 1
                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                    
                    if free_memory and (i + 1) % 100 == 0:
                        import gc
                        gc.collect()
            
            print(f"\n✅ Shotgun batch complete! Tests: {completed_tests}/{total_tests}", file=sys.stderr)
            shotgun_mode_completed = True
        
        # ===================================================================
        # SEQUENTIAL MODE: Original behavior - process each indicator fully
        # Only runs if shotgun mode didn't execute
        # ===================================================================
        indicators_to_process = indicators.copy()
        
        # =================================================================
        # OPTIMIZATION: Load ALL tested hashes ONCE before the indicator loop
        # This replaces N database queries (one per indicator/timeframe/mode)
        # with just 1 query at the start
        # =================================================================
        seq_existing_hashes = None
        if not shotgun_mode_completed and not rerun_duplicates:
            print(f"\n  🔍 Loading all tested hashes from database (one-time load)...", file=sys.stderr)
            seq_existing_hashes = load_all_tested_hashes(epic)
            print(f"  ✓ Loaded {len(seq_existing_hashes):,} existing hashes - no more DB queries needed!", file=sys.stderr)
        
        # Process each indicator (SKIP if shotgun mode completed)
        for i, indicator_name in enumerate(indicators_to_process):
            if shotgun_mode_completed:
                break  # Exit the for loop if shotgun already completed
            print(f"\n\ud83c\udfaf Testing indicator: {indicator_name}", file=sys.stderr)
            
            # Get relevant parameters for this indicator (from database or fallback)
            param_ranges = get_indicator_params(indicator_name, all_param_ranges, epic=epic)
            param_space = ParameterSpace(param_ranges)
            
            # Create optimization config
            opt_config = OptimizationConfig(
                strategy=optimization_strategy,
                num_tests=num_tests,
                max_drawdown_threshold=stop_conditions.get('maxDrawdown'),
                min_win_rate_threshold=stop_conditions.get('minWinRate'),
                min_sharpe_threshold=stop_conditions.get('minSharpe'),
                parallel_cores=parallel_cores,
                rerun_duplicates=rerun_duplicates,
            )
            
            # Create optimizer
            optimizer = BatchOptimizer(opt_config, param_space)
            
            # Loop through crash protection modes
            for crash_protection_enabled in crash_modes_to_test:
                crash_mode_label = "with crash protection" if crash_protection_enabled else "without crash protection"
                print(f"  🛡️ Mode: {crash_mode_label}", file=sys.stderr)
                
                # Loop through timeframes (for multiple mode)
                for current_timeframe in timeframes_to_test:
                    print(f"    📊 Timeframe: {current_timeframe}", file=sys.stderr)
                    
                    # Use cached data instead of reloading
                    df = epic_data_cache[current_timeframe]
                    print(f"      Using cached {len(df)} {current_timeframe} candles", file=sys.stderr)
                    
                    # Define test function for this timeframe and crash mode
                    print(f"      [DEBUG-1] About to define test_func...", file=sys.stderr, flush=True)
                    def test_func(params: Dict) -> Dict:
                        nonlocal completed_tests

                        result = run_backtest_with_params(
                            df, epic, indicator_name, params,
                            initial_balance, monthly_topup, investment_pct, direction,
                            timing_config=config.get('timing_config'),
                            stop_conditions=stop_conditions,
                            timeframe=current_timeframe,
                            engine=engine,
                            crash_protection_enabled=crash_protection_enabled,
                        )
                        completed_tests += 1
                        
                        # Check for pause every 10 tests for better responsiveness
                        if run_id and completed_tests % 10 == 0:
                            status = check_run_status(run_id)
                            if status == 'paused':
                                # Save checkpoint and exit immediately
                                checkpoint_data = {
                                    'last_index': start_index + i,  # Current indicator
                                    'completed_tests': completed_tests,
                                    'total_tests': total_tests,
                                }
                                save_checkpoint(run_id, checkpoint_data)
                                print(f"\n⏸ Paused at test {completed_tests}/{total_tests}", file=sys.stderr)
                                sys.exit(101)  # Exit code 101 = paused (TypeScript handles this)
                        
                        # Output progress (backend parses this)
                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                        
                        # Output result (backend parses this)
                        result_output = {
                            'indicatorName': indicator_name,
                            'indicatorParams': {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']},
                            'leverage': params.get('leverage', 4),
                            'stopLoss': params.get('stop_loss', 3.5),
                            'timeframe': current_timeframe,
                            'crashProtectionEnabled': crash_protection_enabled,
                            # HMH (Hold Means Hold) parameters - extracted from test_spec or defaults
                            'hmhEnabled': False,  # Sequential mode doesn't support HMH yet
                            'hmhStopLossOffset': None,
                            'initialBalance': initial_balance,
                            'finalBalance': result.get('finalBalance', 0),
                            'totalContributions': result.get('totalContributions', 0),
                            'totalReturn': result.get('totalReturn', 0),
                            'totalTrades': result.get('totalTrades', 0),
                            'winningTrades': result.get('winningTrades', 0),
                            'losingTrades': result.get('losingTrades', 0),
                            'winRate': result.get('winRate', 0),
                            'maxDrawdown': result.get('maxDrawdown', 0),
                            'sharpeRatio': result.get('sharpeRatio', 0),
                            # Margin Level (ML) tracking metrics
                            'minMarginLevel': result.get('minMarginLevel'),
                            'liquidationCount': result.get('liquidationCount', 0),
                            'marginLiquidatedTrades': result.get('marginLiquidatedTrades', 0),
                            'totalLiquidationLoss': result.get('totalLiquidationLoss', 0),
                            'marginCloseoutLevel': result.get('marginCloseoutLevel'),
                            'trades': result.get('trades', []),
                            'dailyBalances': result.get('dailyBalances', []),
                            # Queue hash for marking complete (if queue locking is enabled)
                            '_queueHash': params.get('_param_hash'),
                        }
                        print(f"RESULT:{json.dumps(result_output)}", flush=True)
                        
                        return result
                    
                    # ===================================================================
                    # HASH PRE-CHECK (uses pre-loaded cache - NO DB queries!)
                    # ===================================================================
                    print(f"      [DEBUG-2] Starting HASH PRE-CHECK section...", file=sys.stderr, flush=True)
                    import multiprocessing as mp
                    import pandas as pd
                    import hashlib
                    print(f"      [DEBUG-3] Imports done, calling get_total_combinations()...", file=sys.stderr, flush=True)
                    
                    total_possible = param_space.get_total_combinations()
                    print(f"      [DEBUG-4] get_total_combinations returned: {total_possible}", file=sys.stderr, flush=True)
                    print(f"\n  📊 Total unique parameter combinations possible: {total_possible}", file=sys.stderr)
                    
                    if not rerun_duplicates:
                        # Use pre-loaded hash cache (NO DB query needed!)
                        print(f"      [DEBUG-5] Using pre-loaded hash cache...", file=sys.stderr, flush=True)
                        db_config = get_db_config()  # Still needed for queue operations
                        print(f"      [DEBUG-6] db_config obtained", file=sys.stderr, flush=True)
                        
                        # Generate hashes and filter against pre-loaded cache
                        print(f"  🔍 Filtering against cached hashes (no DB query)...", file=sys.stderr, flush=True)
                        all_combos = param_space._get_all_combinations()
                        print(f"      [DEBUG-7] Got {len(all_combos)} all_combos", file=sys.stderr, flush=True)
                        
                        # Filter to available combinations using cached hashes
                        available_combos = []
                        combo_to_hash = {}
                        for combo in all_combos:
                            indicator_params = {k: v for k, v in combo.items() if k not in ['leverage', 'stop_loss']}
                            leverage = combo.get('leverage', 4)
                            stop_loss = combo.get('stop_loss', 3.5)
                            
                            full_hash = generate_param_hash(
                                epic=epic,
                                start_date=start_date,
                                end_date=end_date,
                                direction=direction,
                                initial_balance=initial_balance,
                                monthly_topup=monthly_topup,
                                investment_pct=investment_pct,
                                timing_config=config.get('timing_config', {}),
                                data_source=data_source,
                                indicator_name=indicator_name,
                                indicator_params=indicator_params,
                                leverage=leverage,
                                stop_loss=stop_loss,
                                timeframe=current_timeframe,
                                crash_protection_enabled=crash_protection_enabled
                            )
                            combo_key = json.dumps(combo, sort_keys=True)
                            combo_to_hash[combo_key] = full_hash
                            
                            # Check against pre-loaded cache (instant - no DB query!)
                            if seq_existing_hashes is None or full_hash not in seq_existing_hashes:
                                combo['_param_hash'] = full_hash
                                available_combos.append(combo)
                        
                        already_run = len(all_combos) - len(available_combos)
                        print(f"      [DEBUG-9] Filtered: {already_run} already tested, {len(available_combos)} available", file=sys.stderr, flush=True)
                        print(f"  📈 Already in database: {already_run} results", file=sys.stderr)
                        print(f"  ✨ Available to run: {len(available_combos)} unique tests", file=sys.stderr)
                        
                        if len(available_combos) == 0:
                            print(f"  ⏭️  All {total_possible} combinations for {indicator_name} already in database, skipping...", file=sys.stderr)
                            # Count all as completed (they were done before)
                            completed_tests += num_tests
                            print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                            continue
                        
                        # Sample from available (not-yet-run) combinations
                        if num_tests >= len(available_combos):
                            all_params = available_combos
                            print(f"  🎯 Running all {len(all_params)} remaining unique tests", file=sys.stderr)
                        else:
                            all_params = random.sample(available_combos, num_tests)
                            print(f"  🎯 Randomly selected {len(all_params)} of {len(available_combos)} available tests", file=sys.stderr)
                    else:
                        # Rerun duplicates mode - just sample randomly
                        if num_tests > total_possible:
                            print(f"  ⚠️  Requested {num_tests} tests but only {total_possible} unique combinations exist", file=sys.stderr)
                        all_params = param_space.sample_unique(min(num_tests, total_possible))
                        print(f"  🔄 Rerun duplicates mode: running {len(all_params)} tests", file=sys.stderr)
                    
                    # If no tests to run, skip
                    if len(all_params) == 0:
                        continue
                    
                    # ===================================================================
                    # ATOMIC QUEUE LOCKING: Claim tests before running (BATCH VERSION)
                    # This prevents race conditions between parallel processes
                    # Uses batch operations for 100x faster claiming
                    # ===================================================================
                    if run_id:
                        worker_id = f"proc_{os.getpid()}"
                        
                        # Step 1: Ensure all params have hashes (they should from pre-check)
                        all_hashes = []
                        hash_to_params = {}
                        for params in all_params:
                            param_hash = params.get('_param_hash')
                            if not param_hash:
                                # Generate hash if not already present
                                indicator_params_for_hash = {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']}
                                param_hash = generate_param_hash(
                                    epic=epic,
                                    start_date=config.get('start_date'),
                                    end_date=config.get('end_date'),
                                    direction=direction,
                                    initial_balance=initial_balance,
                                    monthly_topup=monthly_topup,
                                    investment_pct=investment_pct,
                                    timing_config=config.get('timing_config', {'mode': timing_mode}),
                                    data_source=config.get('data_source', 'capital'),
                                    indicator_name=indicator_name,
                                    indicator_params=indicator_params_for_hash,
                                    leverage=params.get('leverage', 4),
                                    stop_loss=params.get('stop_loss', 3.5),
                                    timeframe=current_timeframe,
                                    crash_protection_enabled=crash_protection_enabled
                                )
                                params['_param_hash'] = param_hash
                            
                            all_hashes.append(param_hash)
                            hash_to_params[param_hash] = params
                        
                        # Step 2: Batch claim all tests at once (single DB transaction!)
                        claimed_hashes = batch_claim_tests(
                            hashes=all_hashes,
                            run_id=run_id,
                            worker_id=worker_id,
                            db_config=db_config
                        )
                        
                        # Step 3: Filter to only claimed params
                        claimed_params = [hash_to_params[h] for h in all_hashes if h in claimed_hashes]
                        skipped_claims = len(all_params) - len(claimed_params)
                        
                        if skipped_claims > 0:
                            print(f"  🔒 Queue lock: {len(claimed_params)} claimed, {skipped_claims} already claimed/completed", file=sys.stderr)
                        
                        all_params = claimed_params
                        
                        if len(all_params) == 0:
                            print(f"  ⏭️  All tests already claimed/completed, skipping...", file=sys.stderr)
                            continue
                    
                    # ===================================================================
                    # Run optimization for this timeframe (parallel or sequential)
                    # ===================================================================
                    print(f"      [DEBUG-10] About to run optimization, parallel_cores={parallel_cores}, all_params={len(all_params)}", file=sys.stderr, flush=True)
                    if parallel_cores > 1:
                        # Parallel execution with shared memory
                        print(f"  ⚡ Using {parallel_cores} parallel workers with shared memory", file=sys.stderr, flush=True)
                        print(f"      [DEBUG-11] Entering parallel execution block", file=sys.stderr, flush=True)
                        
                        # === DEBUG: Log parameters to run ===
                        print(f"\n=== PARAMETERS TO RUN ({len(all_params)} TESTS) ===", file=sys.stderr)
                        for test_idx, params in enumerate(all_params[:5]):  # Only log first 5
                            leverage = params.get('leverage', 4)
                            stop_loss = params.get('stop_loss', 3.5)
                            print(f"  Test #{test_idx+1}: {indicator_name} | leverage={leverage}x | stopLoss={stop_loss}%", file=sys.stderr)
                        if len(all_params) > 5:
                            print(f"  ... and {len(all_params) - 5} more", file=sys.stderr)
                        print(f"=== END PARAMETERS ===\n", file=sys.stderr)
                        
                        # Create multiprocessing Manager for worker status tracking
                        import multiprocessing as mp
                        
                        # Create shared memory for DataFrame
                        # Store numeric data and metadata separately to preserve dtypes
                        print(f"      [DEBUG-12] About to create shared memory for DataFrame...", file=sys.stderr, flush=True)
                        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
                        print(f"      [DEBUG-13] numeric_cols: {len(numeric_cols)}", file=sys.stderr, flush=True)
                        datetime_cols = df.select_dtypes(include=['datetime64']).columns.tolist()
                        object_cols = df.select_dtypes(include=['object']).columns.tolist()
                        
                        # Get numeric data as contiguous array
                        numeric_data = df[numeric_cols].values.astype(np.float64, copy=False)
                        
                        # Serialize datetime and object columns
                        import pickle
                        metadata = {
                            'numeric_cols': numeric_cols,
                            'datetime_cols': datetime_cols,
                            'object_cols': object_cols,
                            'datetime_data': {col: df[col].values for col in datetime_cols},
                            'object_data': {col: df[col].values for col in object_cols},
                            'index': df.index.tolist()
                        }
                        metadata_bytes = pickle.dumps(metadata)
                        
                        # Create shared memory for numeric data
                        global _shm_numeric, _shm_meta
                        print(f"      [DEBUG-14] Creating shared memory: numeric_data.nbytes={numeric_data.nbytes}, shape={numeric_data.shape}", file=sys.stderr, flush=True)
                        shm_numeric = shared_memory.SharedMemory(create=True, size=numeric_data.nbytes)
                        print(f"      [DEBUG-15] Shared memory created: name={shm_numeric.name}", file=sys.stderr, flush=True)
                        _shm_numeric = shm_numeric  # Global reference for signal handler cleanup
                        shared_array = np.ndarray(numeric_data.shape, dtype=np.float64, buffer=shm_numeric.buf)
                        shared_array[:] = numeric_data[:]
                        
                        # Create shared memory for metadata
                        shm_meta = shared_memory.SharedMemory(create=True, size=len(metadata_bytes))
                        _shm_meta = shm_meta  # Global reference for signal handler cleanup
                        shm_meta.buf[:len(metadata_bytes)] = metadata_bytes
                        
                        print(f"  💾 Shared memory created: {shm_numeric.name} ({numeric_data.nbytes / 1024 / 1024:.1f}MB numeric + {len(metadata_bytes) / 1024:.1f}KB metadata)", file=sys.stderr)
                        
                        try:
                            # Build worker arguments generator (lazy evaluation to avoid memory overhead)
                            def worker_args_generator():
                                for i, params in enumerate(all_params):
                                    yield (
                                        params, i+1, shm_numeric.name, shm_meta.name, len(metadata_bytes),
                                        numeric_data.shape, epic, indicator_name, initial_balance, monthly_topup,
                                        investment_pct, direction, config.get('timing_config'),
                                        stop_conditions, current_timeframe, crash_protection_enabled,
                                        engine,  # 'original' or 'fast'
                                        worker_timeout_seconds,  # Configurable timeout
                                    )
                            
                            # Run in parallel with real-time results
                            print(f"      [DEBUG-16] Creating multiprocessing Pool with {parallel_cores} processes...", file=sys.stderr, flush=True)
                            with mp.Pool(processes=parallel_cores) as pool:
                                print(f"      [DEBUG-17] Pool created successfully, starting imap_unordered...", file=sys.stderr, flush=True)
                                _pool = pool  # Set global reference for signal handler
                                # Use imap_unordered with chunksize=1 for real-time progress updates
                                # Larger chunksize batches results and delays progress feedback
                                chunksize = 1  # Real-time updates (was: num_tests // (parallel_cores * 4))
                                print(f"  📦 Using chunksize: {chunksize} for {num_tests} tests (real-time mode)", file=sys.stderr)
                                result_iterator = pool.imap_unordered(_parallel_test_worker, worker_args_generator(), chunksize=chunksize)
                                
                                # Process results as they arrive
                                successful_count = 0
                                for worker_result in result_iterator:
                                    if worker_result['success']:
                                        result = worker_result['result']
                                        params = worker_result['params']
                                        test_num = worker_result['test_num']
                                        
                                        completed_tests += 1
                                        successful_count += 1
                                        
                                        # Output progress
                                        print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                                        
                                        # DEBUG: Log the exact parameters being output for this test
                                        indicator_params_out = {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']}
                                        print(f"  📤 Test #{test_num} OUTPUT: indicator={indicator_name} params={json.dumps(indicator_params_out)} leverage={params.get('leverage', 4)} stopLoss={params.get('stop_loss', 3.5)} finalBalance=${result.get('finalBalance', 0):.2f}", file=sys.stderr)
                                        
                                        # Output result
                                        result_output = {
                                            'testNum': test_num,  # Include test number for correlation
                                            'indicatorName': indicator_name,
                                            'indicatorParams': indicator_params_out,
                                            'leverage': params.get('leverage', 4),
                                            'stopLoss': params.get('stop_loss', 3.5),
                                            'timeframe': current_timeframe,
                                            'crashProtectionEnabled': crash_protection_enabled,
                                            'initialBalance': initial_balance,
                                            'finalBalance': result.get('finalBalance', 0),
                                            'totalContributions': result.get('totalContributions', 0),
                                            'totalReturn': result.get('totalReturn', 0),
                                            'totalTrades': result.get('totalTrades', 0),
                                            'winningTrades': result.get('winningTrades', 0),
                                            'losingTrades': result.get('losingTrades', 0),
                                            'winRate': result.get('winRate', 0),
                                            'maxDrawdown': result.get('maxDrawdown', 0),
                                            'sharpeRatio': result.get('sharpeRatio', 0),
                                            # Margin Level (ML) tracking metrics
                                            'minMarginLevel': result.get('minMarginLevel'),
                                            'liquidationCount': result.get('liquidationCount', 0),
                                            'marginLiquidatedTrades': result.get('marginLiquidatedTrades', 0),
                                            'totalLiquidationLoss': result.get('totalLiquidationLoss', 0),
                                            'marginCloseoutLevel': result.get('marginCloseoutLevel'),
                                            'trades': result.get('trades', []),
                                            'dailyBalances': result.get('dailyBalances', []),
                                            # Queue hash for marking complete (if queue locking is enabled)
                                            '_queueHash': params.get('_param_hash'),
                                        }
                                        print(f"RESULT:{json.dumps(result_output)}", flush=True)
                                        
                                        # === PAUSE CHECK (every 10 tests) ===
                                        if run_id and completed_tests % 10 == 0:
                                            status = check_run_status(run_id)
                                            if status == 'paused':
                                                print(f"\n⏸️ PAUSE DETECTED in parallel mode at test {completed_tests}/{total_tests}", file=sys.stderr)
                                                # Terminate pool and clean up before exit
                                                pool.terminate()
                                                pool.join()
                                                _pool = None
                                                # Clean up shared memory BEFORE exiting
                                                shm_numeric.close()
                                                shm_numeric.unlink()
                                                shm_meta.close()
                                                shm_meta.unlink()
                                                _shm_numeric = None  # Clear global reference
                                                _shm_meta = None  # Clear global reference
                                                print(f"  🧹 Shared memory cleaned up before pause", file=sys.stderr)
                                                # Save checkpoint
                                                checkpoint_data = {
                                                    'last_index': start_index + i,  # Current indicator
                                                    'completed_tests': completed_tests,
                                                    'total_tests': total_tests,
                                                }
                                                save_checkpoint(run_id, checkpoint_data)
                                                print(f"PAUSE_CHECKPOINT:{completed_tests}", flush=True)
                                                sys.exit(101)  # Exit code 101 = paused
                                        
                                        # === PROCESS RECYCLING CHECK ===
                                        # After N tests, exit to free memory and let TypeScript respawn us
                                        if enable_process_recycling and completed_tests >= process_recycle_count:
                                            print(f"\n🔄 PROCESS RECYCLE: Reached {completed_tests} tests (limit: {process_recycle_count})", file=sys.stderr)
                                            print(f"   Exiting to free memory. TypeScript will respawn to continue.", file=sys.stderr)
                                            # Terminate pool and clean up before exit
                                            pool.terminate()
                                            pool.join()
                                            _pool = None
                                            # Clean up shared memory
                                            shm_numeric.close()
                                            shm_numeric.unlink()
                                            shm_meta.close()
                                            shm_meta.unlink()
                                            _shm_numeric = None  # Clear global reference
                                            _shm_meta = None  # Clear global reference
                                            print(f"  🧹 Shared memory cleaned up before recycle", file=sys.stderr)
                                            print(f"RECYCLE_CHECKPOINT:{completed_tests}", flush=True)
                                            sys.exit(100)  # Special exit code for "recycle needed"
                                    else:
                                        # Log error
                                        print(f"  ❌ Test {worker_result['test_num']} FAILED: {worker_result['error']}", file=sys.stderr)
                                        print(f"     Parameters: {json.dumps(worker_result['params'])}", file=sys.stderr)
                                        
                                        # Mark failed test in queue (if queue locking is enabled)
                                        failed_hash = worker_result['params'].get('_param_hash')
                                        if failed_hash and db_config:
                                            mark_queue_test_failed(failed_hash, str(worker_result['error'])[:500], db_config)
                                
                                print(f"✅ Completed {indicator_name} on {current_timeframe} ({crash_mode_label}): {successful_count} valid results", file=sys.stderr)
                                
                                # Clear global pool reference
                                _pool = None
                        finally:
                            # Clean up shared memory
                            shm_numeric.close()
                            shm_numeric.unlink()
                            shm_meta.close()
                            shm_meta.unlink()
                            _shm_numeric = None  # Clear global reference
                            _shm_meta = None  # Clear global reference
                            print(f"  🧹 Shared memory cleaned up", file=sys.stderr)
                    else:
                        # Sequential execution with pre-filtered params
                        print(f"  🔄 Sequential execution with {len(all_params)} pre-filtered tests", file=sys.stderr)
                        successful_count = 0
                        for i, params in enumerate(all_params):
                            try:
                                result = run_backtest_with_params(
                                    df, epic, indicator_name, params,
                                    initial_balance, monthly_topup, investment_pct, direction,
                                    timing_config=config.get('timing_config'),
                                    stop_conditions=stop_conditions,
                                    timeframe=current_timeframe,
                                    engine=engine,
                                    crash_protection_enabled=crash_protection_enabled,
                                )
                                completed_tests += 1
                                successful_count += 1
                                
                                # Check for pause every 10 tests
                                if run_id and completed_tests % 10 == 0:
                                    status = check_run_status(run_id)
                                    if status == 'paused':
                                        checkpoint_data = {
                                            'last_index': start_index + i,
                                            'completed_tests': completed_tests,
                                            'total_tests': total_tests,
                                        }
                                        save_checkpoint(run_id, checkpoint_data)
                                        print(f"\n⏸️ Paused at test {completed_tests}/{total_tests}", file=sys.stderr)
                                        sys.exit(101)  # Exit code 101 = paused (TypeScript handles this)
                                
                                # Output progress
                                print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                                
                                # Output result
                                result_output = {
                                    'testNum': i + 1,
                                    'indicatorName': indicator_name,
                                    'indicatorParams': {k: v for k, v in params.items() if k not in ['leverage', 'stop_loss', '_param_hash']},
                                    'leverage': params.get('leverage', 4),
                                    'stopLoss': params.get('stop_loss', 3.5),
                                    'timeframe': current_timeframe,
                                    'crashProtectionEnabled': crash_protection_enabled,
                                    'initialBalance': initial_balance,
                                    'finalBalance': result.get('finalBalance', 0),
                                    'totalContributions': result.get('totalContributions', 0),
                                    'totalReturn': result.get('totalReturn', 0),
                                    'totalTrades': result.get('totalTrades', 0),
                                    'winningTrades': result.get('winningTrades', 0),
                                    'losingTrades': result.get('losingTrades', 0),
                                    'winRate': result.get('winRate', 0),
                                    'maxDrawdown': result.get('maxDrawdown', 0),
                                    'sharpeRatio': result.get('sharpeRatio', 0),
                                    'trades': result.get('trades', []),
                                    'dailyBalances': result.get('dailyBalances', []),
                                    # Queue hash for marking complete (if queue locking is enabled)
                                    '_queueHash': params.get('_param_hash'),
                                }
                                print(f"RESULT:{json.dumps(result_output)}", flush=True)
                                
                                # Process recycling check
                                if enable_process_recycling and completed_tests >= process_recycle_count:
                                    print(f"\n🔄 PROCESS RECYCLE: Reached {completed_tests} tests (limit: {process_recycle_count})", file=sys.stderr)
                                    print(f"   Exiting to free memory. TypeScript will respawn to continue.", file=sys.stderr)
                                    print(f"RECYCLE_CHECKPOINT:{completed_tests}", flush=True)
                                    sys.exit(100)
                            except Exception as test_error:
                                # Log error but continue with next test
                                print(f"  ❌ Test {i + 1} FAILED: {str(test_error)}", file=sys.stderr)
                                print(f"     Parameters: {json.dumps(params)}", file=sys.stderr)
                                
                                # Mark failed test in queue (if queue locking is enabled)
                                failed_hash = params.get('_param_hash')
                                if failed_hash and db_config:
                                    mark_queue_test_failed(failed_hash, str(test_error)[:500], db_config)
                                
                                # Still count as completed (attempted) for progress
                                completed_tests += 1
                                print(f"PROGRESS:{completed_tests}/{total_tests}", flush=True)
                        
                        print(f"✅ Completed {indicator_name} on {current_timeframe} ({crash_mode_label}): {successful_count} valid results", file=sys.stderr)
            
            # Free memory after each indicator if enabled
            if free_memory:
                import gc
                gc.collect()
                print(f"  🧹 Memory freed after {indicator_name}", file=sys.stderr)
            
            # Check for pause after each indicator
            if run_id and (i + 1) % 1 == 0:  # Check after every indicator
                status = check_run_status(run_id)
                if status == 'paused':
                    # Save checkpoint
                    checkpoint_data = {
                        'last_index': start_index + i + 1,  # Next indicator to process
                        'completed_tests': completed_tests,
                        'total_tests': total_tests,
                    }
                    save_checkpoint(run_id, checkpoint_data)
                    print(f"\n⏸ Paused at indicator {start_index + i + 1}/{len(indicators) + start_index}", file=sys.stderr)
                    sys.exit(101)  # Exit code 101 = paused (TypeScript handles this)
        
        print(f"\n✅ Batch optimization complete! Total tests: {completed_tests}/{total_tests}", file=sys.stderr)
        
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()

