"""MCP tools: Trading (execution, banker, treasury, positions).

Phase 2 exposes trading surface over MCP. Where Paperclip already has
HTTP endpoints (finance-events, costs/summary, agents) we call them live.
Where shared/trading/* lives in-process only (Banker, Treasury, ExecutionRouter),
we return a typed ``not_implemented`` envelope pending the Phase 2.5 wiring
that mounts those services inside the MCP daemon.

Tools registered:
    banker.snapshot
    banker.positions
    treasury.evaluate
    execution.submit
    execution.cancel
    execution.status
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext


def _not_implemented(feature: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    out = {
        "status": "not_implemented",
        "feature": feature,
        "message": (
            f"{feature} will be wired in Phase 2.5 when shared/trading/* is "
            "mounted into the MCP daemon process. Until then, the banker and "
            "treasury run inside the Python CLIs (shared/cli/*)."
        ),
    }
    if extra:
        out.update(extra)
    return out


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    t_banker_snapshot = McpTool(
        name="banker.snapshot",
        description=(
            "Per-company P&L snapshot (realised, unrealised, deposits, spend). "
            "Reads from Paperclip finance + costs endpoints — append-only trade "
            "ledger lives in the per-company DB."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "from": {"type": "string", "format": "date-time"},
                "to": {"type": "string", "format": "date-time"},
            },
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading"},
    )

    async def _banker_snapshot(p: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(p["companyId"])
        date_range = {"from": p.get("from"), "to": p.get("to")}
        cost_summary = ctx.paperclip(
            "GET", f"/api/companies/{cid}/costs/summary", query=date_range
        )
        finance_summary = ctx.paperclip(
            "GET",
            f"/api/companies/{cid}/costs/finance-summary",
            query=date_range,
        )
        by_agent = ctx.paperclip(
            "GET", f"/api/companies/{cid}/costs/by-agent", query=date_range
        )
        return {
            "companyId": cid,
            "window": date_range,
            "cost": cost_summary,
            "finance": finance_summary,
            "byAgent": by_agent,
        }

    t_banker_positions = McpTool(
        name="banker.positions",
        description="List open positions for a company.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {"companyId": {"type": "string"}},
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "stub"},
    )

    async def _banker_positions(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("banker.positions", {"echo": p})

    t_treasury_evaluate = McpTool(
        name="treasury.evaluate",
        description=(
            "Ask the Treasury whether a proposed execution is allowed. "
            "Returns {allow, deny_reason, adjusted_notional_usd}. Every "
            "execution.submit must call this first (Rule 1 enforcement)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "venue": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "intendedNotionalUsd": {"type": "number", "minimum": 0},
                "leverage": {"type": "number", "minimum": 1, "default": 1},
                "strategyRef": {"type": "string"},
            },
            "required": ["companyId", "agentId", "venue", "symbol", "side", "intendedNotionalUsd"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "stub"},
    )

    async def _treasury_evaluate(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented(
            "treasury.evaluate",
            {
                "echo": p,
                "note": (
                    "Default-deny is assumed until Phase 2.5 wires shared.trading."
                    "treasury into the MCP daemon."
                ),
            },
        )

    t_execution_submit = McpTool(
        name="execution.submit",
        description=(
            "Submit an order via ExecutionRouter (paper/ccxt/capital/nautilus). "
            "Enforces treasury.evaluate first; writes to Banker + order_events."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "venue": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "orderType": {
                    "type": "string",
                    "enum": ["market", "limit"],
                    "default": "market",
                },
                "quantity": {"type": "number", "exclusiveMinimum": 0},
                "limitPrice": {"type": "number"},
                "timeInForce": {
                    "type": "string",
                    "enum": ["GTC", "IOC", "FOK"],
                    "default": "GTC",
                },
                "dryRun": {"type": "boolean", "default": False},
                "strategyRef": {"type": "string"},
            },
            "required": ["companyId", "agentId", "venue", "symbol", "side", "quantity"],
        },
        read_only=False,
        tags={"phase": "2", "group": "trading", "status": "stub"},
    )

    async def _execution_submit(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("execution.submit", {"echo": p})

    t_execution_cancel = McpTool(
        name="execution.cancel",
        description="Cancel an open order by id.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "orderId": {"type": "string"},
            },
            "required": ["companyId", "orderId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "trading", "status": "stub"},
    )

    async def _execution_cancel(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("execution.cancel", {"echo": p})

    t_execution_status = McpTool(
        name="execution.status",
        description="Get the latest status of an order.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "orderId": {"type": "string"},
            },
            "required": ["companyId", "orderId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "stub"},
    )

    async def _execution_status(p: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("execution.status", {"echo": p})

    return [
        (t_banker_snapshot, _banker_snapshot),
        (t_banker_positions, _banker_positions),
        (t_treasury_evaluate, _treasury_evaluate),
        (t_execution_submit, _execution_submit),
        (t_execution_cancel, _execution_cancel),
        (t_execution_status, _execution_status),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
