"""
shared.execution.nautilus_adapter — NautilusTrader execution bridge.

Phase 26 scaffold. We register the adapter so every place in the stack
(router, CLI, services catalog) sees `nautilus` as a known choice, but
the actual order routing remains a future plug-in because:

* NautilusTrader is LGPL-v3 and installs a Rust runtime + cython
  extensions that are overkill for the platform's current footprint.
* We do not want to force a heavyweight dependency on operators who
  only need paper / ccxt.
* The existing CCXT adapter already covers every venue we support at
  the moment.

This adapter is intentionally *callable* — `submit(...)`,
`cancel(...)`, `poll_updates(...)` return well-formed, safe
OrderUpdate rejections rather than raising. That way the Router can
persist the rejection to `public.order_events` and the operator gets
a clean audit trail even when Nautilus is not installed.

When Nautilus is actually wired in, replace `_NAUTILUS_AVAILABLE` with
an import and fill in the `_delegate_*` helpers.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from shared.execution.protocol import (
    EVENT_REJECT,
    EVENT_SEVERITY_WARNING,
    ExecutionIntent,
    MarketTick,
    OrderUpdate,
    STATUS_REJECTED,
)


try:
    import nautilus_trader  # type: ignore  # noqa: F401
    _NAUTILUS_AVAILABLE = True
except ImportError:
    _NAUTILUS_AVAILABLE = False


class NautilusExecutionAdapter:
    """Adapter stub. Emits rejection updates until real wiring lands."""

    name: str = "nautilus"

    def __init__(self) -> None:
        self.available = _NAUTILUS_AVAILABLE

    async def submit(
        self,
        intent: ExecutionIntent,
        *,
        market: Optional[MarketTick] = None,
    ) -> Sequence[OrderUpdate]:
        client_id = intent.ensure_client_order_id()
        return [
            OrderUpdate(
                client_order_id=client_id,
                status=STATUS_REJECTED,
                event_type=EVENT_REJECT,
                severity=EVENT_SEVERITY_WARNING,
                message=(
                    "nautilus adapter is scaffolded in Phase 26 but not yet wired. "
                    "Use adapter 'paper' or 'ccxt' today. Install nautilus_trader and "
                    "implement _delegate_submit in nautilus_adapter.py to enable."
                ),
            )
        ]

    async def cancel(
        self,
        client_order_id: str,
        *,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> OrderUpdate:
        return OrderUpdate(
            client_order_id=client_order_id,
            status=STATUS_REJECTED,
            event_type=EVENT_REJECT,
            severity=EVENT_SEVERITY_WARNING,
            message="nautilus adapter is scaffolded; cancel not yet wired",
        )

    async def poll_updates(
        self,
        client_order_ids: Sequence[str],
    ) -> List[OrderUpdate]:
        return []


__all__ = ["NautilusExecutionAdapter"]
