"""
Module: trading
Purpose: MCP tools for trading (execution, banker, treasury, positions, wallets)
Location: /opt/tickles/shared/mcp/tools/trading.py

Phase M2 wires the trading surface over MCP. All execution goes through
the paper adapter (in-memory deterministic fills). Live trading is not
unlocked in this phase — that requires a separate CEO decision.

Tools registered:
    banker.snapshot      — Paperclip finance/costs summary
    banker.positions     — Current open positions from positions_current view
    treasury.evaluate    — Evaluate a trade intent against capabilities + capital
    execution.submit     — Submit order via paper adapter (dryRun=true default)
    execution.cancel     — Cancel an open order
    execution.status     — Get order status by order ID or client order ID
    wallet.paper_create  — Create a paper wallet with starting balance
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import time
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext
from . import db_helper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAPER_TAKER_FEE_BPS: float = 4.0
_PAPER_MAKER_FEE_BPS: float = 0.0
_PAPER_SLIPPAGE_BPS: float = 0.0
_DEFAULT_RISK_PER_TRADE_PCT: float = 0.01
_DEFAULT_VENUE: str = "bybit"
_DEFAULT_STARTING_USD: float = 10000.0
_TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})

# Monotonic counter for deterministic client_order_id generation.
_COID_COUNTER = itertools.count(1)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_ts(val: Any) -> Optional[str]:
    """Format a timestamp value to ISO 8601 string.

    Args:
        val: A datetime, string, or None.

    Returns:
        ISO 8601 string or None.
    """
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _decimal_to_float(val: Any) -> Optional[float]:
    """Convert a Decimal/numeric value to float, None-safe.

    Args:
        val: A Decimal, float, int, or None.

    Returns:
        Float value or None.
    """
    if val is None:
        return None
    return float(val)


def _side_to_direction(side: str) -> str:
    """Map MCP 'side' (buy/sell) to execution 'direction' (long/short).

    Args:
        side: Order side — 'buy' or 'sell'.

    Returns:
        Execution direction — 'long' or 'short'.

    Raises:
        ValueError: If side is not 'buy' or 'sell'.
    """
    mapping = {"buy": "long", "sell": "short"}
    result = mapping.get(side.lower())
    if result is None:
        raise ValueError(f"invalid side: {side!r} (expected 'buy' or 'sell')")
    return result


def _generate_client_order_id(
    company_id: str, agent_id: str, symbol: str, direction: str,
) -> str:
    """Generate a deterministic client_order_id for paper orders.

    Uses company, agent, symbol, direction + monotonic counter + timestamp
    to produce a unique but reproducible ID.

    Args:
        company_id: Company identifier.
        agent_id: Agent identifier.
        symbol: Trading pair.
        direction: 'long' or 'short'.

    Returns:
        Client order ID string prefixed with 'tk-'.
    """
    counter = next(_COID_COUNTER)
    material = (
        f"{company_id}|{agent_id}|{symbol}|{direction}|"
        f"{time.time_ns()}|{counter}"
    )
    return "tk-" + hashlib.sha256(material.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Async pool + router singletons
# ---------------------------------------------------------------------------

_ROUTER: Optional[Any] = None
_ROUTER_LOCK = asyncio.Lock()


async def _get_pool() -> Any:
    """Get the shared async DatabasePool (lazy-init via singleton).

    Returns:
        The shared DatabasePool instance.
    """
    from shared.utils.db import DatabasePool

    return await DatabasePool.get_instance()


async def _get_router() -> Any:
    """Lazy-initialize the paper-only ExecutionRouter singleton.

    Creates a PaperExecutionAdapter and ExecutionRouter on first call.
    Subsequent calls return the cached router.

    Returns:
        The shared ExecutionRouter instance.
    """
    global _ROUTER
    if _ROUTER is not None:
        return _ROUTER
    async with _ROUTER_LOCK:
        if _ROUTER is not None:
            return _ROUTER
        from shared.execution.paper import PaperExecutionAdapter
        from shared.execution.router import ExecutionRouter

        pool = await _get_pool()
        paper = PaperExecutionAdapter(
            taker_fee_bps=_PAPER_TAKER_FEE_BPS,
            maker_fee_bps=_PAPER_MAKER_FEE_BPS,
            slippage_bps=_PAPER_SLIPPAGE_BPS,
        )
        _ROUTER = ExecutionRouter(
            pool,
            adapters={"paper": paper},
            default_adapter="paper",
        )
        logger.info("paper execution router initialized")
        return _ROUTER


# ---------------------------------------------------------------------------
# Market price helper (sync, uses db_helper)
# ---------------------------------------------------------------------------


def _get_latest_price(symbol: str, venue: str) -> Optional[Dict[str, Any]]:
    """Read the latest candle close price for a symbol/venue.

    Bid/ask are approximated from close with a 0.02% half-spread
    (0.01% each side), suitable for paper trading.

    Args:
        symbol: Trading pair (e.g. 'BTC/USDT').
        venue: Exchange name (e.g. 'bybit').

    Returns:
        Dict with keys price, bid, ask, ts — or None if no data.
    """
    iid = db_helper.resolve_instrument_id(symbol, venue)
    if iid is None:
        return None
    rows = db_helper.query(
        "SELECT close, timestamp FROM candles "
        "WHERE instrument_id = %s AND timeframe = '1m' "
        "ORDER BY timestamp DESC LIMIT 1",
        (iid,),
    )
    if not rows:
        return None
    close = float(rows[0]["close"])
    half_spread = close * 0.0001
    return {
        "price": close,
        "bid": close - half_spread,
        "ask": close + half_spread,
        "ts": rows[0]["timestamp"],
    }


# ---------------------------------------------------------------------------
# Paper wallet account ID convention
# ---------------------------------------------------------------------------


def _paper_account_id(company_id: str, agent_id: str, venue: str) -> str:
    """Derive the account_id_external for a paper wallet.

    Args:
        company_id: Company identifier.
        agent_id: Agent identifier.
        venue: Exchange name.

    Returns:
        Account ID string in format 'paper_{company}_{agent}_{venue}'.
    """
    return f"paper_{company_id}_{agent_id}_{venue}"


# ---------------------------------------------------------------------------
# Order row formatter
# ---------------------------------------------------------------------------


def _format_order_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """Format an orders table row for MCP response.

    Args:
        r: Dict representing one row from the orders table.

    Returns:
        Clean dict with camelCase keys for the MCP response.
    """
    return {
        "id": r["id"],
        "clientOrderId": r["client_order_id"],
        "externalOrderId": r.get("external_order_id"),
        "adapter": r["adapter"],
        "exchange": r["exchange"],
        "symbol": r["symbol"],
        "direction": r["direction"],
        "orderType": r["order_type"],
        "quantity": float(r["quantity"]),
        "requestedPrice": _decimal_to_float(r.get("requested_price")),
        "status": r["status"],
        "filledQuantity": float(r.get("filled_quantity") or 0),
        "averageFillPrice": _decimal_to_float(r.get("average_fill_price")),
        "feesPaidUsd": float(r.get("fees_paid_usd") or 0),
        "reason": r.get("reason"),
        "submittedAt": _fmt_ts(r.get("submitted_at")),
        "updatedAt": _fmt_ts(r.get("updated_at")),
    }


# ---------------------------------------------------------------------------
# Sync handlers (called via asyncio.to_thread from async MCP handlers)
# ---------------------------------------------------------------------------


def _handle_banker_positions(p: Dict[str, Any]) -> Dict[str, Any]:
    """Read current positions from the positions_current view.

    Args:
        p: MCP tool params with 'companyId'.

    Returns:
        Dict with status, positions list, and count.
    """
    try:
        cid = str(p["companyId"])
        rows = db_helper.query(
            "SELECT * FROM positions_current WHERE company_id = %s ORDER BY symbol",
            (cid,),
        )
        positions = []
        for r in rows:
            positions.append({
                "symbol": r["symbol"],
                "direction": r["direction"],
                "quantity": float(r["quantity"]),
                "averageEntryPrice": _decimal_to_float(r.get("average_entry_price")),
                "notionalUsd": _decimal_to_float(r.get("notional_usd")),
                "unrealisedPnlUsd": _decimal_to_float(r.get("unrealised_pnl_usd")),
                "realizedPnlUsd": float(r.get("realized_pnl_usd") or 0),
                "leverage": int(r.get("leverage") or 1),
                "exchange": r["exchange"],
                "adapter": r["adapter"],
                "ts": _fmt_ts(r.get("ts")),
            })
        return {
            "status": "ok",
            "companyId": cid,
            "positions": positions,
            "count": len(positions),
        }
    except Exception as exc:
        logger.exception("banker.positions DB query failed")
        return {"status": "error", "message": f"failed to read positions: {exc}"}


def _handle_execution_status(p: Dict[str, Any]) -> Dict[str, Any]:
    """Read order status from the orders table.

    Args:
        p: MCP tool params with 'companyId' and 'orderId' or 'clientOrderId'.

    Returns:
        Dict with status and order details, or error/not_found.
    """
    try:
        cid = str(p["companyId"])
        oid = p.get("orderId")
        client_oid = p.get("clientOrderId")

        if oid is not None:
            rows = db_helper.query(
                "SELECT * FROM orders WHERE id = %s AND company_id = %s",
                (int(oid), cid),
            )
        elif client_oid is not None:
            rows = db_helper.query(
                "SELECT * FROM orders WHERE client_order_id = %s "
                "AND company_id = %s",
                (str(client_oid), cid),
            )
        else:
            return {"status": "error", "message": "orderId or clientOrderId required"}

        if not rows:
            return {"status": "not_found", "message": "order not found"}

        return {"status": "ok", "order": _format_order_row(rows[0])}
    except Exception as exc:
        logger.exception("execution.status DB query failed")
        return {"status": "error", "message": f"failed to read order: {exc}"}


def _handle_wallet_paper_create(p: Dict[str, Any]) -> Dict[str, Any]:
    """Create a paper wallet row and seed the banker_balances table.

    Args:
        p: MCP tool params with companyId, agentId, and optional
           startingUsd, venue, currency, contestId.

    Returns:
        Dict with status and wallet details.
    """
    try:
        cid = str(p["companyId"])
        aid = str(p["agentId"])
        venue = str(p.get("venue", _DEFAULT_VENUE))
        starting_usd = float(p.get("startingUsd", _DEFAULT_STARTING_USD))
        currency = str(p.get("currency", "USD"))
        contest_id = p.get("contestId")

        if starting_usd <= 0:
            return {"status": "error", "message": "startingUsd must be positive"}

        account_id = _paper_account_id(cid, aid, venue)

        # Upsert paper_wallets row (reset balance on re-create)
        db_helper.execute(
            "INSERT INTO public.paper_wallets "
            "(company_id, agent_id, exchange, account_id_external, "
            "starting_balance_usd, currency, contest_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (company_id, agent_id, exchange) DO UPDATE SET "
            "starting_balance_usd = EXCLUDED.starting_balance_usd, "
            "is_active = true, "
            "account_id_external = EXCLUDED.account_id_external",
            (cid, aid, venue, account_id, starting_usd, currency, contest_id),
        )

        # Seed banker_balances with the starting balance
        db_helper.execute(
            "INSERT INTO public.banker_balances "
            "(company_id, exchange, account_id_external, account_type, "
            "currency, balance, equity, margin_used, free_margin, source) "
            "VALUES (%s, %s, %s, 'paper', %s, %s, %s, 0, %s, 'paper_wallet')",
            (cid, venue, account_id, currency, starting_usd, starting_usd, starting_usd),
        )

        return {
            "status": "ok",
            "companyId": cid,
            "agentId": aid,
            "venue": venue,
            "accountIdExternal": account_id,
            "startingBalanceUsd": starting_usd,
            "currency": currency,
            "message": "Paper wallet created and banker balance seeded.",
        }
    except Exception as exc:
        logger.exception("wallet.paper_create failed")
        return {"status": "error", "message": f"failed to create wallet: {exc}"}


# ---------------------------------------------------------------------------
# Async handlers
# ---------------------------------------------------------------------------


async def _treasury_evaluate(p: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate a trade intent against capabilities and available capital.

    Calls shared.trading.treasury.Treasury.evaluate() with market data
    from the candles table and the company's capability/balance state.

    Args:
        p: MCP tool params with companyId, agentId, venue, symbol, side,
           intendedNotionalUsd, leverage, strategyRef.

    Returns:
        Dict with approval decision, reasons, and sizing details.
    """
    try:
        cid = str(p["companyId"])
        aid = str(p.get("agentId", ""))
        venue = str(p.get("venue", _DEFAULT_VENUE))
        symbol = str(p["symbol"])
        side = str(p["side"])
        notional_usd = p.get("intendedNotionalUsd")
        leverage = p.get("leverage")
        strategy_ref = p.get("strategyRef")

        direction = _side_to_direction(side)

        # Get market price (sync DB call via thread)
        market_data = await asyncio.to_thread(_get_latest_price, symbol, venue)
        if market_data is None:
            return {
                "status": "error",
                "message": f"no market data for {symbol}@{venue}",
            }

        # Construct TradeIntent
        from shared.trading.capabilities import TradeIntent

        intent = TradeIntent(
            company_id=cid,
            exchange=venue,
            symbol=symbol,
            direction=direction,
            strategy_id=strategy_ref,
            agent_id=aid,
            order_type="market",
            requested_notional_usd=(
                float(notional_usd) if notional_usd else None
            ),
            requested_leverage=int(leverage) if leverage else None,
        )

        # Construct MarketSnapshot + StrategyConfig
        from shared.trading.sizer import MarketSnapshot, StrategyConfig

        market = MarketSnapshot(
            price=market_data["price"],
            bid=market_data["bid"],
            ask=market_data["ask"],
            taker_fee_bps=_PAPER_TAKER_FEE_BPS,
        )
        strategy = StrategyConfig(
            name=strategy_ref or "mcp_default",
            risk_per_trade_pct=_DEFAULT_RISK_PER_TRADE_PCT,
        )

        # Evaluate via Treasury
        pool = await _get_pool()
        account_id = _paper_account_id(cid, aid, venue)

        from shared.trading.treasury import Treasury

        treasury = Treasury()
        decision = await treasury.evaluate(
            pool=pool,
            intent=intent,
            account_id_external=account_id,
            market=market,
            strategy=strategy,
        )

        return {
            "status": "ok",
            "approved": decision.approved,
            "skipped": decision.skipped,
            "reasons": decision.reasons,
            "availableCapitalUsd": decision.available_capital_usd,
            "sized": decision.sized.to_dict() if decision.sized else None,
            "capabilityCheck": (
                decision.capability_check.to_dict()
                if decision.capability_check
                else None
            ),
            "intentHash": decision.intent_hash(),
        }
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        logger.exception("treasury.evaluate failed")
        return {"status": "error", "message": f"treasury evaluation failed: {exc}"}


