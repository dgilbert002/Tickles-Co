"""
Module: wipe_tracked_positions
Purpose: Back up existing tracked_positions rows to JSON, then DELETE all rows
         and reset the sequence. Run with --apply flag to execute.
Location: /opt/tickles/shared/scripts/wipe_tracked_positions.py
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.db import get_shared_pool

logger = logging.getLogger("tickles.scripts.wipe_tracked_positions")

BACKUP_DIR = Path("/opt/tickles/backups")


async def backup_rows(pool) -> list[dict]:
    """Fetch all rows from tracked_positions for backup.

    Args:
        pool: Shared Postgres pool.

    Returns:
        List of row dicts.
    """
    rows = await pool.fetch_all(
        "SELECT * FROM public.tracked_positions ORDER BY id"
    )
    return [dict(r) for r in rows]


def serialise_row(row: dict) -> dict:
    """Convert non-JSON-serialisable types (datetime, Decimal) to strings.

    Args:
        row: Raw row dict from asyncpg.

    Returns:
        JSON-safe dict.
    """
    clean: dict = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            clean[k] = v.isoformat()
        elif hasattr(v, "to_eng_string"):  # Decimal
            clean[k] = str(v)
        else:
            clean[k] = v
    return clean


async def wipe_tracked_positions(pool, apply_flag: bool) -> dict:
    """Back up and wipe tracked_positions table.

    Args:
        pool: Shared Postgres pool.
        apply_flag: If True, execute the DELETE. If False, dry-run only.

    Returns:
        Summary dict with counts.
    """
    rows = await backup_rows(pool)
    count = len(rows)
    logger.info("Found %s rows in tracked_positions", count)

    if count == 0:
        logger.info("tracked_positions is already empty; nothing to do.")
        return {"backed_up": 0, "deleted": 0}

    # Write backup
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"tracked_positions_backup_{timestamp}.json"
    serialised = [serialise_row(r) for r in rows]
    backup_path.write_text(
        json.dumps(serialised, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("Backed up %s rows to %s", count, backup_path)

    if not apply_flag:
        logger.info(
            "DRY RUN: would delete %s rows from tracked_positions. "
            "Re-run with --apply to execute.",
            count,
        )
        return {"backed_up": count, "deleted": 0, "dry_run": True}

    # Delete all rows and reset sequence
    await pool.execute("DELETE FROM public.tracked_positions")
    logger.info("Deleted %s rows from tracked_positions", count)

    # Reset the auto-increment sequence
    try:
        await pool.execute(
            "ALTER SEQUENCE public.tracked_positions_id_seq RESTART WITH 1"
        )
        logger.info("Reset tracked_positions_id_seq to 1")
    except Exception as exc:
        logger.warning(
            "Could not reset sequence (may not exist): %s", exc
        )

    return {"backed_up": count, "deleted": count}


async def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Back up and wipe tracked_positions table."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the wipe (default is dry-run).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    pool = await get_shared_pool()
    try:
        result = await wipe_tracked_positions(pool, args.apply)
        if result.get("dry_run"):
            logger.info("Dry run complete. Use --apply to execute.")
        else:
            logger.info(
                "Wipe complete: backed_up=%s deleted=%s",
                result["backed_up"],
                result["deleted"],
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
