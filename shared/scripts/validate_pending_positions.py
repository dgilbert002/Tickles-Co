"""
Module: validate_pending_positions
Purpose: Periodic validation of pending tracked_positions. Fetches current price
         for each pending position and checks if entry/SL/TP levels are still
         valid. Flags positions where price has moved >20% away from entry
         (likely invalid signal) for manual review.
         Run with --apply flag to execute; dry-run otherwise.
Location: /opt/tickles/shared/scripts/validate_pending_positions.py
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

logger = logging.getLogger("tickles.scripts.validate_pending_positions")

INVALID_DISTANCE_THRESHOLD_PCT = float(
    os.environ.get("VALIDATE_DISTANCE_THRESHOLD_PCT", "20.0")
)


async def validate_pending_positions(pool, apply_flag: bool) -> dict:
    """Validate all pending positions against current price.

    For each pending position, checks whether the current price has moved
    more than INVALID_DISTANCE_THRESHOLD_PCT away from the entry price.
    Such positions are flagged for manual review as the original signal
    is likely no longer valid.

    Args:
        pool: Shared Postgres pool.
        apply_flag: If True, update status_reason for flagged positions.
                    If False, dry-run only.

    Returns:
        Summary dict with counts and flagged positions.
    """
    rows = await pool.fetch_all(
        """
        SELECT id, instrument_symbol, direction, entry_price, current_price,
               stop_loss, take_profit_1,
               distance_to_entry_pct,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0 AS hours_pending,
               created_at, signal_timestamp
        FROM public.tracked_positions
        WHERE status = 'pending'
        ORDER BY created_at
        """
    )
    count = len(rows)
    logger.info("Found %s pending positions for validation", count)

    if count == 0:
        logger.info("No pending positions to validate.")
        return {"found": 0, "flagged": 0, "valid": 0, "updated": 0}

    flagged: list[dict] = []
    valid_count = 0

    for row in rows:
        pos_id = row["id"]
        symbol = row["instrument_symbol"]
        direction = row["direction"]
        entry_price = float(row["entry_price"]) if row["entry_price"] is not None else None
        current_price = float(row["current_price"]) if row["current_price"] is not None else None
        dist_pct = abs(float(row["distance_to_entry_pct"] or 0))
        hours = float(row["hours_pending"] or 0)

        if entry_price is None or current_price is None:
            logger.warning(
                "  SKIP id=%s symbol=%s: missing entry_price or current_price",
                pos_id, symbol,
            )
            continue

        # Check if SL/TP levels are crossed (signal invalidated)
        sl = float(row["stop_loss"]) if row["stop_loss"] is not None else None
        tp = float(row["take_profit_1"]) if row["take_profit_1"] is not None else None

        issues: list[str] = []

        if dist_pct > INVALID_DISTANCE_THRESHOLD_PCT:
            issues.append(
                f"price_moved_{dist_pct:.1f}pct_from_entry"
            )

        if direction == "long":
            if sl is not None and current_price <= sl:
                issues.append("price_below_sl")
            if tp is not None and current_price >= tp:
                issues.append("price_above_tp")
        elif direction == "short":
            if sl is not None and current_price >= sl:
                issues.append("price_above_sl")
            if tp is not None and current_price <= tp:
                issues.append("price_below_tp")

        if issues:
            flagged.append({
                "id": pos_id,
                "symbol": symbol,
                "direction": direction,
                "entry_price": entry_price,
                "current_price": current_price,
                "distance_pct": round(dist_pct, 2),
                "hours_pending": round(hours, 1),
                "issues": issues,
            })
            logger.warning(
                "  FLAGGED id=%s symbol=%s direction=%s entry=%.4f current=%.4f "
                "dist=%.2f%% hours=%.1f issues=%s",
                pos_id, symbol, direction, entry_price, current_price,
                dist_pct, hours, ",".join(issues),
            )
        else:
            valid_count += 1
            logger.info(
                "  OK id=%s symbol=%s direction=%s dist=%.2f%% hours=%.1f",
                pos_id, symbol, direction, dist_pct, hours,
            )

    logger.info(
        "Validation summary: %s valid, %s flagged, %s total",
        valid_count, len(flagged), count,
    )

    if not apply_flag:
        logger.info(
            "DRY RUN: would flag %s positions for review. "
            "Re-run with --apply to update status_reason.",
            len(flagged),
        )
        return {
            "found": count,
            "flagged": len(flagged),
            "valid": valid_count,
            "updated": 0,
            "dry_run": True,
        }

    # Update status_reason for flagged positions
    updated = 0
    for f in flagged:
        reason = "validation_flagged: " + ", ".join(f["issues"])
        await pool.execute(
            """
            UPDATE public.tracked_positions
            SET status_reason = $1, updated_at = NOW()
            WHERE id = $2
            """,
            (reason, f["id"]),
        )
        updated += 1

    logger.info("Updated status_reason for %s flagged positions", updated)
    return {
        "found": count,
        "flagged": len(flagged),
        "valid": valid_count,
        "updated": updated,
    }


async def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Validate pending tracked_positions against current price."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the validation update (default is dry-run).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    pool = await get_shared_pool()
    try:
        result = await validate_pending_positions(pool, args.apply)
        if result.get("dry_run"):
            logger.info("Dry run complete. Use --apply to execute.")
        else:
            logger.info(
                "Validation complete: found=%s valid=%s flagged=%s updated=%s",
                result["found"],
                result["valid"],
                result["flagged"],
                result["updated"],
            )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
