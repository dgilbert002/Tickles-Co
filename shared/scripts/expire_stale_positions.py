"""
Module: expire_stale_positions
Purpose: Expire pending tracked_positions older than 7 days that never reached
         entry price. Sets status='expired', status_reason='entry_never_reached'.
         Run with --apply flag to execute; dry-run otherwise.
Location: /opt/tickles/shared/scripts/expire_stale_positions.py
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.db import get_shared_pool

logger = logging.getLogger("tickles.scripts.expire_stale_positions")

EXPIRY_DAYS = 7


async def expire_stale_positions(pool, apply_flag: bool) -> dict:
    """Find and expire pending positions older than EXPIRY_DAYS.

    Args:
        pool: Shared Postgres pool.
        apply_flag: If True, execute the UPDATE. If False, dry-run only.

    Returns:
        Summary dict with counts.
    """
    rows = await pool.fetch_all(
        """
        SELECT id, instrument_symbol, direction, created_at,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 AS hours_pending
        FROM public.tracked_positions
        WHERE status = 'pending'
          AND created_at < NOW() - make_interval(days => $1)
        ORDER BY created_at
        """,
        (EXPIRY_DAYS,),
    )
    count = len(rows)
    logger.info("Found %s stale pending positions (older than %s days)", count, EXPIRY_DAYS)

    if count == 0:
        logger.info("No stale pending positions to expire.")
        return {"found": 0, "expired": 0}

    for row in rows:
        logger.info(
            "  id=%s symbol=%s direction=%s pending_since=%s hours_pending=%.1f",
            row["id"],
            row["instrument_symbol"],
            row["direction"],
            row["created_at"],
            float(row["hours_pending"] or 0),
        )

    if not apply_flag:
        logger.info(
            "DRY RUN: would expire %s positions. Re-run with --apply to execute.",
            count,
        )
        return {"found": count, "expired": 0, "dry_run": True}

    await pool.execute(
        """
        UPDATE public.tracked_positions
        SET status = 'expired',
            status_reason = 'entry_never_reached',
            updated_at = NOW()
        WHERE status = 'pending'
          AND created_at < NOW() - make_interval(days => $1)
        """,
        (EXPIRY_DAYS,),
    )
    logger.info("Expired %s stale pending positions", count)

    return {"found": count, "expired": count}


async def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Expire pending tracked_positions older than 7 days."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the expiry (default is dry-run).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    pool = await get_shared_pool()
    try:
        result = await expire_stale_positions(pool, args.apply)
        if result.get("dry_run"):
            logger.info("Dry run complete. Use --apply to execute.")
        else:
            logger.info(
                "Expiry complete: found=%s expired=%s",
                result["found"],
                result["expired"],
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
