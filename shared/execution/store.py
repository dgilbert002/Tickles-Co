"""
shared.execution.store — DB persistence for the execution layer.

Phase 26. Every order / fill / position snapshot lands in
`tickles_shared`. The Store is intentionally low-level: it translates
OrderUpdate events (from adapters) into SQL writes and translates
DB rows back into OrderSnapshot / Fill / Position objects.

The Router (router.py) is the single caller of this module; tests use
`shared.trading.memory_pool.InMemoryTradingPool` (extended here with
execution tables) to exercise the Router without Postgres.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.execution.protocol import (
    EVENT_SEVERITY_INFO,
    ExecutionIntent,
    OrderSnapshot,
    OrderUpdate,
    STATUS_NEW,
)

LOG = logging.getLogger("tickles.execution.store")


# ---------------------------------------------------------------------------
# Row models returned to the CLI / API
# ---------------------------------------------------------------------------


@dataclass
class FillRow:
    id: Optional[int]
    order_id: int
    company_id: str
    adapter: str
    exchange: str
    account_id_external: str
    symbol: str
    direction: str
    quantity: float
    price: float
    notional_usd: float
    fee_usd: float
    fee_currency: Optional[str]
    is_maker: Optional[bool]
    liquidity: Optional[str]
    realized_pnl_usd: Optional[float]
    external_fill_id: Optional[str]
    ts: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionSnapshotRow:
    id: Optional[int]
    company_id: str
    adapter: str
    exchange: str
    account_id_external: str
    symbol: str
    direction: str
    quantity: float
    average_entry_price: Optional[float]
    notional_usd: Optional[float]
    unrealised_pnl_usd: Optional[float]
    realized_pnl_usd: float
    leverage: int
    ts: datetime
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ExecutionStore
# ---------------------------------------------------------------------------


_INSERT_ORDER_SQL = """
INSERT INTO public.orders (
    company_id, strategy_id, agent_id, intent_hash, treasury_decision_id,
    adapter, exchange, account_id_external, symbol, direction, order_type,
    quantity, requested_notional_usd, requested_price, time_in_force,
    client_order_id, status, metadata
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10, $11,
    $12, $13, $14, $15,
    $16, $17, $18
)
ON CONFLICT (adapter, client_order_id) DO UPDATE SET
    updated_at = NOW()
RETURNING id
""".strip()


_UPDATE_ORDER_SQL = """
UPDATE public.orders
SET status = $2,
    filled_quantity = $3,
    average_fill_price = $4,
    fees_paid_usd = public.orders.fees_paid_usd + $5,
    external_order_id = COALESCE($6, external_order_id),
    reason = COALESCE($7, reason),
    updated_at = NOW()
WHERE id = $1
""".strip()


_INSERT_EVENT_SQL = """
INSERT INTO public.order_events (order_id, event_type, severity, message, payload, ts)
VALUES ($1, $2, $3, $4, $5, $6)
""".strip()


_INSERT_FILL_SQL = """
INSERT INTO public.fills (
    order_id, company_id, adapter, exchange, account_id_external,
    symbol, direction, quantity, price, notional_usd,
    fee_usd, fee_currency, is_maker, liquidity,
    realized_pnl_usd, external_fill_id, ts, metadata
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, $12, $13, $14,
    $15, $16, $17, $18
)
RETURNING id
""".strip()


_INSERT_POSITION_SQL = """
INSERT INTO public.position_snapshots (
    company_id, adapter, exchange, account_id_external, symbol,
    direction, quantity, average_entry_price, notional_usd,
    unrealised_pnl_usd, realized_pnl_usd, leverage, ts, source, metadata
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11, $12, $13, $14, $15
)
RETURNING id
""".strip()


_FETCH_ORDER_BY_CLIENT_ID_SQL = """
SELECT * FROM public.orders
WHERE adapter = $1 AND client_order_id = $2
LIMIT 1
""".strip()


_FETCH_ORDER_BY_ID_SQL = """
SELECT * FROM public.orders WHERE id = $1
""".strip()


_LIST_OPEN_ORDERS_SQL = """
SELECT * FROM public.orders
WHERE company_id = $1
  AND status IN ('new', 'accepted', 'partially_filled', 'pending_cancel')
