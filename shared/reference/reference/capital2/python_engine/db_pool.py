"""
Database Connection Pool for Backtest Workers
Reuses connections instead of creating new ones for each operation

This module provides:
1. Connection pooling - reuses DB connections instead of creating new ones
2. Batch hash checking - checks multiple hashes in a single query
3. Thread-safe operations with proper locking

Usage:
    from db_pool import get_connection, check_hashes_batch
    
    # Get a pooled connection
    conn = get_connection()
    
    # Check multiple hashes at once (1 query instead of N)
    existing = check_hashes_batch(['hash1', 'hash2', 'hash3'])
"""
import mysql.connector
from mysql.connector import pooling
import os
from typing import Optional, Set, List
import threading
import sys

# Use centralized database config
from db_config import get_database_url, parse_database_url

# Global pool instance (singleton)
_pool: Optional[pooling.MySQLConnectionPool] = None
_pool_lock = threading.Lock()

def get_db_config() -> dict:
    """
    Get database config using centralized db_config module.
    
    Supports both standard MySQL URLs and TiDB Cloud URLs with SSL.
    
    Returns:
        dict: Connection configuration for mysql.connector
    """
    db_url = get_database_url()
    config = parse_database_url(db_url)
    
    # TiDB Cloud requires SSL
    if config.get('host') and 'tidbcloud.com' in config['host']:
        config['ssl_verify_cert'] = True
        config['ssl_verify_identity'] = True
    
    return config

def get_pool(pool_size: int = 5) -> pooling.MySQLConnectionPool:
    """
    Get or create the connection pool (singleton pattern).
    
    The pool is created once and reused across all calls.
    Thread-safe via locking.
    
    Args:
        pool_size: Number of connections to maintain in the pool (default 5)
    
    Returns:
        MySQLConnectionPool instance
    """
    global _pool
    
    with _pool_lock:
        if _pool is None:
            config = get_db_config()
            try:
                _pool = pooling.MySQLConnectionPool(
                    pool_name="backtest_pool",
                    pool_size=pool_size,
                    pool_reset_session=True,
                    **config
                )
                print(f"[DBPool] Created connection pool with {pool_size} connections", file=sys.stderr)
            except Exception as e:
                print(f"[DBPool] Failed to create pool: {e}", file=sys.stderr)
                raise
    
    return _pool

def get_connection():
    """
    Get a connection from the pool.
    
    The connection should be closed after use to return it to the pool.
    
    Returns:
        PooledMySQLConnection
    
    Example:
        conn = get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM table")
            results = cursor.fetchall()
        finally:
            conn.close()  # Returns to pool, doesn't actually close
    """
    return get_pool().get_connection()

def check_hashes_batch(hashes: List[str]) -> Set[str]:
    """
    Check multiple hashes in a single database query.
    
    This is MUCH faster than checking one hash at a time:
    - 1000 individual queries: ~10-20 seconds
    - 1 batch query with 1000 hashes: ~0.1-0.2 seconds
    
    Args:
        hashes: List of paramHash values to check
    
    Returns:
        Set of hashes that already exist in the database
    
    Example:
        hashes_to_check = ['abc123', 'def456', 'ghi789']
        existing = check_hashes_batch(hashes_to_check)
        # existing = {'abc123', 'ghi789'}  # Only these exist in DB
        
        new_hashes = [h for h in hashes_to_check if h not in existing]
        # new_hashes = ['def456']  # Only this one needs to be computed
    """
    if not hashes:
        return set()
    
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Use IN clause for batch lookup - much faster than individual queries
        # MySQL can handle thousands of values in an IN clause
        # Check BOTH testedHashes (global deduplication) AND backtestResults
        placeholders = ','.join(['%s'] * len(hashes))
        
        # Primary check: testedHashes table (contains ALL tested hashes, even below-threshold)
        query = f"SELECT paramHash FROM testedHashes WHERE paramHash IN ({placeholders})"
        cursor.execute(query, hashes)
        existing = {row[0] for row in cursor.fetchall()}
        
        # Fallback: also check backtestResults for any missed hashes
        if len(existing) < len(hashes):
            remaining = [h for h in hashes if h not in existing]
            if remaining:
                placeholders2 = ','.join(['%s'] * len(remaining))
                query2 = f"SELECT paramHash FROM backtestResults WHERE paramHash IN ({placeholders2})"
                cursor.execute(query2, remaining)
                existing.update(row[0] for row in cursor.fetchall())
        
        print(f"[DBPool] Batch hash check: {len(hashes)} checked, {len(existing)} found existing", file=sys.stderr)
        
        return existing
        
    except Exception as e:
        print(f"[DBPool] Error in batch hash check: {e}", file=sys.stderr)
        return set()
        
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()  # Returns to pool


