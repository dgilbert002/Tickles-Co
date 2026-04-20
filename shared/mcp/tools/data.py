"""MCP tools: Data access (market data, alt-data, catalog).

Phase 2 ships read-only queries against Paperclip's existing HTTP surface plus
the Tickles shared asset catalog. Market data is intentionally proxied via
Paperclip heartbeats / the shared ``shared.market_data`` module so the MCP
daemon stays thin. Tools that are not yet wired to a backend return an
explicit ``{ status: "not_implemented", ... }`` envelope so agents get a
clear signal.

Tools registered:
    catalog.list
    catalog.get
    md.quote
    md.candles
    altdata.search
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext


def _not_implemented(feature: str) -> Dict[str, Any]:
    return {
        "status": "not_implemented",
        "feature": feature,
        "message": (
            f"{feature} will be wired in Phase 2.5 when the market-data "
            "gateway is hoisted out of Paperclip heartbeats into a standalone "
            "service. Until then, agents should use Paperclip's heartbeat "
            "tools directly."
        ),
    }


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    # catalog.list — queries Paperclip's catalog endpoint (we already have
    # a shared/catalog module populated in Phase 14+). Falls back gracefully
    # if the endpoint is absent.
    t_catalog_list = McpTool(
        name="catalog.list",
        description=(
            "List tradable instruments from the Tickles asset catalog. "
            "Optional filters: venue, asset_class, symbol_contains."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "assetClass": {"type": "string"},
                "symbolContains": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _catalog_list(p: Dict[str, Any]) -> Dict[str, Any]:
        query = {
            "venue": p.get("venue"),
            "assetClass": p.get("assetClass"),
            "symbol": p.get("symbolContains"),
            "limit": p.get("limit", 50),
        }
        try:
            rows = ctx.paperclip("GET", "/api/catalog/instruments", query=query) or []
            return {"count": len(rows), "instruments": rows}
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return {
                    "count": 0,
                    "instruments": [],
                    "note": (
                        "Paperclip /api/catalog/instruments not exposed yet — "
                        "will be wired in Phase 2.5 (asset catalog HTTP adapter)."
                    ),
                }
            raise

    # catalog.get
    t_catalog_get = McpTool(
        name="catalog.get",
        description="Fetch a single asset catalog row by symbol (venue optional).",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "venue": {"type": "string"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _catalog_get(p: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return ctx.paperclip(
                "GET",
                "/api/catalog/instruments/by-symbol",
                query={"symbol": p["symbol"], "venue": p.get("venue")},
            )
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return _not_implemented("catalog.get")
            raise

    # md.quote — stub until Phase 2.5 md-gateway
    t_md_quote = McpTool(
        name="md.quote",
        description="Latest traded price + book top for a symbol/venue.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "venue": {"type": "string"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data", "status": "stub"},
    )

    async def _md_quote(_: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("md.quote")

    # md.candles
    t_md_candles = McpTool(
        name="md.candles",
        description="OHLCV candles for a symbol/venue/timeframe.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "venue": {"type": "string"},
                "timeframe": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "1h", "4h", "1d"],
                    "default": "5m",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data", "status": "stub"},
    )

    async def _md_candles(_: Dict[str, Any]) -> Dict[str, Any]:
        return _not_implemented("md.candles")

    # altdata.search
    t_alt_search = McpTool(
        name="altdata.search",
        description=(
            "Search across alt-data sources (Discord signals, whales, "
            "news, social) — stub in Phase 2; Phase 2.5 wires to shared.altdata."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["discord", "whales", "news", "social"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            },
            "required": ["query"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data", "status": "stub"},
    )

    async def _alt_search(p: Dict[str, Any]) -> Dict[str, Any]:
        out = _not_implemented("altdata.search")
        out["echo"] = {"query": p.get("query"), "kind": p.get("kind")}
        return out

    return [
        (t_catalog_list, _catalog_list),
        (t_catalog_get, _catalog_get),
        (t_md_quote, _md_quote),
        (t_md_candles, _md_candles),
        (t_alt_search, _alt_search),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
