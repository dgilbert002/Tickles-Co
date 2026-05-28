"""
MCP tools: Competition management (Phase Demo Bridge).
Location: /opt/tickles/shared/mcp/tools/competitions.py

Tools:
  competitions.list          — List all competitions
  competitions.create        — Create a new competition
  competitions.edit          — Edit competition settings
  competitions.assign_agent  — Assign agent to competition with exchange config
  competitions.remove_agent  — Remove agent from competition
  competitions.agent_config  — Get agent's exchange config for a competition
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext
from ..tools import db_helper

logger = logging.getLogger(__name__)


async def _get_pool():
    from shared.utils.db import DatabasePool
    return await DatabasePool.get_instance()


def _fmt_ts(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


# ---------------------------------------------------------------------------
# Sync handlers
# ---------------------------------------------------------------------------

def _handle_list_competitions(p: Dict[str, Any]) -> Dict[str, Any]:
    try:
        active_only = p.get("activeOnly", False)
        query = "SELECT * FROM public.competitions"
        if active_only:
            query += " WHERE is_active = TRUE"
        query += " ORDER BY created_at DESC"
        
        rows = db_helper.query(query)
        competitions = []
        for r in rows:
            competitions.append({
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description"),
                "defaultBalanceMode": r["default_balance_mode"],
                "defaultRiskPct": float(r["default_risk_pct"]),
                "defaultStartingUsd": float(r["default_starting_usd"]),
                "maxConcurrent": r.get("max_concurrent"),
                "isActive": r["is_active"],
                "createdAt": _fmt_ts(r.get("created_at")),
            })
        return {"status": "ok", "competitions": competitions, "count": len(competitions)}
    except Exception as exc:
        logger.exception("competitions.list failed")
        return {"status": "error", "message": str(exc)}


def _handle_create_competition(p: Dict[str, Any]) -> Dict[str, Any]:
    try:
        comp_id = str(p["id"]).lower().replace(" ", "-")
        name = str(p["name"])
        description = p.get("description")
        balance_mode = str(p.get("defaultBalanceMode", "cumulative"))
        risk_pct = float(p.get("defaultRiskPct", 0.05))
        starting_usd = float(p.get("defaultStartingUsd", 200))
        max_concurrent = int(p.get("maxConcurrent", 20))
        
        db_helper.execute(
            """INSERT INTO public.competitions 
               (id, name, description, default_balance_mode, default_risk_pct, 
                default_starting_usd, max_concurrent)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET
               name = EXCLUDED.name,
               description = EXCLUDED.description,
               default_balance_mode = EXCLUDED.default_balance_mode,
               default_risk_pct = EXCLUDED.default_risk_pct,
               default_starting_usd = EXCLUDED.default_starting_usd,
               max_concurrent = EXCLUDED.max_concurrent,
               updated_at = NOW()""",
            (comp_id, name, description, balance_mode, risk_pct, starting_usd, max_concurrent),
        )
        return {"status": "ok", "id": comp_id, "message": f"Competition '{name}' created/updated."}
    except Exception as exc:
        logger.exception("competitions.create failed")
        return {"status": "error", "message": str(exc)}


def _handle_edit_competition(p: Dict[str, Any]) -> Dict[str, Any]:
    try:
        comp_id = str(p["id"])
        updates = []
        params = []
        
        field_map = {
            "name": "name",
            "description": "description",
            "defaultBalanceMode": "default_balance_mode",
            "defaultRiskPct": "default_risk_pct",
            "defaultStartingUsd": "default_starting_usd",
            "maxConcurrent": "max_concurrent",
            "isActive": "is_active",
        }
        
        for param_key, col in field_map.items():
            if param_key in p:
                updates.append(f"{col} = %s")
                val = p[param_key]
                if param_key in ("defaultRiskPct", "defaultStartingUsd", "maxConcurrent"):
                    val = float(val) if param_key != "maxConcurrent" else int(val)
                params.append(val)
        
        if not updates:
            return {"status": "error", "message": "No fields to update"}
        
        updates.append("updated_at = NOW()")
        params.append(comp_id)
        
        db_helper.execute(
            f"UPDATE public.competitions SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )
        return {"status": "ok", "message": f"Competition '{comp_id}' updated."}
    except Exception as exc:
        logger.exception("competitions.edit failed")
        return {"status": "error", "message": str(exc)}


def _handle_assign_agent(p: Dict[str, Any]) -> Dict[str, Any]:
    """Assign an agent to a competition with exchange account configuration."""
    try:
        competition_id = str(p["competitionId"])
        agent_id = str(p["agentId"])
        exchange_account_id = int(p["exchangeAccountId"])
        priority = int(p.get("priority", 0))
        starting_capital = p.get("startingCapitalUsd")  # None = use comp default
        balance_mode = p.get("balanceMode")  # None = use comp default
        risk_pct = p.get("riskPct")  # None = use comp default
        max_concurrent = p.get("maxConcurrent")  # None = use comp default
        
        db_helper.execute(
            """INSERT INTO public.competition_agent_exchanges
               (competition_id, agent_id, exchange_account_id, priority,
                starting_capital_usd, balance_mode, risk_pct, max_concurrent)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (competition_id, agent_id, exchange_account_id) DO UPDATE SET
               priority = EXCLUDED.priority,
               starting_capital_usd = EXCLUDED.starting_capital_usd,
               balance_mode = EXCLUDED.balance_mode,
               risk_pct = EXCLUDED.risk_pct,
               max_concurrent = EXCLUDED.max_concurrent,
               is_active = TRUE""",
            (competition_id, agent_id, exchange_account_id, priority,
             starting_capital, balance_mode, risk_pct, max_concurrent),
        )
        
        return {
            "status": "ok",
            "competitionId": competition_id,
            "agentId": agent_id,
            "exchangeAccountId": exchange_account_id,
            "message": f"Agent '{agent_id}' assigned to '{competition_id}' with priority {priority}.",
        }
    except Exception as exc:
        logger.exception("competitions.assign_agent failed")
        return {"status": "error", "message": str(exc)}


def _handle_remove_agent(p: Dict[str, Any]) -> Dict[str, Any]:
    try:
        competition_id = str(p["competitionId"])
        agent_id = str(p["agentId"])
        exchange_account_id = p.get("exchangeAccountId")  # None = remove ALL assignments for this agent
        
        if exchange_account_id is not None:
            db_helper.execute(
                """UPDATE public.competition_agent_exchanges 
                   SET is_active = FALSE 
                   WHERE competition_id = %s AND agent_id = %s AND exchange_account_id = %s""",
                (competition_id, agent_id, int(exchange_account_id)),
            )
        else:
            db_helper.execute(
                """UPDATE public.competition_agent_exchanges 
                   SET is_active = FALSE 
                   WHERE competition_id = %s AND agent_id = %s""",
                (competition_id, agent_id),
            )
        
        return {"status": "ok", "message": f"Agent '{agent_id}' removed from '{competition_id}'."}
    except Exception as exc:
        logger.exception("competitions.remove_agent failed")
        return {"status": "error", "message": str(exc)}


def _handle_agent_config(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get full agent configuration for a competition, including exchange routing order."""
    try:
        competition_id = str(p["competitionId"])
        agent_id = p.get("agentId")  # None = all agents
        
        if agent_id:
            query = """SELECT cae.*, ea.exchange, ea.account_name, ea.account_type, ea.last_balance,
                              ea.is_active as account_active, ea.last_tested_at
                       FROM public.competition_agent_exchanges cae
                       JOIN public.exchange_accounts ea ON ea.id = cae.exchange_account_id
                       WHERE cae.competition_id = %s AND cae.agent_id = %s AND cae.is_active = TRUE
                       ORDER BY cae.priority"""
            rows = db_helper.query(query, (competition_id, agent_id))
        else:
            query = """SELECT cae.*, ea.exchange, ea.account_name, ea.account_type, ea.last_balance,
                              ea.is_active as account_active, ea.last_tested_at
                       FROM public.competition_agent_exchanges cae
                       JOIN public.exchange_accounts ea ON ea.id = cae.exchange_account_id
                       WHERE cae.competition_id = %s AND cae.is_active = TRUE
                       ORDER BY cae.agent_id, cae.priority"""
            rows = db_helper.query(query, (competition_id,))
        
        configs = []
        for r in rows:
            configs.append({
                "agentId": r["agent_id"],
                "exchange": r["exchange"],
                "accountName": r["account_name"],
                "accountType": r["account_type"],
                "priority": r["priority"],
                "startingCapitalUsd": float(r["starting_capital_usd"]) if r.get("starting_capital_usd") else None,
                "balanceMode": r.get("balance_mode"),
                "riskPct": float(r["risk_pct"]) if r.get("risk_pct") else None,
                "maxConcurrent": r.get("max_concurrent"),
                "lastBalance": float(r["last_balance"]) if r.get("last_balance") else None,
                "accountActive": r.get("account_active"),
                "lastTestedAt": _fmt_ts(r.get("last_tested_at")),
            })
        
        return {"status": "ok", "competitionId": competition_id, "configs": configs, "count": len(configs)}
    except Exception as exc:
        logger.exception("competitions.agent_config failed")
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Tool definitions + registration
# ---------------------------------------------------------------------------

