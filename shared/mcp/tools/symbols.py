"""
Module: symbols
Purpose: MCP tools for the symbol resolution pipeline — cleanup, lookup,
         symbol_mappings CRUD, unresolved_symbols queue, and on-demand
         LLM resolution. Lets agents (Hermes, ChartHacker, Roo, operator)
         inspect and manage the symbol-learning system.

Location: /opt/tickles/shared/mcp/tools/symbols.py
Built:    2026-05-25

Tool catalogue
--------------
Read-only:
  * ``symbol.cleanup``          — Run the cleanup pipeline on any raw text.
  * ``symbol.lookup``           — Full resolution: cleanup → mappings →
                                   unified_instruments → priority routing.
  * ``symbol.mappings.list``    — List entries in the symbol_mappings table.
  * ``symbol.unknowns.list``    — List pending/resolved/unresolvable unknowns.

Operator-action (mutating):
  * ``symbol.mappings.add``     — Add or update a manual mapping entry.
  * ``symbol.unknowns.register``— Register an unknown symbol for future
                                   resolution by the 12h cron or manual run.
  * ``symbol.learn.run``        — Trigger immediate LLM resolution of all
                                   pending unknowns (same as cron but on-demand).
"""
from __future__ import annotations

import logging
import re as _re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext

logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────
async def _get_pool() -> Any:
    from shared.utils.db import DatabasePool
    return await DatabasePool.get_instance()


# Copy of cleanup from interpretation_service (keep in sync)
_REMAP_KEYS = {
    "BITTENSOR/USDT": "TAO/USDT", "ZCASH/USDT": "ZEC/USDT",
    "BEAMX/USDT": "BEAM/USDT",
    "XAU/USD": "XAU/USDT", "BTC/USD": "BTC/USDT", "ETH/USD": "ETH/USDT",
    "GOLD": "XAU/USDT", "XAU": "XAU/USDT",
    "NQ": "QQQ/USDT", "NAS100": "QQQ/USDT", "US100": "QQQ/USDT",
    "NQ/USDT": "QQQ/USDT", "NAS100/USDT": "QQQ/USDT",
}


def _cleanup(raw: str) -> Optional[str]:
    """Strip everything to bare BASE/USDT. Returns None for UNKNOWN."""
    if not raw:
        return None
    s = raw.strip().upper()
    if s == "UNKNOWN":
        return None
    s = s.lstrip("$#")                         # social-media prefixes
    s = _re.sub(r"^[A-Z]+:", "", s)           # exchange prefix
    s = _re.sub(r"[.:/]P$", "", s)            # perp suffix
    s = _re.sub(r":USDT$|:USDC$", "", s)      # CCXT perp suffix
    s = _re.sub(r"[12]!$", "", s)             # futures month
    if "/" in s:
        parts = s.split("/", 1)
        base, quote = parts[0], parts[1]
        if quote in ("USD", "USDC", "BUSD", "TETHERUS"):
            s = f"{base}/USDT"
        s = _REMAP_KEYS.get(s, s)
    else:
        for q in ("USDT", "USDC", "USD", "BUSD"):
            if s.endswith(q) and len(s) > len(q):
                s = s[:-len(q)]
                break
        s = _REMAP_KEYS.get(s, s)
    # Strip contract-multiplier prefixes from BASE.
    # Only strips when there are 3+ leading digits (1000PEPE, 1000000MOG).
    # Single/double-digit prefixes are real tickers (1INCH, 2Z).
    if s and s[0].isdigit():
        _digits = _re.match(r'^(\d+)', s)
        if _digits and len(_digits.group(1)) >= 3:
            if "/" in s:
                _base, _rest = s.split("/", 1)
                _clean = _re.sub(r'^\d+', '', _base)
                if _clean and len(_clean) >= 2:
                    s = f"{_clean}/{_rest}"
            else:
                _clean = _re.sub(r'^\d+', '', s)
                if _clean and len(_clean) >= 2:
                    s = _clean
    # Add /USDT if bare base
    if s and "/" not in s:
        s = f"{s}/USDT"
    # Re-apply remap for slash form after digit strip
    if "/" in s:
        s = _REMAP_KEYS.get(s, s)
    # Reject dominance metrics (USDT.D, BTC.D, etc.)
    if s.endswith(".D/USDT"):
        return None
    return s


