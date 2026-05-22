"""
Module: award_achievements
Purpose: Check for and award gamification achievements to agents.
Location: /opt/tickles/shared/scripts/award_achievements.py

Runs via cron every hour. Checks tracked_positions for achievement
milestones and inserts rows into public.agent_achievements.

Achievement types:
  - first_trade: Agent has at least 1 tracked_position row
  - first_profitable_close: Agent has at least 1 closed position with positive PnL
  - win_streak_3: Agent has 3 consecutive winning closed trades
  - positive_week: Agent has positive total PnL over the last 7 days
  - trades_10: Agent has 10+ total tracked_positions
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

ACHIEVEMENT_CHECKS = [
    {
        "type": "first_trade",
        "label": "First Trade Made",
        "query": """
            SELECT DISTINCT actor_id
            FROM public.tracked_positions
            WHERE actor_id IS NOT NULL
              AND actor_id = $1
            LIMIT 1
        """,
    },
    {
        "type": "first_profitable_close",
        "label": "First Profitable Close",
        "query": """
            SELECT DISTINCT actor_id
            FROM public.tracked_positions
            WHERE actor_id IS NOT NULL
              AND actor_id = $1
              AND status <> 'open'
              AND realized_pnl_usd_final IS NOT NULL
              AND realized_pnl_usd_final > 0
            LIMIT 1
        """,
    },
    {
        "type": "win_streak_3",
        "label": "3-Trade Win Streak",
        "query": """
            WITH ranked AS (
                SELECT
                    actor_id,
                    realized_pnl_usd_final,
                    closed_at,
                    CASE WHEN realized_pnl_usd_final > 0 THEN 1 ELSE 0 END AS is_win
                FROM public.tracked_positions
                WHERE actor_id = $1
                  AND status <> 'open'
                  AND realized_pnl_usd_final IS NOT NULL
                  AND closed_at IS NOT NULL
                ORDER BY closed_at DESC
            )
            SELECT actor_id FROM ranked
            WHERE is_win = 1
            GROUP BY actor_id
            HAVING COUNT(*) >= 3
              AND SUM(is_win) FILTER (
                WHERE closed_at IN (
                    SELECT closed_at FROM ranked r2
                    WHERE r2.actor_id = ranked.actor_id
                    ORDER BY closed_at DESC LIMIT 3
                )
              ) = 3
            LIMIT 1
        """,
    },
    {
        "type": "positive_week",
        "label": "Positive Week",
        "query": """
            SELECT actor_id
            FROM public.tracked_positions
            WHERE actor_id = $1
              AND status <> 'open'
              AND realized_pnl_usd_final IS NOT NULL
              AND COALESCE(closed_at, updated_at, created_at) >= NOW() - INTERVAL '7 days'
            GROUP BY actor_id
            HAVING SUM(COALESCE(realized_pnl_usd_final, 0)) > 0
            LIMIT 1
        """,
    },
    {
        "type": "trades_10",
        "label": "10+ Trades Milestone",
        "query": """
            SELECT actor_id
            FROM public.tracked_positions
            WHERE actor_id = $1
            GROUP BY actor_id
            HAVING COUNT(*) >= 10
            LIMIT 1
        """,
    },
]


async def fetch_distinct_agents(pool) -> List[str]:
    """Fetch all distinct agent_ids from tracked_positions.

    Args:
        pool: Shared Postgres pool.

    Returns:
        List of agent_id strings.
    """
    rows = await pool.fetch_all(
        "SELECT DISTINCT actor_id FROM public.tracked_positions "
        "WHERE actor_id IS NOT NULL ORDER BY actor_id"
    )
    return [r["actor_id"] for r in rows]


async def check_and_award(
    pool,
    agent_id: str,
    achievement: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> bool:
    """Check if an agent qualifies for an achievement and award it.

    Args:
        pool: Shared Postgres pool.
        agent_id: The agent to check.
        achievement: Achievement definition dict with type, label, query.
        dry_run: If True, log only.

    Returns:
        True if the achievement was awarded (or would be).
    """
    ach_type = achievement["type"]

    # Check if already awarded
    existing = await pool.fetch_one(
        "SELECT 1 FROM public.agent_achievements "
        "WHERE agent_id = $1 AND achievement_type = $2 LIMIT 1",
        (agent_id, ach_type),
    )
    if existing is not None:
        return False

    # Check qualification
    try:
        row = await pool.fetch_one(achievement["query"], (agent_id,))
    except Exception as exc:
        logger.warning(
            "Achievement check failed for %s/%s: %s",
            agent_id,
            ach_type,
            exc,
        )
        return False

    if row is None:
        return False

    if dry_run:
        logger.info(
            "[DRY-RUN] Would award '%s' to %s",
            achievement["label"],
            agent_id,
        )
        return True

    try:
        import json as _json

        await pool.execute(
            "INSERT INTO public.agent_achievements "
            "(agent_id, achievement_type, metadata) "
            "VALUES ($1, $2, $3::jsonb) "
            "ON CONFLICT (agent_id, achievement_type) DO NOTHING",
            (
                agent_id,
                ach_type,
                _json.dumps({
                    "label": achievement["label"],
                    "awarded_at": datetime.now(timezone.utc).isoformat(),
                }),
            ),
        )
        logger.info(
            "Awarded '%s' to %s",
            achievement["label"],
            agent_id,
        )
        return True
    except Exception as exc:
        logger.exception(
            "Failed to award '%s' to %s: %s",
            achievement["label"],
            agent_id,
            exc,
        )
        return False


async def award_all(pool, *, dry_run: bool = False) -> int:
    """Check all agents against all achievements.

    Args:
        pool: Shared Postgres pool.
        dry_run: If True, log only.

    Returns:
        Number of achievements awarded.
    """
    agents = await fetch_distinct_agents(pool)
    logger.info("Checking %s agent(s) for achievements", len(agents))

    awarded = 0
    for agent_id in agents:
        for achievement in ACHIEVEMENT_CHECKS:
            result = await check_and_award(
                pool, agent_id, achievement, dry_run=dry_run
            )
            if result:
                awarded += 1

    return awarded


async def main(dry_run: bool = False) -> None:
    """Entry point: connect, check, award, report.

    Args:
        dry_run: If True, no writes are performed.
    """
    pool = await get_shared_pool()
    try:
        total = await award_all(pool, dry_run=dry_run)
        logger.info(
            "%s %s achievement(s)",
            "[DRY-RUN] Would award" if dry_run else "Awarded",
            total,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Award gamification achievements to agents"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write to DB (default: dry-run only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    asyncio.run(main(dry_run=not args.apply))
