"""
Module: compute_drift_indicators
Purpose: Compute drift scores for pending tracked_positions. Drift measures how
         far price has moved away from entry over time. High drift scores flag
         positions for manual review.
         drift_score = (time_pending_hours * distance_from_entry_pct) / 100
         Run with --apply flag to execute; dry-run otherwise.
Location: /opt/tickles/shared/scripts/compute_drift_indicators.py
"""

import argparse
import asyncio
import logging
import os
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

logger = logging.getLogger("tickles.scripts.compute_drift_indicators")

DRIFT_THRESHOLD = float(os.environ.get("DRIFT_SCORE_THRESHOLD", "5.0"))


async def compute_drift_indicators(pool, apply_flag: bool) -> dict:
    """Compute drift scores for all pending positions.

    Drift formula: (hours_pending * abs(distance_from_entry_pct)) / 100
    Higher drift = price has moved further from entry over longer time.

    Args:
        pool: Shared Postgres pool.
        apply_flag: If True, write drift_score to DB. If False, dry-run only.

    Returns:
        Summary dict with counts and flagged positions.
    """
    rows = await pool.fetch_all(
        """
        SELECT id, instrument_symbol, direction, entry_price, current_price,
               distance_to_entry_pct,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 AS hours_pending,
               created_at
        FROM public.tracked_positions
        WHERE status = 'pending'
        ORDER BY created_at
        """
    )
    count = len(rows)
    logger.info("Found %s pending positions for drift computation", count)

    if count == 0:
        logger.info("No pending positions to compute drift for.")
        return {"found": 0, "updated": 0, "flagged": 0}

    flagged: list[dict] = []
    updates: list[tuple[float, int]] = []

    for row in rows:
        hours = float(row["hours_pending"] or 0)
        dist_pct = abs(float(row["distance_to_entry_pct"] or 0))
        drift = (hours * dist_pct) / 100.0

        logger.info(
            "  id=%s symbol=%s hours=%.1f dist_pct=%.2f drift=%.4f",
            row["id"],
            row["instrument_symbol"],
            hours,
            dist_pct,
            drift,
        )

        updates.append((drift, row["id"]))

        if drift > DRIFT_THRESHOLD:
            flagged.append({
                "id": row["id"],
                "symbol": row["instrument_symbol"],
                "direction": row["direction"],
                "hours_pending": round(hours, 1),
                "distance_from_entry_pct": round(dist_pct, 2),
                "drift_score": round(drift, 4),
            })

    if flagged:
        logger.warning(
            "%s positions flagged with drift_score > %s:",
            len(flagged),
            DRIFT_THRESHOLD,
        )
        for f in flagged:
            logger.warning(
                "  FLAGGED id=%s symbol=%s direction=%s hours=%.1f dist=%.2f%% drift=%.4f",
                f["id"],
                f["symbol"],
                f["direction"],
                f["hours_pending"],
                f["distance_from_entry_pct"],
                f["drift_score"],
            )

    if not apply_flag:
        logger.info(
            "DRY RUN: would update drift_score for %s positions. "
            "Re-run with --apply to execute.",
            count,
        )
        return {"found": count, "updated": 0, "flagged": len(flagged), "dry_run": True}

    # Batch update drift_score for all pending positions
    for drift, pos_id in updates:
        await pool.execute(
            "UPDATE public.tracked_positions SET drift_score = $1, updated_at = NOW() WHERE id = $2",
            (drift, pos_id),
        )

    logger.info("Updated drift_score for %s positions", count)
    return {"found": count, "updated": count, "flagged": len(flagged)}


async def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Compute drift indicators for pending tracked_positions."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the drift score update (default is dry-run).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    pool = await get_shared_pool()
    try:
        result = await compute_drift_indicators(pool, args.apply)
        if result.get("dry_run"):
            logger.info("Dry run complete. Use --apply to execute.")
        else:
            logger.info(
                "Drift computation complete: found=%s updated=%s flagged=%s",
                result["found"],
                result["updated"],
                result["flagged"],
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
