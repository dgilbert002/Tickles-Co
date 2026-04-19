"""
shared.execution.protocol — ExecutionAdapter protocol + data classes.

Phase 26. The execution layer is what finally takes an approved
Treasury decision (Phase 25) and places it on a venue. We want this
layer **pluggable** so that the same code path works for:

* `paper`    — in-memory fills for forward-tests / dry runs.
* `ccxt`     — live execution over CCXT (spot + perp).
* `nautilus` — NautilusTrader execution (high performance, real-time).

Every adapter conforms to the same small async protocol. The
ExecutionRouter (see `router.py`) owns:

  1. Ordering (client_order_id generation, idempotency).
  2. Persistence (rows in `public.orders`, `public.fills`,
     `public.order_events`, `public.position_snapshots`).
  3. Banker reconciliation after fills.

Adapters themselves are stateless where possible; they take a fully
formed ExecutionIntent and return one or more OrderUpdate events.

All dataclasses here are **serialisable** (no non-primitive fields) so
the same types flow through the router, the DB, and the CLI.
"""
from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence


# ---------------------------------------------------------------------------
# Enumerated string constants (keep as plain strings for DB compatibility)
# ---------------------------------------------------------------------------

# Adapter names — must match the adapter registered in Router.
ADAPTER_PAPER: str = "paper"
ADAPTER_CCXT: str = "ccxt"
ADAPTER_NAUTILUS: str = "nautilus"

# Order statuses (match column values in public.orders.status)
STATUS_NEW: str = "new"
STATUS_ACCEPTED: str = "accepted"
STATUS_PARTIAL: str = "partially_filled"
STATUS_FILLED: str = "filled"
STATUS_CANCELED: str = "canceled"
STATUS_REJECTED: str = "rejected"
STATUS_EXPIRED: str = "expired"
STATUS_PENDING_CANCEL: str = "pending_cancel"

ACTIVE_STATUSES = frozenset({
    STATUS_NEW, STATUS_ACCEPTED, STATUS_PARTIAL, STATUS_PENDING_CANCEL,
})
TERMINAL_STATUSES = frozenset({
    STATUS_FILLED, STATUS_CANCELED, STATUS_REJECTED, STATUS_EXPIRED,
})

# Order event types (match column values in public.order_events.event_type)
EVENT_SUBMITTED: str = "submitted"
EVENT_ACCEPTED: str = "accepted"
EVENT_PARTIAL_FILL: str = "partial_fill"
EVENT_FILL: str = "fill"
EVENT_CANCEL: str = "cancel"
EVENT_REJECT: str = "reject"
EVENT_EXPIRE: str = "expire"
EVENT_UPDATE: str = "update"

EVENT_SEVERITY_INFO: str = "info"
EVENT_SEVERITY_WARNING: str = "warning"
EVENT_SEVERITY_ERROR: str = "error"

# Order type / direction / TIF
ORDER_TYPE_MARKET: str = "market"
ORDER_TYPE_LIMIT: str = "limit"
ORDER_TYPE_STOP: str = "stop"
ORDER_TYPE_STOP_LIMIT: str = "stop_limit"

DIRECTION_LONG: str = "long"
DIRECTION_SHORT: str = "short"

TIF_GTC: str = "gtc"
TIF_IOC: str = "ioc"
TIF_FOK: str = "fok"
TIF_GTD: str = "gtd"


_CLIENT_ORDER_ID_COUNTER = itertools.count(1)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionIntent:
    """What the Router asks an adapter to do.

    This is the narrow contract between Treasury/Router and adapter.
    It is *derived* from a SizedIntent (Phase 25) but intentionally
    omits the sizing reasoning — adapters should not second-guess sizing.
    """

    company_id: str
    strategy_id: Optional[str]
    agent_id: Optional[str]
    exchange: str
    account_id_external: str
    symbol: str
    direction: str
    order_type: str = ORDER_TYPE_MARKET
    quantity: float = 0.0
    requested_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = TIF_GTC
    client_order_id: Optional[str] = None  # router will fill if None
    requested_notional_usd: Optional[float] = None
    treasury_decision_id: Optional[int] = None
    intent_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def ensure_client_order_id(self) -> str:
        """Deterministically derive a client_order_id if not set."""
        if self.client_order_id:
            return self.client_order_id
        counter = next(_CLIENT_ORDER_ID_COUNTER)
        material = (
            f"{self.company_id}|{self.strategy_id}|{self.agent_id}|"
            f"{self.exchange}|{self.account_id_external}|{self.symbol}|"
            f"{self.direction}|{self.order_type}|{self.quantity}|"
            f"{self.requested_price}|{self.intent_hash}|{time.time_ns()}|"
            f"{counter}|{id(self)}"
        )
        return "tk-" + hashlib.sha256(material.encode()).hexdigest()[:24]