# ── symbol.cleanup ─────────────────────────────────────────────────────
async def _handle_symbol_cleanup(p: Dict[str, Any]) -> Dict[str, Any]:
    """Run the cleanup pipeline on a raw symbol (read-only, diagnostic)."""
    raw = str(p.get("symbol", "")).strip()
    if not raw:
        return {"ok": False, "error": "symbol is required"}

    cleaned = _cleanup(raw)
    base = cleaned.split("/")[0] if cleaned else None
    result: Dict[str, Any] = {
        "ok": True,
        "raw": raw,
        "cleaned": cleaned,
        "base": base,
    }
    if cleaned and cleaned != raw.upper():
        result["transformed"] = True
    else:
        result["transformed"] = False
    return result


# ── symbol.lookup ──────────────────────────────────────────────────────
async def _handle_symbol_lookup(p: Dict[str, Any]) -> Dict[str, Any]:
    """Full resolution: cleanup → mappings → unified_instruments."""
    raw = str(p.get("symbol", "")).strip()
    if not raw:
        return {"ok": False, "error": "symbol is required"}

    cleaned = _cleanup(raw)
    base = cleaned.split("/")[0] if cleaned else None
    result: Dict[str, Any] = {
        "ok": True, "raw": raw, "cleaned": cleaned, "base": base,
    }

    if not cleaned:
        result["resolved"] = False
        result["reason"] = "uncleanable (UNKNOWN or empty)"
        return result

    pool = await _get_pool()

    # 1. Check symbol_mappings
    map_row = await pool.fetch_one(
        """SELECT resolved_base, resolved_exchange, resolved_symbol,
                  asset_type, priority, resolution_method
           FROM symbol_mappings
           WHERE raw_symbol = $1 AND is_active = TRUE
           ORDER BY priority LIMIT 1""",
        (raw.upper(),),
    )
    if map_row:
        result.update({
            "resolved": True,
            "method": "mapping_table",
            "exchange": map_row["resolved_exchange"],
            "exchange_symbol": map_row["resolved_symbol"],
            "resolved_base": map_row["resolved_base"],
            "asset_type": map_row["asset_type"],
            "mapping_priority": map_row["priority"],
            "mapping_method": map_row["resolution_method"],
        })
        return result

    # 2. Check unresolved_symbols — is this a known unknown?
    unk_row = await pool.fetch_one(
        "SELECT status, seen_count FROM unresolved_symbols WHERE raw_symbol = $1",
        (raw.upper(),),
    )
    if unk_row:
        result["unresolved_status"] = unk_row["status"]
        result["unresolved_seen_count"] = unk_row["seen_count"]

    # 3. Direct unified_instruments lookup
    inst_row = await pool.fetch_one(
        """SELECT exchange, exchange_symbol, base_currency, quote_currency, asset_type
           FROM unified_instruments
           WHERE is_active = TRUE AND base_currency = $1 AND quote_currency = 'USDT'
             AND exchange IN ('bybit','blofin','bitget')
             AND exchange_symbol ~ '^[A-Z0-9]+/USDT:USDT$'
           ORDER BY CASE exchange WHEN 'bybit' THEN 1 WHEN 'blofin' THEN 2
                                   WHEN 'bitget' THEN 3 ELSE 99 END
           LIMIT 1""",
        (base,),
    )
    if inst_row:
        result.update({
            "resolved": True,
            "method": "direct_db",
            "exchange": inst_row["exchange"],
            "exchange_symbol": inst_row["exchange_symbol"],
            "resolved_base": inst_row["base_currency"],
            "asset_type": inst_row["asset_type"],
        })
        return result

    result["resolved"] = False
    result["reason"] = "not_in_unified_instruments"
    if not unk_row:
        result["action_needed"] = "register as unknown for future LLM resolution"
    return result


