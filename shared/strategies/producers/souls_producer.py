"""
shared.strategies.producers.souls_producer — turns
:table:`public.agent_decisions` rows whose verdict is ``approve`` or
``propose`` into :class:`StrategyIntent`s.

The souls store holds arbitrary decision payloads keyed by persona,
so this producer understands a small convention: when the decision's
``fields`` dict contains ``{symbol, side, size_base, ...}`` keys, we
treat that as an actionable intent. Verdicts that don't carry that
payload are ignored — they're still in the audit table, just not
promoted to intents.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.strategies.protocol import KIND_SOULS, StrategyIntent


class SoulsProducer:
    name = "souls-composer"
    kind = KIND_SOULS

    VERDICTS = ("approve", "propose")

    def __init__(
        self,
        store: Any,  # SoulsStore from shared.souls
        *,
        name: Optional[str] = None,
        max_decisions: int = 50,
        personas: Optional[List[str]] = None,
    ) -> None:
        self.name = name or "souls-composer"
        self._store = store
        self._max = int(max_decisions)
        self._personas = list(personas) if personas else None

    async def produce(
        self,
        *,
        limit: int = 50,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> List[StrategyIntent]:
        decisions = await self._list_decisions(limit)
        now = datetime.now(timezone.utc)
        out: List[StrategyIntent] = []
        for d in decisions:
            verdict = getattr(d, "verdict", None) or (
                d.get("verdict") if isinstance(d, dict) else None
            )
            if verdict not in self.VERDICTS:
                continue
            persona = getattr(d, "soul_name", None) or (
                d.get("soul_name") if isinstance(d, dict) else None
            )
            if self._personas and persona not in self._personas:
                continue
            fields = getattr(d, "fields", None) or (
                d.get("fields") if isinstance(d, dict) else None
            )
            intent = self._intent_from_fields(
                fields=fields or {}, decision=d, persona=persona or "",
                correlation_id=correlation_id, company_id=company_id, now=now,
            )
            if intent is not None:
                out.append(intent)
        return out

    async def _list_decisions(self, limit: int) -> List[Any]:
        if self._store is None:
            return []
        lister = getattr(self._store, "list_decisions", None)
        if lister is None:
            return []
        return await lister(limit=min(limit, self._max))

    def _intent_from_fields(
        self,
        *,
        fields: Dict[str, Any],
        decision: Any,
        persona: str,
        correlation_id: Optional[str],
        company_id: Optional[str],
        now: datetime,
    ) -> Optional[StrategyIntent]:
        symbol = fields.get("symbol")
        side = (fields.get("side") or "").lower()
        size_base = fields.get("size_base") or fields.get("qty")
        if not symbol or side not in ("buy", "sell") or not size_base:
            return None
        try:
            size_base = float(size_base)
        except (TypeError, ValueError):
            return None
        if size_base <= 0:
            return None
        notional = fields.get("notional_usd") or 0.0
        ref_price = fields.get("reference_price") or fields.get("price")
        did = getattr(decision, "id", None) or (
            decision.get("id") if isinstance(decision, dict) else None
        )
        src_ref = f"agent_decisions.id={did}" if did is not None else None
        md = {
            "persona": persona,
            "verdict": getattr(decision, "verdict", None) or (
                decision.get("verdict") if isinstance(decision, dict) else None
            ),
        }
        extras = {
            k: v for k, v in fields.items()
            if k not in (
                "symbol", "side", "size_base", "qty",
                "notional_usd", "reference_price", "price",
            )
        }
        if extras:
            md["extras"] = extras
        return StrategyIntent(
            id=None, strategy_name=self.name, strategy_kind=self.kind,
            symbol=str(symbol), side=side, size_base=size_base,
            notional_usd=float(notional or 0.0), venue=fields.get("venue"),
            reference_price=(
                None if ref_price is None else float(ref_price)
            ),
            correlation_id=correlation_id,
            source_ref=src_ref,
            priority_score=float(notional or 0.0),
            company_id=company_id, metadata=md, proposed_at=now,
        )


__all__ = ["SoulsProducer"]
