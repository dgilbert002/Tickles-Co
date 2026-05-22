"""
Module: score_competitions
Purpose: Compute participant P&L from tracked_positions and update contest scores.
Location: /opt/tickles/shared/scripts/score_competitions.py

Runs via cron every hour. Reads active contests and their participants,
aggregates realized P&L per participant from tracked_positions, and
updates contest_participants.scores JSONB column.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

# Contest statuses that are eligible for scoring
ACTIVE_STATUSES = ("active",)

# P&L metrics we compute per participant
PNL_METRICS = {
    "total_realized_pnl_usd": "COALESCE(SUM(realized_pnl_usd_final), 0)",
    "total_trades": "COUNT(*)",
    "winning_trades": (
        "COUNT(*) FILTER (WHERE realized_pnl_usd_final IS NOT NULL "
        "AND realized_pnl_usd_final > 0)"
    ),
    "losing_trades": (
        "COUNT(*) FILTER (WHERE realized_pnl_usd_final IS NOT NULL "
        "AND realized_pnl_usd_final < 0)"
    ),
    "breakeven_trades": (
        "COUNT(*) FILTER (WHERE realized_pnl_usd_final IS NOT NULL "
        "AND realized_pnl_usd_final = 0)"
    ),
    "best_trade_usd": "COALESCE(MAX(realized_pnl_usd_final), 0)",
    "worst_trade_usd": "COALESCE(MIN(realized_pnl_usd_final), 0)",
    "open_positions": (
        "COUNT(*) FILTER (WHERE status = 'open')"
    ),
    "unrealized_pnl_usd": (
        "COALESCE(SUM(CASE WHEN status = 'open' "
        "THEN COALESCE(unrealized_pnl_usd, 0) ELSE 0 END), 0)"
    ),
}


async def fetch_active_contests(pool) -> List[Dict[str, Any]]:
    """Fetch all contests that are currently active and eligible for scoring.

    Args:
        pool: Shared Postgres pool.

    Returns:
        List of active contest dicts.
    """
    rows = await pool.fetch_all(
        "SELECT id, name, venues, coins, starting_balance_usd, "
        "status, created_at, ends_at, metadata "
        "FROM contests WHERE status = ANY($1::text[])",
        (list(ACTIVE_STATUSES),),
    )
    return [dict(r) for r in rows]


async def fetch_contest_participants(
    pool, contest_id: str
) -> List[Dict[str, Any]]:
    """Fetch all participants for a given contest.

    Args:
        pool: Shared Postgres pool.
        contest_id: Contest UUID/id.

    Returns:
        List of participant dicts.
    """
    rows = await pool.fetch_all(
        "SELECT contest_id, company_id, agent_id, strategy_ref, "
        "joined_at, COALESCE(scores, '{}'::jsonb) AS scores, "
        "COALESCE(metadata, '{}'::jsonb) AS metadata "
        "FROM contest_participants WHERE contest_id = $1",
        (contest_id,),
    )
    return [dict(r) for r in rows]


async def compute_participant_pnl(
    pool, participant: Dict[str, Any], contest: Dict[str, Any]
) -> Dict[str, Any]:
    """Compute P&L metrics for one contest participant.

    Queries tracked_positions filtered by agent_id and the contest's
    date range. Returns a dict of metric keys → values suitable for
    JSONB storage in contest_participants.scores.

    Args:
        pool: Shared Postgres pool.
        participant: A contest_participants row dict.
        contest: The parent contests row dict.

    Returns:
        Dict of computed metric values.
    """
    agent_id = participant.get("agent_id", "")
    if not agent_id:
        return {}

    ends_at = contest.get("ends_at")
    created_at = contest.get("created_at")

    conditions = ["actor_id = $1"]
    params: List[Any] = [agent_id]

    if created_at is not None:
        conditions.append(
            "COALESCE(signal_timestamp, created_at) >= $2"
        )
        params.append(created_at)
        param_idx = 3
    else:
        param_idx = 2

    if ends_at is not None:
        conditions.append(
            "COALESCE(closed_at, signal_timestamp, created_at) <= $" + str(param_idx)
        )
        params.append(ends_at)
        param_idx += 1

    where_clause = " AND ".join(conditions)

    metrics_select = ",\n            ".join(
        f"{alias} AS {name}" for name, alias in PNL_METRICS.items()
    )

    query = f"""
        SELECT
            {metrics_select}
        FROM public.tracked_positions
        WHERE {where_clause}
    """

    try:
        row = await pool.fetch_one(query, tuple(params))
        if row is None:
            return {k: 0 for k in PNL_METRICS}

        result: Dict[str, Any] = {}
        for key in PNL_METRICS:
            val = row.get(key)
            if isinstance(val, Decimal):
                result[key] = float(val)
            else:
                result[key] = int(val) if val is not None else 0

        result["computed_at"] = datetime.now(timezone.utc).isoformat()
        result["win_rate"] = (
            round(result["winning_trades"] / max(result["total_trades"], 1), 4)
        )
        result["starting_balance_usd"] = float(
            contest.get("starting_balance_usd", 50000) or 50000
        )
        result["equity"] = (
            result["starting_balance_usd"]
            + result["total_realized_pnl_usd"]
            + result["unrealized_pnl_usd"]
        )
        result["return_pct"] = round(
            (result["equity"] - result["starting_balance_usd"])
            / result["starting_balance_usd"]
            * 100,
            2,
        )

        return result
    except Exception as exc:
        logger.exception(
            "Failed to compute P&L for agent_id=%s contest=%s: %s",
            agent_id,
            contest.get("id"),
            exc,
        )
        return {}


async def update_participant_scores(
    pool,
    participant: Dict[str, Any],
    scores: Dict[str, Any],
    *,
    dry_run: bool = False,
) -> bool:
    """Update the scores JSONB column for one participant.

    Uses composite PK (contest_id, company_id, agent_id).

    Args:
        pool: Shared Postgres pool.
        participant: Participant row dict.
        scores: Computed metric dict.
        dry_run: If True, log only.

    Returns:
        True if updated (or would have been), False on error.
    """
    contest_id = participant.get("contest_id")
    company_id = participant.get("company_id")
    agent_id = participant.get("agent_id")
    if not all([contest_id, company_id, agent_id]):
        return False

    if dry_run:
        logger.info(
            "[DRY-RUN] Would update participant %s scores: equity=%s return=%s%%",
            agent_id,
            scores.get("equity", 0),
            scores.get("return_pct", 0),
        )
        return True

    try:
        import json as _json

        await pool.execute(
            "UPDATE contest_participants SET scores = $1::jsonb, "
            "metadata = $2::jsonb "
            "WHERE contest_id = $3 AND company_id = $4 AND agent_id = $5",
            (
                _json.dumps(scores),
                _json.dumps({"last_scored_at": datetime.now(timezone.utc).isoformat()}),
                contest_id,
                company_id,
                agent_id,
            ),
        )
        return True
    except Exception as exc:
        logger.exception(
            "Failed to update scores for participant %s/%s: %s",
            contest_id,
            agent_id,
            exc,
        )
        return False


async def score_all_contests(pool, *, dry_run: bool = False) -> int:
    """Score all active contests.

    Args:
        pool: Shared Postgres pool.
        dry_run: If True, log computed scores without writing.

    Returns:
        Number of participants scored.
    """
    contests = await fetch_active_contests(pool)
    logger.info("Found %s active contest(s)", len(contests))

    scored = 0
    for contest in contests:
        participants = await fetch_contest_participants(pool, contest["id"])
        logger.info(
            "Contest '%s' (%s): %s participant(s)",
            contest.get("name"),
            contest.get("id"),
            len(participants),
        )

        for participant in participants:
            scores = await compute_participant_pnl(pool, participant, contest)
            if scores:
                updated = await update_participant_scores(
                    pool, participant, scores, dry_run=dry_run
                )
                if updated:
                    scored += 1
                    logger.info(
                        "  %s: equity=$%.2f return=%.2f%% trades=%s",
                        participant.get("agent_id"),
                        scores.get("equity", 0),
                        scores.get("return_pct", 0),
                        scores.get("total_trades", 0),
                    )

    return scored


async def main(dry_run: bool = False) -> None:
    """Entry point: connect, score, report.

    Args:
        dry_run: If True, no writes are performed.
    """
    pool = await get_shared_pool()
    try:
        total = await score_all_contests(pool, dry_run=dry_run)
        logger.info(
            "%s %s participant(s) across all active contests",
            "[DRY-RUN] Would score" if dry_run else "Scored",
            total,
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Score competition participants from tracked_positions P&L"
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
