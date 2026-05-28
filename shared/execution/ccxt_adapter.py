"""
shared.execution.ccxt_adapter — unified async CCXT execution adapter.

Supports Bybit, Bitget, BloFin with exchange-native SL/TP, leverage,
position management. Researched and tested against live demo APIs (2026-05-28).

Exchange-specific behaviors:
  Bybit v5:
    - create_order SL/TP: uses stopLoss/takeProfit string params
    - set_sl_tp: POST /v5/position/trading-stop — must NOT include slOrderType
      when only setting SL, and must NOT include tpOrderType when only setting TP
    - Position must be in one-way mode (auto-detected, switched on first use)
    - Max leverage per symbol — adapter catches and clamps
  Bitget v2:
    - create_order SL/TP: uses stopLossPrice/takeProfitPrice string params
    - set_sl_tp: POST /api/mix/v2/order/place-tpsl-order
    - PAPTRADING header for demo
  BloFin:
    - create_order SL/TP: uses stopLossPrice/takeProfitPrice string params  
    - set_sl_tp: place reduce-only limit/stop orders
    - set_sandbox_mode for demo
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

try:
    import ccxt  # type: ignore
    _CCXT_AVAILABLE = True
except ImportError:
    ccxt = None  # type: ignore
    _CCXT_AVAILABLE = False

from shared.execution.protocol import (
    DIRECTION_LONG, DIRECTION_SHORT,
    EVENT_ACCEPTED, EVENT_CANCEL, EVENT_FILL, EVENT_PARTIAL_FILL,
    EVENT_REJECT, EVENT_SEVERITY_ERROR, EVENT_SEVERITY_INFO,
    EVENT_SUBMITTED, EVENT_UPDATE,
    ExecutionIntent, MarketTick,
    ORDER_TYPE_LIMIT, ORDER_TYPE_MARKET,
    OrderUpdate,
    STATUS_ACCEPTED, STATUS_CANCELED, STATUS_FILLED,
    STATUS_NEW, STATUS_PARTIAL, STATUS_REJECTED,
)

LOG = logging.getLogger("tickles.execution.ccxt")

# Per-symbol max leverage (exchange defaults, overridden by live query)
_DEFAULT_MAX_LEVERAGE = {
    "bybit": {"BTC": 100, "ETH": 100, "default": 75},
    "bitget": {"BTC": 125, "ETH": 100, "default": 75},
    "blofin": {"BTC": 100, "ETH": 100, "default": 50},
}


class CcxtExecutionAdapter:
    """Live execution via ccxt. Unified across Bybit/Bitget/BloFin."""

    name: str = "ccxt"

    def __init__(
        self,
        *,
        client_factory: Optional[Any] = None,
        sandbox: bool = True,
        demo_trading: bool = False,
    ) -> None:
        self._factory = client_factory
        self._sandbox = sandbox
        self._demo_trading = demo_trading
        self._clients: Dict[str, Any] = {}
        self._known_orders: Dict[str, Dict[str, Any]] = {}
        self._leverage_cache: Dict[str, int] = {}
        self._position_mode_set: set = set()  # (exchange, account) pairs in one-way mode

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def _get_client(self, exchange: str, account_name: str = "main") -> Any:
        if not _CCXT_AVAILABLE:
            raise RuntimeError("ccxt is not installed")

        client_key = f"{exchange}:{account_name}"
        if client_key in self._clients:
            return self._clients[client_key]

        if self._factory is not None:
            client = self._factory(exchange, account_name)
        else:
            if ccxt is None:
                raise RuntimeError("ccxt module not available")
            cls = getattr(ccxt, exchange, None)
            if cls is None:
                raise RuntimeError(f"ccxt has no exchange named {exchange!r}")

            from shared.utils.credentials import Credentials
            creds = Credentials.get(exchange, account_name)
            if not creds.get("apiKey") or not creds.get("secret"):
                creds = self._load_creds_from_db(exchange, account_name)

            config = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
            config.update(creds)
            client = cls(config)

            self._configure_exchange_mode(client, exchange)

        self._clients[client_key] = client
        return client

    def _load_creds_from_db(self, exchange: str, account_name: str) -> Dict[str, str]:
        try:
            from shared.mcp.tools.db_helper import query as db_query
            rows = db_query(
                "SELECT api_key, api_secret, api_passphrase FROM public.exchange_accounts "
                "WHERE exchange = %s AND account_name = %s AND is_active = TRUE LIMIT 1",
                (exchange, account_name),
            )
            if rows:
                r = rows[0]
                creds = {"apiKey": r["api_key"], "secret": r["api_secret"]}
                if r.get("api_passphrase"):
                    creds["password"] = r["api_passphrase"]
                LOG.info("ccxt: loaded credentials from DB for %s/%s", exchange, account_name)
                return creds
        except Exception:
            pass
        return {}

    def _configure_exchange_mode(self, client: Any, exchange: str):
        """Apply exchange-specific configuration for demo/sandbox environments."""
        if exchange == "bybit":
            if self._demo_trading and hasattr(client, "enable_demo_trading"):
                client.enable_demo_trading(True)
            elif self._sandbox and hasattr(client, "set_sandbox_mode"):
                client.set_sandbox_mode(True)
        elif exchange == "bitget":
            if self._demo_trading or self._sandbox:
                client.headers.update({"PAPTRADING": "1"})
        elif self._sandbox and hasattr(client, "set_sandbox_mode"):
            try:
                client.set_sandbox_mode(True)
            except Exception:
                pass

    async def _ensure_one_way_mode(self, client: Any, exchange: str, account_name: str, symbol: str):
        """Ensure the account is in one-way position mode for this symbol."""
        mode_key = f"{exchange}:{account_name}:{symbol}"
        if mode_key in self._position_mode_set:
            return
        if exchange != "bybit":
            self._position_mode_set.add(mode_key)
            return

        try:
            clean = symbol.replace("/", "").split(":")[0]
            await asyncio.to_thread(
                lambda: client.private_post_v5_position_switch_mode({
                    "category": "linear", "symbol": clean, "mode": 0,
                })
            )
            self._position_mode_set.add(mode_key)
        except Exception:
            pass  # Already in one-way mode or not supported

    # ------------------------------------------------------------------
    # Submit order
    # ------------------------------------------------------------------

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        market: Optional[MarketTick] = None,
    ) -> Sequence[OrderUpdate]:
        client_id = intent.ensure_client_order_id()
        side = "buy" if intent.direction == DIRECTION_LONG else "sell"
        sl = intent.stop_loss
        tp = intent.take_profit
        lev = intent.leverage
        sym = intent.symbol

        updates: List[OrderUpdate] = [OrderUpdate(
            client_order_id=client_id, status=STATUS_NEW,
            event_type=EVENT_SUBMITTED, severity=EVENT_SEVERITY_INFO,
            message=f"ccxt: {side} {sym} qty={intent.quantity}",
        )]

        # Validate
        if intent.quantity <= 0:
            return self._reject(updates, client_id, "quantity must be positive")
        if sl is not None and sl <= 0:
            return self._reject(updates, client_id, "stop_loss must be positive")
        if tp is not None and tp <= 0:
            return self._reject(updates, client_id, "take_profit must be positive")
        if sl is not None and tp is not None:
            if intent.direction == DIRECTION_LONG and sl >= tp:
                return self._reject(updates, client_id, "SL must be below TP for longs")
            if intent.direction != DIRECTION_LONG and sl <= tp:
                return self._reject(updates, client_id, "SL must be above TP for shorts")

        try:
            account_name = (intent.metadata or {}).get("accountName", "main")
            client = self._get_client(intent.exchange, account_name)
        except Exception as exc:
            return self._reject(updates, client_id, str(exc))

        ex = intent.exchange.lower()

        # Ensure one-way mode for Bybit
        await self._ensure_one_way_mode(client, ex, account_name, sym)

        # ── Leverage ──
        if lev and 1 <= lev <= 125:
            cache_key = f"{ex}:{sym}"
            if self._leverage_cache.get(cache_key) != lev:
                try:
                    def _set():
                        params = {"productType": "USDT-FUTURES"} if ex == "bitget" else {}
                        client.set_leverage(int(lev), sym, params=params)
                    await asyncio.to_thread(_set)
                    self._leverage_cache[cache_key] = int(lev)
                except Exception as exc:
                    # Phase 1 (2026-05-29): bumped debug→warning. When this
                    # silently fails the order falls back to the account's
                    # current leverage (often 1x), which needs the FULL notional
                    # as margin and gets rejected with "ab not enough". Surfacing
                    # it makes that failure mode diagnosable instead of invisible.
                    LOG.warning("ccxt: set_leverage(%sx %s) failed, order will use "
                                "account default leverage: %s", lev, sym, exc)

        # ── SL/TP params ──
        params: Dict[str, Any] = {}
        if sl or tp:
            if ex == "bybit":
                # Bybit: string params, no extra wrapper
                if sl:
                    params["stopLoss"] = str(sl)
                if tp:
                    params["takeProfit"] = str(tp)
            elif ex == "bitget":
                # Bitget limit orders with SL/TP: place the order clean first,
                # then attach stops via TPSL endpoint after acceptance.
                # Don't pass stopLossPrice/takeProfitPrice in create_order params
                # for limit orders — Bitget demo rejects them on limit orders.
                if intent.order_type == ORDER_TYPE_LIMIT:
                    pass  # SL/TP attached after order via TPSL
                else:
                    if sl:
                        params["stopLossPrice"] = str(sl)
                    if tp:
                        params["takeProfitPrice"] = str(tp)
            elif ex == "blofin":
                if sl:
                    params["stopLossPrice"] = str(sl)
                if tp:
                    params["takeProfitPrice"] = str(tp)
            else:
                if sl:
                    params["stopLoss"] = {"price": sl}
                if tp:
                    params["takeProfit"] = {"price": tp}

        # ── Place order ──
        def _place() -> Dict[str, Any]:
            order_type = ORDER_TYPE_MARKET if intent.order_type == ORDER_TYPE_MARKET else ORDER_TYPE_LIMIT
            kwargs = {"symbol": sym, "type": order_type, "side": side, "amount": float(intent.quantity)}
            if intent.requested_price:
                kwargs["price"] = float(intent.requested_price)
            if params:
                kwargs["params"] = params
            return client.create_order(**kwargs)

        try:
            raw = await asyncio.to_thread(_place)
        except Exception as exc:
            return self._reject(updates, client_id, str(exc))

        self._known_orders[client_id] = {
            "exchange": intent.exchange, "symbol": sym,
            "account_name": account_name, "raw": raw, "sl": sl, "tp": tp,
        }
        updates.append(self._raw_to_update(client_id, raw,
            default_status=STATUS_ACCEPTED, default_event=EVENT_ACCEPTED))
        
        # Bitget: SL/TP attached after order only for market orders (position exists immediately).
        # For limit orders, skip — position doesn't exist until fill, TPSL would fail.
        if ex == "bitget" and intent.order_type != ORDER_TYPE_LIMIT and (sl or tp):
            try:
                await self._bitget_set_sl_tp(client, sym, sl, tp, intent.direction)
                LOG.info("ccxt: Bitget SL/TP attached: %s SL=%s TP=%s", sym, sl, tp)
            except Exception as exc:
                LOG.warning("ccxt: Bitget SL/TP attach failed (non-fatal): %s", exc)
        
        return updates

    @staticmethod
    def _reject(updates: List, client_id: str, msg: str) -> List[OrderUpdate]:
        updates.append(OrderUpdate(client_order_id=client_id, status=STATUS_REJECTED,
            event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
            message=f"ccxt: {msg}"))
        return updates

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def set_sl_tp(
        self,
        symbol: str,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        exchange: str = "bybit",
        account_name: str = "main",
        direction: str = DIRECTION_LONG,
    ) -> OrderUpdate:
        """Modify SL/TP on an existing open position."""
        client = self._get_client(exchange, account_name)
        ex = exchange.lower()

        def _reject(msg): return OrderUpdate(client_order_id="", status=STATUS_REJECTED,
            event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR, message=f"ccxt set_sl_tp: {msg}")

        if stop_loss is None and take_profit is None:
            return _reject("no SL or TP provided")

        try:
            if ex == "bybit":
                await self._bybit_set_sl_tp(client, symbol, stop_loss, take_profit)
            elif ex == "bitget":
                await self._bitget_set_sl_tp(client, symbol, stop_loss, take_profit, direction)
            else:
                await self._generic_set_sl_tp(client, symbol, stop_loss, take_profit, direction)
        except Exception as exc:
            LOG.warning("ccxt set_sl_tp failed: %s", exc)
            err_str = str(exc)
            # Bybit 34040 = "not modified" — SL/TP already at target, treat as success
            if "34040" in err_str or "not modified" in err_str.lower():
                return OrderUpdate(client_order_id="", status=STATUS_ACCEPTED,
                    event_type=EVENT_UPDATE, severity=EVENT_SEVERITY_INFO,
                    message=f"ccxt: SL/TP unchanged on {symbol} (already at target)")
            return _reject(err_str)

        return OrderUpdate(client_order_id="", status=STATUS_ACCEPTED,
            event_type=EVENT_UPDATE, severity=EVENT_SEVERITY_INFO,
            message=f"ccxt: SL/TP updated on {symbol}")

    async def _bybit_set_sl_tp(self, client, symbol, sl, tp):
        clean = symbol.replace("/", "").split(":")[0]
        body = {"category": "linear", "symbol": clean, "positionIdx": 0}

        if sl is not None and tp is not None:
            # SL+TP together: both triggerBy and orderType required
            body.update({
                "tpslMode": "Full",
                "stopLoss": str(sl), "takeProfit": str(tp),
                "slTriggerBy": "LastPrice", "tpTriggerBy": "LastPrice",
                "slOrderType": "Market", "tpOrderType": "Market",
            })
        elif sl is not None:
            # SL only: do NOT include slOrderType (Bybit rejects it when tpSlMode is empty)
            body.update({
                "stopLoss": str(sl),
                "slTriggerBy": "LastPrice",
            })
        elif tp is not None:
            # TP only: do NOT include tpOrderType (same reason)
            body.update({
                "takeProfit": str(tp),
                "tpTriggerBy": "LastPrice",
            })

        await asyncio.to_thread(lambda: client.private_post_v5_position_trading_stop(body))

    async def _bitget_set_sl_tp(self, client, symbol, sl, tp, direction: str = DIRECTION_LONG):
        clean = symbol.replace("/", "").split(":")[0]
        hold = "long" if direction == DIRECTION_LONG else "short"
        base_body = {
            "symbol": clean, "marginCoin": "USDT",
            "productType": "USDT-FUTURES", "holdSide": hold,
        }
        if sl is not None:
            # SL: trigger below entry for long, above entry for short
            exec_price = round(sl * 0.99, 4) if direction == DIRECTION_LONG else round(sl * 1.01, 4)
            await asyncio.to_thread(lambda: client.private_mix_post_v2_mix_order_place_tpsl_order({
                **base_body, "planType": "loss_plan",
                "triggerPrice": str(sl),
                "executePrice": str(exec_price),
            }))
        if tp is not None:
            # TP: trigger above entry for long, below entry for short
            exec_price = round(tp * 0.99, 4) if direction == DIRECTION_LONG else round(tp * 1.01, 4)
            await asyncio.to_thread(lambda: client.private_mix_post_v2_mix_order_place_tpsl_order({
                **base_body, "planType": "profit_plan",
                "triggerPrice": str(tp),
                "executePrice": str(exec_price),
            }))

    async def _generic_set_sl_tp(self, client, symbol, sl, tp, direction: str = DIRECTION_LONG):
        is_long = direction == DIRECTION_LONG
        close_side = "sell" if is_long else "buy"
        if sl is not None:
            await asyncio.to_thread(lambda: client.create_order(
                symbol=symbol, type="stop_market", side=close_side, amount=1.0,
                params={"stopPrice": sl, "reduceOnly": True}))
        if tp is not None:
            await asyncio.to_thread(lambda: client.create_order(
                symbol=symbol, type="limit", side=close_side, amount=1.0,
                price=tp, params={"reduceOnly": True}))

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    async def adjust_leverage(
        self, symbol: str, leverage: int, *,
        exchange: str = "bybit", account_name: str = "main",
    ) -> OrderUpdate:
        def _reject(msg): return OrderUpdate(client_order_id="", status=STATUS_REJECTED,
            event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR, message=f"ccxt: {msg}")

        if leverage < 1 or leverage > 125:
            return _reject(f"leverage must be 1-125, got {leverage}")

        client = self._get_client(exchange, account_name)
        cache_key = f"{exchange}:{symbol}"
        if self._leverage_cache.get(cache_key) == leverage:
            return OrderUpdate(client_order_id="", status=STATUS_ACCEPTED,
                event_type=EVENT_UPDATE, severity=EVENT_SEVERITY_INFO,
                message=f"leverage already {leverage}x")

        try:
            def _set():
                params = {"productType": "USDT-FUTURES"} if exchange == "bitget" else {}
                client.set_leverage(leverage, symbol, params=params)
            await asyncio.to_thread(_set)
            self._leverage_cache[cache_key] = leverage
            return OrderUpdate(client_order_id="", status=STATUS_ACCEPTED,
                event_type=EVENT_UPDATE, severity=EVENT_SEVERITY_INFO,
                message=f"leverage set to {leverage}x")
        except Exception as exc:
            return _reject(str(exc))

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch_positions(
        self, *, exchange: str = "bybit", account_name: str = "main",
        symbols: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        client = self._get_client(exchange, account_name)
        try:
            raw = await asyncio.to_thread(client.fetch_positions, symbols)
            return [{
                "symbol": p.get("symbol"), "side": p.get("side"),
                "contracts": float(p.get("contracts", 0)),
                "entryPrice": float(p["entryPrice"]) if p.get("entryPrice") is not None else None,
                "unrealizedPnl": float(p.get("unrealizedPnl", 0)),
                "leverage": int(p.get("leverage", 0) or 0),
                "notional": float(p.get("notional", 0) or 0),
                "id": p.get("id"),
            } for p in (raw or []) if float(p.get("contracts", 0)) > 0]
        except Exception as exc:
            LOG.warning("fetch_positions failed: %s", exc)
            return []

    async def fetch_balance(
        self, *, exchange: str = "bybit", account_name: str = "main",
    ) -> Dict[str, float]:
        client = self._get_client(exchange, account_name)
        try:
            bal = await asyncio.to_thread(client.fetch_balance)
            return {k: float(v or 0) for k, v in bal.get("total", {}).items() if float(v or 0) > 0}
        except Exception as exc:
            LOG.warning("fetch_balance failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Cancel / Poll
    # ------------------------------------------------------------------

    async def cancel(
        self, client_order_id: str, *,
        exchange: Optional[str] = None, symbol: Optional[str] = None,
        account_name: str = "main",
    ) -> OrderUpdate:
        info = self._known_orders.get(client_order_id)
        ex = exchange or (info["exchange"] if info else None)
        sym = symbol or (info["symbol"] if info else None)
        if not ex or not sym:
            return OrderUpdate(client_order_id=client_order_id, status=STATUS_REJECTED,
                event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
                message="cancel requires exchange + symbol")
        try:
            stored = (info or {}).get("account_name", account_name)
            client = self._get_client(ex, stored)
            ext_id = (info or {}).get("raw", {}).get("id")
            if not ext_id:
                return OrderUpdate(client_order_id=client_order_id, status=STATUS_REJECTED,
                    event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
                    message="no exchange order ID to cancel")
            raw = await asyncio.to_thread(client.cancel_order, ext_id, sym)
        except Exception as exc:
            return OrderUpdate(client_order_id=client_order_id, status=STATUS_REJECTED,
                event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
                message=f"cancel: {exc}")
        return self._raw_to_update(client_order_id, raw,
            default_status=STATUS_CANCELED, default_event=EVENT_CANCEL)

    async def poll_updates(
        self, client_order_ids: Sequence[str], account_name: str = "main",
    ) -> List[OrderUpdate]:
        out = []
        for cid in client_order_ids:
            info = self._known_orders.get(cid)
            if info is None:
                continue
            try:
                stored_account = info.get("account_name", account_name)
                client = self._get_client(info["exchange"], stored_account)
                ext_id = info.get("raw", {}).get("id")
                if not ext_id:
                    out.append(OrderUpdate(client_order_id=cid, status=STATUS_REJECTED,
                        event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
                        message="poll: no exchange order ID"))
                    continue
                raw = await asyncio.to_thread(client.fetch_order, ext_id, info["symbol"])
            except Exception as exc:
                out.append(OrderUpdate(client_order_id=cid, status=STATUS_REJECTED,
                    event_type=EVENT_REJECT, severity=EVENT_SEVERITY_ERROR,
                    message=f"poll: {exc}"))
                continue
            info["raw"] = raw
            out.append(self._raw_to_update(cid, raw))
        return out

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_status(raw: Optional[str]) -> str:
        m = {"open": STATUS_ACCEPTED, "closed": STATUS_FILLED,
             "canceled": STATUS_CANCELED, "cancelled": STATUS_CANCELED,
             "expired": STATUS_CANCELED, "rejected": STATUS_REJECTED,
             "partially_filled": STATUS_PARTIAL, "partial": STATUS_PARTIAL}
        return m.get((raw or "").lower(), STATUS_ACCEPTED)

    @staticmethod
    def _raw_to_update(
        client_id: str, raw: Dict[str, Any], *,
        default_status: str = STATUS_ACCEPTED,
        default_event: str = EVENT_UPDATE,
    ) -> OrderUpdate:
        status = CcxtExecutionAdapter._translate_status(raw.get("status")) or default_status
        filled = float(raw.get("filled") or 0.0)
        avg = raw.get("average") or raw.get("price")
        fee_info = raw.get("fee") or {}
        fee_usd = float(fee_info.get("cost") or 0.0) if isinstance(fee_info, dict) else 0.0
        event = default_event
        if status == STATUS_PARTIAL: event = EVENT_PARTIAL_FILL
        elif status == STATUS_FILLED: event = EVENT_FILL
        elif status == STATUS_CANCELED: event = EVENT_CANCEL
        elif status == STATUS_REJECTED: event = EVENT_REJECT
        ts_ms = raw.get("timestamp")
        try:
            ts_val = float(ts_ms) if ts_ms else 0
            ts = datetime.fromtimestamp(ts_val / 1000.0, tz=timezone.utc) if ts_val > 0 else datetime.now(timezone.utc)
        except (TypeError, ValueError, OSError):
            ts = datetime.now(timezone.utc)
        return OrderUpdate(
            client_order_id=client_id, status=status, event_type=event,
            severity=EVENT_SEVERITY_INFO if status != STATUS_REJECTED else EVENT_SEVERITY_ERROR,
            message=raw.get("info", {}).get("msg") if isinstance(raw.get("info"), dict) else None,
            external_order_id=str(raw.get("id")) if raw.get("id") else None,
            filled_quantity=filled,
            remaining_quantity=float(raw["remaining"]) if raw.get("remaining") is not None else None,
            last_fill_price=float(avg) if avg is not None else None,
            last_fill_quantity=float(raw.get("lastTradeAmount") or raw.get("lastFillAmount") or 0.0) or None,
            last_fill_fee_usd=fee_usd or None,
            last_fill_external_id=str(raw.get("lastTradeId")) if raw.get("lastTradeId") else None,
            ts=ts,
            payload={"raw_status": raw.get("status"), "symbol": raw.get("symbol")},
        )


__all__ = ["CcxtExecutionAdapter"]
