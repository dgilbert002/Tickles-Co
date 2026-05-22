"""
Module: fees
Purpose: MCP tools for querying backtest fee parameters and funding rates.
Location: /opt/tickles/shared/mcp/tools/fees.py

Agents can query maker/taker fees from the instruments table and the
latest funding rate from derivatives_snapshots to inform backtest
configuration with real exchange data.

Tools registered:
    backtest.fees          — Query maker/taker fees per symbol+exchange
    backtest.funding_rate  — Query latest funding rate for a symbol
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext
from . import db_helper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_EXCHANGE: str = "bybit"

# ---------------------------------------------------------------------------
# Fee query helpers
# ---------------------------------------------------------------------------


def _query_fees(symbol: str, exchange: str) -> Dict[str, Any]:
    """Query maker/taker fees from the instruments table.

    Args:
        symbol: Trading pair symbol (e.g. 'BTC/USDT').
        exchange: Exchange name (e.g. 'bybit').

    Returns:
        Dict with maker_fee_pct, taker_fee_pct, max_leverage, or error.
    """
    rows = db_helper.query(
        "SELECT maker_fee_pct, taker_fee_pct, max_leverage "
        "FROM instruments WHERE symbol = %s AND exchange = %s "
        "AND is_active = true LIMIT 1",
        (symbol, exchange),
    )
    if not rows:
        return {
            "status": "not_found",
            "message": f"no active instrument for {symbol}@{exchange}",
            "symbol": symbol,
            "exchange": exchange,
        }

    r = rows[0]
    return {
        "status": "ok",
        "symbol": symbol,
        "exchange": exchange,
        "makerFeePct": float(r["maker_fee_pct"]) if r["maker_fee_pct"] is not None else None,
        "takerFeePct": float(r["taker_fee_pct"]) if r["taker_fee_pct"] is not None else None,
        "maxLeverage": r["max_leverage"],
    }


def _query_funding_rate(symbol: str, exchange: str) -> Dict[str, Any]:
    """Query the latest funding rate from derivatives_snapshots.

    Args:
        symbol: Trading pair symbol (e.g. 'BTC/USDT').
        exchange: Exchange name (e.g. 'bybit').

    Returns:
        Dict with funding_rate, snapshot_at, or error.
    """
    instrument_id = db_helper.resolve_instrument_id(symbol, exchange)
    if instrument_id is None:
        return {
            "status": "not_found",
            "message": f"no active instrument for {symbol}@{exchange}",
            "symbol": symbol,
            "exchange": exchange,
        }

    rows = db_helper.query(
        "SELECT funding_rate, snapshot_at "
        "FROM derivatives_snapshots "
        "WHERE instrument_id = %s "
        "ORDER BY snapshot_at DESC LIMIT 1",
        (instrument_id,),
    )
    if not rows:
        return {
            "status": "not_found",
            "message": f"no funding rate data for {symbol}@{exchange}",
            "symbol": symbol,
            "exchange": exchange,
            "instrumentId": instrument_id,
        }

    r = rows[0]
    return {
        "status": "ok",
        "symbol": symbol,
        "exchange": exchange,
        "instrumentId": instrument_id,
        "fundingRate": float(r["funding_rate"]) if r["funding_rate"] is not None else None,
        "snapshotAt": r["snapshot_at"].isoformat() if r["snapshot_at"] is not None else None,
    }


# ---------------------------------------------------------------------------
# Sync handler wrappers
# ---------------------------------------------------------------------------


def _handle_fees(p: Dict[str, Any]) -> Dict[str, Any]:
    """Handle backtest.fees tool call.

    Args:
        p: MCP tool params with 'symbol' (required) and optional 'exchange'.

    Returns:
        Dict with fee rates.
    """
    symbol = p.get("symbol")
    if not symbol:
        return {"status": "error", "message": "missing required param: 'symbol'"}
    symbol = str(symbol)
    exchange = str(p.get("exchange", _DEFAULT_EXCHANGE))
    try:
        return _query_fees(symbol, exchange)
    except Exception as exc:
        logger.exception("backtest.fees query failed")
        return {"status": "error", "message": f"fee query failed: {exc}"}


def _handle_funding_rate(p: Dict[str, Any]) -> Dict[str, Any]:
    """Handle backtest.funding_rate tool call.

    Args:
        p: MCP tool params with 'symbol' (required) and optional 'exchange'.

    Returns:
        Dict with latest funding rate.
    """
    symbol = p.get("symbol")
    if not symbol:
        return {"status": "error", "message": "missing required param: 'symbol'"}
    symbol = str(symbol)
    exchange = str(p.get("exchange", _DEFAULT_EXCHANGE))
    try:
        return _query_funding_rate(symbol, exchange)
    except Exception as exc:
        logger.exception("backtest.funding_rate query failed")
        return {"status": "error", "message": f"funding rate query failed: {exc}"}


# ---------------------------------------------------------------------------
# Tool definitions + registration
# ---------------------------------------------------------------------------


def _build_tools(_ctx: ToolContext) -> List[tuple[McpTool, Any]]:
    """Build all backtest fee/funding MCP tools.

    Args:
        _ctx: Shared dependency container (unused but required by interface).

    Returns:
        List of (McpTool, handler) tuples for registration.
    """

    # --- backtest.fees ---
    t_backtest_fees = McpTool(
        name="backtest.fees",
        description=(
            "Query maker and taker fee rates for a trading symbol on a "
            "specific exchange. Returns fee percentages and max leverage "
            "from the instruments table. Use this to configure realistic "
            "fee parameters for backtests."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Trading pair symbol (e.g. 'BTC/USDT', 'ETH/USDT')",
                },
                "exchange": {
                    "type": "string",
                    "default": "bybit",
                    "description": "Exchange name (e.g. 'bybit', 'blofin', 'bitget')",
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _backtest_fees(p: Dict[str, Any]) -> Dict[str, Any]:
        """Query fees via sync helper in thread."""
        return await asyncio.to_thread(_handle_fees, p)

    # --- backtest.funding_rate ---
    t_backtest_funding_rate = McpTool(
        name="backtest.funding_rate",
        description=(
            "Query the latest funding rate for a perpetual futures symbol. "
            "Returns the funding rate from derivatives_snapshots along with "
            "the snapshot timestamp. Use this to configure the funding rate "
            "parameter in backtest configs for realistic perpetual simulations."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Trading pair symbol (e.g. 'BTC/USDT', 'ETH/USDT')",
                },
                "exchange": {
                    "type": "string",
                    "default": "bybit",
                    "description": "Exchange name (e.g. 'bybit', 'blofin', 'bitget')",
                },
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _backtest_funding_rate(p: Dict[str, Any]) -> Dict[str, Any]:
        """Query funding rate via sync helper in thread."""
        return await asyncio.to_thread(_handle_funding_rate, p)

    return [
        (t_backtest_fees, _backtest_fees),
        (t_backtest_funding_rate, _backtest_funding_rate),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all backtest fee/funding tools with the MCP registry.

    Args:
        registry: The tool registry to register tools with.
        ctx: Shared dependency container for the MCP tools.
    """
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)