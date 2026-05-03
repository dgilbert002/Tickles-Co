"""
Module: writer_registry_grep
Purpose: Static-analysis writer-registry enforcement.
Location: /opt/tickles/shared/scripts/writer_registry_grep.py
"""

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Iterable, Tuple, Set, Dict

import asyncpg

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent.parent
SKIP_DIRS = {"tests", "_archive", "migration", "scripts", "reference", "artifacts", ".roo", ".github"}
SKIP_MARKER = "# writer-registry: test-only"
EXPLICIT_MARKER = re.compile(r"# writer-registry: ([\w_-]+)")
INSERT_RE = re.compile(r"INSERT\s+INTO\s+(?:public\.)?(\w+)", re.IGNORECASE)
UPDATE_RE = re.compile(r"UPDATE\s+(?:public\.)?(\w+)\s+SET", re.IGNORECASE)
DELETE_RE = re.compile(r"DELETE\s+FROM\s+(?:public\.)?(\w+)", re.IGNORECASE)

# Path → service inference. Override with explicit marker.
PATH_TO_SERVICE = {
    "intelligence/interpretation_service.py": "interpretation_service",
    "intelligence/postmortem_service.py": "postmortem_service",
    "intelligence/chart_hacker_opinion_service.py": "chart_hacker_opinion_service",
    "intelligence/edge_scorer_service.py": "edge_scorer_service",
    "intelligence/coach_service.py": "coach_service",
    "daemons/surgeon2_trader.py": "surgeon2_trader",
    "memu/listener_service.py": "memu_listener",
    # collectors
    "collectors/discord/discord_collector.py": "discord_collector",
    "collectors/twitter_collector.py": "twitter_collector",
    "collectors/telegram/telegram_collector.py": "telegram_collector",
}

def _infer_service(path: Path, content: str) -> str | None:
    """
    Infer the service name from the file path or an explicit marker.
    
    Args:
        path: The path to the file.
        content: The content of the file.
        
    Returns:
        The service name or None if not found.
    """
    m = EXPLICIT_MARKER.search(content)
    if m:
        return m.group(1)
    
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        return None
        
    for needle, svc in PATH_TO_SERVICE.items():
        if rel.endswith(needle):
            return svc
    return None

def _scan(content: str) -> Iterable[Tuple[str, str]]:
    """
    Scan file content for SQL write operations.
    
    Args:
        content: The content to scan.
        
    Yields:
        (operation, table) tuples.
    """
    for line in content.splitlines():
        if SKIP_MARKER in line:
            continue
        for m in INSERT_RE.finditer(line):
            yield ("INSERT", m.group(1))
        for m in UPDATE_RE.finditer(line):
            yield ("UPDATE", m.group(1))
        for m in DELETE_RE.finditer(line):
            yield ("DELETE", m.group(1))

async def main(dsn: str) -> int:
    """
    Main entry point for the writer-registry grep gate.
    
    Args:
        dsn: The PostgreSQL connection string.
        
    Returns:
        0 if OK, 1 if violations found.
    """
    try:
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
        if not pool:
            logger.error("Failed to create database pool")
            return 1
            
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT table_name, allowed_writer_services FROM public.table_writers")
            
        registry: Dict[str, Set[str]] = {r["table_name"]: set(r["allowed_writer_services"] or []) for r in rows}
        violations: list[str] = []
        
        logger.info(f"Scanning {ROOT} for writer-registry violations...")
        
        for path in ROOT.rglob("*.py"):
            if any(seg in SKIP_DIRS for seg in path.parts):
                continue
                
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                logger.error(f"Failed to read {path}: {e}")
                continue
                
            service = _infer_service(path, content)
            if service is None:
                continue
                
            for op, table in _scan(content):
                if table not in registry:
                    # Unregistered tables are ignored by this gate (handled by separate audit)
                    continue
                    
                allowed = registry[table]
                if service not in allowed:
                    violations.append(
                        f"{path.relative_to(ROOT)}: {op} on '{table}' by '{service}' "
                        f"not in allow-list {sorted(allowed)}"
                    )
                    
        await pool.close()
        
        if violations:
            print("[writer-registry] VIOLATIONS:")
            for v in violations:
                print(f"  - {v}")
            return 1
            
        print("[writer-registry] OK")
        return 0
    except Exception as e:
        logger.exception(f"Error during writer-registry scan: {e}")
        return 1

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Enforce writer-registry constraints via static analysis.")
    ap.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.dsn)))