ORDER BY submitted_at DESC
LIMIT $2
""".strip()


_LIST_FILLS_SQL = """
SELECT * FROM public.fills
WHERE company_id = $1
ORDER BY ts DESC
LIMIT $2
""".strip()


_LIST_POSITIONS_SQL = """
SELECT * FROM public.positions_current
WHERE company_id = $1
ORDER BY symbol
""".strip()


class ExecutionStore:
    """Async DB wrapper for every execution-layer table."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def insert_order(
        self,
        intent: ExecutionIntent,
        *,
        adapter: str,
        client_order_id: str,
        status: str = STATUS_NEW,
    ) -> int:
        metadata = dict(intent.metadata or {})
        metadata.setdefault("intent_hash", intent.intent_hash)
        row = await self._pool.fetch_one(_INSERT_ORDER_SQL, [
            intent.company_id, intent.strategy_id, intent.agent_id,
            intent.intent_hash or "", intent.treasury_decision_id,
            adapter, intent.exchange, intent.account_id_external,
            intent.symbol, intent.direction, intent.order_type,
            float(intent.quantity), intent.requested_notional_usd, intent.requested_price,
            intent.time_in_force,
            client_order_id, status, json.dumps(metadata),
        ])
        if row is None:
            raise RuntimeError("insert_order returned no row")
        return int(row["id"])

    async def update_order(
        self,
        order_id: int,
        *,
        status: str,
        filled_quantity: float,
        average_fill_price: Optional[float],
        last_fee_usd: float,
        external_order_id: Optional[str],
        reason: Optional[str],
    ) -> None:
        await self._pool.execute(_UPDATE_ORDER_SQL, [
            order_id, status, filled_quantity, average_fill_price,
            last_fee_usd, external_order_id, reason,
        ])

    async def insert_event(
        self,
        order_id: int,
        update: OrderUpdate,
    ) -> None:
        payload = dict(update.payload or {})
        await self._pool.execute(_INSERT_EVENT_SQL, [
            order_id, update.event_type, update.severity or EVENT_SEVERITY_INFO,
            update.message, json.dumps(payload), update.ts,
        ])

    async def insert_fill(
        self,
        order_id: int,
        *,
        company_id: str,
        adapter: str,
        exchange: str,
        account_id_external: str,
        symbol: str,
        direction: str,
        quantity: float,
        price: float,
        fee_usd: float,
        fee_currency: Optional[str] = None,
        is_maker: Optional[bool] = None,
        liquidity: Optional[str] = None,
        realized_pnl_usd: Optional[float] = None,
        external_fill_id: Optional[str] = None,
        ts: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        notional = float(quantity) * float(price)
        row = await self._pool.fetch_one(_INSERT_FILL_SQL, [
            order_id, company_id, adapter, exchange, account_id_external,
            symbol, direction, float(quantity), float(price), notional,
            float(fee_usd), fee_currency, is_maker, liquidity,
            realized_pnl_usd, external_fill_id,
            ts or datetime.now(timezone.utc), json.dumps(metadata or {}),
        ])
        if row is None:
            raise RuntimeError("insert_fill returned no row")
        return int(row["id"])

    async def insert_position_snapshot(
        self,
        row: PositionSnapshotRow,
    ) -> int:
        res = await self._pool.fetch_one(_INSERT_POSITION_SQL, [
            row.company_id, row.adapter, row.exchange,
            row.account_id_external, row.symbol,
            row.direction, float(row.quantity), row.average_entry_price,
            row.notional_usd, row.unrealised_pnl_usd,
            float(row.realized_pnl_usd), int(row.leverage),
            row.ts, row.source, json.dumps(row.metadata or {}),
        ])
        if res is None:
            raise RuntimeError("insert_position_snapshot returned no row")
        return int(res["id"])

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_order_by_client_id(
        self, adapter: str, client_order_id: str
    ) -> Optional[OrderSnapshot]:
        row = await self._pool.fetch_one(_FETCH_ORDER_BY_CLIENT_ID_SQL, [adapter, client_order_id])
        return _row_to_order(row) if row else None

    async def get_order(self, order_id: int) -> Optional[OrderSnapshot]:
        row = await self._pool.fetch_one(_FETCH_ORDER_BY_ID_SQL, [order_id])
        return _row_to_order(row) if row else None

    async def list_open_orders(
        self, company_id: str, *, limit: int = 100,
    ) -> List[OrderSnapshot]:
        rows = await self._pool.fetch_all(_LIST_OPEN_ORDERS_SQL, [company_id, int(limit)])
        return [_row_to_order(r) for r in rows]

    async def list_fills(
        self, company_id: str, *, limit: int = 100,
    ) -> List[FillRow]:
        rows = await self._pool.fetch_all(_LIST_FILLS_SQL, [company_id, int(limit)])
        return [_row_to_fill(r) for r in rows]

    async def list_current_positions(
        self, company_id: str
    ) -> List[PositionSnapshotRow]:
        rows = await self._pool.fetch_all(_LIST_POSITIONS_SQL, [company_id])
        return [_row_to_position(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_json(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _row_to_order(row: Dict[str, Any]) -> OrderSnapshot:
    return OrderSnapshot(
        id=row.get("id"),
        client_order_id=row["client_order_id"],
        external_order_id=row.get("external_order_id"),
        adapter=row["adapter"],
        exchange=row["exchange"],
        account_id_external=row["account_id_external"],
        company_id=row["company_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        order_type=row["order_type"],
        quantity=float(row["quantity"]),
        requested_price=float(row["requested_price"]) if row.get("requested_price") is not None else None,
        status=row["status"],
        filled_quantity=float(row.get("filled_quantity") or 0.0),
        average_fill_price=(
            float(row["average_fill_price"]) if row.get("average_fill_price") is not None else None
        ),
        fees_paid_usd=float(row.get("fees_paid_usd") or 0.0),
        reason=row.get("reason"),
        submitted_at=_coerce_dt(row.get("submitted_at")),
        updated_at=_coerce_dt(row.get("updated_at")),
        metadata=_decode_json(row.get("metadata")),
    )


def _row_to_fill(row: Dict[str, Any]) -> FillRow:
    return FillRow(
        id=row.get("id"),
        order_id=int(row["order_id"]),
        company_id=row["company_id"],
        adapter=row["adapter"],
        exchange=row["exchange"],
        account_id_external=row["account_id_external"],
        symbol=row["symbol"],
        direction=row["direction"],
        quantity=float(row["quantity"]),
        price=float(row["price"]),
        notional_usd=float(row["notional_usd"]),
        fee_usd=float(row.get("fee_usd") or 0.0),
        fee_currency=row.get("fee_currency"),
        is_maker=row.get("is_maker"),
        liquidity=row.get("liquidity"),
        realized_pnl_usd=(
            float(row["realized_pnl_usd"]) if row.get("realized_pnl_usd") is not None else None
        ),
        external_fill_id=row.get("external_fill_id"),
        ts=_coerce_dt(row.get("ts")),
        metadata=_decode_json(row.get("metadata")),
    )


def _row_to_position(row: Dict[str, Any]) -> PositionSnapshotRow:
    return PositionSnapshotRow(
        id=row.get("id"),
        company_id=row["company_id"],
        adapter=row["adapter"],
        exchange=row["exchange"],
        account_id_external=row["account_id_external"],
        symbol=row["symbol"],
        direction=row["direction"],
        quantity=float(row["quantity"]),
        average_entry_price=(
            float(row["average_entry_price"]) if row.get("average_entry_price") is not None else None
        ),
        notional_usd=(
            float(row["notional_usd"]) if row.get("notional_usd") is not None else None
        ),
        unrealised_pnl_usd=(
            float(row["unrealised_pnl_usd"]) if row.get("unrealised_pnl_usd") is not None else None
        ),
        realized_pnl_usd=float(row.get("realized_pnl_usd") or 0.0),
        leverage=int(row.get("leverage") or 1),
        ts=_coerce_dt(row.get("ts")),
        source=row.get("source") or "unknown",
        metadata=_decode_json(row.get("metadata")),
    )


def _coerce_dt(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


__all__ = ["ExecutionStore", "FillRow", "PositionSnapshotRow"]