async def _execution_submit(p: Dict[str, Any]) -> Dict[str, Any]:
    """Submit an order (paper-only in M2, dryRun=true by default).

    Flow:
      - dryRun=true (default): simulate fill, no DB writes.
      - dryRun=false + I_am_paper=true: submit through paper adapter.
      - dryRun=false + I_am_paper absent: reject (live not unlocked).

    Args:
        p: MCP tool params with companyId, agentId, symbol, side, and
           optional quantity/notionalUsd, venue, orderType, etc.

    Returns:
        Dict with order details or simulated fill preview.
    """
    try:
        cid = str(p["companyId"])
        aid = str(p.get("agentId", ""))
        venue = str(p.get("venue", _DEFAULT_VENUE))
        symbol = str(p["symbol"])
        side = str(p["side"])
        order_type = str(p.get("orderType", "market"))
        quantity = p.get("quantity")
        notional_usd = p.get("notionalUsd")
        limit_price = p.get("limitPrice")
        tif = str(p.get("timeInForce", "gtc")).lower()
        dry_run = p.get("dryRun", True)
        i_am_paper = p.get("I_am_paper", False)
        strategy_ref = p.get("strategyRef")
        account_name = str(p.get("accountName", "main"))

        direction = _side_to_direction(side)

        # Get market price
        market_data = await asyncio.to_thread(_get_latest_price, symbol, venue)
        if market_data is None:
            return {
                "status": "error",
                "message": f"no market data for {symbol}@{venue}",
            }

        # Validate notionalUsd is positive if provided
        if notional_usd is not None and float(notional_usd) <= 0:
            return {"status": "error", "message": "notionalUsd must be positive"}

        # Calculate quantity from notional if not provided
        if quantity is None and notional_usd is not None:
            ref_price = (
                market_data["ask"] if direction == "long" else market_data["bid"]
            )
            if ref_price <= 0:
                return {
                    "status": "error",
                    "message": f"invalid market price ({ref_price}) for {symbol}",
                }
            quantity = float(notional_usd) / ref_price
        elif quantity is None:
            return {
                "status": "error",
                "message": "quantity or notionalUsd is required",
            }
        quantity = float(quantity)

        # Validate quantity is positive
        if quantity <= 0:
            return {"status": "error", "message": "quantity must be positive"}

        # Validate limitPrice for limit orders
        if order_type == "limit" and limit_price is not None and float(limit_price) <= 0:
            return {"status": "error", "message": "limitPrice must be positive"}

        # --- dryRun: simulate without submitting ---
        if dry_run:
            fill_price = (
                market_data["ask"] if direction == "long" else market_data["bid"]
            )
            fee = quantity * fill_price * _PAPER_TAKER_FEE_BPS / 10_000
            return {
                "status": "ok",
                "dryRun": True,
                "simulatedFill": {
                    "symbol": symbol,
                    "direction": direction,
                    "orderType": order_type,
                    "quantity": round(quantity, 8),
                    "fillPrice": fill_price,
                    "notionalUsd": round(quantity * fill_price, 2),
                    "feeUsd": round(fee, 4),
                    "venue": venue,
                },
                "message": "dryRun=true — no order was submitted.",
            }

        # --- Non-dry-run: must explicitly opt into paper trading ---
        if not i_am_paper:
            return {
                "status": "error",
                "message": (
                    "Live trading is not unlocked in M2. Set I_am_paper=true "
                    "to submit a paper trade, or dryRun=true to simulate."
                ),
            }

        # --- Submit through paper ExecutionRouter ---
        from shared.execution.protocol import ExecutionIntent, MarketTick

        account_id = _paper_account_id(cid, aid, venue)
        client_order_id = _generate_client_order_id(cid, aid, symbol, direction)
        intent = ExecutionIntent(
            company_id=cid,
            strategy_id=strategy_ref,
            agent_id=aid,
            exchange=venue,
            account_id_external=account_id,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            quantity=quantity,
            requested_price=float(limit_price) if limit_price else None,
            time_in_force=tif,
            client_order_id=client_order_id,
            metadata={
                "source": "mcp",
                "I_am_paper": True,
                "accountName": account_name,
            },
        )

        tick = MarketTick(
            symbol=symbol,
            bid=market_data["bid"],
            ask=market_data["ask"],
            last=market_data["price"],
        )

        router = await _get_router()
        snapshot = await router.submit(intent, adapter="paper", market=tick)

        return {
            "status": "ok",
            "dryRun": False,
            "order": {
                "id": snapshot.id,
                "clientOrderId": snapshot.client_order_id,
                "adapter": snapshot.adapter,
                "exchange": snapshot.exchange,
                "symbol": snapshot.symbol,
                "direction": snapshot.direction,
                "orderType": snapshot.order_type,
                "quantity": float(snapshot.quantity),
                "status": snapshot.status,
                "filledQuantity": float(snapshot.filled_quantity),
                "averageFillPrice": _decimal_to_float(
                    snapshot.average_fill_price
                ),
                "feesPaidUsd": float(snapshot.fees_paid_usd),
                "reason": snapshot.reason,
            },
        }
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    except Exception as exc:
        logger.exception("execution.submit failed")
        return {"status": "error", "message": f"order submission failed: {exc}"}


