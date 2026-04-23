"""
Module: meta
Purpose: MCP tools for tool discovery, suggestion, requests, and usage stats.
Location: /opt/tickles/shared/mcp/tools/meta.py

Curious-agent loop (Phase M6): lets agents explore the tool catalogue,
get suggestions, request new tools, and see usage statistics.

Tools registered:
    tools.catalogue     — full menu grouped by tag, with optional filters
    tools.suggest       — regex-based hint tool mapping phrases to MCP calls
    tools.request_new   — request a new MCP tool (stored for CEO review)
    tools.usage_stats   — read invocation stats from mcp_invocations
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..protocol import McpTool
from ..registry import ToolRegistry
from . import db_helper
from .context import ToolContext

logger = logging.getLogger(__name__)

# ---- Constants --------------------------------------------------------

_MAX_SUGGESTIONS: int = 12
_USAGE_DEFAULT_SINCE_DAYS: int = 30
_REQUEST_TABLE: str = "public.mcp_tool_requests"

# ---- Suggestion rules -------------------------------------------------
# Each rule: (regex_pattern, [tool_names], reason_string)
# The regex matches against the lowercased task description.
# Tool names that don't exist in the registry are silently skipped.

_RAW_RULES: List[Tuple[str, List[str], str]] = [
    (
        r"(?:candle|ohlc|price\s+history|bar\s+data|chart\s+data)",
        ["md.candles", "md.quote"],
        "candle / price data access",
    ),
    (
        r"(?:backtest|test\s+strategy|run\s+backtest|simulate)",
        ["backtest.compose", "strategy.list", "engine.list"],
        "backtest workflow",
    ),
    (
        r"(?:indicator|rsi|macd|bollinger|sma|ema|atr|vwap|obv|mfi)",
        ["indicator.list", "indicator.compute_preview"],
        "technical indicator access",
    ),
    (
        r"(?:divergence|overbought|oversold|reversal)",
        ["indicator.compute_preview", "md.candles", "strategy.list"],
        "divergence / reversal analysis",
    ),
    (
        r"(?:order|trade|buy|sell|position|execute)",
        ["execution.submit", "execution.status", "banker.positions"],
        "order execution and positions",
    ),
    (
        r"(?:wallet|balance|capital|funding|equity)",
        ["banker.snapshot", "wallet.paper.create"],
        "wallet and balance access",
    ),
    (
        r"(?:company|agent|provision|create\s+company|new\s+agent)",
        ["company.list", "company.create", "agent.list", "agent.create"],
        "company and agent management",
    ),
    (
        r"(?:memory|remember|recall|forget|learning)",
        ["memory.add", "memory.search", "learnings.read_last_3"],
        "memory and learning storage",
    ),
    (
        r"(?:autopsy|postmortem|feedback|review\s+trade|reflect)",
        ["autopsy.run", "postmortem.run", "feedback.loop"],
        "trade review and reflection",
    ),
    (
        r"(?:coverage|gap|missing\s+data|backfill|hole\s+in\s+data)",
        ["candles.coverage", "candles.backfill"],
        "data coverage and gap filling",
    ),
    (
        r"(?:alt\s*data|sentiment|news|social|onchain|on-chain)",
        ["altdata.search"],
        "alternative data sources",
    ),
    (
        r"(?:regime|trend|volatility|market\s+state|market\s+phase)",
        ["regime.current"],
        "market regime detection",
    ),
    (
        r"(?:sweep|parameter\s+optim|grid\s+search|optimize)",
        ["backtest.plan_sweep", "backtest.top_k"],
        "parameter sweep and optimization",
    ),
    (
        r"(?:top\s+perform|best\s+result|leaderboard|rank)",
        ["backtest.top_k"],
        "top performing backtest results",
    ),
    (
        r"(?:catalog|instrument|symbol|available\s+pair|what\s+can\s+trade)",
        ["catalog.list", "catalog.get"],
        "instrument catalog access",
    ),
    (
        r"(?:treasury|capability|risk\s+check|can\s+i\s+trade|allowed)",
        ["treasury.evaluate"],
        "treasury capability check",
    ),
    (
        r"(?:cancel|abort|stop\s+order)",
        ["execution.cancel"],
        "order cancellation",
    ),
    (
        r"(?:dashboard|overview|summary|snapshot)",
        ["banker.snapshot", "dashboard.snapshot"],
        "dashboard and overview",
    ),
]

_SUGGESTION_RULES: List[Tuple[re.Pattern, List[str], str]] = [
    (re.compile(pat, re.IGNORECASE), tools, reason)
    for pat, tools, reason in _RAW_RULES
]


# ---- Pure helpers (testable without registry) -------------------------


def _group_tools(
    tools: List[McpTool],
    include_stubs: bool = True,
) -> Dict[str, List[Dict[str, Any]]]:
    """Group tools by their tags['group'] value into serialisable dicts.

    Args:
        tools: List of McpTool instances to group.
        include_stubs: If False, exclude tools tagged as stubs.

    Returns:
        Dict mapping group name to list of tool summary dicts.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in tools:
        if not include_stubs and t.tags.get("status") == "stub":
            continue
        group = t.tags.get("group", "uncategorised")
        summary: Dict[str, Any] = {
            "name": t.name,
            "description": t.description,
            "read_only": t.read_only,
            "version": t.version,
        }
        if t.tags.get("status"):
            summary["status"] = t.tags["status"]
        if t.tags.get("destructive"):
            summary["destructive"] = True
        groups.setdefault(group, []).append(summary)
    return groups


