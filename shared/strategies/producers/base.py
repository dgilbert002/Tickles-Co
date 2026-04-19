"""
shared.strategies.producers.base — shared Protocol for producers.

Each producer returns a list of candidate :class:`StrategyIntent`s.
Producers are pure-Python, asynchronous, and stateless except for
the data they read. The composer is what actually decides which
intents survive.
"""
from __future__ import annotations

from typing import List, Optional, Protocol

from shared.strategies.protocol import StrategyIntent


class BaseProducer(Protocol):
    """Protocol every producer implements."""

    name: str
    kind: str

    async def produce(
        self,
        *,
        limit: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[StrategyIntent]: ...


__all__ = ["BaseProducer"]