def _build_tools(ctx: ToolContext) -> list:
    """Build all competition MCP tools."""

    t_list = McpTool(
        name="competitions.list",
        description="List all competitions. Filter by active status.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "activeOnly": {"type": "boolean", "default": False},
            },
        },
        read_only=True,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _list(p): return await asyncio.to_thread(_handle_list_competitions, p)

    t_create = McpTool(
        name="competitions.create",
        description="Create or update a competition. Agents inherit default settings unless they override.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Short ID (e.g. 'blofin-may-2026')"},
                "name": {"type": "string", "description": "Display name"},
                "description": {"type": "string"},
                "defaultBalanceMode": {"type": "string", "enum": ["cumulative", "isolated"], "default": "cumulative"},
                "defaultRiskPct": {"type": "number", "default": 0.05, "description": "Risk per trade (e.g. 0.05 = 5%)"},
                "defaultStartingUsd": {"type": "number", "default": 200},
                "maxConcurrent": {"type": "integer", "default": 20},
            },
            "required": ["id", "name"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _create(p): return await asyncio.to_thread(_handle_create_competition, p)

    t_edit = McpTool(
        name="competitions.edit",
        description="Edit competition settings.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "defaultBalanceMode": {"type": "string", "enum": ["cumulative", "isolated"]},
                "defaultRiskPct": {"type": "number"},
                "defaultStartingUsd": {"type": "number"},
                "maxConcurrent": {"type": "integer"},
                "isActive": {"type": "boolean"},
            },
            "required": ["id"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _edit(p): return await asyncio.to_thread(_handle_edit_competition, p)

    t_assign = McpTool(
        name="competitions.assign_agent",
        description="Assign an agent to a competition with exchange account and priority. Set balanceMode/riskPct to override competition defaults per-agent.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "competitionId": {"type": "string"},
                "agentId": {"type": "string", "description": "Agent ID (e.g. 'copy_opt_lev_parallel')"},
                "exchangeAccountId": {"type": "integer", "description": "ID from accounts.list"},
                "priority": {"type": "integer", "default": 0, "description": "0 = first choice, higher = fallback"},
                "startingCapitalUsd": {"type": "number", "description": "Override competition default"},
                "balanceMode": {"type": "string", "enum": ["cumulative", "isolated"]},
                "riskPct": {"type": "number", "description": "Override competition default risk %"},
                "maxConcurrent": {"type": "integer", "description": "Override max concurrent positions"},
            },
            "required": ["competitionId", "agentId", "exchangeAccountId"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _assign(p): return await asyncio.to_thread(_handle_assign_agent, p)

    t_remove_agent = McpTool(
        name="competitions.remove_agent",
        description="Remove an agent from a competition (or specific exchange account assignment).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "competitionId": {"type": "string"},
                "agentId": {"type": "string"},
                "exchangeAccountId": {"type": "integer", "description": "If omitted, removes ALL exchange assignments for this agent"},
            },
            "required": ["competitionId", "agentId"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _remove_agent(p): return await asyncio.to_thread(_handle_remove_agent, p)

    t_agent_config = McpTool(
        name="competitions.agent_config",
        description="Get agent exchange configuration for a competition — shows all exchange accounts in priority order with balance modes.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "competitionId": {"type": "string"},
                "agentId": {"type": "string", "description": "If omitted, returns config for ALL agents"},
            },
            "required": ["competitionId"],
        },
        read_only=True,
        tags={"phase": "demo", "group": "competitions", "status": "live"},
    )

    async def _agent_config(p): return await asyncio.to_thread(_handle_agent_config, p)

    return [
        (t_list, _list),
        (t_create, _create),
        (t_edit, _edit),
        (t_assign, _assign),
        (t_remove_agent, _remove_agent),
        (t_agent_config, _agent_config),
    ]


def register(registry, ctx):
    """Register all competition tools into the MCP registry."""
    tools = _build_tools(ctx)
    for tool, handler in tools:
        registry.register(tool, handler)
    logger.info("[competitions] registered %d tools", len(tools))
