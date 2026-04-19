"""
shared.execution.paper — deterministic in-memory execution adapter.

Phase 26. The paper adapter is where every approved ExecutionIntent
can be routed safely:

* Market orders fill at `ask` (long) or `bid` (short) with configurable
  slippage (default 0 bps so backtest ≡ paper-live parity stays honest).
* Limit orders either fill immediately if the market has already
  crossed the limit, or remain `accepted` until the caller calls
  `touch(...)` with a new MarketTick.
* Stop orders go into `new` and activate when the stop price is
  breached, then behave as market or limit orders.
* Fees are `taker_fee_bps` (default 4.0) for market / stop / IOC fills
  and `maker_fee_bps` (default 0.0) for resting limit fills.
* Cancellations return an `OrderUpdate(status=canceled)` iff the order
  is still active.
* All operations are deterministic given a fixed MarketTick sequence,
  which is essential for the Rule-1 auditor (Phase 21).

Everything is thread-safe-enough for tests (we use an `asyncio.Lock`)
but the paper adapter is designed to be used from a single event loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from shared.execution.protocol import (
    ACTIVE_STATUSES,
    DIRECTION_LONG,
    EVENT_ACCEPTED,
    EVENT_CANCEL,
    EVENT_FILL,
    EVENT_REJECT,
    EVENT_SEVERITY_ERROR,
    EVENT_SEVERITY_INFO,
    EVENT_SUBMITTED,
    ExecutionIntent,
    MarketTick,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    ORDER_TYPE_STOP_LIMIT,
    OrderUpdate,
    STATUS_ACCEPTED,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_REJECTED,
)


@dataclass
class _PaperOrder:
    intent: ExecutionIntent
    client_order_id: str
    status: str = STATUS_NEW
    filled_quantity: float = 0.0
    pending_updates: List[OrderUpdate] = field(default_factory=list)


class PaperExecutionAdapter:
    """In-memory deterministic execution adapter."""

    name: str = "paper"

    def __init__(
        self,
        *,
        taker_fee_bps: float = 4.0,
        maker_fee_bps: float = 0.0,
        slippage_bps: float = 0.0,
        default_market: Optional[MarketTick] = None,
    ) -> None:
        self.taker_fee_bps = float(taker_fee_bps)
        self.maker_fee_bps = float(maker_fee_bps)
        self.slippage_bps = float(slippage_bps)
        self._default_market = default_market
        self._orders: Dict[str, _PaperOrder] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_market(self, intent: ExecutionIntent, market: Optional[MarketTick]) -> Optional[MarketTick]:
        return market or self._default_market or self._synth_market(intent)

    @staticmethod
    def _synth_market(intent: ExecutionIntent) -> Optional[MarketTick]:
        rp = intent.requested_price
        if rp is None or rp <= 0:
            return None
        return MarketTick(symbol=intent.symbol, bid=rp, ask=rp, last=rp)

    def _apply_slippage(self, price: float, direction: str) -> float:
        adj = price * (self.slippage_bps / 10_000.0)
        return price + adj if direction == DIRECTION_LONG else price - adj

    def _fill_price(self, tick: MarketTick, intent: ExecutionIntent, order_type: str) -> float:
        if order_type in (ORDER_TYPE_LIMIT, ORDER_TYPE_STOP_LIMIT):
            assert intent.requested_price is not None
            return float(intent.requested_price)
        base = tick.ask if intent.direction == DIRECTION_LONG else tick.bid
        if base <= 0:
            base = tick.last or tick.mid
        return self._apply_slippage(base, intent.direction)

    @staticmethod
    def _limit_crossed(tick: MarketTick, intent: ExecutionIntent) -> bool:
        if intent.requested_price is None:
            return False
        if intent.direction == DIRECTION_LONG:
            return tick.ask > 0 and tick.ask <= intent.requested_price
        return tick.bid > 0 and tick.bid >= intent.requested_price

    @staticmethod
    def _stop_triggered(tick: MarketTick, intent: ExecutionIntent) -> bool:
        if intent.stop_price is None:
            return False
        ref = tick.last or tick.mid
        if intent.direction == DIRECTION_LONG:
            return ref >= intent.stop_price
        return ref <= intent.stop_price

    # ------------------------------------------------------------------
    # Public protocol methods
    # ------------------------------------------------------------------

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        market: Optional[MarketTick] = None,
    ) -> Sequence[OrderUpdate]:
        client_id = intent.ensure_client_order_id()
        async with self._lock:
            if client_id in self._orders:
                # Idempotent: return what we've emitted so far.
                return list(self._orders[client_id].pending_updates)

            order = _PaperOrder(intent=intent, client_order_id=client_id)
            self._orders[client_id] = order

            updates: List[OrderUpdate] = []

            if intent.quantity <= 0:
                updates.append(OrderUpdate(
                    client_order_id=client_id,
                    status=STATUS_REJECTED,
                    event_type=EVENT_REJECT,
                    severity=EVENT_SEVERITY_ERROR,
                    message="quantity must be > 0",
                ))
                order.status = STATUS_REJECTED
                order.pending_updates.extend(updates)
                return updates

            updates.append(OrderUpdate(
                client_order_id=client_id,
                status=STATUS_NEW,
                event_type=EVENT_SUBMITTED,
                severity=EVENT_SEVERITY_INFO,
                message="paper: submitted",
            ))

            tick = self._effective_market(intent, market)
            if tick is None and intent.order_type == ORDER_TYPE_MARKET:
                rej = OrderUpdate(
                    client_order_id=client_id,
                    status=STATUS_REJECTED,
                    event_type=EVENT_REJECT,
                    severity=EVENT_SEVERITY_ERROR,
                    message="paper: no market tick; cannot fill market order",
                )
                order.status = STATUS_REJECTED
                updates.append(rej)
                order.pending_updates.extend(updates)
                return updates

            accepted = OrderUpdate(
                client_order_id=client_id,
                status=STATUS_ACCEPTED,
                event_type=EVENT_ACCEPTED,
                severity=EVENT_SEVERITY_INFO,
                message="paper: accepted",
            )
            updates.append(accepted)
            order.status = STATUS_ACCEPTED

            fill = self._maybe_fill(order, tick)
            if fill is not None:
                updates.append(fill)

            order.pending_updates.extend(updates)
            return updates

    async def cancel(
        self,
        client_order_id: str,
        *,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> OrderUpdate:
        async with self._lock:
            order = self._orders.get(client_order_id)
            if order is None:
                return OrderUpdate(
                    client_order_id=client_order_id,
                    status=STATUS_REJECTED,
                    event_type=EVENT_REJECT,
                    severity=EVENT_SEVERITY_ERROR,
                    message="paper: unknown client_order_id",
                )
            if order.status not in ACTIVE_STATUSES:
                return OrderUpdate(
                    client_order_id=client_order_id,
                    status=order.status,
                    event_type=EVENT_CANCEL,
                    severity=EVENT_SEVERITY_INFO,
                    message=f"paper: already in terminal state {order.status}",
                )
            order.status = STATUS_CANCELED
            cancel = OrderUpdate(
                client_order_id=client_order_id,
                status=STATUS_CANCELED,
                event_type=EVENT_CANCEL,
                severity=EVENT_SEVERITY_INFO,
                message="paper: canceled",
            )
            order.pending_updates.append(cancel)
            return cancel

    async def poll_updates(
        self,
        client_order_ids: Sequence[str],
    ) -> List[OrderUpdate]:
        async with self._lock:
            out: List[OrderUpdate] = []
            for cid in client_order_ids:
                order = self._orders.get(cid)
                if order is None:
                    continue
                out.extend(order.pending_updates)
            return out

    # ------------------------------------------------------------------
    # Simulation helpers (used by forward-test drivers and tests)
    # ------------------------------------------------------------------

    async def touch(self, tick: MarketTick) -> List[OrderUpdate]:
        """Re-evaluate resting orders against a new MarketTick."""
        async with self._lock:
            out: List[OrderUpdate] = []
            for order in self._orders.values():
                if order.status not in ACTIVE_STATUSES:
                    continue
                if order.intent.symbol != tick.symbol:
                    continue
                fill = self._maybe_fill(order, tick)
                if fill is not None:
                    out.append(fill)
                    order.pending_updates.append(fill)
            return out

    # ------------------------------------------------------------------
    # Fill engine
    # ------------------------------------------------------------------

    def _maybe_fill(self, order: _PaperOrder, tick: Optional[MarketTick]) -> Optional[OrderUpdate]:
        intent = order.intent
        if tick is None:
            return None

        order_type = intent.order_type

        if order_type == ORDER_TYPE_MARKET:
            return self._emit_fill(order, tick, maker=False, price=self._fill_price(tick, intent, order_type))

        if order_type == ORDER_TYPE_LIMIT:
            if self._limit_crossed(tick, intent):
                return self._emit_fill(order, tick, maker=True, price=float(intent.requested_price or 0.0))
            return None

        if order_type == ORDER_TYPE_STOP:
            if self._stop_triggered(tick, intent):
                return self._emit_fill(order, tick, maker=False, price=self._fill_price(tick, intent, ORDER_TYPE_MARKET))
            return None

        if order_type == ORDER_TYPE_STOP_LIMIT:
            if self._stop_triggered(tick, intent) and self._limit_crossed(tick, intent):
                return self._emit_fill(order, tick, maker=True, price=float(intent.requested_price or 0.0))
            return None

        return None

    def _emit_fill(
        self,
        order: _PaperOrder,
        tick: MarketTick,
        *,
        maker: bool,
        price: float,
    ) -> OrderUpdate:
        qty = float(order.intent.quantity) - float(order.filled_quantity)
        if qty <= 0:
            return OrderUpdate(
                client_order_id=order.client_order_id,
                status=order.status,
                event_type=EVENT_FILL,
                severity=EVENT_SEVERITY_INFO,
                message="paper: already fully filled",
            )
        fee_bps = self.maker_fee_bps if maker else self.taker_fee_bps
        notional = qty * price
        fee_usd = notional * (fee_bps / 10_000.0)
        order.filled_quantity += qty
        order.status = STATUS_FILLED
        ts = datetime.now(timezone.utc)
        return OrderUpdate(
            client_order_id=order.client_order_id,
            status=STATUS_FILLED,
            event_type=EVENT_FILL,
            severity=EVENT_SEVERITY_INFO,
            message=f"paper: filled @ {price:.8f}",
            filled_quantity=order.filled_quantity,
            remaining_quantity=0.0,
            last_fill_price=price,
            last_fill_quantity=qty,
            last_fill_fee_usd=fee_usd,
            last_fill_is_maker=maker,
            ts=ts,
            payload={"tick_ts": tick.ts.isoformat(), "notional_usd": notional},
        )


__all__ = ["PaperExecutionAdapter"]