# ── symbol.mappings.list ───────────────────────────────────────────────
async def _handle_symbol_mappings_list(p: Dict[str, Any]) -> Dict[str, Any]:
    """List symbol_mappings entries, filterable."""
    method = (p.get("method") or "").strip().lower() or None
    asset_type = (p.get("assetType") or "").strip().lower() or None
    limit = max(1, min(int(p.get("limit", 50)), 200))

    where = ["is_active = TRUE"]
    args: List[Any] = []
    idx = 1
    if method:
        where.append(f"resolution_method = ${idx}")
        args.append(method)
        idx += 1
    if asset_type:
        where.append(f"asset_type = ${idx}")
        args.append(asset_type)
        idx += 1

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            f"""SELECT id, raw_symbol, cleaned_base, resolved_base,
                       resolved_exchange, resolved_symbol, asset_type,
                       priority, resolution_method, llm_model, resolved_at,
                       created_at
                FROM symbol_mappings
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT ${idx}""",
            tuple(args) + (limit,),
        )
        return {
            "ok": True,
            "count": len(rows),
            "rows": [
                {
                    "raw_symbol": r["raw_symbol"],
                    "resolved_base": r["resolved_base"],
                    "exchange": r["resolved_exchange"],
                    "symbol": r["resolved_symbol"],
                    "asset_type": r["asset_type"],
                    "priority": r["priority"],
                    "method": r["resolution_method"],
                    "model": r["llm_model"],
                    "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        logger.exception("symbol.mappings.list failed")
        return {"ok": False, "error": str(exc)}


# ── symbol.mappings.add ────────────────────────────────────────────────
async def _handle_symbol_mappings_add(p: Dict[str, Any]) -> Dict[str, Any]:
    """Manually add/update a symbol mapping (operator tool)."""
    raw = str(p.get("symbol", "")).strip()
    if not raw:
        return {"ok": False, "error": "symbol (raw_symbol) is required"}
    resolved_base = str(p.get("resolvedBase", "")).strip()
    exchange = str(p.get("exchange", "")).strip()
    symbol = str(p.get("resolvedSymbol", "")).strip()
    asset_type = str(p.get("assetType", "crypto")).strip()
    priority = max(1, min(int(p.get("priority", 1)), 10))

    if not resolved_base or not exchange or not symbol:
        return {"ok": False, "error": "resolvedBase, exchange, resolvedSymbol required"}

    cleaned = _cleanup(raw)
    try:
        pool = await _get_pool()
        await pool.execute(
            """INSERT INTO symbol_mappings
               (raw_symbol, cleaned_base, resolved_base, resolved_exchange,
                resolved_symbol, asset_type, priority, resolution_method, resolved_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, 'manual', NOW())
               ON CONFLICT (raw_symbol, resolved_exchange, priority)
               DO UPDATE SET resolved_base = $3, resolved_symbol = $5,
                             asset_type = $6, resolution_method = 'manual',
                             resolved_at = NOW()""",
            (raw.upper(), cleaned or "", resolved_base.upper(), exchange.lower(),
             symbol, asset_type, priority),
        )
        # Also mark as resolved in unresolved_symbols
        await pool.execute(
            """INSERT INTO unresolved_symbols (raw_symbol, cleaned_base, status, resolved_at, notes)
               VALUES ($1, $2, 'resolved', NOW(), 'manually added via MCP')
               ON CONFLICT (raw_symbol) DO UPDATE
               SET status = 'resolved', resolved_at = NOW(),
                   notes = 'manually updated via MCP'""",
            (raw.upper(), cleaned or ""),
        )
        logger.info("symbol.mappings.add: %s → %s/%s", raw, exchange, symbol)
        return {"ok": True, "raw_symbol": raw.upper(), "exchange": exchange, "symbol": symbol}
    except Exception as exc:
        logger.exception("symbol.mappings.add failed")
        return {"ok": False, "error": str(exc)}


# ── symbol.unknowns.list ───────────────────────────────────────────────
async def _handle_symbol_unknowns_list(p: Dict[str, Any]) -> Dict[str, Any]:
    """List unresolved_symbols entries, filterable by status."""
    status = (p.get("status") or "").strip().lower() or None
    limit = max(1, min(int(p.get("limit", 50)), 200))

    where = "TRUE"
    args: List[Any] = []
    idx = 1
    if status:
        where = f"status = ${idx}"
        args.append(status)
        idx += 1

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            f"""SELECT raw_symbol, cleaned_base, status, seen_count,
                       first_seen_at, last_seen_at, resolved_at, notes
                FROM unresolved_symbols
                WHERE {where}
                ORDER BY last_seen_at DESC
                LIMIT ${idx}""",
            tuple(args) + (limit,),
        )
        status_counts = await pool.fetch_all(
            "SELECT status, COUNT(*) FROM unresolved_symbols GROUP BY status"
        )
        return {
            "ok": True,
            "count": len(rows),
            "status_summary": {r["status"]: r["count"] for r in status_counts},
            "rows": [
                {
                    "raw_symbol": r["raw_symbol"],
                    "cleaned_base": r["cleaned_base"],
                    "status": r["status"],
                    "seen_count": r["seen_count"],
                    "first_seen": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
                    "last_seen": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                    "notes": r["notes"],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        logger.exception("symbol.unknowns.list failed")
        return {"ok": False, "error": str(exc)}


# ── symbol.unknowns.register ───────────────────────────────────────────
async def _handle_symbol_unknowns_register(p: Dict[str, Any]) -> Dict[str, Any]:
    """Register an unknown symbol for future resolution."""
    raw = str(p.get("symbol", "")).strip()
    if not raw:
        return {"ok": False, "error": "symbol is required"}

    cleaned = _cleanup(raw)
    try:
        pool = await _get_pool()
        await pool.execute(
            """INSERT INTO unresolved_symbols (raw_symbol, cleaned_base, status)
               VALUES ($1, $2, 'pending')
               ON CONFLICT (raw_symbol) DO UPDATE SET
                 last_seen_at = NOW(), seen_count = unresolved_symbols.seen_count + 1,
                 status = CASE WHEN unresolved_symbols.status = 'unresolvable'
                           THEN 'pending' ELSE unresolved_symbols.status END""",
            (raw.upper(), cleaned or ""),
        )
        logger.info("symbol.unknowns.register: %s", raw)
        return {"ok": True, "raw_symbol": raw.upper(), "cleaned_base": cleaned}
    except Exception as exc:
        logger.exception("symbol.unknowns.register failed")
        return {"ok": False, "error": str(exc)}


# ── symbol.learn.run ───────────────────────────────────────────────────
async def _handle_symbol_learn_run(p: Dict[str, Any]) -> Dict[str, Any]:
    """Trigger immediate LLM resolution of all pending unknowns (operator tool)."""
    dry_run = bool(p.get("dryRun", False))

    try:
        pool = await _get_pool()

        # Count pending
        pending_row = await pool.fetch_one(
            "SELECT COUNT(*) AS n FROM unresolved_symbols WHERE status = 'pending'"
        )
        pending_count = pending_row["n"] if pending_row else 0

        if pending_count == 0:
            return {"ok": True, "resolved": 0, "message": "no pending unknowns"}

        if dry_run:
            rows = await pool.fetch_all(
                "SELECT raw_symbol, cleaned_base, seen_count FROM unresolved_symbols WHERE status='pending'"
            )
            return {
                "ok": True,
                "dry_run": True,
                "pending_count": pending_count,
                "pending": [{"symbol": r["raw_symbol"], "seen": r["seen_count"]} for r in rows],
            }

        from shared.utils.symbol_learner import resolve_pending

        count = await resolve_pending(pool)
        logger.info("symbol.learn.run: resolved %d/%d", count, pending_count)
        return {"ok": True, "resolved": count, "pending_before": pending_count}
    except Exception as exc:
        logger.exception("symbol.learn.run failed")
        return {"ok": False, "error": str(exc)}


# ── registration ───────────────────────────────────────────────────────
def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all symbol-resolution tools against the registry."""
    tools: List[tuple[McpTool, Any]] = [
        (
            McpTool(
                name="symbol.cleanup",
                description=(
                    "Run the symbol cleanup pipeline on any raw text. "
                    "Shows what the interpretation_service would extract "
                    "after stripping exchange prefixes, perp suffixes, "
                    "quote remapping, and alias translation. Read-only, "
                    "diagnostic — does not change any state."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Raw symbol text (e.g. 'BYBIT:BTCUSDT.P', 'BITTENSOR/USDT', 'NQ')",
                        },
                    },
                    "required": ["symbol"],
                },
                read_only=True,
                tags={"phase": "symbols", "group": "symbols", "kind": "diagnostic"},
            ),
            _handle_symbol_cleanup,
        ),
        (
            McpTool(
                name="symbol.lookup",
                description=(
                    "Full symbol resolution pipeline: cleanup the raw text, "
                    "check the symbol_mappings table, check unified_instruments, "
                    "apply priority routing (bybit > blofin > bitget). "
                    "Returns the best tradeable instrument or explains why "
                    "resolution failed. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Raw symbol to resolve (e.g. 'BTC/USDT', 'HUSDT.P', 'NQ')",
                        },
                    },
                    "required": ["symbol"],
                },
                read_only=True,
                tags={"phase": "symbols", "group": "symbols", "kind": "resolution"},
            ),
            _handle_symbol_lookup,
        ),
        (
            McpTool(
                name="symbol.mappings.list",
                description=(
                    "List entries in the symbol_mappings table. Filter by "
                    "resolution_method ('manual','llm_auto','exact_match') "
                    "and/or asset_type ('crypto','forex','index','commodity'). "
                    "Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "description": "Filter by resolution_method (manual, llm_auto, exact_match)",
                        },
                        "assetType": {
                            "type": "string",
                            "description": "Filter by asset_type (crypto, forex, index, commodity)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 50,
                        },
                    },
                },
                read_only=True,
                tags={"phase": "symbols", "group": "symbols", "kind": "data"},
            ),
            _handle_symbol_mappings_list,
        ),
        (
            McpTool(
                name="symbol.mappings.add",
                description=(
                    "Add or update a manual symbol mapping. Maps a raw LLM-"
                    "emitted symbol (e.g. 'BITTENSOR/USDT') to a tradeable "
                    "exchange instrument (e.g. bybit/TAO/USDT:USDT). Also "
                    "marks the symbol as resolved in unresolved_symbols. "
                    "Operator tool — mutates the mappings table."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Raw LLM-emitted symbol (e.g. 'BITTENSOR/USDT')",
                        },
                        "resolvedBase": {
                            "type": "string",
                            "description": "Canonical base currency on the exchange (e.g. 'TAO')",
                        },
                        "exchange": {
                            "type": "string",
                            "description": "Exchange to route to (bybit, blofin, bitget, capital.com)",
                        },
                        "resolvedSymbol": {
                            "type": "string",
                            "description": "Exchange symbol in CCXT form (e.g. 'TAO/USDT:USDT')",
                        },
                        "assetType": {
                            "type": "string",
                            "description": "Asset class (crypto, forex, index, commodity, stock)",
                            "default": "crypto",
                        },
                        "priority": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 1,
                            "description": "Priority (1=primary, 2=fallback)",
                        },
                    },
                    "required": ["symbol", "resolvedBase", "exchange", "resolvedSymbol"],
                },
                read_only=False,
                tags={"phase": "symbols", "group": "symbols", "kind": "operator"},
            ),
            _handle_symbol_mappings_add,
        ),
        (
            McpTool(
                name="symbol.unknowns.list",
                description=(
                    "List entries in the unresolved_symbols table — symbols "
                    "that have been seen but couldn't be resolved. Filter by "
                    "status ('pending','resolving','resolved','unresolvable'). "
                    "Includes a status-summary count. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "Filter by status (pending, resolved, unresolvable)",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "default": 50,
                        },
                    },
                },
                read_only=True,
                tags={"phase": "symbols", "group": "symbols", "kind": "data"},
            ),
            _handle_symbol_unknowns_list,
        ),
        (
            McpTool(
                name="symbol.unknowns.register",
                description=(
                    "Register an unknown symbol for future LLM resolution. "
                    "Adds/updates the unresolved_symbols table. If the symbol "
                    "was previously marked 'unresolvable', resets to 'pending' "
                    "so the next 12h cron (or manual symbol.learn.run) will "
                    "retry resolution. Agent tool — safe to call repeatedly."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Unknown symbol to register (e.g. 'SOMENEWCOIN/USDT')",
                        },
                    },
                    "required": ["symbol"],
                },
                read_only=False,
                tags={"phase": "symbols", "group": "symbols", "kind": "agent"},
            ),
            _handle_symbol_unknowns_register,
        ),
        (
            McpTool(
                name="symbol.learn.run",
                description=(
                    "Trigger immediate LLM resolution of all pending unknown "
                    "symbols. Same as the 12-hourly cron but on-demand. Uses "
                    "Gemini Flash via OpenRouter (~$0.0005 per resolution). "
                    "Pass dryRun=true to preview what would be resolved. "
                    "Operator tool — mutates symbol_mappings and "
                    "unresolved_symbols tables."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dryRun": {
                            "type": "boolean",
                            "default": False,
                            "description": "Preview pending unknowns without calling LLM",
                        },
                    },
                },
                read_only=False,
                tags={"phase": "symbols", "group": "symbols", "kind": "operator"},
            ),
            _handle_symbol_learn_run,
        ),
    ]

    for tool, handler in tools:
        registry.register(tool, handler)

    logger.info(
        "symbols: registered %d tools (%d read-only, %d operator)",
        len(tools),
        sum(1 for t, _ in tools if t.read_only),
        sum(1 for t, _ in tools if not t.read_only),
    )