def load_all_tested_hashes(epic: str = None) -> Set[str]:
    """
    Load ALL tested hashes from the database in ONE query.
    
    Call this ONCE at the start of a discovery run, then use the returned
    set to filter tests without any additional DB queries.
    
    Args:
        epic: Optional - filter to only hashes for this epic (faster for large DBs)
    
    Returns:
        Set of all paramHash values that have been tested
    
    Example:
        # At start of discovery:
        all_tested = load_all_tested_hashes('SOXL')
        
        # Then in loop, no DB query needed:
        if my_hash in all_tested:
            skip_this_test()
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        if epic:
            # Filter by epic for faster query on large databases
            query = "SELECT paramHash FROM testedHashes WHERE epic = %s"
            cursor.execute(query, (epic,))
        else:
            # Get all hashes (slower but complete)
            query = "SELECT paramHash FROM testedHashes"
            cursor.execute(query)
        
        all_hashes = {row[0] for row in cursor.fetchall()}
        
        print(f"[DBPool] Loaded {len(all_hashes):,} existing hashes from testedHashes" + 
              (f" for {epic}" if epic else ""), file=sys.stderr)
        
        return all_hashes
        
    except Exception as e:
        print(f"[DBPool] Error loading all hashes: {e}", file=sys.stderr)
        return set()
        
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def check_run_status_pooled(run_id: int) -> Optional[str]:
    """
    Check backtest run status using pooled connection.
    
    Faster than creating a new connection for each status check.
    
    Args:
        run_id: The backtest run ID to check
    
    Returns:
        Status string ('running', 'paused', 'completed', etc.) or None if not found
    """
    if not run_id:
        return None
    
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT status FROM backtestRuns WHERE id = %s",
            (run_id,)
        )
        result = cursor.fetchone()
        return result[0] if result else None
        
    except Exception as e:
        print(f"[DBPool] Error checking run status: {e}", file=sys.stderr)
        return None
        
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def close_pool():
    """
    Close all connections in the pool.
    
    Call this when shutting down to clean up resources.
    """
    global _pool
    
    with _pool_lock:
        if _pool is not None:
            # Note: mysql.connector pooling doesn't have an explicit close method
            # Connections are closed when the pool is garbage collected
            _pool = None
            print("[DBPool] Pool closed", file=sys.stderr)


# ============================================================================
# BATCH INSERT FUNCTIONS (for future use with result batching)
# ============================================================================

def insert_results_batch(results: List[dict]) -> int:
    """
    Insert multiple backtest results in a single transaction.
    
    This is MUCH faster than individual inserts:
    - 100 individual inserts: ~5-10 seconds
    - 1 batch insert with 100 results: ~0.1-0.2 seconds
    
    Args:
        results: List of result dictionaries with keys matching backtestResults columns
    
    Returns:
        Number of results successfully inserted
    
    Note: This function is prepared for future use when we implement
    result batching in backtest_bridge_v2.ts
    """
    if not results:
        return 0
    
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Build the INSERT statement
        columns = [
            'runId', 'indicatorName', 'indicatorParams', 'leverage', 'stopLoss',
            'timeframe', 'crashProtection', 'initialBalance', 'finalBalance',
            'totalContributions', 'totalReturn', 'totalTrades', 'winningTrades',
            'losingTrades', 'winRate', 'maxDrawdown', 'sharpeRatio', 'paramHash'
        ]
        
        placeholders = ', '.join(['%s'] * len(columns))
        column_names = ', '.join(columns)
        
        query = f"INSERT INTO backtestResults ({column_names}) VALUES ({placeholders})"
        
        # Prepare values for batch insert
        values_list = []
        for r in results:
            values = (
                r.get('runId'),
                r.get('indicatorName'),
                r.get('indicatorParams'),  # Should be JSON string
                r.get('leverage'),
                r.get('stopLoss'),
                r.get('timeframe'),
                r.get('crashProtection', False),
                r.get('initialBalance'),
                r.get('finalBalance'),
                r.get('totalContributions'),
                r.get('totalReturn'),
                r.get('totalTrades'),
                r.get('winningTrades'),
                r.get('losingTrades'),
                r.get('winRate'),
                r.get('maxDrawdown'),
                r.get('sharpeRatio'),
                r.get('paramHash'),
            )
            values_list.append(values)
        
        # Execute batch insert
        cursor.executemany(query, values_list)
        conn.commit()
        
        inserted = cursor.rowcount
        print(f"[DBPool] Batch insert: {inserted} results inserted", file=sys.stderr)
        
        return inserted
        
    except Exception as e:
        print(f"[DBPool] Error in batch insert: {e}", file=sys.stderr)
        if conn:
            conn.rollback()
        return 0
        
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