async def _execution_cancel(p: Dict[str, Any]) -> Dict[str, Any]:
    """Cancel an open order.

    Looks up the order by orderId or clientOrderId, verifies ownership
    and non-terminal status, then cancels via the paper ExecutionRouter.

    Args:
        p: MCP tool params with companyId and orderId or clientOrderId.

    Returns:
        Dict with cancellation result.
    """
    try:
        cid = str(p["companyId"])
        oid = p.get("orderId")
        client_oid = p.get("clientOrderId")
        account_name = str(p.get("accountName", "main"))

        # Look up the order to get client_order_id + adapter
        if oid is not None:
            rows = await asyncio.to_thread(
                db_helper.query,
                "SELECT * FROM orders WHERE id = %s AND company_id = %s",
                (int(oid), cid),
            )
        elif client_oid is not None:
            rows = await asyncio.to_thread(
                db_helper.query,
                "SELECT * FROM orders WHERE client_order_id = %s "
                "AND company_id = %s",
                (str(client_oid), cid),
            )
        else:
            return {
                "status": "error",
                "message": "orderId or clientOrderId required",
            }

        if not rows:
            return {"status": "not_found", "message": "order not found"}

        order_row = rows[0]
        if order_row["status"] in _TERMINAL_STATUSES:
            return {
                "status": "ok",
                "message": (
                    f"order already in terminal state: {order_row['status']}"
                ),
                "orderStatus": order_row["status"],
            }

        # Cancel through the router; fall back to direct DB update
        # if the paper adapter lost in-memory state (daemon restart).
        # Only paper-adapter orders are eligible for the DB fallback.
        adapter_name = order_row["adapter"]
        try:
            router = await _get_router()
            snapshot = await router.cancel(
                order_row["client_order_id"],
                adapter=adapter_name,
                account_name=account_name,
            )
            return {
                "status": "ok",
                "order": {
                    "id": snapshot.id,
                    "clientOrderId": snapshot.client_order_id,
                    "status": snapshot.status,
                    "reason": snapshot.reason,
                },
            }
        except ValueError as exc:
            exc_lower = str(exc).lower()
            is_missing = "no order" in exc_lower or "not found" in exc_lower
            if not is_missing:
                raise
            if adapter_name != "paper":
                return {
                    "status": "error",
                    "message": (
                        f"cannot cancel {adapter_name} order via DB fallback; "
                        "only paper orders support this path"
                    ),
                }
            logger.warning(
                "paper adapter lost in-memory state for order %s, "
                "falling back to direct DB cancel",
                order_row["client_order_id"],
            )
        # Direct DB fallback: mark order as canceled (paper-only)
        _CANCEL_REASON = "canceled via MCP (paper adapter lost state)"
        order_id = int(order_row["id"])
        await asyncio.to_thread(
            db_helper.execute,
            "UPDATE public.orders SET status = 'canceled', "
            "reason = %s, updated_at = NOW() WHERE id = %s",
            (_CANCEL_REASON, order_id),
        )
        await asyncio.to_thread(
            db_helper.execute,
            "INSERT INTO public.order_events "
            "(order_id, event_type, severity, message, ts) "
            "VALUES (%s, 'cancel', 'info', %s, NOW())",
            (order_id, _CANCEL_REASON),
        )
        return {
            "status": "ok",
            "order": {
                "id": order_id,
                "clientOrderId": order_row["client_order_id"],
                "status": "canceled",
                "reason": _CANCEL_REASON,
            },
        }
    except Exception as exc:
        logger.exception("execution.cancel failed")
        return {"status": "error", "message": f"order cancellation failed: {exc}"}


