"""
Module: routing
Purpose: Round 13 (2026-05-24) MCP tools for the exchange router and
         tracked-positions diagnostics. Lets agents (Hermes, ChartHacker,
         Roo, the operator) inspect routing decisions, query
         unified_instruments, diagnose individual positions, and trigger
         the reconciler — all without writing custom SQL.

         Read-only tools have side-effect-free handlers. Operator tools
         (cancel, reconcile, clear_cache) DO mutate; their handlers
         clearly mark themselves and default the dangerous flag to
         dry-run / off so an agent calling without parameters does the
         safe thing.

Location: /opt/tickles/shared/mcp/tools/routing.py
Round:    Round 13 (2026-05-24)

Tool catalogue
--------------
Read-only:
  * ``router.resolve``        — wrap ``resolve_market`` for symbol
                                 inspection.
  * ``router.distribution``   — per-exchange counts of tracked_positions
                                 by status, optionally windowed.
  * ``instruments.search``    — query ``unified_instruments`` by symbol /
                                 exchange / asset_type.
  * ``positions.diagnose``    — full state of one tracked_position:
                                 latest update row, status_reason, what
                                 the router would say if re-resolved.

Operator-action (mutating):
  * ``router.clear_cache``    — drops the in-process resolver cache.
                                 Useful after a manual edit to
                                 unified_instruments.
  * ``positions.cancel``      — sets a row to status='cancelled' with a
                                 caller-supplied reason. Refuses to
                                 cancel rows with status NOT IN
                                 ('pending','partial_exit') unless
                                 ``allowOpen=True`` is explicitly passed.
  * ``positions.reconcile``   — runs ``round13_reconcile_routing``
                                 in-process. ``apply=False`` by default
                                 (dry-run); ``includeOpen=False`` by
                                 default. Caller must set BOTH apply
                                 and includeOpen=true to mutate live
                                 rows.

Conventions
-----------
* All handlers return ``{"ok": True/False, ...}`` per the project's
  established MCP shape.
* All handlers log function name + parameters + a one-line result tag
  per the project rule.
* Every tool here is registered with ``tags={"phase": "13",
  "group": "routing"}`` so the manage panel can filter Round-13 tools.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_pool() -> Any:
    """Return the shared asyncpg pool. Imports lazily so tests can stub."""
    from shared.utils.db import DatabasePool

    return await DatabasePool.get_instance()


def _fmt_ts(val: Any) -> Optional[str]:
    """Format a datetime / timestamp for JSON output."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _routed_to_dict(routed: Any) -> Dict[str, Any]:
    """Serialise a ``RoutedMarket`` dataclass for JSON return.

    Args:
        routed: A ``shared.utils.exchange_router.RoutedMarket`` instance.

    Returns:
        Plain dict with all RoutedMarket fields + a derived
        ``display_symbol`` field that mirrors the dashboard's
        ``dispSymbol()`` helper (``BTC/USDT:USDT`` → ``BTCUSDT.P``).
    """
    canonical = routed.canonical_symbol or ""
    display = canonical
    # Mirror the dashboard's dispSymbol() shape conversion server-side
    # so MCP callers get the friendly form too.
    if canonical and "/" in canonical:
        try:
            parts = canonical.split("/", 1)
            base = parts[0]
            tail = parts[1]
            if ":" in tail:
                quote = tail.split(":", 1)[0]
                display = f"{base}{quote}.P"
            else:
                display = f"{base}{tail}"
        except (IndexError, ValueError):
            display = canonical
    return {
        "supported": routed.supported,
        "exchange": routed.exchange,
        "exchange_symbol": routed.exchange_symbol,
        "asset_class": routed.asset_class,
        "epic_code": routed.epic_code,
        "ccxt_perp_symbol": routed.ccxt_perp_symbol,
        "canonical_symbol": routed.canonical_symbol,
        "display_symbol": display,
        "unsupported_reason": routed.unsupported_reason,
        "raw_input": routed.raw_input,
    }