@dataclass
class OrderUpdate:
    """A state transition emitted by an adapter for an order.

    One ExecutionIntent typically produces a stream of OrderUpdates:
    submitted → accepted → partial_fill → fill → filled, or the
    rejection / cancellation variants.

    An OrderUpdate does **not** own the DB row — the Router persists it.
    """

    client_order_id: str
    status: str
    event_type: str = EVENT_UPDATE
    severity: str = EVENT_SEVERITY_INFO
    message: Optional[str] = None
    external_order_id: Optional[str] = None
    filled_quantity: float = 0.0
    remaining_quantity: Optional[float] = None
    last_fill_price: Optional[float] = None
    last_fill_quantity: Optional[float] = None
    last_fill_fee_usd: Optional[float] = None
    last_fill_is_maker: Optional[bool] = None
    last_fill_external_id: Optional[str] = None
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_fill(self) -> bool:
        return self.event_type in (EVENT_PARTIAL_FILL, EVENT_FILL)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass
class OrderSnapshot:
    """Immutable view of an order's current known state (for CLI / API)."""

    id: Optional[int]
    client_order_id: str
    external_order_id: Optional[str]
    adapter: str
    exchange: str
    account_id_external: str
    company_id: str
    symbol: str
    direction: str
    order_type: str
    quantity: float
    requested_price: Optional[float]
    status: str
    filled_quantity: float
    average_fill_price: Optional[float]
    fees_paid_usd: float
    reason: Optional[str]
    submitted_at: datetime
    updated_at: datetime
    metadata: Dict[str, Any]

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Market snapshot used by adapters that simulate (paper) or need a
# fallback price.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketTick:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last


# ---------------------------------------------------------------------------
# ExecutionAdapter protocol
# ---------------------------------------------------------------------------


class ExecutionAdapter(Protocol):
    """Plug-in point for every execution backend.

    Design notes:
    * All methods are async so live adapters (ccxt, nautilus) can await
      network I/O without the router caring.
    * `submit(...)` returns a *sequence* of OrderUpdates; the paper
      adapter synthesises a synthetic submitted→filled stream, while a
      live adapter may only emit `submitted` here and stream fills
      later via `poll_updates(...)`.
    * Adapters MUST be idempotent on `client_order_id`: calling
      `submit(...)` twice with the same id returns the existing stream.
    * Adapters MUST NOT write to the DB; that is the Router's job.
    """

    name: str  # one of ADAPTER_* constants

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        market: Optional[MarketTick] = None,
    ) -> Sequence[OrderUpdate]:
        ...

    async def cancel(
        self,
        client_order_id: str,
        *,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> OrderUpdate:
        ...

    async def poll_updates(
        self,
        client_order_ids: Sequence[str],
    ) -> List[OrderUpdate]:
        ...


__all__ = [
    "ADAPTER_PAPER", "ADAPTER_CCXT", "ADAPTER_NAUTILUS",
    "STATUS_NEW", "STATUS_ACCEPTED", "STATUS_PARTIAL", "STATUS_FILLED",
    "STATUS_CANCELED", "STATUS_REJECTED", "STATUS_EXPIRED", "STATUS_PENDING_CANCEL",
    "ACTIVE_STATUSES", "TERMINAL_STATUSES",
    "EVENT_SUBMITTED", "EVENT_ACCEPTED", "EVENT_PARTIAL_FILL", "EVENT_FILL",
    "EVENT_CANCEL", "EVENT_REJECT", "EVENT_EXPIRE", "EVENT_UPDATE",
    "EVENT_SEVERITY_INFO", "EVENT_SEVERITY_WARNING", "EVENT_SEVERITY_ERROR",
    "ORDER_TYPE_MARKET", "ORDER_TYPE_LIMIT", "ORDER_TYPE_STOP", "ORDER_TYPE_STOP_LIMIT",
    "DIRECTION_LONG", "DIRECTION_SHORT",
    "TIF_GTC", "TIF_IOC", "TIF_FOK", "TIF_GTD",
    "ExecutionIntent", "OrderUpdate", "OrderSnapshot", "MarketTick",
    "ExecutionAdapter",
]
