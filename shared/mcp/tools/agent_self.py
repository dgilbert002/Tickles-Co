"""
MCP tools: Agent self-discovery — wallet, performance, open positions.
Agents querying their own state: balance, P&L, trade history.
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext

logger = logging.getLogger(__name__)


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return str(value)


async def _handle_agent_wallet(params: dict, ctx: ToolContext) -> dict:
    """agent.wallet — Get current wallet balance and equity for an agent."""
    agent_id = str(params.get("agent_id", ""))
    if not agent_id:
        return {"status": "error", "message": "agent_id is required"}

    from shared.services.banker import get_balance
    balance = await get_balance(agent_id)
    if not balance:
        return {"status": "error", "message": f"no wallet found for {agent_id}"}
    return {"status": "ok", "wallet": _jsonify(balance)}


async def _handle_agent_performance(params: dict, ctx: ToolContext) -> dict:
    """agent.performance — Get win rate, P&L, total trades for an agent."""
    agent_id = str(params.get("agent_id", ""))
    if not agent_id:
        return {"status": "error", "message": "agent_id is required"}

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # From competition scores (live)
        row = await conn.fetchrow("""
            SELECT equity_usd, realized_pnl_usd, return_pct, win_rate,
                   total_trades, open_positions, total_fees_usd
            FROM contest_participants
            WHERE agent_id = $1 AND contest_id = 'copy-trade-scenarios'
        """, agent_id)

        if row:
            return {"status": "ok", "performance": {
                "agent_id": agent_id,
                "equity_usd": float(row["equity_usd"] or 0),
                "realized_pnl_usd": float(row["realized_pnl_usd"] or 0),
                "return_pct": float(row["return_pct"] or 0),
                "win_rate": float(row["win_rate"] or 0),
                "total_trades": int(row["total_trades"] or 0),
                "open_positions": int(row["open_positions"] or 0),
                "total_fees_usd": float(row["total_fees_usd"] or 0),
            }}

        # Fallback: compute from tracked_positions
        row = await conn.fetchrow("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE realized_pnl_usd_final > 0) as wins,
                   SUM(COALESCE(realized_pnl_usd_final, 0)) as total_pnl
            FROM tracked_positions
            WHERE actor_id = $1 AND status = 'closed'
        """, agent_id)
        if row and row["total"] > 0:
            return {"status": "ok", "performance": {
                "agent_id": agent_id,
                "total_trades": int(row["total"]),
                "wins": int(row["wins"] or 0),
                "win_rate": round(int(row["wins"] or 0) / max(int(row["total"]), 1), 3),
                "total_pnl_usd": float(row["total_pnl"] or 0),
            }}
    return {"status": "ok", "performance": {"agent_id": agent_id, "total_trades": 0}}


async def _handle_agent_open_positions(params: dict, ctx: ToolContext) -> dict:
    """agent.open_positions — List current open paper positions for an agent."""
    agent_id = str(params.get("agent_id", ""))
    if not agent_id:
        return {"status": "error", "message": "agent_id is required"}

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, direction, entry_price, sl_price, tp_price,
                   allocated, leverage, entered_at
            FROM competition_trades
            WHERE agent_id = $1 AND contest_id = 'copy-trade-scenarios'
              AND exit_price IS NULL
            ORDER BY entered_at DESC
        """, agent_id)
    positions = []
    for r in rows:
        positions.append({
            "symbol": r["symbol"], "direction": r["direction"],
            "entry_price": float(r["entry_price"]) if r["entry_price"] else None,
            "sl_price": float(r["sl_price"]) if r["sl_price"] else None,
            "tp_price": float(r["tp_price"]) if r["tp_price"] else None,
            "allocated_usd": float(r["allocated"]) if r["allocated"] else 0,
            "leverage": float(r["leverage"]) if r["leverage"] else 1,
            "entered_at": r["entered_at"].isoformat() if r["entered_at"] else None,
        })
    return {"status": "ok", "agent_id": agent_id, "open_positions": positions,
            "count": len(positions)}


async def _handle_agent_trade_history(params: dict, ctx: ToolContext) -> dict:
    """agent.trade_history — Get closed trade history for an agent."""
    agent_id = str(params.get("agent_id", ""))
    limit = min(int(params.get("limit", 20)), 100)
    if not agent_id:
        return {"status": "error", "message": "agent_id is required"}

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, direction, entry_price, exit_price, pnl, fees,
                   exit_reason, entered_at, exited_at
            FROM competition_trades
            WHERE agent_id = $1 AND contest_id = 'copy-trade-scenarios'
              AND exit_price IS NOT NULL
            ORDER BY exited_at DESC
            LIMIT $2
        """, agent_id, limit)
    trades = []
    for r in rows:
        trades.append({
            "symbol": r["symbol"], "direction": r["direction"],
            "entry": float(r["entry_price"]) if r["entry_price"] else None,
            "exit": float(r["exit_price"]) if r["exit_price"] else None,
            "pnl": float(r["pnl"]) if r["pnl"] else 0,
            "fees": float(r["fees"]) if r["fees"] else 0,
            "exit_reason": r["exit_reason"],
            "entered_at": r["entered_at"].isoformat() if r["entered_at"] else None,
            "exited_at": r["exited_at"].isoformat() if r["exited_at"] else None,
        })
    return {"status": "ok", "agent_id": agent_id, "trades": trades, "count": len(trades)}


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register agent self-discovery tools."""
    tools = [
        McpTool(
            name="agent.wallet",
            description="Get current wallet balance, equity, free margin for an agent. Query your own paper wallet.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent ID (e.g. copy_spot_seq)"},
                },
                "required": ["agent_id"],
            },
            handler=lambda args, **kw: _handle_agent_wallet(args, ctx),
        ),
        McpTool(
            name="agent.performance",
            description="Get win rate, P&L, total trades, and return % for an agent from competition scores.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent ID (e.g. copy_spot_seq)"},
                },
                "required": ["agent_id"],
            },
            handler=lambda args, **kw: _handle_agent_performance(args, ctx),
        ),
        McpTool(
            name="agent.open_positions",
            description="List current open paper positions for an agent with entry, SL, TP, allocation.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent ID (e.g. copy_lev_parallel)"},
                },
                "required": ["agent_id"],
            },
            handler=lambda args, **kw: _handle_agent_open_positions(args, ctx),
        ),
        McpTool(
            name="agent.trade_history",
            description="Get closed trade history for an agent with P&L, fees, exit reason.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent ID"},
                    "limit": {"type": "integer", "description": "Max trades to return (default 20)"},
                },
                "required": ["agent_id"],
            },
            handler=lambda args, **kw: _handle_agent_trade_history(args, ctx),
        ),
    ]
    for tool in tools:
        registry.register(tool)
    logger.info("agent.* MCP tools registered: wallet, performance, open_positions, trade_history")