# ---------------------------------------------------------------------------
# Venue helpers (module-level for testability)
# ---------------------------------------------------------------------------


def _is_capital_venue(venue: str) -> bool:
    """Return True if the venue refers to Capital.com.

    Accepts both 'capital' and 'capitalcom' (the adapter's name property).
    """
    return venue.lower() in ("capital", "capitalcom")


async def _build_adapter(venue: str, account_name: str) -> Any:
    """Create and authenticate an adapter for the given venue/account.

    Returns a ready-to-use adapter that the caller must ``close()``.
    """
    from shared.utils.credentials import Credentials

    # Normalise: Credentials.get() expects "capital" as the exchange key.
    creds_key = "capital" if _is_capital_venue(venue) else venue
    creds = Credentials.get(creds_key, account_name)
    is_paper = Credentials.is_paper(creds_key, account_name)

    if _is_capital_venue(venue):
        from shared.connectors.capital_adapter import CapitalAdapter
        adapter = CapitalAdapter(environment=creds.get("environment", "demo"))
        await adapter.authenticate(
            creds["email"],
            creds["password"],
            creds["apiKey"],
            account_id=creds.get("accountId"),
        )
    else:
        from shared.connectors.ccxt_adapter import CCXTAdapter
        adapter = CCXTAdapter(
            exchange_id=venue,
            use_sandbox=is_paper,
            config=creds,
            account_name=account_name,
        )
    return adapter


