"""
Module: test_mcp_trading_tools
Purpose: Live integration tests for the new trading MCP tools.
Location: /opt/tickles/shared/tests/test_mcp_trading_tools.py

Run: python3 shared/tests/test_mcp_trading_tools.py
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.utils.config import load_env
load_env()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_tool(tool_name: str, handler: Any, params: Dict[str, Any]):
    print(f"\n>>> Testing tool: {tool_name} with params: {params}")
    try:
        result = await handler(params)
        if result.get("status") == "ok":
            print(f"✅ {tool_name} OK")
            # Print a snippet of the data
            data_key = [k for k in result.keys() if k not in ["status", "message"]][0]
            print(f"   Data ({data_key}): {str(result[data_key])[:200]}...")
        else:
            print(f"❌ {tool_name} FAILED: {result.get('message')}")
    except Exception as e:
        print(f"💥 {tool_name} CRASHED: {e}")

async def run_tests():
    # Override COLLECTION_EXCHANGES to include capitalcom
    os.environ["COLLECTION_EXCHANGES"] = "bybit,blofin,bitget,capitalcom"
    
    from shared.mcp.tools.trading import _build_tools
    from shared.mcp.tools.context import ToolContext
    
    ctx = ToolContext()
    tools = _build_tools(ctx)
    
    # Map tool names to handlers
    tool_map = {tool.name: handler for tool, handler in tools}
    
    test_cases = [
        # Market Ticker
        ("market_ticker", {"venue": "bybit", "symbol": "BTC/USDT:USDT"}),
        ("market_ticker", {"venue": "capitalcom", "symbol": "BTCUSD"}),
        
        # Market Funding
        ("market_funding", {"venue": "bybit", "symbol": "BTC/USDT:USDT"}),
        ("market_funding", {"venue": "capitalcom", "symbol": "BTCUSD"}),
        
        # Market Hours
        ("market_hours", {"venue": "bybit", "symbol": "BTC/USDT"}),
        ("market_hours", {"venue": "capitalcom", "symbol": "BTCUSD"}),
        
        # Account History
        ("account_history", {"venue": "bybit"}),
        ("account_history", {"venue": "capitalcom"}),
    ]
    
    print("=" * 80)
    print("STARTING MCP TRADING TOOLS LIVE TESTS")
    print("=" * 80)
    
    for tool_name, params in test_cases:
        handler = tool_map.get(tool_name)
        if handler:
            await test_tool(tool_name, handler, params)
        else:
            print(f"⚠️ Tool {tool_name} not found in registry")

    # Close any open adapters if possible (though handlers don't expose them)
    # In a real scenario, we might want to add a cleanup mechanism to _build_adapters
    print("\n" + "=" * 80)
    print("TESTS COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(run_tests())
