"""
Module: check_system_freshness
Purpose: System-wide freshness audit utility for Tickles trading infrastructure.
Location: /opt/tickles/shared/scripts/check_system_freshness.py
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add /opt/tickles to sys.path to ensure shared imports work
sys.path.append("/opt/tickles")

import psycopg2
from shared.utils.config import load_env
from shared.utils.freshness import validate_freshness, StaleDataError

logger = logging.getLogger(__name__)

# Constants
DEFAULT_THRESHOLD_SECONDS = 180.0
DB_TIMEOUT = 5


def _get_db_conn() -> psycopg2.extensions.connection:
    """Get a PostgreSQL connection using environment variables."""
    import urllib.parse
    load_env()
    host = os.environ.get("DB_HOST", "localhost")
    port = int(os.environ.get("DB_PORT", "5432"))
    user = os.environ.get("DB_USER", "postgres")
    password = os.environ.get("DB_PASS", "")
    dbname = os.environ.get("DB_NAME", "tickles")
    safe_password = urllib.parse.quote(password, safe="")
    dsn = f"postgresql://{user}:{safe_password}@{host}:{port}/{dbname}"
    return psycopg2.connect(dsn, connect_timeout=DB_TIMEOUT)


def _parse_iso_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp string to a datetime object."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _file_age_seconds(path: Path) -> Optional[float]:
    """Get the age of a file in seconds."""
    if not path.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).total_seconds()
    except Exception:
        return None


def check_market_state(workspace: Path, threshold: float) -> Tuple[bool, str]:
    """Check freshness of MARKET_STATE.json and MARKET_INDICATORS.json."""
    market_state = workspace / "MARKET_STATE.json"
    market_ind = workspace / "MARKET_INDICATORS.json"

    results = []
    all_fresh = True

    for file_path in [market_state, market_ind]:
        age = _file_age_seconds(file_path)
        if age is None:
            results.append(f"  {file_path.name}: MISSING")
            all_fresh = False
            continue

        try:
            with open(file_path) as f:
                data = json.load(f)
            ts = data.get("timestamp") if isinstance(data, dict) else None
            if ts:
                lag = validate_freshness(ts, threshold_seconds=threshold, context=file_path.name)
                results.append(f"  {file_path.name}: lag={lag:.2f}s (fresh)")
            else:
                results.append(f"  {file_path.name}: lag={age:.2f}s (no timestamp, using mtime)")
                if age > threshold:
                    all_fresh = False
        except StaleDataError as e:
            results.append(f"  {file_path.name}: STALE ({e})")
            all_fresh = False
        except Exception as e:
            results.append(f"  {file_path.name}: ERROR ({e})")
            all_fresh = False

    return all_fresh, "\n".join(results)


def check_db_funding(threshold: float) -> Tuple[bool, str]:
    """Check freshness of funding rates in the database."""
    try:
        conn = _get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, funding_rate, recorded_at
                FROM derivatives_snapshots
                ORDER BY recorded_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return False, "  DB Funding: NO DATA"

            symbol, rate, recorded_at = row
            lag = validate_freshness(recorded_at, threshold_seconds=threshold, context="db_funding")
            return True, f"  DB Funding: {symbol} rate={rate:.6f} lag={lag:.2f}s (fresh)"
    except StaleDataError as e:
        return False, f"  DB Funding: STALE ({e})"
    except Exception as e:
        return False, f"  DB Funding: ERROR ({e})"


def check_db_candles(threshold: float) -> Tuple[bool, str]:
    """Check freshness of candle data in the database."""
    try:
        conn = _get_db_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT symbol, timeframe, timestamp
                FROM candles
                ORDER BY timestamp DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                return False, "  DB Candles: NO DATA"

            symbol, tf, ts = row
            lag = validate_freshness(ts, threshold_seconds=threshold, context="db_candles")
            return True, f"  DB Candles: {symbol} {tf} lag={lag:.2f}s (fresh)"
    except StaleDataError as e:
        return False, f"  DB Candles: STALE ({e})"
    except Exception as e:
        return False, f"  DB Candles: ERROR ({e})"


def check_llm_state(workspace: Path, threshold: float) -> Tuple[bool, str]:
    """Check freshness of LLM runner state files.

    The legacy ``TRADE_STATE.md`` overlay is no longer written (state lives in
    mem0 and the ``.surgeon_state.json`` sidecar), so we only check the JSON
    sidecar for freshness.
    """
    state_file = workspace / ".surgeon_state.json"

    results = []
    all_fresh = True

    age = _file_age_seconds(state_file)
    if age is None:
        results.append(f"  {state_file.name}: MISSING")
        all_fresh = False
    elif age > threshold:
        results.append(f"  {state_file.name}: STALE age={age:.2f}s")
        all_fresh = False
    else:
        results.append(f"  {state_file.name}: age={age:.2f}s (fresh)")

    return all_fresh, "\n".join(results)


def check_scanner_output(output_dir: Path, threshold: float) -> Tuple[bool, str]:
    """Check freshness of scanner output files."""
    market_state = output_dir / "MARKET_STATE.json"
    market_ind = output_dir / "MARKET_INDICATORS.json"

    results = []
    all_fresh = True

    for file_path in [market_state, market_ind]:
        age = _file_age_seconds(file_path)
        if age is None:
            results.append(f"  Scanner {file_path.name}: MISSING")
            all_fresh = False
            continue

        try:
            with open(file_path) as f:
                data = json.load(f)
            ts = data.get("timestamp")
            if ts:
                lag = validate_freshness(ts, threshold_seconds=threshold, context=f"scanner_{file_path.name}")
                results.append(f"  Scanner {file_path.name}: lag={lag:.2f}s (fresh)")
            else:
                results.append(f"  Scanner {file_path.name}: lag={age:.2f}s (no timestamp, using mtime)")
                if age > threshold:
                    all_fresh = False
        except StaleDataError as e:
            results.append(f"  Scanner {file_path.name}: STALE ({e})")
            all_fresh = False
        except Exception as e:
            results.append(f"  Scanner {file_path.name}: ERROR ({e})")
            all_fresh = False

    return all_fresh, "\n".join(results)


def run_audit(workspaces: List[Path], scanner_dirs: List[Path], threshold: float) -> Dict[str, Any]:
    """Run a comprehensive freshness audit across all configured paths."""
    logger.info("Starting system freshness audit (threshold=%.0fs)", threshold)

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "threshold_seconds": threshold,
        "overall_fresh": True,
        "checks": {},
    }

    # Check workspaces (LLM runners)
    for ws in workspaces:
        fresh, msg = check_market_state(ws, threshold)
        results["checks"][f"market_state_{ws.name}"] = {"fresh": fresh, "details": msg}
        if not fresh:
            results["overall_fresh"] = False

        fresh, msg = check_llm_state(ws, threshold)
        results["checks"][f"llm_state_{ws.name}"] = {"fresh": fresh, "details": msg}
        if not fresh:
            results["overall_fresh"] = False

    # Check scanner outputs
    for sd in scanner_dirs:
        fresh, msg = check_scanner_output(sd, threshold)
        results["checks"][f"scanner_{sd.name}"] = {"fresh": fresh, "details": msg}
        if not fresh:
            results["overall_fresh"] = False

    # Check database freshness
    fresh, msg = check_db_funding(threshold)
    results["checks"]["db_funding"] = {"fresh": fresh, "details": msg}
    if not fresh:
        results["overall_fresh"] = False

    fresh, msg = check_db_candles(threshold)
    results["checks"]["db_candles"] = {"fresh": fresh, "details": msg}
    if not fresh:
        results["overall_fresh"] = False

    return results


def print_report(results: Dict[str, Any]) -> None:
    """Print a human-readable freshness audit report."""
    print(f"\n{'='*60}")
    print(f"SYSTEM FRESHNESS AUDIT — {results['timestamp']}")
    print(f"Threshold: {results['threshold_seconds']:.0f}s")
    print(f"Overall Status: {'✅ FRESH' if results['overall_fresh'] else '❌ STALE DATA DETECTED'}")
    print(f"{'='*60}\n")

    for check_name, check_data in results["checks"].items():
        status = "✅" if check_data["fresh"] else "❌"
        print(f"{status} {check_name}")
        print(check_data["details"])
        print()

    print(f"{'='*60}")
    if not results["overall_fresh"]:
        print("WARNING: Stale data detected. Trading agents may be using outdated information.")
        print("Recommend: Check scanner daemons, database collectors, and network connectivity.")
    print(f"{'='*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="System-wide freshness audit for Tickles trading infrastructure")
    parser.add_argument("--workspace", type=str, nargs="+", default=["/root/.openclaw/workspace/rubicon_surgeon"],
                        help="Workspace directories to check")
    parser.add_argument("--scanner-dir", type=str, nargs="+", default=["/root/.openclaw/workspace/rubicon_surgeon"],
                        help="Scanner output directories to check")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_SECONDS,
                        help=f"Freshness threshold in seconds (default: {DEFAULT_THRESHOLD_SECONDS})")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--quiet", action="store_true", help="Only output on failure")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if not args.quiet else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")

    workspaces = [Path(p) for p in args.workspace]
    scanner_dirs = [Path(p) for p in args.scanner_dir]

    results = run_audit(workspaces, scanner_dirs, args.threshold)

    if args.json:
        print(json.dumps(results, indent=2))
    elif not args.quiet or not results["overall_fresh"]:
        print_report(results)

    sys.exit(0 if results["overall_fresh"] else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
