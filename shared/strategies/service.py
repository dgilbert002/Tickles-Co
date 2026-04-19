"""
shared.strategies.service — thin service wrapper around the composer.

The service owns the producer list, the optional gate/submit callables,
and the persistent :class:`StrategyStore`. It exposes a single
:meth:`tick` method that runs one composition pass and returns the
:class:`CompositionResult` for operator/CLI visibility.
"""
from __future__ import annotations

from typing import Iterable, Optional

from shared.strategies.composer import (
    ComposerConfig,
    GateCallable,
    StrategyComposer,
    SubmitCallable,
)
from shared.strategies.producers.base import BaseProducer
from shared.strategies.protocol import CompositionResult
from shared.strategies.store import StrategyStore


class StrategyComposerService:
    def __init__(
        self,
        store: StrategyStore,
        producers: Iterable[BaseProducer],
        *,
        gate: Optional[GateCallable] = None,
        submit: Optional[SubmitCallable] = None,
        config: Optional[ComposerConfig] = None,
    ) -> None:
        self._composer = StrategyComposer(
            store, producers, gate=gate, submit=submit, config=config,
        )
        self._store = store

    async def tick(
        self,
        *,
        limit_per_producer: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> CompositionResult:
        return await self._composer.tick(
            limit_per_producer=limit_per_producer,
            correlation_id=correlation_id,
            company_id=company_id, persist=persist,
        )


__all__ = ["StrategyComposerService"]
