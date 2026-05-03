"""Module: llm_spend_tracker
Purpose: Total LLM spend tracker across all roles, companies, and agents.
Location: /opt/tickles/shared/utils/llm_spend_tracker.py
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import asyncpg

from shared.utils.db import get_shared_pool


@dataclass
class SpendSummary:
    """Roll-up of LLM spend for a given time period."""
    period: str                    # 'day', 'week', 'month'
    period_start: datetime
    total_usd: float
    by_role: Dict[str, float]
    by_company: Dict[str, float]
    by_agent: Dict[str, float]
    call_count: int
    total_tokens: int


async def get_spend_summary(
    period: str = "day",
    company_id: Optional[str] = None,
    role: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> SpendSummary:
    """Roll up api_cost_log for the given period.

    No hard caps — this is a visibility/dashboard tool. The loop detector
    handles runaway spend prevention.

    Args:
        period: 'day' | 'week' | 'month' — looks back 1 period from now.
        company_id: Optional filter — only spend for this company.
        role: Optional filter — only spend for this role.
        agent_id: Optional filter — only spend for this agent.

    Returns:
        SpendSummary with breakdowns by role, company, and agent.

    Raises:
        ValueError: If period is not one of 'day', 'week', 'month'.
        asyncpg.PostgresError: On database failure.
    """
    now = datetime.now(timezone.utc)
    if period == "day":
        start = now - timedelta(days=1)
    elif period == "week":
        start = now - timedelta(weeks=1)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        raise ValueError(f"Unknown period: {period}")

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # Build WHERE clause with parameterized filters
        where_clauses = ["created_at >= $1"]
        params: List = [start]
        param_idx = 2

        if company_id is not None:
            where_clauses.append(f"company_id = ${param_idx}")
            params.append(company_id)
            param_idx += 1
        if role is not None:
            where_clauses.append(f"role = ${param_idx}")
            params.append(role)
            param_idx += 1
        if agent_id is not None:
            where_clauses.append(f"agent_id = ${param_idx}")
            params.append(agent_id)
            param_idx += 1

        where_sql = " AND ".join(where_clauses)

        row = await conn.fetchrow(
            f"""
            SELECT
                COALESCE(SUM(cost_usd), 0) as total_usd,
                COUNT(*) as call_count,
                COALESCE(SUM(tokens_in + tokens_out), 0) as total_tokens
            FROM api_cost_log
            WHERE {where_sql}
            """,
            *params,
        )

        # By-role breakdown
        role_rows = await conn.fetch(
            f"""
            SELECT role, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql}
            GROUP BY role
            ORDER BY usd DESC
            """,
            *params,
        )

        # By-company breakdown
        company_rows = await conn.fetch(
            f"""
            SELECT company_id, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql}
            GROUP BY company_id
            ORDER BY usd DESC
            """,
            *params,
        )

        # By-agent breakdown
        agent_rows = await conn.fetch(
            f"""
            SELECT agent_id, COALESCE(SUM(cost_usd), 0) as usd
            FROM api_cost_log
            WHERE {where_sql} AND agent_id IS NOT NULL
            GROUP BY agent_id
            ORDER BY usd DESC
            """,
            *params,
        )

    return SpendSummary(
        period=period,
        period_start=start,
        total_usd=float(row["total_usd"]),
        by_role={r["role"]: float(r["usd"]) for r in role_rows},
        by_company={
            (r["company_id"] or "unknown"): float(r["usd"])
            for r in company_rows
        },
        by_agent={r["agent_id"]: float(r["usd"]) for r in agent_rows},
        call_count=int(row["call_count"]),
        total_tokens=int(row["total_tokens"]),
    )


async def get_daily_spend_series(
    days: int = 30,
    company_id: Optional[str] = None,
) -> List[Dict]:
    """Return daily spend for the last N days, one row per day.

    Args:
        days: Number of days to look back.
        company_id: Optional filter for a specific company.

    Returns:
        List of dicts: [{"date": "2026-04-29", "total_usd": 12.34, "call_count": 56}, ...]

    Raises:
        asyncpg.PostgresError: On database failure.
    """
    start = datetime.now(timezone.utc) - timedelta(days=days)
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        company_filter = "AND company_id = $2" if company_id else ""
        params = [start, company_id] if company_id else [start]

        rows = await conn.fetch(
            f"""
            SELECT
                (created_at AT TIME ZONE 'UTC')::date as day,
                COALESCE(SUM(cost_usd), 0) as total_usd,
                COUNT(*) as call_count
            FROM api_cost_log
            WHERE created_at >= $1 {company_filter}
            GROUP BY day
            ORDER BY day DESC
            """,
            *params,
        )
    return [
        {
            "date": str(r["day"]),
            "total_usd": float(r["total_usd"]),
            "call_count": int(r["call_count"]),
        }
        for r in rows
    ]
