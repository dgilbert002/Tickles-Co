"""
Module: contest
Purpose: MCP tools for paper trading contests
Location: /opt/tickles/shared/mcp/tools/contest.py
"""

import logging
import uuid
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext
from . import db_helper

logger = logging.getLogger(__name__)

# Constants
_DEFAULT_DURATION_DAYS = 7
_DEFAULT_STARTING_USD = 10000.0
_DEFAULT_VENUE = "bybit"

def _fmt_ts(val: Any) -> Optional[str]:
    """Format a datetime or string as ISO 8601."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)

def _paper_account_id(company_id: str, agent_id: str, venue: str) -> str:
    """Generate a deterministic paper account ID."""
    return f"paper_{company_id}_{agent_id}_{venue}"

def _handle_contest_create(p: Dict[str, Any]) -> Dict[str, Any]:
    """Create a new paper trading contest and provision wallets for agents.

    Args:
        p: MCP tool params with name, venues[], coins[], durationDays,
           startingPaperUsdPerAgent, agentIds[], companyId.

    Returns:
        Dict with status and contest details.
    """
    try:
        name = str(p["name"])
        venues = list(p.get("venues", [_DEFAULT_VENUE]))
        coins = list(p.get("coins", ["BTC", "ETH", "SOL"]))
        duration_days = float(p.get("durationDays", _DEFAULT_DURATION_DAYS))
        starting_usd = float(p.get("startingPaperUsdPerAgent", _DEFAULT_STARTING_USD))
        agent_ids = list(p.get("agentIds", []))
        company_id = str(p["companyId"])

        contest_id = str(uuid.uuid4())[:8]
        ends_at = datetime.now(timezone.utc) + timedelta(days=duration_days)

        # 1. Create contest row
        db_helper.execute(
            "INSERT INTO public.contests (id, name, venues, coins, starting_balance_usd, ends_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (contest_id, name, venues, coins, starting_usd, ends_at)
        )

        # 2. Provision agents
        provisioned = []
        for aid in agent_ids:
            # Join participant table
            db_helper.execute(
                "INSERT INTO public.contest_participants (contest_id, company_id, agent_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (contest_id, company_id, aid)
            )

            # Create paper wallets for each venue in the contest
            for venue in venues:
                account_id = _paper_account_id(company_id, aid, venue)
                
                # Upsert paper_wallets
                db_helper.execute(
                    "INSERT INTO public.paper_wallets "
                    "(company_id, agent_id, exchange, account_id_external, "
                    "starting_balance_usd, contest_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (company_id, agent_id, exchange) DO UPDATE SET "
                    "starting_balance_usd = EXCLUDED.starting_balance_usd, "
                    "contest_id = EXCLUDED.contest_id, "
                    "is_active = true",
                    (company_id, aid, venue, account_id, starting_usd, contest_id)
                )

                # Seed banker_balances
                db_helper.execute(
                    "INSERT INTO public.banker_balances "
                    "(company_id, exchange, account_id_external, account_type, "
                    "currency, balance, equity, margin_used, free_margin, source) "
                    "VALUES (%s, %s, %s, 'paper', 'USD', %s, %s, 0, %s, 'contest_seed')",
                    (company_id, venue, account_id, starting_usd, starting_usd, starting_usd)
                )
            
            provisioned.append(aid)

        return {
            "status": "ok",
            "contestId": contest_id,
            "name": name,
            "endsAt": _fmt_ts(ends_at),
            "agentsProvisioned": provisioned,
            "message": f"Contest '{name}' created with {len(provisioned)} agents."
        }
    except Exception as exc:
        logger.exception("contest.create failed")
        return {"status": "error", "message": f"failed to create contest: {exc}"}

def _handle_contest_join(p: Dict[str, Any]) -> Dict[str, Any]:
    """Join an existing contest.

    Args:
        p: MCP tool params with contestId, companyId, agentId, strategyRef.

    Returns:
        Dict with status.
    """
    try:
        contest_id = str(p["contestId"])
        company_id = str(p["companyId"])
        agent_id = str(p["agentId"])
        strategy_ref = p.get("strategyRef")

        # Verify contest exists and is active
        contest = db_helper.query(
            "SELECT venues, starting_balance_usd FROM public.contests WHERE id = %s AND status = 'active'",
            (contest_id,)
        )
        if not contest:
            return {"status": "error", "message": f"contest {contest_id} not found or not active"}

        venues = contest[0]["venues"]
        starting_usd = float(contest[0]["starting_balance_usd"])

        # Join participant table
        db_helper.execute(
            "INSERT INTO public.contest_participants (contest_id, company_id, agent_id, strategy_ref) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (contest_id, company_id, agent_id) DO UPDATE SET "
            "strategy_ref = EXCLUDED.strategy_ref",
            (contest_id, company_id, agent_id, strategy_ref)
        )

        # Provision wallets
        for venue in venues:
            account_id = _paper_account_id(company_id, agent_id, venue)
            db_helper.execute(
                "INSERT INTO public.paper_wallets "
                "(company_id, agent_id, exchange, account_id_external, "
                "starting_balance_usd, contest_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (company_id, agent_id, exchange) DO UPDATE SET "
                "starting_balance_usd = EXCLUDED.starting_balance_usd, "
                "contest_id = EXCLUDED.contest_id, "
                "is_active = true",
                (company_id, agent_id, venue, account_id, starting_usd, contest_id)
            )
            db_helper.execute(
                "INSERT INTO public.banker_balances "
                "(company_id, exchange, account_id_external, account_type, "
                "currency, balance, equity, margin_used, free_margin, source) "
                "VALUES (%s, %s, %s, 'paper', 'USD', %s, %s, 0, %s, 'contest_join')",
                (company_id, venue, account_id, starting_usd, starting_usd, starting_usd)
            )

        return {
            "status": "ok",
            "contestId": contest_id,
            "message": f"Agent {agent_id} joined contest {contest_id}."
        }
    except Exception as exc:
        logger.exception("contest.join failed")
        return {"status": "error", "message": f"failed to join contest: {exc}"}

def _handle_contest_leaderboard(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get the current leaderboard for a contest.

    Args:
        p: MCP tool params with contestId.

    Returns:
        Dict with ranked participants and their P&L.
    """
    try:
        contest_id = str(p["contestId"])

        # 1. Get contest info
        contest = db_helper.query(
            "SELECT name, starting_balance_usd, status FROM public.contests WHERE id = %s",
            (contest_id,)
        )
        if not contest:
            return {"status": "error", "message": f"contest {contest_id} not found"}

        starting_balance = float(contest[0]["starting_balance_usd"])

        # 2. Get latest equity for all participants in this contest
        # We sum equity across all venues for each agent in the contest
        sql = """
            SELECT 
                p.agent_id,
                p.company_id,
                p.strategy_ref,
                SUM(b.equity) as total_equity
            FROM public.contest_participants p
            JOIN public.paper_wallets w ON w.contest_id = p.contest_id 
                AND w.company_id = p.company_id AND w.agent_id = p.agent_id
            JOIN public.banker_balances b ON b.account_id_external = w.account_id_external
            WHERE p.contest_id = %s
            GROUP BY p.agent_id, p.company_id, p.strategy_ref
            ORDER BY total_equity DESC
        """
        rows = db_helper.query(sql, (contest_id,))

        rankings = []
        for i, r in enumerate(rows):
            equity = float(r["total_equity"])
            pnl_usd = equity - starting_balance
            pnl_pct = (pnl_usd / starting_balance * 100) if starting_balance > 0 else 0
            
            rankings.append({
                "rank": i + 1,
                "agentId": r["agent_id"],
                "companyId": r["company_id"],
                "strategy": r["strategy_ref"],
                "equity": equity,
                "pnlUsd": pnl_usd,
                "pnlPct": pnl_pct
            })

        return {
            "status": "ok",
            "contestId": contest_id,
            "contestName": contest[0]["name"],
            "contestStatus": contest[0]["status"],
            "rankings": rankings
        }
    except Exception as exc:
        logger.exception("contest.leaderboard failed")
        return {"status": "error", "message": f"failed to get leaderboard: {exc}"}

