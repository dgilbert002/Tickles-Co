"""Phase 34 producers — adapters that turn upstream signals into
:class:`StrategyIntent`s for the composer to evaluate."""
from __future__ import annotations

from shared.strategies.producers.arb_producer import ArbProducer
from shared.strategies.producers.copy_producer import CopyProducer
from shared.strategies.producers.souls_producer import SoulsProducer
from shared.strategies.producers.base import BaseProducer

__all__ = [
    "ArbProducer",
    "BaseProducer",
    "CopyProducer",
    "SoulsProducer",
]