# ---------------------------------------------------------------------------
# Tool definitions + registration
# ---------------------------------------------------------------------------


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    """Build all trading MCP tools bound to the given context.

    Args:
        ctx: Shared dependency container for Paperclip access.

    Returns:
        List of (McpTool, handler) tuples for registration.
    """

    # --- banker.snapshot (wired to Paperclip) ---
    t_banker_snapshot = McpTool(
        name="banker.snapshot",
        description=(
            "Per-company P&L snapshot (realised, unrealised, deposits, spend). "
            "Reads from Paperclip finance + costs endpoints."
        ),
        version="2",
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
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    async def _banker_snapshot(p: Dict[str, Any]) -> Dict[str, Any]:
        """Read P&L snapshot from Paperclip finance endpoints."""
        try:
            cid = str(p["companyId"])
            date_range = {"from": p.get("from"), "to": p.get("to")}
            cost_summary = ctx.paperclip(
                "GET",
                f"/api/companies/{cid}/costs/summary",
                query=date_range,
            )
            finance_summary = ctx.paperclip(
                "GET",
                f"/api/companies/{cid}/costs/finance-summary",
                query=date_range,
            )
            by_agent = ctx.paperclip(
                "GET",
                f"/api/companies/{cid}/costs/by-agent",
                query=date_range,
            )
            return {
                "companyId": cid,
                "window": date_range,
                "cost": cost_summary,
                "finance": finance_summary,
                "byAgent": by_agent,
            }
        except Exception as exc:
            logger.exception("banker.snapshot failed")
            return {
                "status": "error",
                "message": f"Paperclip finance query failed: {exc}",
            }

    # --- banker.positions ---
    t_banker_positions = McpTool(
        name="banker.positions",
        description=(
            "List current open positions for a company. Reads from the "
            "positions_current view which tracks the latest position "
            "snapshot per symbol."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
            },
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    async def _banker_positions(p: Dict[str, Any]) -> Dict[str, Any]:
        """Read current positions via sync DB helper in thread."""
        return await asyncio.to_thread(_handle_banker_positions, p)

    # --- treasury.evaluate ---
    t_treasury_evaluate = McpTool(
        name="treasury.evaluate",
        description=(
            "Ask the Treasury whether a proposed trade is allowed. Checks "
            "capabilities (max notional, leverage, allowed venues/symbols) "
            "and available capital from the banker. Returns approval "
            "decision with sizing details. Every execution.submit should "
            "call this first (Rule 1 enforcement)."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "venue": {
                    "type": "string",
                    "default": "bybit",
                    "description": "Exchange name (lowercase, e.g. 'bybit')",
                },
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "intendedNotionalUsd": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Desired trade size in USD",
                },
                "leverage": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                },
                "strategyRef": {"type": "string"},
            },
            "required": [
                "companyId",
                "agentId",
                "venue",
                "symbol",
                "side",
                "intendedNotionalUsd",
            ],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    # --- execution.submit ---
    t_execution_submit = McpTool(
        name="execution.submit",
        description=(
            "Submit an order via the paper execution adapter. dryRun=true "
            "(default) simulates the fill without submitting. To actually "
            "submit a paper trade, set dryRun=false AND I_am_paper=true. "
            "Live trading is not unlocked in M2."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "venue": {
                    "type": "string",
                    "default": "bybit",
                },
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "orderType": {
                    "type": "string",
                    "enum": ["market", "limit"],
                    "default": "market",
                },
                "quantity": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Order quantity in base currency",
                },
                "notionalUsd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": (
                        "Alternative to quantity: specify USD amount. "
                        "Quantity is calculated from market price."
                    ),
                },
                "limitPrice": {"type": "number"},
                "timeInForce": {
                    "type": "string",
                    "enum": ["gtc", "ioc", "fok"],
                    "default": "gtc",
                },
                "dryRun": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true, simulate without submitting",
                },
                "I_am_paper": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required=true when dryRun=false to confirm "
                        "paper trading intent"
                    ),
                },
                "strategyRef": {"type": "string"},
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["companyId", "agentId", "symbol", "side"],
        },
        read_only=False,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    # --- execution.cancel ---
    t_execution_cancel = McpTool(
        name="execution.cancel",
        description=(
            "Cancel an open order by orderId or clientOrderId. Only "
            "non-terminal orders can be cancelled."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "orderId": {
                    "type": "string",
                    "description": "Numeric order ID from the orders table",
                },
                "clientOrderId": {
                    "type": "string",
                    "description": "Client order ID (idempotency key)",
                },
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["companyId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    # --- execution.status ---
    t_execution_status = McpTool(
        name="execution.status",
        description=(
            "Get the current status of an order by orderId or clientOrderId."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "orderId": {
                    "type": "string",
                    "description": "Numeric order ID from the orders table",
                },
                "clientOrderId": {
                    "type": "string",
                    "description": "Client order ID (idempotency key)",
                },
            },
            "required": ["companyId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    # --- wallet.paper_create ---
    t_wallet_paper_create = McpTool(
        name="wallet.paper_create",
        description=(
            "Create a paper trading wallet for an agent. Seeds the banker "
            "with the starting balance so Treasury can evaluate trades. "
            "Re-creating a wallet resets the starting balance."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "startingUsd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 10000,
                    "description": "Starting paper balance in USD",
                },
                "venue": {
                    "type": "string",
                    "default": "bybit",
                    "description": "Exchange name (lowercase)",
                },
                "currency": {
                    "type": "string",
                    "default": "USD",
                },
                "contestId": {
                    "type": "string",
                    "description": "Optional contest ID for competition wallets",
                },
            },
            "required": ["companyId", "agentId"],
        },
        read_only=False,
        tags={"phase": "2", "group": "trading", "status": "live"},
    )

    async def _execution_status_async(p: Dict[str, Any]) -> Dict[str, Any]:
        """Read order status via sync DB helper in thread."""
        return await asyncio.to_thread(_handle_execution_status, p)

    async def _wallet_paper_create(p: Dict[str, Any]) -> Dict[str, Any]:
        """Create paper wallet via sync DB helper in thread."""
        return await asyncio.to_thread(_handle_wallet_paper_create, p)

    t_trade_validate = McpTool(
        name="trading_trade_validate",
        description="Validate a closed live/paper trade against its shadow (forward-test) counterpart to attribute drift.",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {
                    "type": "string",
                    "description": "Company ID (e.g. 'jarvais')",
                },
                "tradeId": {
                    "type": "integer",
                    "description": "The ID of the closed trade in Postgres.",
                },
            },
            "required": ["companyId", "tradeId"],
        },
        read_only=False,
        tags={"phase": "6", "group": "trading", "status": "live"},
    )

    async def _trade_validate(p: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a trade via ValidationEngine."""
        from shared.trading.validation import ValidationEngine

        try:
            cid = str(p["companyId"])
            tid = int(p["tradeId"])
            engine = ValidationEngine(cid)
            try:
                result = await engine.validate_trade(tid)
            finally:
                engine.ch.close()
            return result
        except (ValueError, TypeError) as e:
            return {"status": "error", "message": f"Invalid parameter: {e}"}
        except Exception as e:
            logger.error("Trade validation failed: %s", e)
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # Strategy Review — aggregate validation → verdict
    # ------------------------------------------------------------------
    t_strategy_review = McpTool(
        name="trading_strategy_review",
        description=(
            "Review a strategy's aggregate validation results. Compares "
            "backtest profitability with live/shadow performance, computes "
            "a CONTINUE / CAUTION / STOP verdict, and provides actionable "
            "recommendations. Use this before scaling up or abandoning a strategy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {
                    "type": "string",
                    "description": "Company ID (e.g. 'jarvais')",
                },
                "strategyId": {
                    "type": "integer",
                    "description": "The strategy ID to review.",
                },
                "minTrades": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 5,
                    "description": "Minimum validated trades before issuing a confident verdict.",
                },
            },
            "required": ["companyId", "strategyId"],
        },
        read_only=True,
        tags={"phase": "6", "group": "trading", "status": "live"},
    )

    async def _strategy_review(p: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate validation results for a strategy and produce a verdict."""
        from shared.trading.validation import ValidationEngine

        try:
            cid = str(p["companyId"])
            sid = int(p["strategyId"])
            min_trades = max(1, int(p.get("minTrades", 5)))
            engine = ValidationEngine(cid)
            try:
                result = await engine.review_strategy(sid, min_trades=min_trades)
            finally:
                engine.ch.close()
            return result
        except (ValueError, TypeError) as e:
            return {"status": "error", "message": f"Invalid parameter: {e}"}
        except Exception as e:
            logger.error("Strategy review failed: %s", e)
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # Strategy Autopsy — deep-dive trade comparisons + learning
    # ------------------------------------------------------------------
    t_strategy_autopsy = McpTool(
        name="trading_strategy_autopsy",
        description=(
            "Deep-dive autopsy of a strategy's live vs shadow trades. "
            "Shows per-trade comparison, detects drift patterns (slippage "
            "bias, fee drift, data corrections), and generates actionable "
            "learnings the agent can use to improve or fine-tune the strategy."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {
                    "type": "string",
                    "description": "Company ID (e.g. 'jarvais')",
                },
                "strategyId": {
                    "type": "integer",
                    "description": "The strategy ID to autopsy.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Maximum number of trade comparisons to return.",
                },
            },
            "required": ["companyId", "strategyId"],
        },
        read_only=True,
        tags={"phase": "6", "group": "trading", "status": "live"},
    )

    async def _strategy_autopsy(p: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-dive autopsy of a strategy's live vs shadow trades."""
        from shared.trading.validation import ValidationEngine

        try:
            cid = str(p["companyId"])
            sid = int(p["strategyId"])
            limit = max(1, min(100, int(p.get("limit", 20))))
            engine = ValidationEngine(cid)
            try:
                result = await engine.autopsy_strategy(sid, limit=limit)
            finally:
                engine.ch.close()
            return result
        except (ValueError, TypeError) as e:
            return {"status": "error", "message": f"Invalid parameter: {e}"}
        except Exception as e:
            logger.error("Strategy autopsy failed: %s", e)
            return {"status": "error", "message": str(e)}

    # --- market.ticker ---
    t_market_ticker = McpTool(
        name="market_ticker",
        description="Fetch current ticker (bid/ask/last/spread) for a symbol from a live exchange.",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string", "description": "Exchange name (e.g. 'bybit', 'capitalcom')"},
                "symbol": {"type": "string", "description": "Trading symbol or epic"},
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["venue", "symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "market", "status": "live"},
    )

    async def _market_ticker(p: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch live ticker via exchange adapter."""
        try:
            venue = str(p["venue"])
            symbol = str(p["symbol"])
            account_name = str(p.get("accountName", "main"))

            adapter = await _build_adapter(venue, account_name)
            try:
                ticker = await adapter.fetch_ticker(symbol)
                return {"status": "ok", "ticker": ticker}
            finally:
                await adapter.close()
        except Exception as e:
            logger.error("market_ticker failed: %s", e)
            return {"status": "error", "message": str(e)}

    # --- market.funding ---
    t_market_funding = McpTool(
        name="market_funding",
        description="Fetch current funding rate or overnight holding costs for a symbol.",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "symbol": {"type": "string"},
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["venue", "symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "market", "status": "live"},
    )

    async def _market_funding(p: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch live funding rate via exchange adapter."""
        try:
            venue = str(p["venue"])
            symbol = str(p["symbol"])
            account_name = str(p.get("accountName", "main"))

            adapter = await _build_adapter(venue, account_name)
            try:
                funding = await adapter.fetch_funding_rate(symbol)
                return {"status": "ok", "funding": funding}
            finally:
                await adapter.close()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- market.hours ---
    t_market_hours = McpTool(
        name="market_hours",
        description="Get detailed market open/close schedule and current status for a symbol.",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "symbol": {"type": "string"},
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["venue", "symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "market", "status": "live"},
    )

    async def _market_hours(p: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch market hours via exchange adapter."""
        try:
            venue = str(p["venue"])
            symbol = str(p["symbol"])
            account_name = str(p.get("accountName", "main"))

            adapter = await _build_adapter(venue, account_name)
            try:
                hours = await adapter.get_market_hours(symbol)
                return {"status": "ok", "marketHours": hours}
            finally:
                await adapter.close()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- account.history ---
    t_account_history = McpTool(
        name="account_history",
        description="Fetch account history (open orders, positions, recent trades) from a live exchange.",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "symbol": {"type": "string", "description": "Optional symbol filter"},
                "accountName": {"type": "string", "default": "main"},
            },
            "required": ["venue"],
        },
        read_only=True,
        tags={"phase": "2", "group": "account", "status": "live"},
    )

    async def _account_history(p: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch account history via exchange adapter."""
        try:
            venue = str(p["venue"])
            symbol = p.get("symbol")
            account_name = str(p.get("accountName", "main"))

            adapter = await _build_adapter(venue, account_name)
            try:
                open_orders = await adapter.fetch_open_orders(symbol)
                positions = await adapter.fetch_positions(symbol)
                trades = await adapter.fetch_trades(symbol)
                balance = await adapter.fetch_balance()
                return {
                    "status": "ok",
                    "venue": venue,
                    "accountName": account_name,
                    "balance": balance,
                    "openOrders": open_orders,
                    "positions": positions,
                    "trades": trades,
                }
            finally:
                await adapter.close()
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # --- market.subscribe ---
    t_market_subscribe = McpTool(
        name="market_subscribe",
        description=(
            "Subscribe to live market data (tick, trade, l1, mark, funding, candle). "
            "The gateway handles deduplication and fan-out via Redis. "
            "Data will be published to 'md.<venue>.<symbol>.<channel>'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string", "description": "Exchange name (e.g. 'binance', 'capital')"},
                "symbol": {"type": "string", "description": "Trading symbol or epic"},
                "channels": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["tick", "trade", "l1", "mark", "funding", "candle"]
                    },
                    "minItems": 1
                },
                "agentId": {"type": "string", "description": "ID of the agent requesting the data"}
            },
            "required": ["venue", "symbol", "channels", "agentId"],
        },
        tags={"phase": "17", "group": "market", "status": "live"},
    )

    async def _market_subscribe(p: Dict[str, Any]) -> Dict[str, Any]:
        """Subscribe to market data via the Gateway (reconcile-only)."""
        try:
            from shared.gateway.redis_bus import RedisBus
            from shared.gateway.schema import SubscriptionKey, TickChannel, channel_for
            import os

            redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
            bus = RedisBus(redis_url)
            await bus.connect()
            try:
                venue = p["venue"].lower()
                symbol = p["symbol"]
                agent_id = p["agentId"]
                channels = [TickChannel(c) for c in p["channels"]]
                
                results = []
                for channel in channels:
                    key = SubscriptionKey(exchange=venue, symbol=symbol, channel=channel)
                    await bus.add_desired_sub(key, agent_id)
                    results.append({
                        "channel": channel.value,
                        "redisChannel": channel_for(venue, symbol, channel),
                        "status": "requested"
                    })
                
                return {
                    "status": "ok",
                    "message": "Subscriptions requested. The Gateway daemon will reconcile shortly.",
                    "subscriptions": results
                }
            finally:
                await bus.close()
        except Exception as e:
            logger.error("market_subscribe failed: %s", e)
            return {"status": "error", "message": str(e)}

    # --- market.unsubscribe ---
    t_market_unsubscribe = McpTool(
        name="market_unsubscribe",
        description="Unsubscribe from live market data.",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "symbol": {"type": "string"},
                "channels": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "agentId": {"type": "string"}
            },
            "required": ["venue", "symbol", "channels", "agentId"],
        },
        tags={"phase": "17", "group": "market", "status": "live"},
    )

    async def _market_unsubscribe(p: Dict[str, Any]) -> Dict[str, Any]:
        """Unsubscribe from market data via the Gateway (reconcile-only)."""
        try:
            from shared.gateway.redis_bus import RedisBus
            from shared.gateway.schema import SubscriptionKey, TickChannel
            import os

            redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
            bus = RedisBus(redis_url)
            await bus.connect()
            try:
                venue = p["venue"].lower()
                symbol = p["symbol"]
                channels = [TickChannel(c) for c in p["channels"]]
                
                for channel in channels:
                    key = SubscriptionKey(exchange=venue, symbol=symbol, channel=channel)
                    await bus.remove_desired_sub(key)
                
                return {
                    "status": "ok",
                    "message": "Unsubscribe requested. The Gateway daemon will reconcile shortly."
                }
            finally:
                await bus.close()
        except Exception as e:
            logger.error("market_unsubscribe failed: %s", e)
            return {"status": "error", "message": str(e)}

    return [
        (t_banker_snapshot, _banker_snapshot),
        (t_banker_positions, _banker_positions),
        (t_treasury_evaluate, _treasury_evaluate),
        (t_execution_submit, _execution_submit),
        (t_execution_cancel, _execution_cancel),
        (t_execution_status, _execution_status_async),
        (t_wallet_paper_create, _wallet_paper_create),
        (t_trade_validate, _trade_validate),
        (t_strategy_review, _strategy_review),
        (t_strategy_autopsy, _strategy_autopsy),
        (t_market_ticker, _market_ticker),
        (t_market_funding, _market_funding),
        (t_market_hours, _market_hours),
        (t_account_history, _account_history),
        (t_market_subscribe, _market_subscribe),
        (t_market_unsubscribe, _market_unsubscribe),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all trading tools with the MCP registry.

    Args:
        registry: The tool registry to register tools with.
        ctx: Shared dependency container for the MCP tools.
    """
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