def _handle_contest_end(p: Dict[str, Any]) -> Dict[str, Any]:
    """Manually end a contest and freeze balances.

    Args:
        p: MCP tool params with contestId.

    Returns:
        Dict with status.
    """
    try:
        contest_id = str(p["contestId"])

        # Update status
        affected = db_helper.execute(
            "UPDATE public.contests SET status = 'ended' WHERE id = %s AND status = 'active'",
            (contest_id,)
        )
        if affected == 0:
            return {"status": "error", "message": f"contest {contest_id} not found or already ended"}

        # Deactivate wallets
        db_helper.execute(
            "UPDATE public.paper_wallets SET is_active = false WHERE contest_id = %s",
            (contest_id,)
        )

        return {
            "status": "ok",
            "contestId": contest_id,
            "message": f"Contest {contest_id} ended and wallets deactivated."
        }
    except Exception as exc:
        logger.exception("contest.end failed")
        return {"status": "error", "message": f"failed to end contest: {exc}"}

def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    """Build all contest MCP tools."""
    
    t_create = McpTool(
        name="contest.create",
        description="Create a new paper trading contest and provision wallets for agents.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Contest name"},
                "companyId": {"type": "string", "description": "Company owning the contest"},
                "agentIds": {"type": "array", "items": {"type": "string"}, "description": "Initial agents to join"},
                "venues": {"type": "array", "items": {"type": "string"}, "description": "Allowed venues (default: bybit)"},
                "coins": {"type": "array", "items": {"type": "string"}, "description": "Allowed coins"},
                "durationDays": {"type": "number", "description": "Duration in days (default: 7)"},
                "startingPaperUsdPerAgent": {"type": "number", "description": "Starting balance (default: 10000)"}
            },
            "required": ["name", "companyId"]
        },
        tags={"group": "contest", "status": "live"}
    )

    t_join = McpTool(
        name="contest.join",
        description="Join an existing contest with an agent.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "contestId": {"type": "string", "description": "Contest ID to join"},
                "companyId": {"type": "string", "description": "Company ID"},
                "agentId": {"type": "string", "description": "Agent ID"},
                "strategyRef": {"type": "string", "description": "Optional strategy reference"}
            },
            "required": ["contestId", "companyId", "agentId"]
        },
        tags={"group": "contest", "status": "live"}
    )

    t_leaderboard = McpTool(
        name="contest.leaderboard",
        description="Get the current leaderboard for a contest.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "contestId": {"type": "string", "description": "Contest ID"}
            },
            "required": ["contestId"]
        },
        tags={"group": "contest", "status": "live"}
    )

    t_end = McpTool(
        name="contest.end",
        description="Manually end a contest and freeze balances.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "contestId": {"type": "string", "description": "Contest ID"}
            },
            "required": ["contestId"]
        },
        tags={"group": "contest", "status": "live", "destructive": True}
    )

    return [
        (t_create, _handle_contest_create),
        (t_join, _handle_contest_join),
        (t_leaderboard, _handle_contest_leaderboard),
        (t_end, _handle_contest_end)
    ]

def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register contest tools on the registry."""
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
