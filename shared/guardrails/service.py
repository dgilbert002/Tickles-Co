"""
shared.guardrails.service — orchestrates the evaluator against
persisted rules + live snapshots and writes one event per decision.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from shared.guardrails.evaluator import decisions_block_intent, evaluate
from shared.guardrails.protocol import (
    ProtectionDecision,
    ProtectionSnapshot,
    STATUS_RESOLVED,
    STATUS_TRIGGERED,
)
from shared.guardrails.store import GuardrailsStore, ProtectionEventRow

logger = logging.getLogger("tickles.guardrails.service")


class GuardrailsService:
    """Thin glue between the evaluator and :class:`GuardrailsStore`."""

    def __init__(self, store: GuardrailsStore) -> None:
        self._store = store

    async def tick(
        self,
        snapshot: ProtectionSnapshot,
        *,
        rule_type: Optional[str] = None,
        persist: bool = True,
        only_triggered: bool = False,
    ) -> List[ProtectionDecision]:
        """Evaluate every enabled rule for the given snapshot.

        By default, all decisions (triggered + resolved) are
        persisted so the ``crash_protection_active`` view reflects
        the current state. Set ``only_triggered=True`` if you want
        an event row only when a rule actually fires.
        """
        rules = await self._store.list_rules(
            company_id=snapshot.company_id,
            rule_type=rule_type,
            enabled_only=True,
        )
        decisions = evaluate(rules, snapshot)

        if persist:
            for dec in decisions:
                if only_triggered and dec.status != STATUS_TRIGGERED:
                    continue
                try:
                    await self._store.insert_event(dec)
                except Exception as exc:  # pragma: no cover - logged, not raised
                    logger.exception(
                        "guardrails insert_event failed scope=%s/%s/%s/%s type=%s: %s",
                        dec.company_id, dec.universe, dec.exchange, dec.symbol,
                        dec.rule.rule_type, exc,
                    )

        return decisions

    async def list_active(
        self,
        *,
        company_id: Optional[str] = None,
        triggered_only: bool = True,
    ) -> List[ProtectionEventRow]:
        return await self._store.list_active(
            company_id=company_id, triggered_only=triggered_only,
        )

    async def list_events(
        self,
        *,
        company_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[ProtectionEventRow]:
        return await self._store.list_events(
            company_id=company_id, status=status, limit=limit,
        )

    async def is_intent_blocked(
        self,
        *,
        company_id: str,
        universe: Optional[str],
        exchange: str,
        symbol: str,
    ) -> List[ProtectionEventRow]:
        """Return rows from ``crash_protection_active`` that currently
        halt new orders for the given intent scope.

        The Execution Layer calls this before submitting an order;
        strategies/Treasury can also call it for pre-flight checks.
        """
        active = await self._store.list_active(
            company_id=company_id, triggered_only=True,
        )
        blockers: List[ProtectionEventRow] = []
        for row in active:
            if row.action != "halt_new_orders":
                continue
            if row.status != STATUS_TRIGGERED:
                continue
            if row.company_id not in (None, company_id):
                continue
            if row.universe not in (None, universe):
                continue
            if row.exchange not in (None, exchange):
                continue
            if row.symbol not in (None, symbol):
                continue
            blockers.append(row)
        return blockers


__all__ = [
    "GuardrailsService",
    "decisions_block_intent",
    "STATUS_TRIGGERED",
    "STATUS_RESOLVED",
]
