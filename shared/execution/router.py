"""
shared.execution.router — routes ExecutionIntents through adapters.

Phase 26. The Router is the single entry point for the rest of the
platform to place / cancel orders. It:

1. Accepts an ExecutionIntent (already sized and approved by Treasury).
2. Chooses an ExecutionAdapter by name (paper / ccxt / nautilus).
3. Persists the order row and every OrderUpdate event / fill / position
   snapshot via ExecutionStore.
4. Returns an OrderSnapshot that reflects the order's current state.

Design invariants:
* All persistence happens here, not in adapters.
* The Router is idempotent per (adapter, client_order_id) — calling
  `submit(...)` twice on the same intent returns the existing snapshot
  rather than creating a duplicate row.
* We never silently swallow adapter rejections; every rejection lands
  as an `order_events` row with `severity='error'`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.execution.protocol import (
    ACTIVE_STATUSES,
    ADAPTER_CCXT,
    ADAPTER_NAUTILUS,
    ADAPTER_PAPER,
    DIRECTION_LONG,
    EVENT_CANCEL,
    ExecutionAdapter,
    ExecutionIntent,
    MarketTick,
    OrderSnapshot,
    OrderUpdate,
    STATUS_ACCEPTED,
    STATUS_CANCELED,
    STATUS_NEW,
    STATUS_REJECTED,
)
from shared.execution.store import ExecutionStore, PositionSnapshotRow

LOG = logging.getLogger("tickles.execution.router")


class ExecutionRouter:
    """Route intents through adapters and persist every event."""

    def __init__(
        self,
        pool: Any,
        *,
        adapters: Dict[str, ExecutionAdapter],
        default_adapter: str = ADAPTER_PAPER,
    ) -> None:
        if default_adapter not in adapters:
            raise ValueError(
                f"default adapter {default_adapter!r} not in adapters {sorted(adapters)!r}"
            )
        self._pool = pool
        self._store = ExecutionStore(pool)
        self._adapters = dict(adapters)
        self._default = default_adapter

    # ------------------------------------------------------------------

    def adapter_names(self) -> List[str]:
        return sorted(self._adapters.keys())

    def get_adapter(self, name: Optional[str]) -> ExecutionAdapter:
        resolved = name or self._default
        if resolved not in self._adapters:
            raise ValueError(
                f"unknown adapter {resolved!r}; available: {sorted(self._adapters)}"
            )
        return self._adapters[resolved]

    # ------------------------------------------------------------------
    # submit
    # ------------------------------------------------------------------

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        adapter: Optional[str] = None,
        market: Optional[MarketTick] = None,
    ) -> OrderSnapshot:
        adapter_obj = self.get_adapter(adapter)
        adapter_name = adapter_obj.name

        client_order_id = intent.ensure_client_order_id()

        existing = await self._store.get_order_by_client_id(adapter_name, client_order_id)
        if existing is not None:
            return existing

        order_id = await self._store.insert_order(
            intent,
            adapter=adapter_name,
            client_order_id=client_order_id,
            status=STATUS_NEW,
        )
        LOG.info(
            "router.submit start adapter=%s company=%s symbol=%s qty=%s id=%d",
            adapter_name, intent.company_id, intent.symbol, intent.quantity, order_id,
        )

        updates = list(await adapter_obj.submit(intent, market=market))
        await self._apply_updates(order_id, intent, adapter_name, updates)

        snapshot = await self._store.get_order(order_id)
        assert snapshot is not None
        return snapshot

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    async def cancel(
        self,
        client_order_id: str,
        *,
        adapter: Optional[str] = None,
    ) -> OrderSnapshot:
        adapter_obj = self.get_adapter(adapter)
        adapter_name = adapter_obj.name

        existing = await self._store.get_order_by_client_id(adapter_name, client_order_id)
        if existing is None:
            raise ValueError(
                f"cancel: no order for adapter={adapter_name} client_order_id={client_order_id}"
            )
        if existing.is_terminal:
            return existing

        update = await adapter_obj.cancel(
            client_order_id,
            exchange=existing.exchange,
            symbol=existing.symbol,
        )
        assert existing.id is not None
        await self._store.insert_event(existing.id, update)
        new_status = update.status or STATUS_CANCELED
        await self._store.update_order(
            existing.id,
            status=new_status,
            filled_quantity=existing.filled_quantity,
            average_fill_price=existing.average_fill_price,
            last_fee_usd=0.0,
            external_order_id=update.external_order_id,
            reason=update.message,
        )
        out = await self._store.get_order(existing.id)
        assert out is not None
        return out

    # ------------------------------------------------------------------
    # poll
    # ------------------------------------------------------------------

    async def poll(
        self,
        *,
        adapter: Optional[str] = None,
        company_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[OrderSnapshot]:
        adapter_obj = self.get_adapter(adapter)
        adapter_name = adapter_obj.name

        open_orders = (
            await self._store.list_open_orders(company_id, limit=limit)
            if company_id
            else []
        )
        open_orders = [o for o in open_orders if o.adapter == adapter_name]
        if not open_orders:
            return []

        client_ids = [o.client_order_id for o in open_orders]
        updates = await adapter_obj.poll_updates(client_ids)

        by_cid: Dict[str, List[OrderUpdate]] = {}
        for u in updates:
            by_cid.setdefault(u.client_order_id, []).append(u)

        for order in open_orders:
            order_updates = by_cid.get(order.client_order_id, [])
            if not order_updates:
                continue
            assert order.id is not None
            intent_shim = _order_to_intent(order)
            await self._apply_updates(order.id, intent_shim, adapter_name, order_updates)

        refreshed: List[OrderSnapshot] = []
        for order in open_orders:
            assert order.id is not None
            snap = await self._store.get_order(order.id)
            if snap is not None:
                refreshed.append(snap)
        return refreshed

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _apply_updates(
        self,
        order_id: int,
        intent: ExecutionIntent,
        adapter_name: str,
        updates: List[OrderUpdate],
    ) -> None:
        filled_qty = 0.0
        avg_price: Optional[float] = None
        avg_acc_notional = 0.0
        last_status = STATUS_NEW
        external_id: Optional[str] = None
        last_message: Optional[str] = None

        for update in updates:
            await self._store.insert_event(order_id, update)

            if update.status:
                last_status = update.status
            if update.external_order_id:
                external_id = update.external_order_id
            if update.message:
                last_message = update.message

            fee = float(update.last_fill_fee_usd or 0.0)

            if update.is_fill and update.last_fill_price and update.last_fill_quantity:
                qty = float(update.last_fill_quantity)
                price = float(update.last_fill_price)
                filled_qty += qty
                avg_acc_notional += qty * price
                avg_price = (
                    avg_acc_notional / filled_qty if filled_qty > 0 else price
                )
                await self._store.insert_fill(
                    order_id,
                    company_id=intent.company_id,
                    adapter=adapter_name,
                    exchange=intent.exchange,
                    account_id_external=intent.account_id_external,
                    symbol=intent.symbol,
                    direction=intent.direction,
                    quantity=qty,
                    price=price,
                    fee_usd=fee,
                    is_maker=update.last_fill_is_maker,
                    liquidity=(
                        "maker" if update.last_fill_is_maker is True
                        else "taker" if update.last_fill_is_maker is False
                        else None
                    ),
                    external_fill_id=update.last_fill_external_id,
                    ts=update.ts,
                    metadata={"update_payload": update.payload},
                )

                await self._maybe_snapshot_position(
                    intent=intent,
                    adapter_name=adapter_name,
                    filled_qty=filled_qty,
                    avg_price=avg_price,
                    ts=update.ts,
                )

            await self._store.update_order(
                order_id,
                status=last_status,
                filled_quantity=filled_qty,
                average_fill_price=avg_price,
                last_fee_usd=fee,
                external_order_id=external_id,
                reason=last_message,
            )

    async def _maybe_snapshot_position(
        self,
        *,
        intent: ExecutionIntent,
        adapter_name: str,
        filled_qty: float,
        avg_price: Optional[float],
        ts: datetime,
    ) -> None:
        if not avg_price:
            return
        notional = filled_qty * avg_price
        row = PositionSnapshotRow(
            id=None,
            company_id=intent.company_id,
            adapter=adapter_name,
            exchange=intent.exchange,
            account_id_external=intent.account_id_external,
            symbol=intent.symbol,
            direction=DIRECTION_LONG if intent.direction == DIRECTION_LONG else "short",
            quantity=filled_qty,
            average_entry_price=avg_price,
            notional_usd=notional,
            unrealised_pnl_usd=0.0,
            realized_pnl_usd=0.0,
            leverage=int(intent.metadata.get("leverage", 1)) if intent.metadata else 1,
            ts=ts or datetime.now(timezone.utc),
            source="adapter",
            metadata={"client_order_id": intent.client_order_id},
        )
        try:
            await self._store.insert_position_snapshot(row)
        except Exception as exc:
            LOG.debug("position snapshot skipped: %s", exc)


def _order_to_intent(order: OrderSnapshot) -> ExecutionIntent:
    return ExecutionIntent(
        company_id=order.company_id,
        strategy_id=None,
        agent_id=None,
        exchange=order.exchange,
        account_id_external=order.account_id_external,
        symbol=order.symbol,
        direction=order.direction,
        order_type=order.order_type,
        quantity=order.quantity,
        requested_price=order.requested_price,
        client_order_id=order.client_order_id,
        metadata=order.metadata or {},
    )


__all__ = [
    "ExecutionRouter",
    "ADAPTER_PAPER",
    "ADAPTER_CCXT",
    "ADAPTER_NAUTILUS",
    "ACTIVE_STATUSES",
    "STATUS_ACCEPTED",
    "STATUS_CANCELED",
    "STATUS_REJECTED",
    "EVENT_CANCEL",
]
