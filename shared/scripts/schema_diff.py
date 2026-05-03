"""
Module: schema_diff
Purpose: Schema drift detector — used as both a CI gate and a daily live-DB canary.
Location: /opt/tickles/shared/scripts/schema_diff.py
"""

import argparse
import difflib
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Callable, Awaitable

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

NOISE_PATTERNS = [
    re.compile(r"^-- Dumped (from|by) .*$", re.MULTILINE),
    re.compile(r"^-- Started on .*$", re.MULTILINE),
    re.compile(r"^-- Completed on .*$", re.MULTILINE),
    re.compile(r"OWNER TO \w+;", re.MULTILINE),
    re.compile(r"^SET .*$", re.MULTILINE),
    re.compile(r"^SELECT pg_catalog\..*$", re.MULTILINE),
    re.compile(r"^--\s*$", re.MULTILINE), # Empty comments
]

def _normalise(sql: str) -> str:
    """
    Remove noise from pg_dump output to make it comparable.
    
    Args:
        sql: The SQL string to normalise.
        
    Returns:
        The normalised SQL string.
    """
    for pat in NOISE_PATTERNS:
        sql = pat.sub("", sql)
    # collapse blank lines
    sql = re.sub(r"\n{3,}", "\n\n", sql)
    return sql.strip() + "\n"

def _pg_dump(dsn: str) -> str:
    """
    Execute pg_dump to get the schema.
    
    Args:
        dsn: The PostgreSQL connection string.
        
    Returns:
        The normalised schema SQL.
        
    Raises:
        RuntimeError: If pg_dump fails.
    """
    try:
        # We use --schema-only, --no-owner, --no-privileges to get a clean schema
        cmd = ["pg_dump", "--schema-only", "--no-owner", "--no-privileges", dsn]
        logger.info(f"Running: {' '.join(cmd)}")
        process = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return _normalise(process.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"pg_dump failed: {e.stderr}")
        raise RuntimeError(f"Failed to dump schema from {dsn}: {e.stderr}") from e

def diff_against_snapshot(dsn: str, snapshot_name: str) -> int:
    """
    Compare live schema against a saved snapshot.
    
    Args:
        dsn: The PostgreSQL connection string.
        snapshot_name: The name of the snapshot (e.g. 'tickles_shared').
        
    Returns:
        0 if they match, 1 otherwise.
    """
    try:
        actual = _pg_dump(dsn)
        snapshot_path = SNAPSHOT_DIR / f"{snapshot_name}.snapshot.sql"
        
        if not snapshot_path.exists():
            logger.error(f"Snapshot file not found: {snapshot_path}")
            return 1
            
        expected = _normalise(snapshot_path.read_text())
        
        if actual == expected:
            logger.info(f"[schema-diff] {snapshot_name}: OK")
            return 0
            
        diff = "\n".join(difflib.unified_diff(
            expected.splitlines(), actual.splitlines(),
            fromfile=f"{snapshot_name} (canonical)",
            tofile=f"{snapshot_name} (actual)",
            lineterm="",
        ))
        logger.warning(f"[schema-diff] {snapshot_name}: DRIFT DETECTED\n{diff}")
        return 1
    except Exception as e:
        logger.exception(f"Error during schema diff: {e}")
        return 1

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Detect schema drift against canonical snapshots.")
    ap.add_argument("--dsn", required=True, help="PostgreSQL DSN (e.g. postgresql://user:pass@host:port/db)")
    ap.add_argument("--snapshot", required=True, choices=["tickles_shared", "tickles_company"], 
                    help="Which snapshot to compare against")
    
    args = ap.parse_args()
    
    # Ensure snapshot directory exists
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    
    sys.exit(diff_against_snapshot(args.dsn, args.snapshot))
