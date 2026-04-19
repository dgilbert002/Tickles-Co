"""Phase 26 execution layer — public API."""
from __future__ import annotations

from pathlib import Path

from shared.execution.ccxt_adapter import CcxtExecutionAdapter
from shared.execution.nautilus_adapter import NautilusExecutionAdapter
from shared.execution.paper import PaperExecutionAdapter
from shared.execution.protocol import (
    ACTIVE_STATUSES,
    ADAPTER_CCXT,
    ADAPTER_NAUTILUS,
    ADAPTER_PAPER,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    EVENT_ACCEPTED,
    EVENT_CANCEL,
    EVENT_EXPIRE,
    EVENT_FILL,
    EVENT_PARTIAL_FILL,
    EVENT_REJECT,
    EVENT_SEVERITY_ERROR,
    EVENT_SEVERITY_INFO,
    EVENT_SEVERITY_WARNING,
    EVENT_SUBMITTED,
    EVENT_UPDATE,
    ExecutionAdapter,
    ExecutionIntent,
    MarketTick,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    ORDER_TYPE_STOP_LIMIT,
    OrderSnapshot,
    OrderUpdate,
    STATUS_ACCEPTED,
    STATUS_CANCELED,
    STATUS_EXPIRED,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_PARTIAL,
    STATUS_PENDING_CANCEL,
    STATUS_REJECTED,
    TERMINAL_STATUSES,
    TIF_FOK,
    TIF_GTC,
    TIF_GTD,
    TIF_IOC,
)
from shared.execution.router import ExecutionRouter
from shared.execution.store import ExecutionStore, FillRow, PositionSnapshotRow

MIGRATION_PATH: Path = (
    Path(__file__).parent / "migrations" / "2026_04_19_phase26_execution.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


def default_adapters() -> dict:
    """Return the default adapter trio (paper + ccxt + nautilus).

    The paper adapter is always safe to create. The ccxt adapter is
    constructed in sandbox mode (`sandbox=True`); a real deployment
    overrides this via the CLI or a config-driven factory. The
    nautilus adapter is a safe stub until it is actually wired.
    """
    return {
        ADAPTER_PAPER: PaperExecutionAdapter(),
        ADAPTER_CCXT: CcxtExecutionAdapter(sandbox=True),
        ADAPTER_NAUTILUS: NautilusExecutionAdapter(),
    }


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "default_adapters",
    "ExecutionAdapter",
    "ExecutionIntent",
    "OrderUpdate",
    "OrderSnapshot",
    "MarketTick",
    "ExecutionRouter",
    "ExecutionStore",
    "FillRow",
    "PositionSnapshotRow",
    "PaperExecutionAdapter",
    "CcxtExecutionAdapter",
    "NautilusExecutionAdapter",
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
]