def _match_suggestions(
    text: str,
    registry: ToolRegistry,
) -> List[Dict[str, Any]]:
    """Match task description against suggestion rules.

    Args:
        text: Lowercased task description to match.
        registry: Tool registry to look up tool details.

    Returns:
        Deduplicated list of suggestion dicts, capped at _MAX_SUGGESTIONS.
    """
    seen: set[str] = set()
    suggestions: List[Dict[str, Any]] = []
    for pattern, tool_names, reason in _SUGGESTION_RULES:
        if not pattern.search(text):
            continue
        for name in tool_names:
            if name in seen:
                continue
            tool = registry.get(name)
            if tool is None or not tool.enabled:
                continue
            seen.add(name)
            suggestions.append({
                "tool": name,
                "reason": reason,
                "description": tool.description,
            })
            if len(suggestions) >= _MAX_SUGGESTIONS:
                return suggestions
    return suggestions


def _content_hash(name: str, rationale: str) -> str:
    """Compute a deterministic SHA-256 hash for tool request dedup.

    Args:
        name: Requested tool name.
        rationale: Justification for the tool.

    Returns:
        Hex digest string.
    """
    raw = f"{name}\n{rationale}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_since(since: Optional[str]) -> datetime:
    """Parse an optional ISO 8601 'since' parameter with fallback.

    Args:
        since: ISO 8601 datetime string, or None for default.

    Returns:
        UTC-aware datetime (defaults to 30 days ago).
    """
    if since:
        try:
            dt = datetime.fromisoformat(since)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            logger.warning("usage_stats: invalid since=%r, using default", since)
    return datetime.now(tz=timezone.utc) - timedelta(days=_USAGE_DEFAULT_SINCE_DAYS)


def _fmt_dt(val: Any) -> Optional[str]:
    """Format a datetime value to ISO 8601 string.

    Args:
        val: datetime object or string.

    Returns:
        ISO 8601 string or None.
    """
    if isinstance(val, datetime):
        return val.isoformat()
    if val is not None:
        return str(val)
    return None


# ---- Build & Register -------------------------------------------------


