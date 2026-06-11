"""
MCP tools: Exchange accounts management (Phase Demo Bridge).
Location: /opt/tickles/shared/mcp/tools/accounts.py

Tools:
  accounts.list      — List all exchange accounts
  accounts.add       — Add new exchange account with API credentials
  accounts.edit      — Edit account details/credentials  
  accounts.remove    — Remove an account
  accounts.test      — Test API connectivity, return balance + positions
  accounts.sync_markets — Fetch perpetuals from exchange, update unified_instruments
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_pool():
    from shared.utils.db import DatabasePool
    return await DatabasePool.get_instance()


def _fmt_ts(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


async def _build_ccxt_client(exchange_id: str, api_key: str, secret: str, 
                              passphrase: str = None, sandbox: bool = True):
    """Build and return a CCXT client for testing connectivity."""
    import ccxt.async_support as ccxt
    exchange_class = getattr(ccxt, exchange_id)
    config = {
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'},
    }
    if passphrase:
        config['password'] = passphrase
    client = exchange_class(config)
    # For Bitget demo: use PAPTRADING header directly. CCXT's set_sandbox_mode
    # conflates demo symbols with demo environment, causing "exchange environment
    # is incorrect" (40099). Injecting the header directly avoids this.
    # For Bybit demo: don't call set_sandbox_mode (that's testnet). Callers use
    # enable_demo_trading(True) which routes to api-demo.bybit.com.
    if exchange_id == 'bitget' and sandbox:
        client.headers.update({'PAPTRADING': '1'})
    elif exchange_id != 'bybit' and sandbox:
        try:
            client.set_sandbox_mode(True)
        except Exception:
            pass  # exchange doesn't support sandbox mode
    return client


# ---------------------------------------------------------------------------
# Sync handlers
# ---------------------------------------------------------------------------

def _handle_list_accounts(p: Dict[str, Any]) -> Dict[str, Any]:
    """List all configured exchange accounts."""
    try:
        exchange_filter = p.get('exchange')
        active_only = p.get('activeOnly', False)
        
        query = "SELECT * FROM public.exchange_accounts"
        conditions = []
        params = []
        
        if exchange_filter:
            conditions.append("exchange = %s")
            params.append(str(exchange_filter))
        if active_only:
            conditions.append("is_active = TRUE")
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY exchange, account_name"
        
        rows = db_helper.query(query, tuple(params) if params else None)
        accounts = []
        for r in rows:
            accounts.append({
                "id": r["id"],
                "exchange": r["exchange"],
                "accountName": r["account_name"],
                "description": r.get("description"),
                "accountType": r["account_type"],
                "isActive": r["is_active"],
                "lastTestedAt": _fmt_ts(r.get("last_tested_at")),
                "lastBalance": float(r["last_balance"]) if r.get("last_balance") else None,
                "lastError": r.get("last_error"),
                "createdAt": _fmt_ts(r.get("created_at")),
            })
        
        return {"status": "ok", "accounts": accounts, "count": len(accounts)}
    except Exception as exc:
        logger.exception("accounts.list failed")
        return {"status": "error", "message": str(exc)}


def _handle_add_account(p: Dict[str, Any]) -> Dict[str, Any]:
    """Add a new exchange account."""
    try:
        exchange = str(p["exchange"]).lower()
        account_name = str(p["accountName"])
        description = p.get("description")
        account_type = str(p.get("accountType", "demo"))
        api_key = str(p.get("apiKey", ""))
        api_secret = str(p.get("apiSecret", ""))
        api_passphrase = p.get("apiPassphrase")
        
        if not api_key or not api_secret:
            return {"status": "error", "message": "apiKey and apiSecret are required"}
        
        db_helper.execute(
            """INSERT INTO public.exchange_accounts 
               (exchange, account_name, description, account_type, api_key, api_secret, api_passphrase)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (exchange, account_name) DO UPDATE SET
               description = EXCLUDED.description,
               account_type = EXCLUDED.account_type,
               api_key = EXCLUDED.api_key,
               api_secret = EXCLUDED.api_secret,
               api_passphrase = EXCLUDED.api_passphrase,
               updated_at = NOW()""",
            (exchange, account_name, description, account_type, api_key, api_secret, api_passphrase),
        )
        
        return {
            "status": "ok",
            "exchange": exchange,
            "accountName": account_name,
            "message": f"Account '{exchange}/{account_name}' saved successfully.",
        }
    except Exception as exc:
        logger.exception("accounts.add failed")
        return {"status": "error", "message": str(exc)}


def _handle_edit_account(p: Dict[str, Any]) -> Dict[str, Any]:
    """Edit an existing exchange account."""
    try:
        account_id = int(p["id"])
        
        updates = []
        params = []
        for field in ["description", "account_type", "api_key", "api_secret", 
                       "api_passphrase", "is_active"]:
            if field in p:
                col = field
                if field == "account_type":
                    col = "account_type"
                elif field == "api_key":
                    col = "api_key"
                elif field == "api_secret":
                    col = "api_secret"
                elif field == "api_passphrase":
                    col = "api_passphrase"
                elif field == "is_active":
                    col = "is_active"
                updates.append(f"{col} = %s")
                params.append(p[field])
        
        if not updates:
            return {"status": "error", "message": "No fields to update"}
        
        updates.append("updated_at = NOW()")
        params.append(account_id)
        
        db_helper.execute(
            f"UPDATE public.exchange_accounts SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )
        return {"status": "ok", "message": f"Account #{account_id} updated."}
    except Exception as exc:
        logger.exception("accounts.edit failed")
        return {"status": "error", "message": str(exc)}


def _handle_remove_account(p: Dict[str, Any]) -> Dict[str, Any]:
    """Remove an exchange account."""
    try:
        account_id = int(p["id"])
        
        # Check if account is used in any competition assignments
        rows = db_helper.query(
            "SELECT COUNT(*) as cnt FROM public.competition_agent_exchanges WHERE exchange_account_id = %s",
            (account_id,),
        )
        if rows and rows[0]["cnt"] > 0:
            return {
                "status": "error",
                "message": f"Account #{account_id} is used by {rows[0]['cnt']} competition-agent assignments. Remove those first.",
            }
        
        db_helper.execute("DELETE FROM public.exchange_accounts WHERE id = %s", (account_id,))
        return {"status": "ok", "message": f"Account #{account_id} removed."}
    except Exception as exc:
        logger.exception("accounts.remove failed")
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Async handlers (need CCXT access)
# ---------------------------------------------------------------------------

async def _handle_test_account(p: Dict[str, Any]) -> Dict[str, Any]:
    """Test API connectivity for an exchange account. Fetches balance + positions."""
    try:
        account_id = int(p.get("id", 0))
        
        # Load account from DB
        pool = await _get_pool()
        row = await pool.fetch_one(
            "SELECT * FROM public.exchange_accounts WHERE id = %s", (account_id,)
        )
        if not row:
            return {"status": "error", "message": f"Account #{account_id} not found"}
        
        exchange = row["exchange"]
        api_key = row["api_key"]
        api_secret = row["api_secret"]
        api_passphrase = row.get("api_passphrase")
        account_type = row["account_type"]
        sandbox = account_type == "demo"
        
        result = {"status": "ok", "exchange": exchange, "accountName": row["account_name"]}
        
        try:
            client = await _build_ccxt_client(exchange, api_key, api_secret, api_passphrase, sandbox)
            
            # Bybit demo trading uses api-demo.bybit.com (not testnet, not PAPTRADING).
            if exchange == 'bybit' and account_type == 'demo' and hasattr(client, 'enable_demo_trading'):
                client.enable_demo_trading(True)
            
            # Test balance
            try:
                balance = await client.fetch_balance()
                total = balance.get('total', {})
                usdt = float(total.get('USDT', 0) or 0)
                free = float(balance.get('free', {}).get('USDT', 0) or 0)
                used = float(balance.get('used', {}).get('USDT', 0) or 0)
                
                # Bitget UTA: if V2 fetch_balance returns 0, try CCXT V3 UTA endpoint
                if usdt == 0 and exchange == 'bitget' and hasattr(client, 'privateUtaGetV3AccountAssets'):
                    try:
                        uta = await client.privateUtaGetV3AccountAssets({})
                        data = uta.get('data', {})
                        assets = data.get('assets', []) if isinstance(data, dict) else []
                        for a in assets:
                            if a.get('coin') == 'USDT':
                                usdt = float(a.get('balance', 0) or 0)
                                free = float(a.get('available', 0) or 0)
                                break
                        # Also check top-level usdtEquity
                        if usdt == 0 and isinstance(data, dict):
                            usdt = float(data.get('usdtEquity', 0) or 0)
                    except Exception:
                        pass
                
                result["balance"] = {
                    "totalUsdt": round(usdt, 2),
                    "freeUsdt": round(free, 2),
                    "usedMarginUsdt": round(used, 2),
                }
            except Exception as e:
                logger.warning("accounts.test: balance fetch failed for %s/%s: %s", exchange, row["account_name"], e)
                result["balance"] = {"error": str(e)[:200]}
            
            # Test positions
            try:
                positions = await client.fetch_positions()
                open_pos = []
                for pos in (positions or []):
                    if float(pos.get('contracts', 0)) > 0:
                        open_pos.append({
                            "symbol": pos['symbol'],
                            "side": pos['side'],
                            "quantity": float(pos.get('contracts', 0)),
                            "entryPrice": float(pos.get('entryPrice', 0)),
                            "unrealizedPnl": float(pos.get('unrealizedPnl', 0)),
                            "leverage": int(pos.get('leverage', 0)),
                        })
                result["positions"] = {"count": len(open_pos), "open": open_pos[:20]}
            except Exception as e:
                result["positions"] = {"error": str(e)[:200]}
            
            # Test market access
            try:
                markets = await client.load_markets()
                perp_count = len([k for k in markets if ':USDT' in k or markets[k].get('swap')])
                result["markets"] = {"totalPerpetuals": perp_count}
            except Exception as e:
                result["markets"] = {"error": str(e)[:200]}
            
            await client.close()
            
            # Update last_tested_at and last_balance
            last_balance = result.get("balance", {}).get("totalUsdt")
            await pool.execute(
                "UPDATE public.exchange_accounts SET last_tested_at = NOW(), last_balance = %s, last_error = NULL WHERE id = %s",
                (last_balance, account_id),
            )
            
            result["tested"] = True
            result["message"] = "Connection successful."
            
        except Exception as e:
            err_str = str(e)[:300]
            await pool.execute(
                "UPDATE public.exchange_accounts SET last_tested_at = NOW(), last_error = %s WHERE id = %s",
                (err_str, account_id),
            )
            result["tested"] = False
            result["error"] = err_str
            result["message"] = f"Connection failed: {err_str}"
        
        return result
        
    except Exception as exc:
        logger.exception("accounts.test failed")
        return {"status": "error", "message": str(exc)}


async def _handle_sync_markets(p: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch perpetual markets from exchange and update unified_instruments."""
    try:
        account_id = int(p.get("id", 0))
        
        pool = await _get_pool()
        row = await pool.fetch_one(
            "SELECT * FROM public.exchange_accounts WHERE id = %s", (account_id,)
        )
        if not row:
            return {"status": "error", "message": f"Account #{account_id} not found"}
        
        exchange = row["exchange"]
        api_key = row["api_key"]
        api_secret = row["api_secret"]
        passphrase = row.get("api_passphrase")
        sandbox = row["account_type"] == "demo"
        
        client = await _build_ccxt_client(exchange, api_key, api_secret, passphrase, sandbox)
        # Bybit demo trading uses api-demo.bybit.com
        if exchange == 'bybit' and row["account_type"] == 'demo' and hasattr(client, 'enable_demo_trading'):
            client.enable_demo_trading(True)
        try:
            markets = await client.load_markets()
        finally:
            await client.close()
        
        # Filter to perpetuals only
        added = 0
        updated = 0
        for symbol, m in markets.items():
            if not (m.get('swap') or m.get('linear') or ':USDT' in symbol):
                continue
            
            base = m.get('base', symbol.split('/')[0])
            quote = m.get('quote', 'USDT')
            asset_type = 'crypto'
            
            # Determine canonical symbol
            canonical_symbol = f"{base}/{quote}"
            
            # Upsert into unified_instruments
            existing = await pool.fetch_one(
                "SELECT id FROM public.unified_instruments WHERE exchange = %s AND exchange_symbol = %s",
                (exchange, symbol),
            )
            if existing:
                await pool.execute(
                    """UPDATE public.unified_instruments 
                       SET canonical_asset = %s, base_currency = %s, quote_currency = %s,
                           canonical_symbol = %s, is_active = TRUE, last_synced_at = NOW()
                       WHERE id = %s""",
                    (base, base, quote, canonical_symbol, existing["id"]),
                )
                updated += 1
            else:
                await pool.execute(
                    """INSERT INTO public.unified_instruments
                       (canonical_asset, asset_type, base_currency, quote_currency, exchange, 
                        exchange_symbol, canonical_symbol, is_active, last_synced_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, NOW())""",
                    (base, asset_type, base, quote, exchange, symbol, canonical_symbol),
                )
                added += 1
        
        return {
            "status": "ok",
            "exchange": exchange,
            "added": added,
            "updated": updated,
            "totalPerpetuals": added + updated,
            "message": f"Synced {added} new + {updated} updated perpetuals from {exchange}.",
        }
        
    except Exception as exc:
        logger.exception("accounts.sync_markets failed")
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# Tool definitions + registration
# ---------------------------------------------------------------------------

