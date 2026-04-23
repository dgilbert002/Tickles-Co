"""
shared.execution.ccxt_adapter — thin async adapter over CCXT.

Phase 26. Every exchange we care about already has a ccxt client
(binance, bybit, okx, coinbase, kraken, binanceus). This adapter keeps
the live path deliberately thin:

1. Build a ccxt client per (exchange, account) pair.
2. Translate an ExecutionIntent into a ccxt `create_order(...)` call.
3. Translate the ccxt response into an OrderUpdate stream.
4. Provide `poll_updates(...)` that asks ccxt for the latest order
   state + any new trades (fills).

Live credentials are loaded via `shared.utils.credentials` (already
in use elsewhere) so this module never sees API keys directly.

If `ccxt` is not installed we still return the class — the Router will
short-circuit with a clean error, which is much friendlier than
import-time failure.
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
    DIRECTION_LONG,
    EVENT_ACCEPTED,
    EVENT_CANCEL,
    EVENT_FILL,
    EVENT_PARTIAL_FILL,
    EVENT_REJECT,
    EVENT_SEVERITY_ERROR,
    EVENT_SEVERITY_INFO,
    EVENT_SUBMITTED,
    EVENT_UPDATE,
    ExecutionIntent,
    MarketTick,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    OrderUpdate,
    STATUS_ACCEPTED,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_PARTIAL,
    STATUS_REJECTED,
)

LOG = logging.getLogger("tickles.execution.ccxt")


class CcxtExecutionAdapter:
    """Live execution via ccxt. Intentionally thin."""

    name: str = "ccxt"

    def __init__(
        self,
        *,
        client_factory: Optional[Any] = None,
        sandbox: bool = True,
    ) -> None:
        self._factory = client_factory
        self._sandbox = sandbox
        self._clients: Dict[str, Any] = {} # Keyed by (exchange, account_name)
        self._known_orders: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------

    def _get_client(self, exchange: str, account_name: str = "main") -> Any:
        if not _CCXT_AVAILABLE:
            raise RuntimeError("ccxt is not installed; cannot use live adapter")
        
        client_key = f"{exchange}:{account_name}"
        cached = self._clients.get(client_key)
        if cached is not None:
            return cached
            
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
            is_paper = Credentials.is_paper(exchange, account_name) or self._sandbox
            
            # Base config, enable rate limiting, use SWAP markets by default
            exchange_config = {
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
            exchange_config.update(creds)

            client = cls(exchange_config)
            if is_paper and hasattr(client, "set_sandbox_mode"):
                client.set_sandbox_mode(True)
                
        self._clients[client_key] = client
        return client

    # ------------------------------------------------------------------
    # ExecutionAdapter protocol
    # ------------------------------------------------------------------

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        market: Optional[MarketTick] = None,
    ) -> Sequence[OrderUpdate]:
        client_id = intent.ensure_client_order_id()
        side = "buy" if intent.direction == DIRECTION_LONG else "sell"
        params: Dict[str, Any] = {"clientOrderId": client_id}
        if intent.metadata:
            params.update({f"meta_{k}": v for k, v in intent.metadata.items()})

        updates: List[OrderUpdate] = [OrderUpdate(
            client_order_id=client_id,
            status=STATUS_NEW,
            event_type=EVENT_SUBMITTED,
            severity=EVENT_SEVERITY_INFO,
            message=f"ccxt: submitting {side} {intent.symbol} qty={intent.quantity}",
        )]

        try:
            account_name = (intent.metadata or {}).get("accountName", "main")
            client = self._get_client(intent.exchange, account_name)
        except Exception as exc:
            updates.append(OrderUpdate(
                client_order_id=client_id,
                status=STATUS_REJECTED,
                event_type=EVENT_REJECT,
                severity=EVENT_SEVERITY_ERROR,
                message=f"ccxt: {exc}",
            ))
            return updates

        def _place() -> Dict[str, Any]:
            order_type = ORDER_TYPE_MARKET if intent.order_type == ORDER_TYPE_MARKET else ORDER_TYPE_LIMIT
            return client.create_order(
                symbol=intent.symbol,
                type=order_type,
                side=side,
                amount=float(intent.quantity),
                price=float(intent.requested_price) if intent.requested_price else None,
                params=params,
            )

        try:
            raw = await asyncio.to_thread(_place)
        except Exception as exc:
            LOG.warning("ccxt submit failed: %s", exc)
            updates.append(OrderUpdate(
                client_order_id=client_id,
                status=STATUS_REJECTED,
                event_type=EVENT_REJECT,
                severity=EVENT_SEVERITY_ERROR,
                message=f"ccxt: {exc}",
            ))
            return updates

        self._known_orders[client_id] = {
            "exchange": intent.exchange,
            "symbol": intent.symbol,
            "account_name": (intent.metadata or {}).get("accountName", "main"),
            "raw": raw,
        }

        updates.append(self._raw_to_update(client_id, raw, default_status=STATUS_ACCEPTED, default_event=EVENT_ACCEPTED))
        return updates

    async def cancel(
        self,
        client_order_id: str,
        *,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        account_name: str = "main",
    ) -> OrderUpdate:
        info = self._known_orders.get(client_order_id)
        ex = exchange or (info["exchange"] if info else None)
        sym = symbol or (info["symbol"] if info else None)
        if not ex or not sym:
            return OrderUpdate(
                client_order_id=client_order_id,
                status=STATUS_REJECTED,
                event_type=EVENT_REJECT,
                severity=EVENT_SEVERITY_ERROR,
                message="ccxt: cancel requires exchange + symbol",
            )
        try:
            # Prefer the account_name stored at submit time.
            stored_account = (info or {}).get("account_name", account_name)
            client = self._get_client(ex, stored_account)
            external_id = (info or {}).get("raw", {}).get("id")
            raw = await asyncio.to_thread(
                client.cancel_order, external_id or client_order_id, sym
            )
        except Exception as exc:
            return OrderUpdate(
                client_order_id=client_order_id,
                status=STATUS_REJECTED,
                event_type=EVENT_REJECT,
                severity=EVENT_SEVERITY_ERROR,
                message=f"ccxt: cancel failed: {exc}",
            )
        return self._raw_to_update(
            client_order_id, raw, default_status=STATUS_CANCELED, default_event=EVENT_CANCEL
        )

    async def poll_updates(
        self,
        client_order_ids: Sequence[str],
        account_name: str = "main",
    ) -> List[OrderUpdate]:
        out: List[OrderUpdate] = []
        for cid in client_order_ids:
            info = self._known_orders.get(cid)
            if info is None:
                continue
            try:
                # Prefer the account_name stored at submit time so that
                # polling always targets the correct exchange account.
                stored_account = info.get("account_name", account_name)
                client = self._get_client(info["exchange"], stored_account)
                external_id = info.get("raw", {}).get("id")
                raw = await asyncio.to_thread(client.fetch_order, external_id or cid, info["symbol"])
            except Exception as exc:
                out.append(OrderUpdate(
                    client_order_id=cid,
                    status=STATUS_REJECTED,
                    event_type=EVENT_REJECT,
                    severity=EVENT_SEVERITY_ERROR,
                    message=f"ccxt: poll failed: {exc}",
                ))
                continue
            info["raw"] = raw
            out.append(self._raw_to_update(cid, raw))
        return out

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    @staticmethod
    def _translate_status(raw_status: Optional[str]) -> str:
        mapping = {
            "open": STATUS_ACCEPTED,
            "closed": STATUS_FILLED,
            "canceled": STATUS_CANCELED,
            "cancelled": STATUS_CANCELED,
            "expired": STATUS_CANCELED,
            "rejected": STATUS_REJECTED,
            "partially_filled": STATUS_PARTIAL,
            "partial": STATUS_PARTIAL,
        }
        if not raw_status:
            return STATUS_ACCEPTED
        return mapping.get(raw_status.lower(), STATUS_ACCEPTED)

    @staticmethod
    def _raw_to_update(
        client_id: str,
        raw: Dict[str, Any],
        *,
        default_status: str = STATUS_ACCEPTED,
        default_event: str = EVENT_UPDATE,
    ) -> OrderUpdate:
        status = CcxtExecutionAdapter._translate_status(raw.get("status")) or default_status
        filled = float(raw.get("filled") or 0.0)
        remaining = raw.get("remaining")
        avg = raw.get("average") or raw.get("price")
        fee_info = raw.get("fee") or {}
        fee_usd = float(fee_info.get("cost") or 0.0)
        event = default_event
        if status == STATUS_PARTIAL:
            event = EVENT_PARTIAL_FILL
        elif status == STATUS_FILLED:
            event = EVENT_FILL
        elif status == STATUS_CANCELED:
            event = EVENT_CANCEL
        elif status == STATUS_REJECTED:
            event = EVENT_REJECT
        ts_ms = raw.get("timestamp")
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc) if ts_ms else datetime.now(timezone.utc)
        return OrderUpdate(
            client_order_id=client_id,
            status=status,
            event_type=event,
            severity=EVENT_SEVERITY_INFO if status != STATUS_REJECTED else EVENT_SEVERITY_ERROR,
            message=raw.get("info", {}).get("msg") if isinstance(raw.get("info"), dict) else None,
            external_order_id=str(raw.get("id")) if raw.get("id") else None,
            filled_quantity=filled,
            remaining_quantity=float(remaining) if remaining is not None else None,
            last_fill_price=float(avg) if avg is not None else None,
            last_fill_quantity=float(raw.get("lastTradeAmount") or raw.get("lastFillAmount") or 0.0) or None,
            last_fill_fee_usd=fee_usd or None,
            last_fill_external_id=str(raw.get("lastTradeId")) if raw.get("lastTradeId") else None,
            ts=ts,
            payload={"raw_status": raw.get("status"), "symbol": raw.get("symbol")},
        )


__all__ = ["CcxtExecutionAdapter"]
