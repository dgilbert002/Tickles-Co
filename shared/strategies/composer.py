"""
shared.strategies.composer — central coordinator.

Flow:

1. Gather candidate :class:`StrategyIntent`s from every configured
   producer (:mod:`shared.strategies.producers`).
2. Dedupe by ``(strategy_name, source_ref)`` in memory — the store
   has a matching partial unique index as a belt-and-braces layer.
3. Rank by ``priority_score`` desc (ties broken by ``strategy_name``).
4. Persist every candidate to :table:`public.strategy_intents` with
   ``status='pending'``.
5. Optionally evaluate through a ``gate`` callable
   (``(intent) -> GateDecision``). When no gate is provided every
   intent stays ``pending`` — Phase 34 only proposes; later phases
   will wire Treasury + Guardrails gates.
6. If a ``submit`` callable is provided, run survivors through it,
   capture the resulting ``order_id``, and mark them ``submitted``.

The composer is deliberately a pure orchestrator — *all* execution
semantics live in the caller's ``gate`` + ``submit`` callables so
tests can substitute deterministic stubs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, List, Optional

from shared.strategies.producers.base import BaseProducer
from shared.strategies.protocol import (
    STATUS_APPROVED,
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SKIPPED,
    STATUS_SUBMITTED,
    CompositionResult,
    StrategyIntent,
)
from shared.strategies.store import StrategyStore

LOG = logging.getLogger("tickles.strategies.composer")


@dataclass
class GateDecision:
    approved: bool
    reason: Optional[str] = None
    size_base_override: Optional[float] = None
    notional_usd_override: Optional[float] = None


GateCallable = Callable[[StrategyIntent], Awaitable[GateDecision]]
SubmitResult = Optional[int]
SubmitCallable = Callable[[StrategyIntent], Awaitable[SubmitResult]]


@dataclass
class ComposerConfig:
    auto_persist: bool = True
    dedupe_in_memory: bool = True
    ranking_desc: bool = True


class StrategyComposer:
    def __init__(
        self,
        store: StrategyStore,
        producers: Iterable[BaseProducer],
        *,
        gate: Optional[GateCallable] = None,
        submit: Optional[SubmitCallable] = None,
        config: Optional[ComposerConfig] = None,
    ) -> None:
        self._store = store
        self._producers: List[BaseProducer] = list(producers)
        self._gate = gate
        self._submit = submit
        self._config = config or ComposerConfig()

    async def tick(
        self,
        *,
        limit_per_producer: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> CompositionResult:
        if persist is None:
            persist = self._config.auto_persist
        gathered: List[StrategyIntent] = []
        for p in self._producers:
            try:
                chunk = await p.produce(
                    limit=limit_per_producer,
                    correlation_id=correlation_id, company_id=company_id,
                )
            except Exception as e:  # pragma: no cover
                LOG.warning("producer %s failed: %s", p.name, e)
                continue
            gathered.extend(chunk)

        if self._config.dedupe_in_memory:
            gathered = _dedupe(gathered)

        gathered.sort(
            key=lambda i: (
                -i.priority_score if self._config.ranking_desc
                else i.priority_score,
                i.strategy_name, i.symbol, i.side,
            ),
        )

        result = CompositionResult.empty()
        result.proposed = len(gathered)

        for intent in gathered:
            intent.status = STATUS_PENDING
            if persist:
                try:
                    new_id = await self._store.record_intent(intent)
                    if new_id == 0:
                        intent.status = STATUS_DUPLICATE
                        result.duplicate += 1
                        result.intents.append(intent)
                        continue
                    intent.id = new_id
                except Exception as e:  # pragma: no cover
                    LOG.warning("persist intent failed: %s", e)
                    intent.status = STATUS_FAILED
                    intent.decision_reason = f"persist_failed:{e!s}"
                    result.failed += 1
                    result.intents.append(intent)
                    continue

            if self._gate is None:
                # No gate wired yet — leave as pending.
                result.intents.append(intent)
                continue

            try:
                decision = await self._gate(intent)
            except Exception as e:  # pragma: no cover
                LOG.warning("gate raised for %s: %s", intent.source_ref, e)
                decision = GateDecision(approved=False,
                                         reason=f"gate_error:{e!s}")
            now = datetime.now(timezone.utc)
            intent.decided_at = now
            if not decision.approved:
                intent.status = STATUS_REJECTED
                intent.decision_reason = decision.reason or "rejected"
                result.rejected += 1
                if persist:
                    try:
                        await self._store.update_intent_status(
                            intent.id or 0, intent.status,
                            reason=intent.decision_reason, decided_at=now,
                        )
                    except Exception as e:  # pragma: no cover
                        LOG.debug("update_intent_status failed: %s", e)
                result.intents.append(intent)
                continue

            if decision.size_base_override is not None:
                intent.size_base = float(decision.size_base_override)
            if decision.notional_usd_override is not None:
                intent.notional_usd = float(decision.notional_usd_override)

            intent.status = STATUS_APPROVED
            intent.decision_reason = decision.reason or "approved"
            result.approved += 1
            if persist:
                try:
                    await self._store.update_intent_status(
                        intent.id or 0, intent.status,
                        reason=intent.decision_reason, decided_at=now,
                    )
                except Exception as e:  # pragma: no cover
                    LOG.debug("update_intent_status failed: %s", e)

            if self._submit is None:
                result.intents.append(intent)
                continue

            try:
                order_id = await self._submit(intent)
            except Exception as e:  # pragma: no cover
                LOG.warning("submit failed for %s: %s",
                            intent.source_ref, e)
                intent.status = STATUS_FAILED
                intent.decision_reason = f"submit_error:{e!s}"
                result.failed += 1
                if persist:
                    try:
                        await self._store.update_intent_status(
                            intent.id or 0, intent.status,
                            reason=intent.decision_reason,
                        )
                    except Exception:  # pragma: no cover
                        pass
                result.intents.append(intent)
                continue

            intent.order_id = order_id
            intent.status = STATUS_SUBMITTED if order_id else STATUS_SKIPPED
            intent.submitted_at = datetime.now(timezone.utc)
            if intent.status == STATUS_SUBMITTED:
                result.submitted += 1
            else:
                result.skipped += 1
                intent.decision_reason = (
                    intent.decision_reason or "submit_returned_no_order"
                )
            if persist:
                try:
                    await self._store.update_intent_status(
                        intent.id or 0, intent.status,
                        reason=intent.decision_reason,
                        order_id=intent.order_id,
                        submitted_at=intent.submitted_at,
                    )
                except Exception as e:  # pragma: no cover
                    LOG.debug("update_intent_status failed: %s", e)
            result.intents.append(intent)

        return result


def _dedupe(intents: List[StrategyIntent]) -> List[StrategyIntent]:
    seen = set()
    out: List[StrategyIntent] = []
    for i in intents:
        if i.source_ref is not None:
            key = (i.strategy_name, i.source_ref)
            if key in seen:
                continue
            seen.add(key)
        out.append(i)
    return out


__all__ = [
    "ComposerConfig",
    "GateCallable",
    "GateDecision",
    "StrategyComposer",
    "SubmitCallable",
]