def _build_tools(
    registry: ToolRegistry, ctx: ToolContext
) -> list[tuple[McpTool, Any]]:
    """Build all meta MCP tools bound to the given registry and context.

    Args:
        registry: Tool registry (needed by catalogue + suggest handlers).
        ctx: Shared tool context for HTTP clients and config.

    Returns:
        List of (McpTool, handler) pairs.
    """

    # ---- tools.catalogue ------------------------------------------------

    t_catalogue = McpTool(
        name="tools.catalogue",
        description=(
            "List all registered MCP tools, grouped by tag. "
            "Optionally filter by group, include disabled tools, "
            "or exclude stubs. This is the agent's shopping list."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "description": "Filter to a single group (e.g. 'data', 'trading')",
                },
                "includeDisabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include tools that are currently disabled",
                },
                "includeStubs": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include tools tagged as stubs (not yet fully implemented)",
                },
            },
        },
        read_only=True,
        tags={"phase": "6", "group": "meta", "status": "live"},
    )

    def _handle_catalogue(p: Dict[str, Any]) -> Dict[str, Any]:
        """List all registered tools, optionally filtered and grouped."""
        try:
            group_filter = p.get("group")
            include_disabled = bool(p.get("includeDisabled", False))
            include_stubs = bool(p.get("includeStubs", True))

            tools = registry.list_tools(include_disabled=include_disabled)
            grouped = _group_tools(tools, include_stubs=include_stubs)

            if group_filter:
                grouped = {
                    k: v for k, v in grouped.items() if k == group_filter
                }

            total = sum(len(v) for v in grouped.values())
            return {
                "status": "ok",
                "groups": grouped,
                "total_tools": total,
                "groups_count": len(grouped),
            }
        except Exception as exc:
            logger.error("catalogue: %s", exc, exc_info=True)
            return {"status": "error", "message": str(exc)}

    # ---- tools.suggest --------------------------------------------------

    t_suggest = McpTool(
        name="tools.suggest",
        description=(
            "Given a natural-language task description, suggest relevant MCP tools. "
            "Uses regex pattern matching to map phrases like 'find divergence' to "
            "tool calls such as indicator.compute_preview + md.candles."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "taskDescription": {
                    "type": "string",
                    "description": "What you want to do, in plain English",
                },
            },
            "required": ["taskDescription"],
        },
        read_only=True,
        tags={"phase": "6", "group": "meta", "status": "live"},
    )

    def _handle_suggest(p: Dict[str, Any]) -> Dict[str, Any]:
        """Suggest tools based on a natural-language task description."""
        try:
            desc = p.get("taskDescription")
            if not desc or not isinstance(desc, str):
                return {
                    "status": "error",
                    "message": "missing required param: 'taskDescription'",
                }

            suggestions = _match_suggestions(desc.lower(), registry)
            return {
                "status": "ok",
                "task_description": desc,
                "suggestions": suggestions,
                "match_count": len(suggestions),
            }
        except Exception as exc:
            logger.error("suggest: %s", exc, exc_info=True)
            return {"status": "error", "message": str(exc)}

    # ---- tools.request_new ----------------------------------------------

    t_request_new = McpTool(
        name="tools.request_new",
        description=(
            "Request a new MCP tool that doesn't exist yet. "
            "The request is stored for CEO review. Duplicate requests "
            "(same name + rationale) are deduplicated automatically."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Proposed tool name (e.g. 'onchain.whale_entries')",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this tool is needed and what it should do",
                },
                "exampleInput": {
                    "type": "object",
                    "description": "Example input parameters (optional)",
                },
                "exampleOutput": {
                    "type": "object",
                    "description": "Example output structure (optional)",
                },
                "requestedBy": {
                    "type": "string",
                    "description": "Agent or user requesting (optional)",
                },
            },
            "required": ["name", "rationale"],
        },
        read_only=False,
        tags={"phase": "6", "group": "meta", "status": "live"},
    )

    def _handle_request_new(p: Dict[str, Any]) -> Dict[str, Any]:
        """Store a request for a new MCP tool in the database."""
        try:
            name = p.get("name")
            if not name:
                return {
                    "status": "error",
                    "message": "missing required param: 'name'",
                }
            name = str(name)

            rationale = p.get("rationale")
            if not rationale:
                return {
                    "status": "error",
                    "message": "missing required param: 'rationale'",
                }
            rationale = str(rationale)

            example_input = p.get("exampleInput")
            example_output = p.get("exampleOutput")
            requested_by = str(p.get("requestedBy", "anonymous"))
            content_hash = _content_hash(name, rationale)

            # Check for existing open request with same hash (dedup)
            existing = db_helper.query(
                f"SELECT id, status FROM {_REQUEST_TABLE} "
                f"WHERE content_hash = %s LIMIT 1",
                (content_hash,),
            )
            if existing:
                row = existing[0]
                return {
                    "status": "ok",
                    "request_id": row["id"],
                    "name": name,
                    "message": (
                        f"request already exists (id={row['id']}, "
                        f"status={row['status']})"
                    ),
                    "dedup": True,
                }

            # Insert new request
            db_helper.execute(
                f"INSERT INTO {_REQUEST_TABLE} "
                f"(name, rationale, example_input, example_output, "
                f"requested_by, content_hash) "
                f"VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s)",
                (
                    name,
                    rationale,
                    json.dumps(example_input, default=str)
                    if example_input
                    else None,
                    json.dumps(example_output, default=str)
                    if example_output
                    else None,
                    requested_by,
                    content_hash,
                ),
            )

            # Fetch the inserted row to get the ID
            rows = db_helper.query(
                f"SELECT id FROM {_REQUEST_TABLE} "
                f"WHERE content_hash = %s LIMIT 1",
                (content_hash,),
            )
            request_id = rows[0]["id"] if rows else None

            return {
                "status": "ok",
                "request_id": request_id,
                "name": name,
                "message": "tool request stored for CEO review",
            }
        except RuntimeError as exc:
            logger.error("request_new: db error: %s", exc)
            return {
                "status": "error",
                "message": f"database unavailable: {exc}",
            }
        except Exception as exc:
            logger.error("request_new: %s", exc, exc_info=True)
            return {"status": "error", "message": str(exc)}

    # ---- tools.usage_stats ----------------------------------------------

    t_usage_stats = McpTool(
        name="tools.usage_stats",
        description=(
            "Read invocation statistics from the audit log. Shows which tools "
            "are hot (frequently called) and which are cold, with error counts "
            "and average latency. Defaults to the last 30 days."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO 8601 datetime to start from "
                        "(default: 30 days ago)"
                    ),
                },
            },
        },
        read_only=True,
        tags={"phase": "6", "group": "meta", "status": "live"},
    )

    def _handle_usage_stats(p: Dict[str, Any]) -> Dict[str, Any]:
        """Read invocation statistics from mcp_invocations."""
        try:
            since = _parse_since(p.get("since"))
            rows = db_helper.query(
                "SELECT tool_name, "
                "  COUNT(*) AS call_count, "
                "  COUNT(*) FILTER (WHERE status = 'error') AS error_count, "
                "  ROUND(AVG(latency_ms)::numeric, 1) AS avg_latency_ms, "
                "  MAX(started_at) AS last_called_at "
                "FROM public.mcp_invocations "
                "WHERE started_at >= %s "
                "GROUP BY tool_name "
                "ORDER BY call_count DESC",
                (since,),
            )

            tools_stats: List[Dict[str, Any]] = []
            for r in rows:
                stat: Dict[str, Any] = {
                    "tool_name": r["tool_name"],
                    "call_count": int(r["call_count"]),
                    "error_count": int(r["error_count"]),
                }
                if r.get("avg_latency_ms") is not None:
                    stat["avg_latency_ms"] = float(r["avg_latency_ms"])
                if r.get("last_called_at") is not None:
                    stat["last_called_at"] = _fmt_dt(r["last_called_at"])
                tools_stats.append(stat)

            total_calls = sum(s["call_count"] for s in tools_stats)
            return {
                "status": "ok",
                "since": since.isoformat(),
                "tools": tools_stats,
                "total_calls": total_calls,
                "unique_tools": len(tools_stats),
            }
        except RuntimeError as exc:
            logger.error("usage_stats: db error: %s", exc)
            return {
                "status": "error",
                "message": f"database unavailable: {exc}",
            }
        except Exception as exc:
            logger.error("usage_stats: %s", exc, exc_info=True)
            return {"status": "error", "message": str(exc)}

    return [
        (t_catalogue, _handle_catalogue),
        (t_suggest, _handle_suggest),
        (t_request_new, _handle_request_new),
        (t_usage_stats, _handle_usage_stats),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all meta tools on the given registry.

    Args:
        registry: The in-process tool registry to register into.
        ctx: Shared tool context for HTTP clients and config.
    """
    for tool, handler in _build_tools(registry, ctx):
        registry.register(tool, handler)
