"""
Module: recompute_edge_scores
Purpose: 90-day historical backfill for edge_score recomputation.
Location: /opt/tickles/shared/scripts/recompute_edge_scores.py
"""

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from shared.intelligence.edge_scorer import FORMULA_VERSION, ScorerInputs, compute_edge_score
from shared.intelligence.edge_scorer_service import EdgeScorerService
from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)


async def backfill(
    company_id: str = "jarvais",
    days: int = 90,
    dry_run: bool = True,
) -> dict:
    """Recompute edge scores for the last N days.

    Args:
        company_id: Company database to target.
        days: Number of days to backfill.
        dry_run: If True, compute but do not write.

    Returns:
        Dict with actors_scored and windows_processed.
    """
    service = EdgeScorerService(company_id=company_id)
    pool = await get_shared_pool()
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)

    async with pool.acquire() as conn:
        actors = await conn.fetch(
            """
            SELECT DISTINCT actor_type, actor_id
            FROM tracked_positions
            WHERE status = 'closed'
              AND closed_at >= $1
            """,
            start,
        )

    total_windows = 0
    for row in actors:
        actor_type = row["actor_type"]
        actor_id = row["actor_id"]
        for window in ("7d", "30d", "90d", "all"):
            try:
                async with pool.acquire() as conn:
                    inputs = await service._fetch_inputs(conn, actor_type, actor_id, window)
                    if len(inputs.closed_positions) < 3:
                        continue
                    out = compute_edge_score(inputs)
                    if not dry_run:
                        await service._upsert(conn, actor_type, actor_id, window, out)
                    total_windows += 1
            except Exception as exc:
                logger.exception(
                    "backfill error %s/%s/%s: %s",
                    actor_type,
                    actor_id,
                    window,
                    exc,
                )

    return {
        "actors_scored": len(actors),
        "windows_processed": total_windows,
        "days": days,
        "dry_run": dry_run,
        "formula_version": FORMULA_VERSION,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Recompute edge scores historically")
    parser.add_argument("--company", default="jarvais", help="Company slug")
    parser.add_argument("--days", type=int, default=90, help="Days to backfill")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Compute but do not write")
    args = parser.parse_args()

    result = asyncio.run(backfill(company_id=args.company, days=args.days, dry_run=args.dry_run))
    logger.info("Backfill complete: %s", result)


if __name__ == "__main__":
    main()