def _build_tools(ctx: ToolContext) -> list:
    """Build all accounts MCP tools."""

    # --- accounts.list ---
    t_list = McpTool(
        name="accounts.list",
        description="List all configured exchange accounts. Optionally filter by exchange or active status.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "exchange": {"type": "string", "description": "Filter by exchange name (e.g. 'bybit')"},
                "activeOnly": {"type": "boolean", "default": False},
            },
        },
        read_only=True,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    async def _list(p): return await asyncio.to_thread(_handle_list_accounts, p)

    # --- accounts.add ---
    t_add = McpTool(
        name="accounts.add",
        description="Add a new exchange account with API credentials. Updates existing if (exchange, account_name) matches.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "exchange": {"type": "string", "description": "Exchange ID (e.g. 'bybit', 'blofin', 'bitget')"},
                "accountName": {"type": "string", "description": "Account identifier (e.g. 'DEMO', 'DEMO_SHADDOW')"},
                "description": {"type": "string", "description": "Human-readable description"},
                "accountType": {"type": "string", "enum": ["demo", "live", "paper"], "default": "demo"},
                "apiKey": {"type": "string", "description": "API key"},
                "apiSecret": {"type": "string", "description": "API secret"},
                "apiPassphrase": {"type": "string", "description": "API passphrase (Bitget, BloFin)"},
            },
            "required": ["exchange", "accountName", "apiKey", "apiSecret"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    async def _add(p): return await asyncio.to_thread(_handle_add_account, p)

    # --- accounts.edit ---
    t_edit = McpTool(
        name="accounts.edit",
        description="Edit an existing exchange account's details or credentials.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "description": {"type": "string"},
                "accountType": {"type": "string", "enum": ["demo", "live", "paper"]},
                "apiKey": {"type": "string"},
                "apiSecret": {"type": "string"},
                "apiPassphrase": {"type": "string"},
                "isActive": {"type": "boolean"},
            },
            "required": ["id"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    async def _edit(p): return await asyncio.to_thread(_handle_edit_account, p)

    # --- accounts.remove ---
    t_remove = McpTool(
        name="accounts.remove",
        description="Remove an exchange account. Fails if the account is assigned to any competition agents.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    async def _remove(p): return await asyncio.to_thread(_handle_remove_account, p)

    # --- accounts.test ---
    t_test = McpTool(
        name="accounts.test",
        description="Test API connectivity for an exchange account. Fetches balance, open positions, and available markets. Updates last_tested_at and last_balance on success.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
        },
        read_only=True,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    # --- accounts.sync_markets ---  
    t_sync = McpTool(
        name="accounts.sync_markets",
        description="Fetch all perpetual markets from the exchange and sync them into unified_instruments. Use after adding a new exchange or when new coins are listed.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
        },
        read_only=False,
        tags={"phase": "demo", "group": "accounts", "status": "live"},
    )

    return [
        (t_list, _list),
        (t_add, _add),
        (t_edit, _edit),
        (t_remove, _remove),
        (t_test, _handle_test_account),
        (t_sync, _handle_sync_markets),
    ]


def register(registry, ctx):
    """Register all accounts tools into the MCP registry."""
    tools = _build_tools(ctx)
    for tool, handler in tools:
        registry.register(tool, handler)
    logger.info("[accounts] registered %d tools", len(tools))
