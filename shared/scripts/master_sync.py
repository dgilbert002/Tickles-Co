"""
Module: master_sync
Purpose: Master schema sync — applies tickles_shared_pg.sql + tickles_company_pg.sql
         to an ephemeral DB and validates fresh apply and snapshot additive apply.
Location: /opt/tickles/shared/scripts/master_sync.py
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
SHARED_SQL = ROOT / "migration/tickles_shared_pg.sql"
COMPANY_SQL = ROOT / "migration/tickles_company_pg.sql"

def _run(cmd: list[str]) -> None:
    """
    Run a command and exit on failure.
    
    Args:
        cmd: The command to run.
    """
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[master-sync] FAIL: {' '.join(cmd)}\n{res.stderr}", file=sys.stderr)
        sys.exit(1)

def gate_fresh(dsn: str) -> None:
    """
    Validate that the master SQL files apply cleanly to a fresh database.
    
    Args:
        dsn: The PostgreSQL connection string.
    """
    # We use -v ON_ERROR_STOP=1 to ensure we fail on any SQL error
    # We also need to handle the \c commands in the SQL files if they exist, 
    # but since we are passing the DSN, we might want to strip them or ensure they match.
    # For the gate, we assume the DSN points to the correct DB and we strip \c.
    
    temp_shared = Path("/tmp/shared_gate.sql")
    temp_company = Path("/tmp/company_gate.sql")
    
    shared_content = SHARED_SQL.read_text()
    company_content = COMPANY_SQL.read_text()
    
    # Strip \c commands
    import re
    shared_content = re.sub(r"\\c\s+\w+", "-- stripped c", shared_content)
    company_content = re.sub(r"\\c\s+\w+", "-- stripped c", company_content)
    
    temp_shared.write_text(shared_content)
    temp_company.write_text(company_content)
    
    try:
        _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(temp_shared)])
        _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(temp_company)])
        print("[master-sync] fresh: OK")
    finally:
        if temp_shared.exists(): temp_shared.unlink()
        if temp_company.exists(): temp_company.unlink()

def gate_snapshot(dsn: str, snapshot_path: Path) -> None:
    """
    Validate that the master SQL files apply additively to a snapshot.
    
    Args:
        dsn: The PostgreSQL connection string.
        snapshot_path: Path to the snapshot SQL file.
    """
    _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(snapshot_path)])
    
    # Now apply master SQL additively — must not error
    temp_shared = Path("/tmp/shared_gate_snap.sql")
    temp_company = Path("/tmp/company_gate_snap.sql")
    
    shared_content = SHARED_SQL.read_text()
    company_content = COMPANY_SQL.read_text()
    
    import re
    shared_content = re.sub(r"\\c\s+\w+", "-- stripped c", shared_content)
    company_content = re.sub(r"\\c\s+\w+", "-- stripped c", company_content)
    
    temp_shared.write_text(shared_content)
    temp_company.write_text(company_content)
    
    try:
        _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(temp_shared)])
        _run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-f", str(temp_company)])
        print("[master-sync] snapshot: OK")
    finally:
        if temp_shared.exists(): temp_shared.unlink()
        if temp_company.exists(): temp_company.unlink()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate master schema sync.")
    ap.add_argument("--mode", choices=["fresh", "snapshot"], required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--snapshot", help="Path to snapshot SQL", default=None)
    args = ap.parse_args()
    
    if args.mode == "fresh":
        gate_fresh(args.dsn)
    else:
        if not args.snapshot:
            print("--snapshot required for snapshot mode", file=sys.stderr)
            sys.exit(2)
        gate_snapshot(args.dsn, Path(args.snapshot))
