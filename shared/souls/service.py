"""
shared.souls.service — SoulsService.

Wires the three modernised souls (Apex / Quant / Ledger) to the
:class:`SoulsStore` and the ``agent_personas`` / ``agent_decisions``
tables so every verdict is auditable. The service is deterministic and
does not require an LLM — LLM-backed souls plug in later by overriding
the ``decide`` method.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.souls.personas import ApexSoul, LedgerSoul, QuantSoul
from shared.souls.protocol import (
    ROLE_BOOKKEEPER,
    ROLE_DECISION,
    ROLE_RESEARCH,
    SOUL_APEX,
    SOUL_LEDGER,
    SOUL_QUANT,
    SoulContext,
    SoulDecision,
    SoulPersona,
)
from shared.souls.store import SoulDecisionRow, SoulsStore


@dataclass
class SoulsConfig:
    enabled_personas: tuple = (SOUL_APEX, SOUL_QUANT, SOUL_LEDGER)
    auto_seed_personas: bool = True


class SoulsService:
    """High-level orchestration over Apex/Quant/Ledger."""

    def __init__(
        self,
        store: SoulsStore,
        *,
        apex: Optional[ApexSoul] = None,
        quant: Optional[QuantSoul] = None,
        ledger: Optional[LedgerSoul] = None,
        config: Optional[SoulsConfig] = None,
    ) -> None:
        self._store = store
        self._apex = apex or ApexSoul()
        self._quant = quant or QuantSoul()
        self._ledger = ledger or LedgerSoul()
        self._config = config or SoulsConfig()
        self._persona_ids: Dict[str, int] = {}

    # ------------------------------------------------------------------

    async def seed_personas(self) -> Dict[str, int]:
        personas = {
            SOUL_APEX: SoulPersona(
                id=None, name=SOUL_APEX, role=ROLE_DECISION,
                description="Senior decision soul (deterministic aggregator)",
                default_llm=None, enabled=True,
            ),
            SOUL_QUANT: SoulPersona(
                id=None, name=SOUL_QUANT, role=ROLE_RESEARCH,
                description="Hypothesis generator (deterministic)",
                default_llm=None, enabled=True,
            ),
            SOUL_LEDGER: SoulPersona(
                id=None, name=SOUL_LEDGER, role=ROLE_BOOKKEEPER,
                description="Bookkeeper / journal writer",
                default_llm=None, enabled=True,
            ),
        }
        out: Dict[str, int] = {}
        for name, persona in personas.items():
            pid = await self._store.upsert_persona(persona)
            self._persona_ids[name] = pid
            out[name] = pid
        return out

    async def _persona_id(self, name: str) -> int:
        if name in self._persona_ids and self._persona_ids[name]:
            return self._persona_ids[name]
        existing = await self._store.get_persona(name)
        if existing and existing.id:
            self._persona_ids[name] = int(existing.id)
            return int(existing.id)
        if self._config.auto_seed_personas:
            await self.seed_personas()
            return self._persona_ids.get(name, 0)
        return 0

    # ------------------------------------------------------------------

    async def run_apex(
        self, context: SoulContext, *, persist: bool = True,
    ) -> SoulDecision:
        decision = self._apex.decide(context)
        if persist:
            await self._persist(SOUL_APEX, decision, context)
        return decision

    async def run_quant(
        self, context: SoulContext, *, persist: bool = True,
    ) -> SoulDecision:
        decision = self._quant.decide(context)
        if persist:
            await self._persist(SOUL_QUANT, decision, context)
        return decision

    async def run_ledger(
        self, context: SoulContext, *, persist: bool = True,
    ) -> SoulDecision:
        decision = self._ledger.decide(context)
        if persist:
            await self._persist(SOUL_LEDGER, decision, context)
        return decision

    async def _persist(
        self, name: str, decision: SoulDecision, context: SoulContext,
    ) -> int:
        pid = await self._persona_id(name)
        if not pid:
            return 0
        return await self._store.record_decision(
            pid, decision, context.fields or {}
        )

    # ------------------------------------------------------------------

    async def latest_decisions(
        self,
        *,
        persona: Optional[str] = None,
        company_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[SoulDecisionRow]:
        persona_id: Optional[int] = None
        if persona:
            persona_id = await self._persona_id(persona) or None
        return await self._store.list_latest_per_correlation(
            persona_id=persona_id, company_id=company_id, limit=limit,
        )

    async def decisions(self, **kwargs: Any) -> List[SoulDecisionRow]:
        return await self._store.list_decisions(**kwargs)


__all__ = ["SoulsConfig", "SoulsService"]