# ---------------------------------------------------------------------------
# Tool: router.resolve
# ---------------------------------------------------------------------------
async def _handle_router_resolve(p: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a free-text symbol through ``exchange_router.resolve_market``.

    Params:
        symbol (str, required): Trader-text symbol — anything from
            ``BTC`` to ``BTCUSDT.P`` to ``GOLD`` to ``USDT.D``.
        hintExchange (str, optional): Preferred exchange (``bybit`` /
            ``bitget`` / ``blofin``). Plumbed to ``resolve_market`` as
            the secondary tiebreaker.

    Returns:
        ``ok``, plus the serialised ``RoutedMarket`` fields including
        the new ``display_symbol`` (Bybit-on-TradingView form).
    """
    symbol = str(p.get("symbol", "")).strip()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}
    hint_exchange = p.get("hintExchange") or None
    hint_exchange = str(hint_exchange).lower() if hint_exchange else None

    try:
        from shared.utils.exchange_router import resolve_market

        routed = await resolve_market(symbol, hint_exchange=hint_exchange)
        result: Dict[str, Any] = {"ok": True, "input": symbol}
        result.update(_routed_to_dict(routed))
        logger.info(
            "router.resolve(symbol=%r, hint=%r) -> %s/%s sup=%s",
            symbol, hint_exchange, routed.exchange, routed.exchange_symbol,
            routed.supported,
        )
        return result
    except Exception as exc:
        logger.exception("router.resolve failed for symbol=%r", symbol)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: router.distribution
# ---------------------------------------------------------------------------
async def _handle_router_distribution(p: Dict[str, Any]) -> Dict[str, Any]:
    """Per-exchange counts of tracked_positions broken down by status.

    Params:
        windowDays (int, optional): Lookback window over ``created_at``
            (default 7). Use 0 for "all time".
        company (str, optional): Filter by ``company_id`` (default all).

    Returns:
        ``ok``, ``window_days``, ``rows`` — list of
        ``{exchange, status, n}`` plus a ``totals`` summary.
    """
    window_days = int(p.get("windowDays", 7))
    company = p.get("company")

    where_clauses: List[str] = []
    args: List[Any] = []
    arg_idx = 1
    if window_days > 0:
        where_clauses.append(f"created_at >= NOW() - INTERVAL '{window_days} days'")
    if company:
        where_clauses.append(f"company_id = ${arg_idx}")
        args.append(str(company))
        arg_idx += 1
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            f"""
            SELECT instrument_exchange, status, COUNT(*) AS n
            FROM public.tracked_positions
            {where_sql}
            GROUP BY instrument_exchange, status
            ORDER BY instrument_exchange, status
            """,
            tuple(args),
        )
        breakdown = [
            {
                "exchange": r["instrument_exchange"],
                "status": r["status"],
                "n": int(r["n"]),
            }
            for r in rows
        ]
        # totals per exchange + per status
        per_exchange: Counter = Counter()
        per_status: Counter = Counter()
        for r in breakdown:
            per_exchange[r["exchange"]] += r["n"]
            per_status[r["status"]] += r["n"]
        return {
            "ok": True,
            "window_days": window_days,
            "company_filter": company,
            "rows": breakdown,
            "totals": {
                "per_exchange": dict(per_exchange),
                "per_status": dict(per_status),
                "grand_total": sum(per_exchange.values()),
            },
        }
    except Exception as exc:
        logger.exception("router.distribution failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: router.clear_cache
# ---------------------------------------------------------------------------
async def _handle_router_clear_cache(p: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the in-process resolver cache.

    Useful after manual edits to ``unified_instruments`` so the next
    ``resolve_market`` call sees the latest rows. Idempotent.

    Params: none.
    """
    try:
        from shared.utils.exchange_router import _CACHE, clear_cache

        before = len(_CACHE)
        clear_cache()
        logger.info("router.clear_cache() dropped %d entries", before)
        return {"ok": True, "entries_dropped": before}
    except Exception as exc:
        logger.exception("router.clear_cache failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: instruments.search
# ---------------------------------------------------------------------------
async def _handle_instruments_search(p: Dict[str, Any]) -> Dict[str, Any]:
    """Query ``public.unified_instruments`` with simple substring + filters.

    Params:
        query (str, optional): Substring matched against
            ``exchange_symbol``, ``canonical_symbol`` (case-insensitive).
            Empty string returns the whole table (capped by ``limit``).
        exchange (str, optional): Restrict to a specific exchange.
        assetType (str, optional): Restrict to ``crypto`` / ``forex`` /
            etc. (the ``asset_type`` column).
        activeOnly (bool, optional): Default True. Filter
            ``is_active=TRUE``.
        limit (int, optional): Max rows (default 50, max 500).

    Returns:
        ``ok``, ``count``, ``rows``.
    """
    query = (p.get("query") or "").strip()
    exchange = (p.get("exchange") or "").strip().lower() or None
    asset_type = (p.get("assetType") or "").strip().lower() or None
    active_only = bool(p.get("activeOnly", True))
    limit = max(1, min(int(p.get("limit", 50)), 500))

    where_clauses: List[str] = []
    args: List[Any] = []
    arg_idx = 1

    if active_only:
        where_clauses.append("is_active = TRUE")

    if query:
        where_clauses.append(
            f"(exchange_symbol ILIKE ${arg_idx} OR canonical_symbol ILIKE ${arg_idx})"
        )
        args.append(f"%{query}%")
        arg_idx += 1

    if exchange:
        where_clauses.append(f"exchange = ${arg_idx}")
        args.append(exchange)
        arg_idx += 1

    if asset_type:
        where_clauses.append(f"asset_type = ${arg_idx}")
        args.append(asset_type)
        arg_idx += 1

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            f"""
            SELECT exchange, exchange_symbol, canonical_symbol, asset_type, is_active
            FROM public.unified_instruments
            {where_sql}
            ORDER BY exchange, exchange_symbol
            LIMIT ${arg_idx}
            """,
            tuple(args) + (limit,),
        )
        out_rows = [
            {
                "exchange": r["exchange"],
                "exchange_symbol": r["exchange_symbol"],
                "canonical_symbol": r["canonical_symbol"],
                "asset_type": r["asset_type"],
                "is_active": bool(r["is_active"]),
            }
            for r in rows
        ]
        return {
            "ok": True,
            "count": len(out_rows),
            "limit": limit,
            "filters": {
                "query": query or None,
                "exchange": exchange,
                "asset_type": asset_type,
                "active_only": active_only,
            },
            "rows": out_rows,
        }
    except Exception as exc:
        logger.exception("instruments.search failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: positions.diagnose
# ---------------------------------------------------------------------------
async def _handle_positions_diagnose(p: Dict[str, Any]) -> Dict[str, Any]:
    """Full diagnostic for one ``tracked_positions`` row.

    Pulls the row, the latest ``position_updates`` snapshot, and re-runs
    the router against the stored ``instrument_symbol`` so the caller
    can see whether routing would change today (useful for diagnosing
    rows persisted before a router upgrade).

    Params:
        positionId (int, required): tracked_positions.id.

    Returns:
        ``ok``, ``position`` (row dict), ``last_update`` (row dict or
        None), ``router_says`` (current resolve_market output).
    """
    pid = p.get("positionId")
    try:
        position_id = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        return {"ok": False, "error": "positionId must be an integer"}
    if position_id <= 0:
        return {"ok": False, "error": "positionId is required"}

    try:
        pool = await _get_pool()
        row = await pool.fetch_one(
            """
            SELECT id, instrument_symbol, instrument_exchange,
                   instrument_symbol_normalised, epic_code,
                   direction, entry_price, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   status, status_reason, signal_source,
                   actor_type, actor_id,
                   trader_profile_id, news_item_id, media_item_id,
                   signal_interpretation_id, correlation_id,
                   current_price, price_updated_at,
                   unrealized_pnl_pct, unrealized_pnl_usd,
                   distance_to_entry_pct, distance_to_sl_pct,
                   distance_to_tp1_pct, time_in_trade_minutes,
                   created_at, updated_at, signal_timestamp,
                   closed_at, exit_price, exit_timestamp, exit_reason,
                   realized_pnl_usd, realized_pnl_pct, outcome,
                   company_id
            FROM public.tracked_positions
            WHERE id = $1
            """,
            (position_id,),
        )
        if row is None:
            return {"ok": False, "error": f"position {position_id} not found"}

        position = {k: row[k] for k in row.keys()}
        # Coerce special types for JSON friendliness
        for ts_col in (
            "created_at", "updated_at", "signal_timestamp",
            "closed_at", "exit_timestamp", "price_updated_at",
        ):
            position[ts_col] = _fmt_ts(position.get(ts_col))
        for num_col in (
            "entry_price", "stop_loss", "take_profit_1", "take_profit_2",
            "take_profit_3", "current_price", "unrealized_pnl_pct",
            "unrealized_pnl_usd", "distance_to_entry_pct", "distance_to_sl_pct",
            "distance_to_tp1_pct", "exit_price", "realized_pnl_usd",
            "realized_pnl_pct",
        ):
            v = position.get(num_col)
            position[num_col] = float(v) if v is not None else None

        # Latest position_updates row (if any)
        last_update_row = await pool.fetch_one(
            """
            SELECT id, position_id, price, unrealized_pnl_pct,
                   unrealized_pnl_usd, distance_to_entry_pct,
                   distance_to_sl_pct, distance_to_tp1_pct,
                   time_in_trade_minutes, update_source,
                   timestamp, created_at
            FROM public.position_updates
            WHERE position_id = $1
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (position_id,),
        )
        last_update: Optional[Dict[str, Any]] = None
        if last_update_row is not None:
            last_update = {k: last_update_row[k] for k in last_update_row.keys()}
            last_update["timestamp"] = _fmt_ts(last_update.get("timestamp"))
            last_update["created_at"] = _fmt_ts(last_update.get("created_at"))
            for num_col in (
                "price", "unrealized_pnl_pct", "unrealized_pnl_usd",
                "distance_to_entry_pct", "distance_to_sl_pct",
                "distance_to_tp1_pct",
            ):
                v = last_update.get(num_col)
                last_update[num_col] = float(v) if v is not None else None

        # Re-route via current router (the row may pre-date the latest
        # router policy — e.g. legacy spot canonical, capital epic).
        from shared.utils.exchange_router import resolve_market

        routed = await resolve_market(
            position["instrument_symbol"] or "",
            hint_exchange=position["instrument_exchange"] or None,
        )
        router_says = _routed_to_dict(routed)

        # Drift detection: would re-resolving change anything?
        drift: Dict[str, Any] = {
            "exchange_changes": (
                routed.exchange != position["instrument_exchange"]
                if routed.supported
                else None
            ),
            "symbol_changes": (
                (routed.canonical_symbol or "") != (position["instrument_symbol"] or "")
                if routed.supported
                else None
            ),
            "now_unsupported": (
                routed.unsupported_reason
                if not routed.supported
                else None
            ),
        }

        return {
            "ok": True,
            "position": position,
            "last_update": last_update,
            "router_says": router_says,
            "drift": drift,
        }
    except Exception as exc:
        logger.exception("positions.diagnose failed for id=%s", position_id)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: positions.cancel
# ---------------------------------------------------------------------------
async def _handle_positions_cancel(p: Dict[str, Any]) -> Dict[str, Any]:
    """Manually cancel a tracked position.

    Default-deny on currently-OPEN rows. Operator must pass
    ``allowOpen=True`` AND the row's actual status to confirm intent.

    Params:
        positionId (int, required)
        reason (str, required): Free-form note. Stamped into
            ``status_reason`` as ``manual:<reason>:<utc>``.
        allowOpen (bool, optional): Default False. Required to cancel
            rows where status IN ('open','partial_exit') — those carry
            real exposure.

    Returns:
        ``ok``, ``previous_status``, ``new_status_reason``.
    """
    pid = p.get("positionId")
    try:
        position_id = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        return {"ok": False, "error": "positionId must be an integer"}
    if position_id <= 0:
        return {"ok": False, "error": "positionId is required"}

    reason = (p.get("reason") or "").strip()
    if not reason:
        return {"ok": False, "error": "reason is required"}
    if len(reason) > 200:
        return {"ok": False, "error": "reason must be <=200 chars"}

    allow_open = bool(p.get("allowOpen", False))

    now_iso = datetime.now(timezone.utc).isoformat()
    full_reason = f"manual:{reason}:{now_iso}"[:512]

    try:
        pool = await _get_pool()
        row = await pool.fetch_one(
            "SELECT status FROM public.tracked_positions WHERE id = $1",
            (position_id,),
        )
        if row is None:
            return {"ok": False, "error": f"position {position_id} not found"}
        prev_status = row["status"]
        if prev_status == "cancelled":
            return {
                "ok": True,
                "previous_status": prev_status,
                "new_status_reason": full_reason,
                "no_op": True,
            }
        if prev_status in ("open", "partial_exit") and not allow_open:
            return {
                "ok": False,
                "error": (
                    f"position {position_id} is {prev_status} (real exposure). "
                    "Pass allowOpen=true to confirm cancellation."
                ),
                "previous_status": prev_status,
            }
        if prev_status == "closed":
            return {
                "ok": False,
                "error": f"position {position_id} is already closed",
                "previous_status": prev_status,
            }
        await pool.execute(
            """
            UPDATE public.tracked_positions
            SET status = 'cancelled',
                status_reason = $1,
                updated_at = NOW(),
                closed_at = COALESCE(closed_at, NOW())
            WHERE id = $2
            """,
            (full_reason, position_id),
        )
        logger.info(
            "positions.cancel(id=%d) %s -> cancelled (reason=%s)",
            position_id, prev_status, reason[:60],
        )
        return {
            "ok": True,
            "position_id": position_id,
            "previous_status": prev_status,
            "new_status": "cancelled",
            "new_status_reason": full_reason,
        }
    except Exception as exc:
        logger.exception("positions.cancel failed for id=%s", position_id)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool: positions.reconcile
# ---------------------------------------------------------------------------
async def _handle_positions_reconcile(p: Dict[str, Any]) -> Dict[str, Any]:
    """Run the Round 13 reconciler in-process.

    Wraps ``shared.scripts.round13_reconcile_routing.reconcile``. Every
    flag defaults to the SAFE option (dry-run, pending-only).

    Params:
        apply (bool, optional): Default False. When True, persists the
            re-routings.
        includeOpen (bool, optional): Default False. When True, also
            rewrites currently-open rows' instrument_symbol to perp form.
            Open rows are NEVER cancelled regardless of this flag.
        limit (int, optional): Cap row scan (default 1000, max 10000).

    Returns:
        ``ok``, ``apply``, ``include_open``, ``summary`` (counter dict),
        ``actions`` (truncated list of human-readable strings).
    """
    apply = bool(p.get("apply", False))
    include_open = bool(p.get("includeOpen", False))
    raw_limit = int(p.get("limit", 1000))
    limit = max(1, min(raw_limit, 10000))

    try:
        # We re-implement the script's main loop in-process so we can
        # return a structured action list to the MCP caller (instead of
        # the script's stdout-only logging). The fetch helper is reused.
        from shared.scripts.round13_reconcile_routing import fetch_target_rows
        from shared.utils.exchange_router import (
            clear_cache,
            resolve_market,
            unsupported_status_reason,
        )
        pool = await _get_pool()
        rows = await fetch_target_rows(include_open=include_open, limit=limit)
        counter: Counter = Counter()
        actions: List[Dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        for r in rows:
            clear_cache()
            sym = r["instrument_symbol"]
            cur_ex = r["instrument_exchange"]
            cur_epic = r["epic_code"] if "epic_code" in r else None
            cur_status = r["status"]
            routed = await resolve_market(sym, cur_ex)
            row_id = int(r["id"])

            if routed.supported:
                new_ex = routed.exchange
                new_epic = routed.epic_code
                new_sym = routed.canonical_symbol or sym
                unchanged = (
                    new_ex == cur_ex
                    and new_epic == cur_epic
                    and new_sym == sym
                )
                if unchanged:
                    counter["no_change"] += 1
                    continue
                action = "perp_form" if (
                    new_ex == cur_ex and new_epic == cur_epic and new_sym != sym
                ) else "rerouted"
                counter[action] += 1
                actions.append({
                    "id": row_id,
                    "action": action,
                    "from_symbol": sym,
                    "to_symbol": new_sym,
                    "from_exchange": cur_ex,
                    "to_exchange": new_ex,
                    "from_epic": cur_epic,
                    "to_epic": new_epic,
                })
                if apply:
                    await pool.execute(
                        """
                        UPDATE public.tracked_positions
                        SET instrument_exchange = $1,
                            epic_code = $2,
                            instrument_symbol = $3,
                            instrument_symbol_normalised = $4,
                            updated_at = NOW()
                        WHERE id = $5
                          AND status = ANY($6::text[])
                        """,
                        (
                            new_ex,
                            new_epic,
                            new_sym,
                            new_sym,
                            row_id,
                            ["pending", "open", "partial_exit"],
                        ),
                    )
            else:
                if cur_status in ("open", "partial_exit"):
                    counter[f"skip_open_unsupported:{routed.unsupported_reason}"] += 1
                    actions.append({
                        "id": row_id,
                        "action": "skip_open_unsupported",
                        "symbol": sym,
                        "status": cur_status,
                        "reason": routed.unsupported_reason,
                    })
                    continue
                reason_str = unsupported_status_reason(routed, now_iso)
                counter[f"cancelled:{routed.unsupported_reason}"] += 1
                actions.append({
                    "id": row_id,
                    "action": "cancelled",
                    "symbol": sym,
                    "reason": routed.unsupported_reason,
                    "status_reason": reason_str,
                })
                if apply:
                    await pool.execute(
                        """
                        UPDATE public.tracked_positions
                        SET status = 'cancelled',
                            status_reason = $1,
                            updated_at = NOW(),
                            closed_at = COALESCE(closed_at, NOW())
                        WHERE id = $2
                          AND status = 'pending'
                        """,
                        (reason_str[:512], row_id),
                    )

        # Truncate the actions list for cheap JSON rendering — the
        # caller can re-run with --apply to commit, summary is
        # representative.
        truncated = actions[:200]
        logger.info(
            "positions.reconcile(apply=%s, include_open=%s) summary=%s",
            apply, include_open, dict(counter),
        )
        return {
            "ok": True,
            "apply": apply,
            "include_open": include_open,
            "scanned": len(rows),
            "summary": dict(counter),
            "actions_shown": len(truncated),
            "actions_total": len(actions),
            "actions": truncated,
        }
    except Exception as exc:
        logger.exception("positions.reconcile failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------
def _build_tools(_ctx: ToolContext) -> List[Tuple[McpTool, Any]]:
    """Build all routing MCP tools."""
    return [
        (
            McpTool(
                name="router.resolve",
                description=(
                    "Round 13: Resolve a trader-text symbol to "
                    "(exchange, exchange_symbol, perp form, asset_class). "
                    "Read-only. Useful before pushing a signal through "
                    "to confirm routing."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": (
                                "Trader-text symbol "
                                "(BTC, BTCUSDT.P, GOLD, BTC/USD, USDT.D)."
                            ),
                        },
                        "hintExchange": {
                            "type": "string",
                            "description": (
                                "Optional preferred exchange "
                                "(bybit/bitget/blofin)."
                            ),
                        },
                    },
                    "required": ["symbol"],
                },
                read_only=True,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_router_resolve,
        ),
        (
            McpTool(
                name="router.distribution",
                description=(
                    "Round 13: Per-exchange counts of tracked_positions "
                    "broken down by status. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "windowDays": {
                            "type": "integer",
                            "description": (
                                "Lookback window over created_at "
                                "(default 7, 0=all time)."
                            ),
                        },
                        "company": {
                            "type": "string",
                            "description": "Optional company_id filter.",
                        },
                    },
                },
                read_only=True,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_router_distribution,
        ),
        (
            McpTool(
                name="router.clear_cache",
                description=(
                    "Round 13: Drop the in-process resolver cache. "
                    "Mutating but cheap and idempotent."
                ),
                input_schema={"type": "object", "properties": {}},
                read_only=False,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_router_clear_cache,
        ),
        (
            McpTool(
                name="instruments.search",
                description=(
                    "Round 13: Query unified_instruments by substring + "
                    "exchange + asset_type. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Substring matched against exchange_symbol / "
                                "canonical_symbol (case-insensitive)."
                            ),
                        },
                        "exchange": {
                            "type": "string",
                            "description": (
                                "Restrict to one exchange "
                                "(bybit/bitget/blofin/capital.com)."
                            ),
                        },
                        "assetType": {
                            "type": "string",
                            "description": "crypto / forex / etc.",
                        },
                        "activeOnly": {
                            "type": "boolean",
                            "description": "Default true.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Default 50, max 500.",
                        },
                    },
                },
                read_only=True,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_instruments_search,
        ),
        (
            McpTool(
                name="positions.diagnose",
                description=(
                    "Round 13: Full diagnostic view of one "
                    "tracked_positions row — current row, latest "
                    "position_updates snapshot, current router output, "
                    "and drift flags. Read-only."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "positionId": {
                            "type": "integer",
                            "description": "tracked_positions.id",
                        },
                    },
                    "required": ["positionId"],
                },
                read_only=True,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_positions_diagnose,
        ),
        (
            McpTool(
                name="positions.cancel",
                description=(
                    "Round 13: Manually cancel a tracked position with "
                    "an operator-supplied reason. Default-deny on open "
                    "rows — pass allowOpen=true to override."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "positionId": {
                            "type": "integer",
                            "description": "tracked_positions.id",
                        },
                        "reason": {
                            "type": "string",
                            "description": (
                                "Free-form reason (<=200 chars). Stamped "
                                "as manual:<reason>:<utc>."
                            ),
                        },
                        "allowOpen": {
                            "type": "boolean",
                            "description": (
                                "Required to cancel currently-open rows. "
                                "Default false."
                            ),
                        },
                    },
                    "required": ["positionId", "reason"],
                },
                read_only=False,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_positions_cancel,
        ),
        (
            McpTool(
                name="positions.reconcile",
                description=(
                    "Round 13: Re-route existing tracked_positions through "
                    "the current router policy. Default is dry-run "
                    "(apply=false). Set apply=true to commit; set "
                    "includeOpen=true to also rewrite open rows' symbols "
                    "to perp form (open rows are never cancelled)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "apply": {
                            "type": "boolean",
                            "description": "Default false (dry-run).",
                        },
                        "includeOpen": {
                            "type": "boolean",
                            "description": (
                                "Also process open / partial_exit rows. "
                                "Default false."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows scanned (default 1000, max 10000).",
                        },
                    },
                },
                read_only=False,
                tags={"phase": "13", "group": "routing"},
            ),
            _handle_positions_reconcile,
        ),
    ]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all Round 13 routing MCP tools."""
    tools = _build_tools(ctx)
    for tool, handler in tools:
        registry.register(tool, handler)
    logger.info("[routing] registered %d Round 13 tools", len(tools))
