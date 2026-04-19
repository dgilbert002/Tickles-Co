"""
shared.gateway.subscriptions — in-memory ref-counted subscription registry.

The 21-year-old version
-----------------------
Imagine 47 agents all asking for "Binance BTC/USDT trades".  We must
open exactly **one** websocket — not 47.  And when 46 unsubscribe, the
last one still wants the data, so we must keep the socket open until
the 47th leaves.

This is just a counter keyed by ``SubscriptionKey``.  When the count
goes from 0 -> 1 we tell the gateway "open it".  When it goes 1 -> 0
we say "close it".  Idempotent and thread-safe (asyncio lock).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Set

from shared.gateway.schema import SubscriptionKey


@dataclass
class SubscriptionLease:
    """Returned by :meth:`SubscriptionRegistry.add` describing the change."""

    key: SubscriptionKey
    new_count: int
    activated: bool = False
    """True when the count went 0 -> 1 (caller should start a stream)."""


@dataclass
class SubscriptionReleased:
    key: SubscriptionKey
    new_count: int
    deactivated: bool = False
    """True when the count went 1 -> 0 (caller should stop the stream)."""


@dataclass
class SubscriptionRegistry:
    counts: Dict[SubscriptionKey, int] = field(default_factory=dict)
    requesters: Dict[SubscriptionKey, Set[str]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, key: SubscriptionKey, requested_by: str) -> SubscriptionLease:
        async with self._lock:
            new_count = self.counts.get(key, 0) + 1
            self.counts[key] = new_count
            self.requesters.setdefault(key, set()).add(requested_by)
            return SubscriptionLease(key=key, new_count=new_count, activated=(new_count == 1))

    async def remove(self, key: SubscriptionKey, requested_by: str) -> SubscriptionReleased:
        async with self._lock:
            existing = self.counts.get(key, 0)
            if existing <= 0:
                return SubscriptionReleased(key=key, new_count=0, deactivated=False)
            new_count = existing - 1
            if new_count == 0:
                del self.counts[key]
                self.requesters.pop(key, None)
            else:
                self.counts[key] = new_count
                self.requesters.get(key, set()).discard(requested_by)
            return SubscriptionReleased(
                key=key, new_count=new_count, deactivated=(new_count == 0)
            )

    def has(self, key: SubscriptionKey) -> bool:
        return self.counts.get(key, 0) > 0

    def keys(self) -> List[SubscriptionKey]:
        return list(self.counts.keys())

    def total(self) -> int:
        return sum(self.counts.values())

    def snapshot(self) -> List[Dict[str, object]]:
        return [
            {
                "exchange": key.exchange,
                "symbol": key.symbol,
                "channel": key.channel.value,
                "count": count,
                "requesters": sorted(self.requesters.get(key, set())),
            }
            for key, count in self.counts.items()
        ]
